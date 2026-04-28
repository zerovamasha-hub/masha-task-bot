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
TELEGRAM_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
NOTION_API_KEY    = os.environ["NOTION_API_KEY"]
NOTION_DB_ID      = os.environ["NOTION_DATABASE_ID"]
CHAT_ID           = os.environ["CHAT_ID"]

_raw_ids = os.environ.get("ALLOWED_USER_IDS", "")
ALLOWED_IDS = set(x.strip() for x in _raw_ids.split(",") if x.strip())

BALI_TZ = pytz.timezone("Asia/Makassar")

# Google Calendar (опционально)
_gcreds_b64        = os.environ.get("GOOGLE_CREDENTIALS_BASE64", "")
GOOGLE_CREDS_JSON  = ""
if _gcreds_b64:
    import base64 as _b64
    GOOGLE_CREDS_JSON = _b64.b64decode(_gcreds_b64).decode("utf-8")
GOOGLE_CALENDAR_ID  = os.environ.get("GOOGLE_CALENDAR_ID", "primary")

# Notion: страница с контекстом о Маше
NOTION_CONTEXT_PAGE_ID = "3506e819-ab77-814c-b2bf-cecb528867ad"

# ─── Кеш контекста (обновляется раз в час) ───────────────────────────────────
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

- "query"    — asking about tasks (что срочного, покажи, дашборд, что по X, план)
- "done"     — marking task complete (сделала, выполнила, закрыла, готово)
- "write"    — asking to write/draft something (напиши, составь, помоги написать, сделай текст, напомни)
- "calendar" — adding event to calendar (добавь встречу, запланируй, внеси в календарь, напоминание на)
- "add"      — adding new tasks

Message: {text}

Reply ONLY: query / done / write / calendar / add"""

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


# ─── Google Calendar ──────────────────────────────────────────────────────────
def _get_calendar_service():
    if not GOOGLE_CREDS_JSON:
        return None
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        creds = service_account.Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/calendar"]
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
            end_dt_obj = dt.fromisoformat(start_dt) + td(minutes=duration)
            end_dt = end_dt_obj.isoformat()
            event = {
                "summary": title,
                "start": {"dateTime": start_dt, "timeZone": "Asia/Makassar"},
                "end":   {"dateTime": end_dt,   "timeZone": "Asia/Makassar"},
            }
        else:
            event = {
                "summary": title,
                "start": {"date": date},
                "end":   {"date": date},
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
        now = datetime.now(BALI_TZ)
        start = now.replace(hour=0, minute=0, second=0).isoformat()
        end   = now.replace(hour=23, minute=59, second=59).isoformat()
        result = svc.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            timeMin=start, timeMax=end,
            singleEvents=True, orderBy="startTime"
        ).execute()
        events = []
        for e in result.get("items", []):
            s = e["start"].get("dateTime", e["start"].get("date", ""))
            time_str = s[11:16] if "T" in s else ""
            events.append({"title": e.get("summary", ""), "time": time_str})
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
        if intent in ("query", "done", "write", "calendar", "add"):
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
    """Читает страницу 🧠 Контекст из Notion. Кешируется на 1 час."""
    now = datetime.now()
    if (_context_cache["text"]
            and _context_cache["updated_at"]
            and (now - _context_cache["updated_at"]).seconds < 3600):
        return _context_cache["text"]

    try:
        async with NotionClient(auth=NOTION_API_KEY) as notion:
            page    = await notion.pages.retrieve(page_id=NOTION_CONTEXT_PAGE_ID)
            blocks  = await notion.blocks.children.list(block_id=NOTION_CONTEXT_PAGE_ID)

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
        logger.info(f"Context loaded: {len(context)} chars")
        return context
    except Exception as e:
        logger.error(f"Context load error: {e}")
        return _context_cache.get("text", "")


# ─── Claude: ИИ-помощник (написать текст) ────────────────────────────────────
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


# ─── Claude: распознать событие для календаря ────────────────────────────────
async def parse_calendar_event(text: str) -> dict:
    today = datetime.now(BALI_TZ).strftime("%Y-%m-%d (%A)")
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


# ─── Notion: добавить задачу ──────────────────────────────────────────────────
VALID_PROJECTS = {"Golden Goose", "Bedugul Banya", "Fairway", "FlipLab",
                  "Instagram", "General", "Shopping", "Family"}

def _resolve_project(task: dict) -> str:
    """Определяет Project для задачи. Покупки → Shopping/Family. Неизвестное → General."""
    explicit = task.get("project", "")
    if task.get("shopping"):
        zone = task.get("zone", "")
        if zone == "Family":
            return "Family"
        if explicit in ("Golden Goose", "Bedugul Banya", "Fairway", "FlipLab", "Instagram"):
            return explicit
        return "Shopping"
    # Если проект не из известного списка или пустой — General
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


# ─── Notion: получить задачи ──────────────────────────────────────────────────
async def get_tasks(zone: str = None, priority: str = None, limit: int = 30) -> list[dict]:
    filters_list = [
        {"property": "Done", "checkbox": {"equals": False}},
    ]
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


# ─── Notion: шопинг-лист ─────────────────────────────────────────────────────
async def get_shopping_list() -> dict[str, list[str]]:
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


# ─── Форматирование ───────────────────────────────────────────────────────────
def format_task_list(tasks: list[dict], title: str) -> str:
    if not tasks:
        return f"{title}\n\n🎉 Нет открытых задач!"
    by_priority = {"Urgent": [], "High": [], "Medium": [], "Low": [], "": []}
    for t in tasks:
        by_priority.setdefault(t["priority"], []).append(t)
    lines = [title, ""]
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
    by_zone: dict[str, list] = {}
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
        lines.append(f"\n⚡ *Срочно:*")
        for t in urgent_all[:5]:
            dl = f" · до {t['deadline']}" if t.get("deadline") else ""
            lines.append(f"  🔴 {t['task']}{dl}")
    lines.append(f"\nВсего: {len(tasks)}")
    return "\n".join(lines)


def format_shopping_list(by_zone: dict[str, list[str]], title: str = None) -> str:
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


# ─── Обработка запроса (чтение из Notion) ────────────────────────────────────
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

    # Календарь на сегодня
    events = await get_today_events()
    if events:
        lines.append("📅 *Сегодня в календаре:*")
        for e in events:
            t = f" в {e['time']}" if e["time"] else ""
            lines.append(f"  • {e['title']}{t}")
        lines.append("")

    # Срочные задачи
    urgent = await get_tasks(priority="Urgent", limit=10)
    if urgent:
        lines.append("🔴 *Срочно:*")
        for t in urgent[:5]:
            dl = f" · до {t['deadline']}" if t.get("deadline") else ""
            lines.append(f"  {ZONE_EMOJI.get(t['zone'],'')} {t['task']}{dl}")
        lines.append("")

    # Горящие дедлайны
    deadline_tasks = await get_deadline_tasks()
    if deadline_tasks:
        lines.append("⏰ *Дедлайны сегодня/завтра:*")
        for t in deadline_tasks:
            dl = "сегодня" if t["deadline"] == now.strftime("%Y-%m-%d") else "завтра"
            lines.append(f"  {ZONE_EMOJI.get(t['zone'],'')} {t['task']} — {dl}")
        lines.append("")

    if not urgent and not deadline_tasks and not events:
        lines.append("✨ Срочных задач нет — хороший день!")

    # Шопинг (только пн и чт)
    if include_shopping:
        by_zone = await get_shopping_list()
        if by_zone:
            lines.append(format_shopping_list(by_zone, title="🛒 *Шопинг-лист:*"))

    text = "\n".join(lines)
    await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")


# ─── Хэндлер: текстовые сообщения ────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = str(update.effective_user.id)
    if ALLOWED_IDS and uid not in ALLOWED_IDS:
        await update.message.reply_text("⛔ Нет доступа.")
        return

    text = update.message.text.strip()
    t    = text.lower()

    # «Купила X»
    if t.startswith("купила ") or t.startswith("купил "):
        item  = text.split(" ", 1)[1].strip()
        found = await mark_done_by_name(item, shopping_only=True)
        msg   = f"✅ Отметила как куплено: {item}" if found else f"🤔 Не нашла «{item}» в списке покупок."
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
                item = text[start:start+len(item)]
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

    # ИИ-помощник — написать текст
    if intent == "write":
        try:
            result = await ai_write(text)
            await thinking.edit_text(f"✍️ Готово:\n\n{result}")
        except Exception as e:
            logger.exception("Write error")
            await thinking.edit_text(f"❌ Ошибка: {e}")
        return

    # Добавить событие в календарь
    if intent == "calendar":
        try:
            event = await parse_calendar_event(text)
            title = event.get("title", text)
            date  = event.get("date")
            if not date:
                await thinking.edit_text("📅 Не поняла дату — уточни, пожалуйста.")
                return
            success = await add_calendar_event(
                title=title,
                date=date,
                time=event.get("time"),
                duration=event.get("duration_minutes", 60),
                description=event.get("description"),
            )
            if success:
                time_str = f" в {event['time']}" if event.get("time") else ""
                await thinking.edit_text(f"📅 Добавила в календарь:\n{title}\n{date}{time_str}")
            else:
                await thinking.edit_text("⚠️ Календарь не подключён. Добавь GOOGLE_SERVICE_ACCOUNT_JSON.")
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
        audio.name = "voice.ogg"
        transcript = await oai.audio.transcriptions.create(model="whisper-1", file=audio, language="ru")
        text = transcript.text
        logger.info(f"Voice: {text}")
    except Exception as e:
        await thinking.edit_text(f"❌ Не смогла распознать: {e}")
        return

    await thinking.edit_text(f"🎤 «{text}»\n⏳ Разбираю...")
    intent = await detect_intent(text)

    if intent == "query":
        reply = await handle_query(text)
        await thinking.edit_text(f"🎤 «{text}»\n\n{reply}", parse_mode="Markdown")
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

    marked = [i for i in items if await mark_done_by_name(i, shopping_only=True)]
    if marked:
        await thinking.edit_text("✅ Отметила:\n" + "\n".join(f"• {i}" for i in marked))
    else:
        await thinking.edit_text("🧾 На чеке:\n" + "\n".join(f"• {i}" for i in items) + "\n\nНе нашла в списке покупок.")


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
        "✍️ *ИИ-помощник:*\n"
        "  «напиши письмо Роби о броне»\n"
        "  «составь пост про баню»\n\n"
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
    by_zone  = await get_shopping_list()
    await thinking.edit_text(format_shopping_list(by_zone), parse_mode="Markdown")

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
        "Добавить задачи — просто напиши\n"
        "Запросить — «что срочного», «что по Fairway», «дашборд»\n"
        "Закрыть — «сделала X», «купила X»\n"
        "Написать текст — «напиши письмо/пост/сообщение»\n"
        "Добавить в календарь — «добавь встречу X в пятницу в 11:00»\n\n"
        "/shop — список покупок\n"
        "/tasks — дашборд\n\n"
        "Каждое утро в 9:00 — брифинг с задачами и календарём ☀️",
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

    logger.info("🤖 Bot started!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
