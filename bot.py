import os
import io
import re
import json
import logging
import asyncio
from datetime import datetime, timedelta
import pytz
from telegram import Update
from telegram.ext import (
    Application, MessageHandler, CommandHandler,
    filters, ContextTypes
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import anthropic
from openai import AsyncOpenAI
from notion_client import AsyncClient as NotionClient

# ─── Логи ────────────────────────────────────────────────────────────────────
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Конфиг ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN        = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY     = os.environ["ANTHROPIC_API_KEY"]
NOTION_API_KEY        = os.environ["NOTION_API_KEY"]
NOTION_DB_ID          = os.environ["NOTION_DATABASE_ID"]
NOTION_SHOPPING_DB_ID = os.environ.get("NOTION_SHOPPING_DB_ID", "")  # отдельная таблица покупок
CHAT_ID               = os.environ["CHAT_ID"]

_raw_ids    = os.environ.get("ALLOWED_USER_IDS", "")
ALLOWED_IDS = set(x.strip() for x in _raw_ids.split(",") if x.strip())

BALI_TZ = pytz.timezone("Asia/Makassar")

# Google Calendar (опционально)
_gcreds_b64       = os.environ.get("GOOGLE_CREDENTIALS_BASE64", "")
GOOGLE_CREDS_JSON = ""
if _gcreds_b64:
    import base64 as _b64
    GOOGLE_CREDS_JSON = _b64.b64decode(_gcreds_b64).decode("utf-8")
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")

# Notion: страница с контекстом о Маше
NOTION_CONTEXT_PAGE_ID = "3506e819-ab77-814c-b2bf-cecb528867ad"

# Файл для хранения напоминаний (переживает рестарт в рамках деплоя)
REMINDERS_FILE = "/tmp/masha_reminders.json"

# ─── Кеш контекста ───────────────────────────────────────────────────────────
_context_cache: dict = {"text": "", "updated_at": None}

# ─── Клиенты ─────────────────────────────────────────────────────────────────
ai  = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
oai = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"]) if os.environ.get("OPENAI_API_KEY") else None

# ─── Эмодзи ──────────────────────────────────────────────────────────────────
ZONE_EMOJI = {
    "Golden Goose": "💼", "Fairway": "📐", "FlipLab": "🏺",
    "Personal": "👤",    "Kids": "👦",   "Home": "🏠",
    "Finance": "💰",     "Family": "👨‍👩‍👧", "Travel": "✈️",
    "Property": "🏘️",   "Docs/Visa": "📋",
}
PRIORITY_EMOJI = {"Urgent": "🔴", "High": "🟠", "Medium": "🟡", "Low": "🟢"}
STORE_EMOJI    = {"Папайя": "🏪", "Онлайн": "💻", "Рынок": "🥦", "Другое": "🛍️"}

ZONE_ALIASES = {
    "гусь": "Golden Goose", "гг": "Golden Goose", "golden goose": "Golden Goose",
    "баня": "Golden Goose", "ресторан": "Golden Goose", "бедугул": "Golden Goose",
    "fairway": "Fairway", "фэйрвэй": "Fairway", "фейрвей": "Fairway",
    "fliplab": "FlipLab", "флиплаб": "FlipLab", "керамика": "FlipLab",
    "личное": "Personal", "инстаграм": "Personal", "здоровье": "Personal",
    "дети": "Kids", "кирилл": "Kids", "кирюша": "Kids", "школа": "Kids",
    "дом": "Home", "еда": "Home", "бун": "Home", "собака": "Home",
    "финансы": "Finance", "деньги": "Finance",
    "семья": "Family", "родители": "Family",
    "путешествия": "Travel", "поездка": "Travel",
    "квартира": "Property", "аренда": "Property", "недвижимость": "Property",
    "документы": "Docs/Visa", "виза": "Docs/Visa", "китас": "Docs/Visa",
}

# ─── Промпты ─────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = f"""You are a task assistant for Masha. Parse messages and extract ALL tasks.

ZONES (use exactly):
- Golden Goose  → restaurant, banya (Bedugul Banya), GG operations. Also: "гусь", "ГГ"
- Fairway       → brand, website, marketing, merchandise, content
- FlipLab       → ceramic studio
- Personal      → health, fitness, Instagram, personal purchases, nutritionist
- Kids          → son Kirill (Year 1), nanny, clubs, vaccinations, kids purchases
- Home          → food, household shopping, staff salaries, dog (Bun/Бун)
- Finance       → payments, budgets
- Family        → birthdays, holidays, grandparents, gifts, photos
- Travel        → trips, visas, bookings
- Property      → rental apartment: tenants, rent, repairs, utilities, contracts
- Docs/Visa     → KITAS, passports, permits, insurance, official documents

PROJECTS: Golden Goose | Bedugul Banya | Fairway | FlipLab | Instagram | General

SHOPPING DETECTION — set "shopping": true if buying/purchasing anything.

Return ONLY valid JSON:
{{
  "tasks": [
    {{
      "task": "task name in Russian",
      "zone": "zone from the list",
      "project": "project from the list",
      "priority": "Urgent|High|Medium|Low",
      "deadline": "YYYY-MM-DD or null",
      "notes": "extra context or null",
      "shopping": true or false
    }}
  ]
}}

Today: {datetime.now().strftime("%Y-%m-%d")} ({datetime.now().strftime("%A")})
Convert relative dates to actual YYYY-MM-DD."""

RECEIPT_PROMPT = """Look at this receipt photo. Extract purchased items.
Return ONLY JSON: {{"items": ["item1", "item2", ...]}}
Just product names in Russian, no details."""

INTENT_PROMPT = """Classify this message from Masha (Russian). Reply with ONE word only:

- "query"    — asking about tasks (что срочного, покажи, дашборд, что по X, план на сегодня)
- "done"     — marking task complete (сделала, выполнила, закрыла, готово, отметь выполненной)
- "edit"     — changing a task (поменяй приоритет, измени, перенеси, сделай срочным, поставь дедлайн)
- "find"     — searching tasks (найди задачу, поищи задачу про X, есть ли задача)
- "remind"   — set reminder (напомни, поставь напоминание, напомни мне про X в Y)
- "write"    — asking to write/draft something (напиши, составь, помоги написать, сделай текст)
- "calendar" — adding event to calendar (добавь встречу, запланируй, внеси в календарь)
- "add"      — adding new tasks

Message: {text}

Reply ONLY: query / done / edit / find / remind / write / calendar / add"""

WRITER_SYSTEM = """You are Masha's personal assistant. She's a Russian-speaking entrepreneur in Bali.
She runs: Golden Goose restaurant, Bedugul Banya, Fairway brand, FlipLab ceramic studio.
Her son Kirill is in Year 1. Dog Bun. Nanny helps with kids.

Write exactly what she asked for — letter, post, message, reminder, plan.
Write in Russian unless specified otherwise. Be concise, warm, professional.
Don't add explanations — just the text she needs."""

CALENDAR_PROMPT = """Extract event details from this message. Return ONLY JSON:
{{
  "title": "event title in Russian",
  "date": "YYYY-MM-DD or null",
  "time": "HH:MM or null",
  "duration_minutes": 60,
  "description": "details or null"
}}

Today: {today}. Convert relative dates (завтра, в пятницу, на следующей неделе) to YYYY-MM-DD.
If no time specified — use null.

Message: {{text}}"""

EDIT_PROMPT = """Extract what task to edit from this message. Return ONLY JSON:
{{
  "task_name": "keywords to find the task",
  "field": "priority" or "deadline",
  "value": "for priority: Urgent/High/Medium/Low; for deadline: YYYY-MM-DD"
}}

Today: {today}. Convert relative dates to YYYY-MM-DD.
Priority mapping: срочно/срочный/urgent → Urgent, важно/высокий → High, средний → Medium, низкий → Low.

Examples:
- "поменяй приоритет звонка с Роби на срочный" → {{"task_name": "звонок с Роби", "field": "priority", "value": "Urgent"}}
- "перенеси встречу по сайту на пятницу" → {{"task_name": "встреча по сайту", "field": "deadline", "value": "2026-05-02"}}
- "сделай задачу про керамику срочной" → {{"task_name": "керамика", "field": "priority", "value": "Urgent"}}

Message: {text}"""

REMINDER_PROMPT = """Extract reminder details from this message. Return ONLY JSON:
{{
  "text": "what to remind about, short phrase in Russian",
  "datetime": "YYYY-MM-DDTHH:MM"
}}

Today: {today} (timezone: Bali, UTC+8).
Time conversion: завтра=tomorrow, через час=+1h, вечером=19:00, утром=09:00, в полдень=12:00.
If no time given — use 09:00 next day.

Message: {text}"""

FIND_PROMPT = """Extract the search keyword from this message. Return ONLY 1-3 words in Russian (no explanation).
Examples:
- "найди задачу про сайт" → сайт
- "поищи задачи по рекламе" → реклама
- "есть что-то про баню" → баня
- "покажи задачи с дедлайном" → дедлайн

Message: {text}"""

SHOPPING_PARSE_PROMPT = """Parse this shopping item message from Masha. Return ONLY JSON:
{{
  "items": [
    {{
      "item": "item name in Russian",
      "store": "Папайя|Онлайн|Рынок|Другое",
      "zone": "Home|Kids|Family|Personal|Golden Goose|Fairway|FlipLab|Finance|Travel|Property|Docs/Visa",
      "notes": "size, brand, details or null"
    }}
  ]
}}

Store rules: продукты/овощи/фрукты/молоко → Рынок or Папайя (default Папайя);
Amazon/Shopee/Tokopedia/заказать онлайн → Онлайн; всё остальное → Другое.

Today: {today}
Message: {text}"""


# ─── Google Calendar ──────────────────────────────────────────────────────────
def _get_calendar_service():
    if not GOOGLE_CREDS_JSON:
        return None
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        creds = service_account.Credentials.from_service_account_info(
            creds_dict, scopes=["https://www.googleapis.com/auth/calendar"]
        )
        return build("calendar", "v3", credentials=creds, cache_discovery=False)
    except Exception as e:
        logger.error(f"Calendar init error: {e}")
        return None


async def add_calendar_event(title: str, date: str, time: str = None,
                              duration: int = 60, description: str = None) -> bool:
    def _add():
        svc = _get_calendar_service()
        if not svc:
            return False
        if time:
            start_dt = f"{date}T{time}:00"
            from datetime import datetime as dt, timedelta as td
            end_dt = (dt.fromisoformat(start_dt) + td(minutes=duration)).isoformat()
            event  = {
                "summary": title,
                "start":   {"dateTime": start_dt, "timeZone": "Asia/Makassar"},
                "end":     {"dateTime": end_dt,   "timeZone": "Asia/Makassar"},
            }
        else:
            event = {
                "summary": title,
                "start":   {"date": date},
                "end":     {"date": date},
            }
        if description:
            event["description"] = description
        svc.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
        return True
    return await asyncio.get_event_loop().run_in_executor(None, _add)


async def get_today_events() -> list[dict]:
    def _get():
        svc = _get_calendar_service()
        if not svc:
            return []
        now   = datetime.now(BALI_TZ)
        start = now.replace(hour=0,  minute=0,  second=0).isoformat()
        end   = now.replace(hour=23, minute=59, second=59).isoformat()
        result = svc.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            timeMin=start, timeMax=end,
            singleEvents=True, orderBy="startTime"
        ).execute()
        events = []
        for e in result.get("items", []):
            s = e["start"].get("dateTime", e["start"].get("date", ""))
            events.append({"title": e.get("summary", ""), "time": s[11:16] if "T" in s else ""})
        return events
    return await asyncio.get_event_loop().run_in_executor(None, _get)


# ─── Claude: определить намерение ────────────────────────────────────────────
async def detect_intent(text: str) -> str:
    t = text.lower().strip()

    if t.startswith("купила ") or t.startswith("купил "):
        return "bought"

    done_words = ["сделала", "выполнила", "закрыла", "готово", "сделано", "выполнено"]
    if any(t.startswith(w) for w in done_words):
        return "done"

    edit_words = ["поменяй", "измени приоритет", "измени дедлайн", "перенеси задачу",
                  "сделай срочной", "сделай срочным", "поставь приоритет", "поставь дедлайн"]
    if any(w in t for w in edit_words):
        return "edit"

    find_words = ["найди задачу", "поищи задачу", "найди все задачи", "есть задача про",
                  "есть ли задача", "поищи про"]
    if any(w in t for w in find_words):
        return "find"

    remind_words = ["напомни мне", "поставь напоминание", "напомни про", "напомни о"]
    if any(w in t for w in remind_words):
        return "remind"

    query_words = ["что ", "покажи", "дашборд", "план на", "какие ", "срочн", "список задач"]
    if any(w in t for w in query_words):
        return "query"

    write_words = ["напиши", "составь", "помоги написать", "сделай текст", "напомни текст",
                   "напиши письмо", "напиши пост", "напиши сообщение", "придумай"]
    if any(w in t for w in write_words):
        return "write"

    cal_words = ["добавь встречу", "добавь событие", "внеси в календарь", "запланируй",
                 "напоминание на ", "встреча в ", "встреча с "]
    if any(w in t for w in cal_words):
        return "calendar"

    try:
        resp = await ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": INTENT_PROMPT.format(text=text)}],
        )
        intent = resp.content[0].text.strip().lower()
        if intent in ("query", "done", "edit", "find", "remind", "write", "calendar", "add"):
            return intent
    except Exception:
        pass
    return "add"


# ─── Claude: парсинг задач ────────────────────────────────────────────────────
async def parse_tasks(text: str) -> list[dict]:
    response = await ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    if not response.content:
        return []
    raw = response.content[0].text.strip()
    logger.info(f"Claude raw: {raw[:200]}")
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    if not raw:
        return []
    return json.loads(raw).get("tasks", [])


# ─── Notion: загрузить контекст ──────────────────────────────────────────────
async def load_context() -> str:
    now = datetime.now()
    if (_context_cache["text"]
            and _context_cache["updated_at"]
            and (now - _context_cache["updated_at"]).seconds < 3600):
        return _context_cache["text"]
    try:
        async with NotionClient(auth=NOTION_API_KEY) as notion:
            blocks = await notion.blocks.children.list(block_id=NOTION_CONTEXT_PAGE_ID)
        lines = []
        for block in blocks["results"]:
            btype = block["type"]
            rich  = block.get(btype, {}).get("rich_text", [])
            text  = "".join(r["plain_text"] for r in rich)
            if text.strip():
                lines.append(text)
        context = "\n".join(lines)
        _context_cache["text"]       = context
        _context_cache["updated_at"] = now
        return context
    except Exception as e:
        logger.error(f"Context load error: {e}")
        return _context_cache.get("text", "")


# ─── Claude: ИИ-помощник ─────────────────────────────────────────────────────
async def ai_write(prompt: str) -> str:
    context = await load_context()
    system  = WRITER_SYSTEM
    if context:
        system += f"\n\n---\nКОНТЕКСТ О МАШЕ И ЕЁ ПРОЕКТАХ:\n{context}"
    response = await ai.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


# ─── Claude: событие для календаря ───────────────────────────────────────────
async def parse_calendar_event(text: str) -> dict:
    today  = datetime.now(BALI_TZ).strftime("%Y-%m-%d (%A)")
    prompt = CALENDAR_PROMPT.format(today=today).replace("{text}", text)
    response = await ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return json.loads(raw)


# ─── Claude: чтение чека ─────────────────────────────────────────────────────
async def parse_receipt(image_data: bytes, mime_type: str) -> list[str]:
    import base64
    b64 = base64.standard_b64encode(image_data).decode()
    response = await ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64}},
            {"type": "text", "text": RECEIPT_PROMPT}
        ]}],
    )
    return json.loads(response.content[0].text.strip()).get("items", [])


# ─── Claude: парсинг редактирования ──────────────────────────────────────────
async def parse_edit_intent(text: str) -> dict:
    today  = datetime.now(BALI_TZ).strftime("%Y-%m-%d (%A)")
    prompt = EDIT_PROMPT.format(today=today, text=text)
    response = await ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return json.loads(raw)


# ─── Claude: парсинг напоминания ─────────────────────────────────────────────
async def parse_reminder_intent(text: str) -> dict:
    today  = datetime.now(BALI_TZ).strftime("%Y-%m-%d %H:%M")
    prompt = REMINDER_PROMPT.format(today=today, text=text)
    response = await ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return json.loads(raw)


# ─── Claude: ключевое слово для поиска ───────────────────────────────────────
async def parse_find_keyword(text: str) -> str:
    response = await ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=20,
        messages=[{"role": "user", "content": FIND_PROMPT.format(text=text)}],
    )
    return response.content[0].text.strip().lower()


# ─── Notion: добавить задачу ─────────────────────────────────────────────────
VALID_PROJECTS = {"Golden Goose", "Bedugul Banya", "Fairway", "FlipLab",
                  "Instagram", "General", "Shopping", "Family"}

def _resolve_project(task: dict) -> str:
    explicit = task.get("project", "")
    if task.get("shopping"):
        zone = task.get("zone", "")
        if zone == "Family":
            return "Family"
        if explicit in ("Golden Goose", "Bedugul Banya", "Fairway", "FlipLab", "Instagram"):
            return explicit
        return "Shopping"
    if explicit in VALID_PROJECTS:
        return explicit
    return "General"


async def add_to_notion(task: dict) -> None:
    project = _resolve_project(task)
    props = {
        "Task":     {"title":    [{"text": {"content": task["task"]}}]},
        "Zone":     {"select":   {"name": task.get("zone", "Personal")}},
        "Project":  {"select":   {"name": project}},
        "Priority": {"select":   {"name": task.get("priority", "Medium")}},
        "Status":   {"select":   {"name": "To Do"}},
        "Source":   {"select":   {"name": "Chat"}},
        "Shopping": {"checkbox": task.get("shopping", False)},
        "Done":     {"checkbox": False},
    }
    if task.get("deadline"):
        props["Deadline"] = {"date": {"start": task["deadline"]}}
    if task.get("notes"):
        props["Notes"] = {"rich_text": [{"text": {"content": task["notes"]}}]}
    async with NotionClient(auth=NOTION_API_KEY) as notion:
        await notion.pages.create(parent={"database_id": NOTION_DB_ID}, properties=props)


# ─── Notion: получить задачи ─────────────────────────────────────────────────
async def get_tasks(zone: str = None, priority: str = None, limit: int = 30) -> list[dict]:
    filters_list = [{"property": "Done", "checkbox": {"equals": False}}]
    if zone:
        filters_list.append({"property": "Zone", "select": {"equals": zone}})
    if priority:
        filters_list.append({"property": "Priority", "select": {"equals": priority}})
    async with NotionClient(auth=NOTION_API_KEY) as notion:
        result = await notion.databases.query(
            database_id=NOTION_DB_ID,
            filter={"and": filters_list},
            sorts=[{"property": "Priority", "direction": "ascending"},
                   {"property": "Deadline",  "direction": "ascending"}],
            page_size=limit,
        )
    tasks = []
    for page in result["results"]:
        p = page["properties"]
        tasks.append({
            "id":       page["id"],
            "task":     p["Task"]["title"][0]["text"]["content"] if p["Task"]["title"] else "?",
            "zone":     p["Zone"]["select"]["name"]     if p["Zone"]["select"]     else "",
            "priority": p["Priority"]["select"]["name"] if p["Priority"]["select"] else "",
            "deadline": p["Deadline"]["date"]["start"]  if p["Deadline"]["date"]   else None,
        })
    return tasks


# ─── Notion: поиск задачи по ключевому слову ─────────────────────────────────
async def find_tasks_by_keyword(keyword: str) -> list[dict]:
    async with NotionClient(auth=NOTION_API_KEY) as notion:
        result = await notion.databases.query(
            database_id=NOTION_DB_ID,
            filter={"and": [
                {"property": "Done",  "checkbox": {"equals": False}},
                {"property": "Task",  "title":    {"contains": keyword}},
            ]},
            page_size=20,
        )
    tasks = []
    for page in result["results"]:
        p = page["properties"]
        tasks.append({
            "id":       page["id"],
            "task":     p["Task"]["title"][0]["text"]["content"] if p["Task"]["title"] else "?",
            "zone":     p["Zone"]["select"]["name"]     if p["Zone"]["select"]     else "",
            "priority": p["Priority"]["select"]["name"] if p["Priority"]["select"] else "",
            "deadline": p["Deadline"]["date"]["start"]  if p["Deadline"]["date"]   else None,
        })
    return tasks


# ─── Notion: редактировать задачу ────────────────────────────────────────────
async def edit_task_in_notion(task_name: str, field: str, value: str) -> str:
    """Находит задачу по имени и меняет приоритет или дедлайн. Возвращает название найденной задачи."""
    async with NotionClient(auth=NOTION_API_KEY) as notion:
        result = await notion.databases.query(
            database_id=NOTION_DB_ID,
            filter={"and": [
                {"property": "Done", "checkbox": {"equals": False}},
                {"property": "Task", "title":    {"contains": task_name}},
            ]},
            page_size=5,
        )
        if not result["results"]:
            return ""
        page    = result["results"][0]
        page_id = page["id"]
        actual  = page["properties"]["Task"]["title"][0]["text"]["content"] if page["properties"]["Task"]["title"] else task_name

        if field == "priority":
            props = {"Priority": {"select": {"name": value}}}
        elif field == "deadline":
            props = {"Deadline": {"date": {"start": value}}}
        else:
            return ""

        await notion.pages.update(page_id=page_id, properties=props)
        return actual


# ─── Notion: задачи с дедлайном сегодня/завтра ───────────────────────────────
async def get_deadline_tasks() -> list[dict]:
    today    = datetime.now(BALI_TZ).strftime("%Y-%m-%d")
    tomorrow = (datetime.now(BALI_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
    async with NotionClient(auth=NOTION_API_KEY) as notion:
        result = await notion.databases.query(
            database_id=NOTION_DB_ID,
            filter={"and": [
                {"property": "Done",     "checkbox": {"equals": False}},
                {"property": "Deadline", "date":     {"on_or_before": tomorrow}},
                {"property": "Deadline", "date":     {"on_or_after":  today}},
            ]},
        )
    tasks = []
    for page in result["results"]:
        p = page["properties"]
        tasks.append({
            "task":     p["Task"]["title"][0]["text"]["content"] if p["Task"]["title"] else "?",
            "zone":     p["Zone"]["select"]["name"]     if p["Zone"]["select"]     else "",
            "deadline": p["Deadline"]["date"]["start"]  if p["Deadline"]["date"]   else None,
        })
    return tasks


# ─── Notion: шопинг из основной таблицы (если нет отдельной) ─────────────────
async def get_shopping_list() -> dict[str, list[str]]:
    """Шопинг из основной таблицы задач (старый режим)."""
    async with NotionClient(auth=NOTION_API_KEY) as notion:
        result = await notion.databases.query(
            database_id=NOTION_DB_ID,
            filter={"and": [
                {"property": "Shopping", "checkbox": {"equals": True}},
                {"property": "Done",     "checkbox": {"equals": False}},
            ]}
        )
    by_zone: dict[str, list[str]] = {}
    for page in result["results"]:
        p    = page["properties"]
        task = p["Task"]["title"][0]["text"]["content"] if p["Task"]["title"] else "?"
        zone = p["Zone"]["select"]["name"] if p["Zone"]["select"] else "Другое"
        by_zone.setdefault(zone, []).append(task)
    return by_zone


# ─── Notion: отдельная таблица покупок ───────────────────────────────────────
async def add_to_shopping_db(item: str, store: str = "Другое",
                              zone: str = "Home", notes: str = None) -> None:
    """Добавляет покупку в отдельную таблицу Shopping List."""
    props = {
        "Item":  {"title":  [{"text": {"content": item}}]},
        "Store": {"select": {"name": store}},
        "Zone":  {"select": {"name": zone}},
        "Done":  {"checkbox": False},
    }
    if notes:
        props["Notes"] = {"rich_text": [{"text": {"content": notes}}]}
    async with NotionClient(auth=NOTION_API_KEY) as notion:
        await notion.pages.create(parent={"database_id": NOTION_SHOPPING_DB_ID}, properties=props)


async def get_shopping_db_list() -> dict[str, list[str]]:
    """Шопинг из отдельной таблицы Shopping List, разбитый по магазинам."""
    async with NotionClient(auth=NOTION_API_KEY) as notion:
        result = await notion.databases.query(
            database_id=NOTION_SHOPPING_DB_ID,
            filter={"property": "Done", "checkbox": {"equals": False}},
            sorts=[{"property": "Store", "direction": "ascending"}],
        )
    by_store: dict[str, list[str]] = {}
    for page in result["results"]:
        p     = page["properties"]
        item  = p["Item"]["title"][0]["text"]["content"] if p["Item"]["title"] else "?"
        store = p["Store"]["select"]["name"] if p["Store"]["select"] else "Другое"
        by_store.setdefault(store, []).append(item)
    return by_store


async def mark_shopping_db_done(item_name: str) -> bool:
    """Отмечает покупку в отдельной таблице как выполненную."""
    async with NotionClient(auth=NOTION_API_KEY) as notion:
        result = await notion.databases.query(
            database_id=NOTION_SHOPPING_DB_ID,
            filter={"and": [
                {"property": "Done", "checkbox": {"equals": False}},
                {"property": "Item", "title":    {"contains": item_name.lower()}},
            ]},
            page_size=5,
        )
        if not result["results"]:
            return False
        await notion.pages.update(
            page_id=result["results"][0]["id"],
            properties={"Done": {"checkbox": True}}
        )
        return True


# ─── Notion: отметить как выполненное ────────────────────────────────────────
async def mark_done_by_name(task_name: str, shopping_only: bool = False) -> bool:
    filters_list = [{"property": "Done", "checkbox": {"equals": False}}]
    if shopping_only:
        filters_list.append({"property": "Shopping", "checkbox": {"equals": True}})
    async with NotionClient(auth=NOTION_API_KEY) as notion:
        result = await notion.databases.query(
            database_id=NOTION_DB_ID, filter={"and": filters_list}
        )
        task_lower = task_name.lower()
        for page in result["results"]:
            title = page["properties"]["Task"]["title"]
            if title and task_lower in title[0]["text"]["content"].lower():
                await notion.pages.update(
                    page_id=page["id"],
                    properties={"Done": {"checkbox": True}}
                )
                return True
    return False


# ─── Напоминания ─────────────────────────────────────────────────────────────
def _save_reminder(remind_text: str, remind_at_iso: str) -> None:
    reminders = []
    try:
        if os.path.exists(REMINDERS_FILE):
            with open(REMINDERS_FILE, "r") as f:
                reminders = json.load(f)
    except Exception:
        pass
    reminders.append({"text": remind_text, "at": remind_at_iso})
    with open(REMINDERS_FILE, "w") as f:
        json.dump(reminders, f)


def _load_and_schedule_reminders(scheduler: AsyncIOScheduler, bot) -> None:
    if not os.path.exists(REMINDERS_FILE):
        return
    try:
        with open(REMINDERS_FILE, "r") as f:
            reminders = json.load(f)
    except Exception:
        return
    now = datetime.now(BALI_TZ)
    future = []
    for r in reminders:
        try:
            remind_at = datetime.fromisoformat(r["at"]).replace(tzinfo=BALI_TZ)
            if remind_at > now:
                scheduler.add_job(
                    _send_reminder_message, trigger="date",
                    run_date=remind_at,
                    kwargs={"bot": bot, "text": r["text"]},
                )
                future.append(r)
        except Exception:
            pass
    with open(REMINDERS_FILE, "w") as f:
        json.dump(future, f)
    logger.info(f"Loaded {len(future)} pending reminders")


async def _send_reminder_message(bot, text: str) -> None:
    await bot.send_message(chat_id=CHAT_ID, text=f"⏰ *Напоминание:* {text}", parse_mode="Markdown")


def schedule_reminder(scheduler: AsyncIOScheduler, bot,
                      remind_text: str, remind_at: datetime) -> None:
    scheduler.add_job(
        _send_reminder_message, trigger="date",
        run_date=remind_at,
        kwargs={"bot": bot, "text": remind_text},
    )
    _save_reminder(remind_text, remind_at.isoformat())


# ─── Форматирование ───────────────────────────────────────────────────────────
def format_task_list(tasks: list[dict], title: str) -> str:
    if not tasks:
        return f"{title}\n\n🎉 Нет открытых задач!"
    by_priority = {"Urgent": [], "High": [], "Medium": [], "Low": [], "": []}
    for t in tasks:
        by_priority.setdefault(t["priority"], []).append(t)
    lines  = [title, ""]
    labels = {"Urgent": "Срочно", "High": "Важно", "Medium": "Скоро", "Low": "Когда-нибудь"}
    for prio in ["Urgent", "High", "Medium", "Low", ""]:
        items = by_priority.get(prio, [])
        if not items:
            continue
        lines.append(f"{PRIORITY_EMOJI.get(prio,'•')} {labels.get(prio,'Остальное')}:")
        for t in items:
            dl = f" · до {t['deadline']}" if t.get("deadline") else ""
            lines.append(f"  {ZONE_EMOJI.get(t['zone'],'')} {t['task']}{dl}")
        lines.append("")
    lines.append("_Напиши «сделала [название]» чтобы закрыть_")
    return "\n".join(lines)


def format_dashboard(tasks: list[dict]) -> str:
    if not tasks:
        return "📊 Дашборд\n\n🎉 Нет открытых задач!"
    by_zone:  dict[str, list] = {}
    urgent_all = []
    for t in tasks:
        by_zone.setdefault(t["zone"], []).append(t)
        if t["priority"] == "Urgent":
            urgent_all.append(t)
    lines = ["📊 *Дашборд задач*\n"]
    for z in ["Golden Goose", "Fairway", "FlipLab", "Personal", "Kids", "Home",
               "Finance", "Family", "Travel", "Property", "Docs/Visa"]:
        items = by_zone.get(z, [])
        if items:
            u = sum(1 for t in items if t["priority"] == "Urgent")
            lines.append(f"{ZONE_EMOJI.get(z,'')} {z} — {len(items)} задач{' · ' + str(u) + ' срочных 🔴' if u else ''}")
    if urgent_all:
        lines.append("\n⚡ *Срочно:*")
        for t in urgent_all[:5]:
            dl = f" · до {t['deadline']}" if t.get("deadline") else ""
            lines.append(f"  🔴 {t['task']}{dl}")
    lines.append(f"\nВсего: {len(tasks)}")
    return "\n".join(lines)


def format_shopping_by_zone(by_zone: dict[str, list[str]], title: str = None) -> str:
    """Шопинг-лист, сгруппированный по зоне (для основной таблицы)."""
    if not by_zone:
        return "🛒 Список покупок пуст — всё куплено! 🎉"
    today = datetime.now(BALI_TZ)
    lines = [title or f"🛒 Шопинг-лист — {today.strftime('%d %B')}\n"]
    for zone, tasks in sorted(by_zone.items()):
        lines.append(f"\n{ZONE_EMOJI.get(zone,'•')} {zone}:")
        for t in tasks:
            lines.append(f"  ☐ {t}")
    lines.append("\n\n_Напиши «купила [название]» или пришли фото чека_")
    return "\n".join(lines)


def format_shopping_by_store(by_store: dict[str, list[str]]) -> str:
    """Шопинг-лист из отдельной таблицы, сгруппированный по магазину."""
    if not by_store:
        return "🛒 Список покупок пуст — всё куплено! 🎉"
    today = datetime.now(BALI_TZ)
    lines = [f"🛒 *Шопинг-лист — {today.strftime('%d %B')}*\n"]
    for store in ["Папайя", "Рынок", "Онлайн", "Другое"]:
        items = by_store.get(store, [])
        if not items:
            continue
        lines.append(f"{STORE_EMOJI.get(store,'🛍️')} *{store}:*")
        for item in items:
            lines.append(f"  ☐ {item}")
        lines.append("")
    lines.append("_Напиши «купила [название]» или пришли фото чека_")
    return "\n".join(lines)


def format_reply(added: list[dict], failed: list[str]) -> str:
    if not added and not failed:
        return "Не нашла задач 🤔 Попробуй написать конкретнее."
    count = len(added)
    word  = "задачу" if count == 1 else ("задачи" if 2 <= count <= 4 else "задач")
    lines = [f"✅ Добавила {count} {word} в Notion:\n"]
    for t in added:
        dl   = f"  · до {t['deadline']}" if t.get("deadline") else ""
        cart = " 🛒" if t.get("shopping") else ""
        lines.append(f"{ZONE_EMOJI.get(t.get('zone',''),'•')}{PRIORITY_EMOJI.get(t.get('priority',''),'')} {t['task']}{cart}{dl}")
    if failed:
        lines.append(f"\n⚠️ Не удалось добавить: {', '.join(failed)}")
    return "\n".join(lines)


# ─── Обработка запроса ────────────────────────────────────────────────────────
async def handle_query(text: str) -> str:
    t = text.lower()
    if "дашборд" in t or "dashboard" in t or "все задачи" in t or "общая картина" in t:
        tasks = await get_tasks(limit=100)
        return format_dashboard(tasks)
    zone = None
    for alias, zone_name in ZONE_ALIASES.items():
        if alias in t:
            zone = zone_name
            break
    priority = None
    if "срочн" in t:
        priority = "Urgent"
    elif "важн" in t:
        priority = "High"
    tasks = await get_tasks(zone=zone, priority=priority, limit=30)
    if zone and priority:
        title = f"📋 {ZONE_EMOJI.get(zone,'')} {zone} — {PRIORITY_EMOJI.get(priority,'')} {priority}"
    elif zone:
        title = f"📋 {ZONE_EMOJI.get(zone,'')} {zone} — открытые задачи"
    elif priority:
        title = f"📋 {PRIORITY_EMOJI.get(priority,'')} Срочные задачи"
    else:
        title = "📋 Открытые задачи"
    return format_task_list(tasks, title)


# ─── Утренний брифинг ─────────────────────────────────────────────────────────
async def send_morning_briefing(bot, include_shopping: bool = False) -> None:
    now   = datetime.now(BALI_TZ)
    lines = [f"☀️ *Доброе утро, Маша!*\n_{now.strftime('%A, %d %B')}_\n"]

    events = await get_today_events()
    if events:
        lines.append("📅 *Сегодня в календаре:*")
        for e in events:
            t = f" в {e['time']}" if e["time"] else ""
            lines.append(f"  • {e['title']}{t}")
        lines.append("")

    urgent = await get_tasks(priority="Urgent", limit=10)
    if urgent:
        lines.append("🔴 *Срочно:*")
        for t in urgent[:5]:
            dl = f" · до {t['deadline']}" if t.get("deadline") else ""
            lines.append(f"  {ZONE_EMOJI.get(t['zone'],'')} {t['task']}{dl}")
        lines.append("")

    deadline_tasks = await get_deadline_tasks()
    if deadline_tasks:
        lines.append("⏰ *Дедлайны сегодня/завтра:*")
        for t in deadline_tasks:
            dl = "сегодня" if t["deadline"] == now.strftime("%Y-%m-%d") else "завтра"
            lines.append(f"  {ZONE_EMOJI.get(t['zone'],'')} {t['task']} — {dl}")
        lines.append("")

    if not urgent and not deadline_tasks and not events:
        lines.append("✨ Срочных задач нет — хороший день!")

    if include_shopping:
        if NOTION_SHOPPING_DB_ID:
            by_store = await get_shopping_db_list()
            if by_store:
                lines.append(format_shopping_by_store(by_store))
        else:
            by_zone = await get_shopping_list()
            if by_zone:
                lines.append(format_shopping_by_zone(by_zone, title="🛒 *Шопинг-лист:*"))

    await bot.send_message(chat_id=CHAT_ID, text="\n".join(lines), parse_mode="Markdown")


# ─── Хэндлер: текстовые сообщения ────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = str(update.effective_user.id)
    if ALLOWED_IDS and uid not in ALLOWED_IDS:
        await update.message.reply_text("⛔ Нет доступа.")
        return

    text = update.message.text.strip()
    t    = text.lower()

    # Шопинг-лист из Notion
    shop_triggers = ["шопинг лист", "шопинг-лист", "список покупок", "пришли шопинг",
                     "покажи шопинг", "что купить", "шоп лист"]
    if any(w in t for w in shop_triggers):
        thinking = await update.message.reply_text("⏳ Загружаю...")
        if NOTION_SHOPPING_DB_ID:
            by_store = await get_shopping_db_list()
            await thinking.edit_text(format_shopping_by_store(by_store), parse_mode="Markdown")
        else:
            by_zone = await get_shopping_list()
            await thinking.edit_text(format_shopping_by_zone(by_zone), parse_mode="Markdown")
        return

    # «Купила X»
    if t.startswith("купила ") or t.startswith("купил "):
        item = text.split(" ", 1)[1].strip()
        # Сначала ищем в отдельной таблице, потом в основной
        found = False
        if NOTION_SHOPPING_DB_ID:
            found = await mark_shopping_db_done(item)
        if not found:
            found = await mark_done_by_name(item, shopping_only=True)
        msg = f"✅ Отметила как куплено: {item}" if found else f"🤔 Не нашла «{item}» в списке покупок."
        await update.message.reply_text(msg)
        return

    # «Сделала X»
    done_prefixes = ["сделала ", "выполнила ", "закрыла ", "готово — ", "готово: "]
    for prefix in done_prefixes:
        if t.startswith(prefix):
            item  = text[len(prefix):].strip()
            found = await mark_done_by_name(item)
            msg   = f"✅ Закрыла задачу: {item}" if found else f"🤔 Не нашла задачу «{item}»."
            await update.message.reply_text(msg)
            return

    # «Отметь X выполненной» / «закрой задачу X»
    done_patterns = [
        r"отметь\s+сделан\w*[:\s]+(.+)",
        r"пометь\s+сделан\w*[:\s]+(.+)",
        r"отметь\s+выполнен\w*[:\s]+(.+)",
        r"отметь\s+(.+?)\s+(?:как\s+)?(?:выполнен\w*|готов\w*|сделан\w*)",
        r"пометь\s+(.+?)\s+(?:как\s+)?(?:выполнен\w*|готов\w*|сделан\w*)",
        r"закрой\s+(?:задачу\s+)?(.+)",
    ]
    for pattern in done_patterns:
        m = re.search(pattern, t)
        if m:
            item  = m.group(1).strip()
            start = t.find(item)
            if start >= 0:
                item = text[start:start + len(item)]
            found = await mark_done_by_name(item)
            if found:
                await update.message.reply_text(f"✅ Закрыла задачу: {item}")
                return
            break

    thinking = await update.message.reply_text("⏳ Думаю...")
    intent   = await detect_intent(text)
    logger.info(f"Intent: {intent} | {text[:60]}")

    # Запрос задач
    if intent == "query":
        try:
            reply = await handle_query(text)
            await thinking.edit_text(reply, parse_mode="Markdown")
        except Exception as e:
            logger.exception("Query error")
            await thinking.edit_text(f"❌ Ошибка: {e}")
        return

    # Поиск задачи по ключевому слову
    if intent == "find":
        try:
            keyword = await parse_find_keyword(text)
            tasks   = await find_tasks_by_keyword(keyword)
            if tasks:
                reply = format_task_list(tasks, f"🔍 Задачи по «{keyword}»")
            else:
                reply = f"🔍 Не нашла задач по «{keyword}»."
            await thinking.edit_text(reply, parse_mode="Markdown")
        except Exception as e:
            logger.exception("Find error")
            await thinking.edit_text(f"❌ Ошибка поиска: {e}")
        return

    # Редактирование задачи
    if intent == "edit":
        try:
            edit = await parse_edit_intent(text)
            task_found = await edit_task_in_notion(
                task_name=edit["task_name"],
                field=edit["field"],
                value=edit["value"],
            )
            if task_found:
                field_ru = "приоритет" if edit["field"] == "priority" else "дедлайн"
                await thinking.edit_text(f"✏️ Обновила {field_ru} задачи:\n«{task_found}» → {edit['value']}")
            else:
                await thinking.edit_text(f"🤔 Не нашла задачу «{edit['task_name']}».")
        except Exception as e:
            logger.exception("Edit error")
            await thinking.edit_text(f"❌ Ошибка: {e}")
        return

    # Напоминание
    if intent == "remind":
        try:
            reminder = await parse_reminder_intent(text)
            remind_at = datetime.fromisoformat(reminder["datetime"]).replace(tzinfo=BALI_TZ)
            schedule_reminder(context.application.scheduler, context.bot,
                              reminder["text"], remind_at)
            time_str = remind_at.strftime("%d %B в %H:%M")
            await thinking.edit_text(f"⏰ Напомню {time_str}:\n_{reminder['text']}_",
                                     parse_mode="Markdown")
        except Exception as e:
            logger.exception("Remind error")
            await thinking.edit_text(f"❌ Ошибка: {e}")
        return

    # ИИ-помощник
    if intent == "write":
        try:
            result = await ai_write(text)
            await thinking.edit_text(f"✍️ Готово:\n\n{result}")
        except Exception as e:
            logger.exception("Write error")
            await thinking.edit_text(f"❌ Ошибка: {e}")
        return

    # Календарь
    if intent == "calendar":
        try:
            event   = await parse_calendar_event(text)
            title   = event.get("title", text)
            date    = event.get("date")
            if not date:
                await thinking.edit_text("📅 Не поняла дату — уточни, пожалуйста.")
                return
            success = await add_calendar_event(
                title=title, date=date,
                time=event.get("time"),
                duration=event.get("duration_minutes", 60),
                description=event.get("description"),
            )
            if success:
                time_str = f" в {event['time']}" if event.get("time") else ""
                await thinking.edit_text(f"📅 Добавила в календарь:\n{title}\n{date}{time_str}")
            else:
                await thinking.edit_text("⚠️ Календарь не подключён.")
        except Exception as e:
            logger.exception("Calendar error")
            await thinking.edit_text(f"❌ Ошибка календаря: {e}")
        return

    # Добавление задач
    try:
        tasks = await parse_tasks(text)
    except Exception as e:
        logger.exception("Parse error")
        await thinking.edit_text(f"❌ Ошибка Claude: {e}")
        return

    added, failed = [], []
    for task in tasks:
        try:
            await add_to_notion(task)
            added.append(task)
        except Exception as e:
            logger.error(f"Notion error «{task.get('task')}»: {e}")
            failed.append(task.get("task", "?"))
    await thinking.edit_text(format_reply(added, failed))


# ─── Хэндлер: голосовые ──────────────────────────────────────────────────────
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = str(update.effective_user.id)
    if ALLOWED_IDS and uid not in ALLOWED_IDS:
        return
    if not oai:
        await update.message.reply_text("🎤 Голосовые не настроены. Добавь OPENAI_API_KEY.")
        return

    thinking = await update.message.reply_text("🎤 Слушаю...")
    try:
        file  = await context.bot.get_file(update.message.voice.file_id)
        data  = bytes(await file.download_as_bytearray())
        audio = io.BytesIO(data)
        audio.name  = "voice.ogg"
        transcript  = await oai.audio.transcriptions.create(model="whisper-1", file=audio, language="ru")
        text = transcript.text
        logger.info(f"Voice: {text}")
    except Exception as e:
        await thinking.edit_text(f"❌ Не смогла распознать: {e}")
        return

    await thinking.edit_text(f"🎤 «{text}»\n⏳ Разбираю...")

    tl = text.lower()
    shop_triggers = ["шопинг лист", "шопинг-лист", "список покупок", "пришли шопинг",
                     "покажи шопинг", "что купить", "шоп лист"]
    if any(w in tl for w in shop_triggers):
        if NOTION_SHOPPING_DB_ID:
            by_store = await get_shopping_db_list()
            await thinking.edit_text(f"🎤 «{text}»\n\n" + format_shopping_by_store(by_store),
                                     parse_mode="Markdown")
        else:
            by_zone = await get_shopping_list()
            await thinking.edit_text(f"🎤 «{text}»\n\n" + format_shopping_by_zone(by_zone),
                                     parse_mode="Markdown")
        return

    intent = await detect_intent(text)

    if intent == "query":
        reply = await handle_query(text)
        await thinking.edit_text(f"🎤 «{text}»\n\n{reply}", parse_mode="Markdown")
        return

    if intent == "find":
        keyword = await parse_find_keyword(text)
        tasks   = await find_tasks_by_keyword(keyword)
        reply   = format_task_list(tasks, f"🔍 Задачи по «{keyword}»") if tasks else f"🔍 Не нашла задач по «{keyword}»."
        await thinking.edit_text(f"🎤 «{text}»\n\n{reply}", parse_mode="Markdown")
        return

    if intent == "edit":
        edit = await parse_edit_intent(text)
        task_found = await edit_task_in_notion(edit["task_name"], edit["field"], edit["value"])
        if task_found:
            field_ru = "приоритет" if edit["field"] == "priority" else "дедлайн"
            await thinking.edit_text(f"🎤 «{text}»\n\n✏️ Обновила {field_ru}: «{task_found}» → {edit['value']}")
        else:
            await thinking.edit_text(f"🎤 «{text}»\n\n🤔 Не нашла задачу «{edit['task_name']}».")
        return

    if intent == "remind":
        reminder  = await parse_reminder_intent(text)
        remind_at = datetime.fromisoformat(reminder["datetime"]).replace(tzinfo=BALI_TZ)
        schedule_reminder(context.application.scheduler, context.bot, reminder["text"], remind_at)
        time_str  = remind_at.strftime("%d %B в %H:%M")
        await thinking.edit_text(f"🎤 «{text}»\n\n⏰ Напомню {time_str}: {reminder['text']}")
        return

    if intent == "write":
        result = await ai_write(text)
        await thinking.edit_text(f"🎤 «{text}»\n\n✍️ {result}")
        return

    if intent == "calendar":
        event   = await parse_calendar_event(text)
        success = await add_calendar_event(
            title=event.get("title", text),
            date=event.get("date", ""),
            time=event.get("time"),
            duration=event.get("duration_minutes", 60),
        )
        if success:
            await thinking.edit_text(f"📅 Добавила в календарь: {event.get('title')}")
        else:
            await thinking.edit_text("⚠️ Календарь не подключён.")
        return

    tasks = await parse_tasks(text)
    added, failed = [], []
    for task in tasks:
        try:
            await add_to_notion(task)
            added.append(task)
        except Exception as e:
            failed.append(task.get("task", "?"))
    await thinking.edit_text(f"🎤 «{text}»\n\n" + format_reply(added, failed))


# ─── Хэндлер: фото чека ──────────────────────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = str(update.effective_user.id)
    if ALLOWED_IDS and uid not in ALLOWED_IDS:
        return
    thinking = await update.message.reply_text("🧾 Читаю чек...")
    try:
        photo = update.message.photo[-1]
        file  = await context.bot.get_file(photo.file_id)
        data  = bytes(await file.download_as_bytearray())
        items = await parse_receipt(data, "image/jpeg")
    except Exception as e:
        await thinking.edit_text(f"❌ Не смогла прочитать чек: {e}")
        return

    marked = []
    for i in items:
        found = False
        if NOTION_SHOPPING_DB_ID:
            found = await mark_shopping_db_done(i)
        if not found:
            found = await mark_done_by_name(i, shopping_only=True)
        if found:
            marked.append(i)

    if marked:
        await thinking.edit_text("✅ Отметила:\n" + "\n".join(f"• {i}" for i in marked))
    else:
        await thinking.edit_text("🧾 На чеке:\n" + "\n".join(f"• {i}" for i in items) +
                                 "\n\nНе нашла в списке покупок.")


# ─── Команды ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Привет, Маша!\n\n"
        "📥 *Добавить задачи:*\n"
        "  Просто напиши или надиктуй\n\n"
        "📤 *Посмотреть задачи:*\n"
        "  «что срочного», «что по гусю», «дашборд»\n\n"
        "✅ *Закрыть:*\n"
        "  «сделала X», «купила X», фото чека 🧾\n\n"
        "✏️ *Редактировать:*\n"
        "  «поменяй приоритет X на срочный»\n"
        "  «перенеси X на пятницу»\n\n"
        "🔍 *Найти:*\n"
        "  «найди задачу про сайт»\n\n"
        "⏰ *Напомни:*\n"
        "  «напомни про звонок с Роби завтра в 10:00»\n\n"
        "✍️ *ИИ-помощник:*\n"
        "  «напиши письмо/пост/сообщение»\n\n"
        "📅 *Календарь:*\n"
        "  «добавь встречу с Роби в пятницу в 11:00»\n\n"
        "Команды: /shop /tasks /help",
        parse_mode="Markdown"
    )


async def cmd_shop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = str(update.effective_user.id)
    if ALLOWED_IDS and uid not in ALLOWED_IDS:
        return
    thinking = await update.message.reply_text("⏳ Загружаю...")
    if NOTION_SHOPPING_DB_ID:
        by_store = await get_shopping_db_list()
        await thinking.edit_text(format_shopping_by_store(by_store), parse_mode="Markdown")
    else:
        by_zone = await get_shopping_list()
        await thinking.edit_text(format_shopping_by_zone(by_zone), parse_mode="Markdown")


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = str(update.effective_user.id)
    if ALLOWED_IDS and uid not in ALLOWED_IDS:
        return
    thinking = await update.message.reply_text("⏳ Загружаю...")
    tasks = await get_tasks(limit=50)
    await thinking.edit_text(format_dashboard(tasks), parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📋 *Что умею:*\n\n"
        "Добавить задачи — просто напиши или надиктуй\n"
        "Запросить — «что срочного», «что по Fairway», «дашборд»\n"
        "Закрыть — «сделала X», «купила X»\n"
        "Редактировать — «поменяй приоритет X на срочный», «перенеси X на пятницу»\n"
        "Найти — «найди задачу про сайт»\n"
        "Напомни — «напомни про встречу с Роби завтра в 10:00»\n"
        "Написать текст — «напиши письмо/пост/сообщение»\n"
        "Добавить в календарь — «добавь встречу X в пятницу в 11:00»\n\n"
        "/shop — список покупок\n"
        "/tasks — дашборд\n\n"
        "Каждое утро в 9:00 — брифинг ☀️",
        parse_mode="Markdown"
    )


# ─── Запуск ───────────────────────────────────────────────────────────────────
def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CommandHandler("shop",  cmd_shop))
    app.add_handler(CommandHandler("tasks", cmd_tasks))

    app.add_handler(MessageHandler(filters.TEXT  & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    scheduler = AsyncIOScheduler(timezone=BALI_TZ)

    # Сохраняем scheduler в app, чтобы хэндлеры могли добавлять задания
    app.scheduler = scheduler

    # Утренний брифинг — каждый день в 9:00
    scheduler.add_job(
        send_morning_briefing, trigger="cron",
        hour=9, minute=0,
        kwargs={"bot": app.bot, "include_shopping": False}
    )
    # По пн и чт — брифинг + шопинг
    scheduler.add_job(
        send_morning_briefing, trigger="cron",
        day_of_week="mon,thu", hour=9, minute=0,
        kwargs={"bot": app.bot, "include_shopping": True}
    )
    scheduler.start()

    # Восстанавливаем сохранённые напоминания после рестарта
    _load_and_schedule_reminders(scheduler, app.bot)

    logger.info("🤖 Bot started!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
