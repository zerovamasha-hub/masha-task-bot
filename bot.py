import os
import json
import logging
from datetime import datetime
import pytz
from telegram import Update
from telegram.ext import (
    Application, MessageHandler, CommandHandler,
    filters, ContextTypes
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import anthropic
from notion_client import AsyncClient as NotionClient

# ─── Логи ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── Конфиг ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
NOTION_API_KEY    = os.environ["NOTION_API_KEY"]
NOTION_DB_ID      = os.environ["NOTION_DATABASE_ID"]
CHAT_ID           = os.environ["CHAT_ID"]          # Telegram chat ID для напоминаний

_raw_ids = os.environ.get("ALLOWED_USER_IDS", "")
ALLOWED_IDS = set(x.strip() for x in _raw_ids.split(",") if x.strip())

BALI_TZ = pytz.timezone("Asia/Makassar")

# ─── Клиент Claude ───────────────────────────────────────────────────────────
ai = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

# ─── Эмодзи ──────────────────────────────────────────────────────────────────
ZONE_EMOJI = {
    "Golden Goose": "💼", "Fairway": "📐", "FlipLab": "🏺",
    "Personal": "👤",    "Kids": "👦",   "Home": "🏠",
    "Finance": "💰",     "Family": "👨‍👩‍👧", "Travel": "✈️",
    "Property": "🏘️",   "Docs/Visa": "📋",
}
PRIORITY_EMOJI = {"Urgent": "🔴", "High": "🟠", "Medium": "🟡", "Low": "🟢"}

# ─── Системный промпт ─────────────────────────────────────────────────────────
SYSTEM_PROMPT = f"""You are a task assistant for Masha. Parse messages and extract ALL tasks.

ZONES (use exactly):
- Golden Goose  → restaurant, banya (Bedugul Banya), GG operations
- Fairway       → brand, website, marketing, merchandise, content
- FlipLab       → ceramic studio
- Personal      → health, fitness, Instagram, personal purchases, nutritionist
- Kids          → son Kirill (Year 1), nanny, clubs, vaccinations, kids purchases
- Home          → food, household shopping, staff salaries, dog (Bun)
- Finance       → payments, budgets
- Family        → birthdays, holidays, grandparents, gifts, photos
- Travel        → trips, visas, bookings
- Property      → rental apartment: tenants, rent, repairs, utilities, contracts
- Docs/Visa     → KITAS, passports, permits, insurance, official documents

PROJECTS: Golden Goose | Bedugul Banya | Fairway | FlipLab | Instagram | General

CURRENT PRIORITIES:
- Golden Goose: launch banya → social media → automation
- Fairway: website launch → marketing plan → content
- Personal: health, nutritionist plan, family nutrition, kitchen purchase

SHOPPING DETECTION — set "shopping": true if the task involves buying/purchasing anything:
- food, groceries, household items
- clothes, shoes for anyone
- supplements, medicine
- dog food, pet supplies
- any physical item to purchase

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

Priority: Urgent=hard deadline/blocks others, High=this week, Medium=soon, Low=someday
Today: {datetime.now().strftime("%Y-%m-%d")} ({datetime.now().strftime("%A")})
Convert relative dates ("by Friday", "this week") to actual YYYY-MM-DD."""

RECEIPT_PROMPT = """Look at this receipt or shopping photo. Extract what was purchased.
Return ONLY a JSON list of item names in Russian:
{"items": ["item1", "item2", ...]}
Be concise — just the product names, no details."""


# ─── Claude: парсинг задач ────────────────────────────────────────────────────
async def parse_tasks(text: str) -> list[dict]:
    response = await ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    if not response.content:
        logger.error("Claude returned empty content")
        return []
    raw = response.content[0].text.strip()
    logger.info(f"Claude raw response: {raw[:200]}")
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return json.loads(raw).get("tasks", [])


# ─── Claude: чтение чека ─────────────────────────────────────────────────────
async def parse_receipt(image_data: bytes, mime_type: str) -> list[str]:
    import base64
    b64 = base64.standard_b64encode(image_data).decode()
    response = await ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64}},
                {"type": "text", "text": RECEIPT_PROMPT}
            ]
        }],
    )
    return json.loads(response.content[0].text.strip()).get("items", [])


# ─── Notion: добавить задачу ──────────────────────────────────────────────────
async def add_to_notion(task: dict) -> None:
    props = {
        "Task":     {"title":    [{"text": {"content": task["task"]}}]},
        "Zone":     {"select":   {"name": task.get("zone", "Personal")}},
        "Project":  {"select":   {"name": task.get("project", "General")}},
        "Priority": {"select":   {"name": task.get("priority", "Medium")}},
        "Status":   {"select":   {"name": "To Do"}},
        "Source":   {"select":   {"name": "Chat"}},
        "Shopping": {"checkbox": task.get("shopping", False)},
    }
    if task.get("deadline"):
        props["Deadline"] = {"date": {"start": task["deadline"]}}
    if task.get("notes"):
        props["Notes"] = {"rich_text": [{"text": {"content": task["notes"]}}]}

    async with NotionClient(auth=NOTION_API_KEY) as notion:
        await notion.pages.create(
            parent={"database_id": NOTION_DB_ID},
            properties=props,
        )


# ─── Notion: получить шопинг-лист ────────────────────────────────────────────
async def get_shopping_list() -> dict[str, list[str]]:
    """Возвращает {zone: [tasks]} для всех открытых покупок."""
    async with NotionClient(auth=NOTION_API_KEY) as notion:
        result = await notion.databases.query(
            database_id=NOTION_DB_ID,
            filter={
                "and": [
                    {"property": "Shopping", "checkbox": {"equals": True}},
                    {"property": "Status",   "select":   {"does_not_equal": "Done"}},
                ]
            }
        )

    by_zone: dict[str, list[str]] = {}
    for page in result["results"]:
        props = page["properties"]
        task  = props["Task"]["title"][0]["text"]["content"] if props["Task"]["title"] else "?"
        zone  = props["Zone"]["select"]["name"] if props["Zone"]["select"] else "Другое"
        by_zone.setdefault(zone, []).append(task)

    return by_zone


# ─── Notion: отметить задачу куплено ─────────────────────────────────────────
async def mark_bought(task_name: str) -> bool:
    """Ищет задачу по названию и ставит Status=Done."""
    async with NotionClient(auth=NOTION_API_KEY) as notion:
        result = await notion.databases.query(
            database_id=NOTION_DB_ID,
            filter={
                "and": [
                    {"property": "Shopping", "checkbox": {"equals": True}},
                    {"property": "Status",   "select":   {"does_not_equal": "Done"}},
                ]
            }
        )
        # Ищем ближайшее совпадение
        task_lower = task_name.lower()
        for page in result["results"]:
            title = page["properties"]["Task"]["title"]
            if title and task_lower in title[0]["text"]["content"].lower():
                await notion.pages.update(
                    page_id=page["id"],
                    properties={"Status": {"select": {"name": "Done"}}}
                )
                return True
    return False


# ─── Форматирование шопинг-листа ─────────────────────────────────────────────
def format_shopping_list(by_zone: dict[str, list[str]], title: str = None) -> str:
    if not by_zone:
        return "🛒 Список покупок пуст — всё куплено! 🎉"

    today = datetime.now(BALI_TZ)
    week  = f"{today.strftime('%d')}–{(today).strftime('%d %B')}"
    lines = [title or f"🛒 Шопинг-лист — {week}\n"]

    for zone, tasks in sorted(by_zone.items()):
        emoji = ZONE_EMOJI.get(zone, "•")
        lines.append(f"\n{emoji} {zone}:")
        for t in tasks:
            lines.append(f"  ☐ {t}")

    lines.append("\n\n_Напиши «купила [название]» чтобы отметить — или пришли фото чека_")
    return "\n".join(lines)


# ─── Форматирование подтверждения задач ──────────────────────────────────────
def format_reply(added: list[dict], failed: list[str]) -> str:
    if not added and not failed:
        return "Не нашла задач 🤔 Попробуй написать конкретнее."

    count = len(added)
    word  = "задачу" if count == 1 else ("задачи" if 2 <= count <= 4 else "задач")
    lines = [f"✅ Добавила {count} {word} в Notion:\n"]

    for t in added:
        z  = ZONE_EMOJI.get(t.get("zone", ""), "•")
        p  = PRIORITY_EMOJI.get(t.get("priority", ""), "")
        dl = f"  · до {t['deadline']}" if t.get("deadline") else ""
        cart = " 🛒" if t.get("shopping") else ""
        lines.append(f"{z}{p} {t['task']}{cart}{dl}")

    if failed:
        lines.append(f"\n⚠️ Не удалось добавить: {', '.join(failed)}")
    return "\n".join(lines)


# ─── Хэндлер: текстовые сообщения ────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = str(update.effective_user.id)
    if ALLOWED_IDS and uid not in ALLOWED_IDS:
        await update.message.reply_text("⛔ Нет доступа.")
        return

    text = update.message.text

    # Отметить куплено
    if text.lower().startswith("купила "):
        item = text[7:].strip()
        found = await mark_bought(item)
        if found:
            await update.message.reply_text(f"✅ Отметила как куплено: {item}")
        else:
            await update.message.reply_text(f"🤔 Не нашла «{item}» в списке покупок. Проверь написание.")
        return

    thinking = await update.message.reply_text("⏳ Разбираю...")

    try:
        tasks = await parse_tasks(text)
    except Exception as e:
        logger.exception("Claude error")
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
        logger.exception("Receipt error")
        await thinking.edit_text(f"❌ Не смогла прочитать чек: {e}")
        return

    marked = []
    for item in items:
        if await mark_bought(item):
            marked.append(item)

    if marked:
        await thinking.edit_text(
            f"✅ Отметила как куплено:\n" + "\n".join(f"• {i}" for i in marked)
        )
    else:
        await thinking.edit_text(
            "🧾 Вижу на чеке:\n" + "\n".join(f"• {i}" for i in items) +
            "\n\nНо не нашла эти позиции в списке покупок."
        )


# ─── Команды ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Привет, Маша!\n\n"
        "Пиши задачи как удобно — одну, список или поток мыслей. "
        "Покупки помечу 🛒 и добавлю в шопинг-лист.\n\n"
        "Команды:\n"
        "/shop — текущий список покупок\n"
        "/help — подсказка\n\n"
        "Чтобы отметить куплено:\n"
        "• Напиши «купила корм для Буна»\n"
        "• Пришли фото чека 🧾"
    )

async def cmd_shop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = str(update.effective_user.id)
    if ALLOWED_IDS and uid not in ALLOWED_IDS:
        return
    thinking = await update.message.reply_text("⏳ Загружаю список...")
    by_zone  = await get_shopping_list()
    await thinking.edit_text(format_shopping_list(by_zone), parse_mode="Markdown")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📋 Как пользоваться:\n\n"
        "Пиши задачи любым способом — я разберу зону и приоритет.\n"
        "Покупки автоматически попадают в шопинг-лист 🛒\n\n"
        "Отметить куплено:\n"
        "• «Купила корм Буну» — найду и закрою\n"
        "• Фото чека — прочитаю и закрою нужные позиции\n\n"
        "/shop — посмотреть текущий список\n\n"
        "По понедельникам и четвергам в 9:00 буду присылать список сама 📅"
    )


# ─── Плановая рассылка шопинг-листа ──────────────────────────────────────────
async def send_shopping_reminder(bot) -> None:
    by_zone = await get_shopping_list()
    text    = format_shopping_list(
        by_zone,
        title="🛒 Шопинг-лист на эту неделю\n"
    )
    await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")


# ─── Запуск ───────────────────────────────────────────────────────────────────
def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CommandHandler("shop",  cmd_shop))

    # Сообщения
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Расписание: пн и чт в 9:00 по Бали (Asia/Makassar)
    scheduler = AsyncIOScheduler(timezone=BALI_TZ)
    scheduler.add_job(
        send_shopping_reminder,
        trigger="cron",
        day_of_week="mon,thu",
        hour=9, minute=0,
        args=[app.bot]
    )
    scheduler.start()

    logger.info("🤖 Bot started!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
