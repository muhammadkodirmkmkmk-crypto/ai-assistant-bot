import os
import re
import json
import uuid
import logging
import asyncio
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
import gspread
from google.oauth2.service_account import Credentials

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

DB_PATH = Path(__file__).parent / "contacts.json"

# ── Local JSON database ───────────────────────────────────────────────────────

def db_load() -> list[dict]:
    if DB_PATH.exists():
        try:
            return json.loads(DB_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def db_save(records: list[dict]) -> None:
    DB_PATH.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def db_add(record: dict) -> None:
    records = db_load()
    records.append(record)
    db_save(records)


def db_recent(hours: int = 24) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    result = []
    for r in db_load():
        try:
            ts = datetime.fromisoformat(r["saved_at"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                result.append(r)
        except Exception:
            pass
    return result

# ── Google Sheets ─────────────────────────────────────────────────────────────

def sheets_write(contact_name: str, phone: str, date_str: str, sender_name: str) -> None:
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(os.environ["SPREADSHEET_ID"])
    sheet = spreadsheet.sheet1
    # Columns: A=name, B=phone, C=date, D=added_by — inserted at row 6 pushing data down
    sheet.insert_row(
        [contact_name, phone, date_str, sender_name],
        index=6,
        value_input_option="USER_ENTERED",
    )

# ── State ─────────────────────────────────────────────────────────────────────

# Pending confirmations waiting for admin approval: {callback_id: data_dict}
pending: dict[str, dict] = {}

flask_app = Flask(__name__)
ptb_app: Application | None = None
bot_loop: asyncio.AbstractEventLoop | None = None

# ── Helpers ───────────────────────────────────────────────────────────────────

def admin_id() -> int:
    return int(os.environ["ADMIN_TELEGRAM_ID"])


def get_sender(message) -> tuple[str, str]:
    """Return (display_name, @username_or_dash)."""
    user = message.from_user
    if user:
        name = f"{user.first_name or ''} {user.last_name or ''}".strip() or "Unknown"
        uname = f"@{user.username}" if user.username else "—"
    else:
        name = (message.sender_chat.title if message.sender_chat else "Unknown")
        uname = "—"
    return name, uname


def chat_title(message) -> str:
    return message.chat.title or message.chat.username or str(message.chat.id)


def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%d.%m.%Y")

# ── Handlers ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Привет! Я *iikoleadbot*.\n\n"
        "Я слежу за группой *Zetta media* и собираю контакты.\n\n"
        "📌 *Что я умею:*\n"
        "• Ловлю контакты в группе и отправляю тебе на подтверждение\n"
        "• Принимаю контакты напрямую в личку\n"
        "• После ✅ — записываю в Google Таблицы\n"
        "• /check — список контактов за последние 24 часа",
        parse_mode="Markdown",
    )


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != admin_id():
        return

    recent = db_recent(24)
    if not recent:
        await update.message.reply_text("📭 За последние 24 часа контактов не было.")
        return

    lines = [f"📋 *Контакты за последние 24 часа* ({len(recent)} шт.):\n"]
    for i, r in enumerate(recent, 1):
        lines.append(
            f"{i}. 📱 `{r['phone']}` — {r['contact_name']}\n"
            f"   👤 Добавил: {r['sender_name']}\n"
            f"   💬 Группа: {r['chat_name']}\n"
            f"   🕐 {r['date']}"
        )

    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")


async def process_contact(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    is_dm: bool = False,
) -> None:
    """Shared logic for contacts from group and DM."""
    message = update.message or update.channel_post
    if not message or not message.contact:
        return

    contact = message.contact
    phone = contact.phone_number or ""
    if not phone:
        logger.info("Contact without phone number, skipping.")
        return
    if not phone.startswith("+"):
        phone = "+" + phone

    contact_name = f"{contact.first_name or ''} {contact.last_name or ''}".strip() or "—"
    sender_name, sender_uname = get_sender(message)
    group = "Личка" if is_dm else chat_title(message)
    date = now_str()

    callback_id = str(uuid.uuid4())[:8]
    pending[callback_id] = {
        "phone": phone,
        "contact_name": contact_name,
        "sender_name": sender_name,
        "sender_uname": sender_uname,
        "chat_name": group,
        "date": date,
    }

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm:{callback_id}"),
        InlineKeyboardButton("❌ Отклонить",   callback_data=f"reject:{callback_id}"),
    ]])

    source_label = "📩 из лички" if is_dm else f"💬 группа: *{group}*"
    text = (
        f"📱 *Новый контакт*\n\n"
        f"*Имя:* {contact_name}\n"
        f"*Телефон:* `{phone}`\n"
        f"*Кто скинул:* {sender_name} ({sender_uname})\n"
        f"*Источник:* {source_label}\n"
        f"*Дата:* {date}\n\n"
        f"Добавить в таблицу?"
    )

    await context.bot.send_message(
        chat_id=admin_id(),
        text=text,
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    logger.info("Queued contact for confirmation: %s (%s)", phone, contact_name)


async def handle_contact_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Contacts sent in any group/channel."""
    await process_contact(update, context, is_dm=False)


async def handle_contact_dm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Contacts sent directly to the bot (private chat)."""
    if update.effective_user and update.effective_user.id == admin_id():
        await process_contact(update, context, is_dm=True)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":", 1)
    if len(parts) != 2:
        return
    action, callback_id = parts
    data = pending.pop(callback_id, None)

    if data is None:
        await query.edit_message_text("⚠️ Запрос уже обработан или устарел.")
        return

    if action == "confirm":
        try:
            def write():
                sheets_write(
                    data["contact_name"],
                    data["phone"],
                    data["date"],
                    data["sender_name"],
                )

            await asyncio.to_thread(write)

            # Persist to local JSON db
            db_add({
                "phone":        data["phone"],
                "contact_name": data["contact_name"],
                "sender_name":  data["sender_name"],
                "chat_name":    data["chat_name"],
                "date":         data["date"],
                "saved_at":     datetime.now(timezone.utc).isoformat(),
            })

            await query.edit_message_text(
                f"✅ *Сохранено в таблицу*\n\n"
                f"👤 *Имя:* {data['contact_name']}\n"
                f"📱 *Телефон:* `{data['phone']}`\n"
                f"🕐 *Дата:* {data['date']}\n"
                f"👥 *Добавил:* {data['sender_name']}",
                parse_mode="Markdown",
            )
            logger.info("Saved to sheet: %s %s", data["contact_name"], data["phone"])

        except Exception as e:
            logger.error("Sheet write failed: %s", e)
            await query.edit_message_text(f"❌ Ошибка при записи в таблицу:\n`{e}`", parse_mode="Markdown")

    elif action == "reject":
        await query.edit_message_text(
            f"❌ *Отклонено*\n\n"
            f"👤 {data['contact_name']}\n"
            f"📱 `{data['phone']}`",
            parse_mode="Markdown",
        )
        logger.info("Rejected: %s %s", data["contact_name"], data["phone"])

# ── Flask webhook routes ──────────────────────────────────────────────────────

@flask_app.route("/webhook", methods=["POST"])
def webhook():
    logger.info("=== WEBHOOK HIT === method=%s path=%s", request.method, request.path)
    logger.info("Headers: %s", dict(request.headers))

    raw = request.get_data(as_text=True)
    logger.info("Raw body (%d bytes): %s", len(raw), raw[:2000])

    if ptb_app is None or bot_loop is None:
        logger.error("Bot not ready yet")
        return jsonify({"error": "bot not ready"}), 503

    try:
        data = json.loads(raw) if raw else None
    except Exception as e:
        logger.error("JSON parse error: %s", e)
        return jsonify({"error": "invalid json"}), 400

    if not data:
        logger.warning("Empty body received")
        return jsonify({"error": "empty body"}), 400

    logger.info("Update type keys: %s", list(data.keys()))

    try:
        update = Update.de_json(data, ptb_app.bot)
        logger.info("Parsed update_id=%s effective_chat=%s", update.update_id,
                    update.effective_chat.id if update.effective_chat else "None")
        if update.message:
            logger.info("Message content_type=%s has_contact=%s text=%r",
                        update.message.content_type,
                        bool(update.message.contact),
                        update.message.text)
        future = asyncio.run_coroutine_threadsafe(
            ptb_app.process_update(update), bot_loop
        )
        future.result(timeout=30)
    except Exception as e:
        logger.error("Update processing error: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500

    return jsonify({"ok": True})


@flask_app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "mode": "webhook"})

# ── Bot thread ────────────────────────────────────────────────────────────────

def run_bot_thread():
    global ptb_app, bot_loop

    bot_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(bot_loop)

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    webhook_url = os.environ.get("WEBHOOK_URL", "").rstrip("/")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("check", cmd_check))

    # Contacts in groups / channels
    app.add_handler(MessageHandler(
        filters.CONTACT & ~filters.ChatType.PRIVATE,
        handle_contact_group,
    ))
    # Contacts in DM (only from admin)
    app.add_handler(MessageHandler(
        filters.CONTACT & filters.ChatType.PRIVATE,
        handle_contact_dm,
    ))

    app.add_handler(CallbackQueryHandler(handle_callback))

    ptb_app = app

    async def start():
        await app.initialize()
        if webhook_url:
            await app.bot.set_webhook(
                url=f"{webhook_url}/webhook",
                allowed_updates=["message", "channel_post", "callback_query"],
                drop_pending_updates=False,
            )
            logger.info("Webhook registered: %s/webhook", webhook_url)
        else:
            logger.warning("WEBHOOK_URL not set — set it in Railway environment variables")
        await app.start()
        logger.info("Bot ready. Listening for contacts...")
        await asyncio.Event().wait()

    bot_loop.run_until_complete(start())

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    threading.Thread(target=run_bot_thread, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    logger.info("Flask webhook server starting on port %d", port)
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
