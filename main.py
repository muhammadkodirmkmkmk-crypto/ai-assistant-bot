import os
import re
import json
import uuid
import logging
import asyncio
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
import gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

PHONE_REGEX = re.compile(
    r"""
    (?:(?:\+|00)\d{1,3}[\s\-]?)?   # optional country code
    (?:\(?\d{1,4}\)?[\s\-]?)?       # optional area code
    \d{3,5}                          # first digit group
    [\s\-\.]?
    \d{3,5}                          # second digit group
    (?:[\s\-\.]?\d{2,5})?            # optional trailing digits
    """,
    re.VERBOSE,
)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# In-memory store for pending confirmations: {callback_id: row_data}
pending: dict[str, dict] = {}


def get_sheets_client():
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def get_or_create_sheet(client):
    spreadsheet_id = os.environ["SPREADSHEET_ID"]
    spreadsheet = client.open_by_key(spreadsheet_id)
    sheet = spreadsheet.sheet1
    return sheet


def extract_phone_numbers(text: str) -> list[str]:
    numbers = PHONE_REGEX.findall(text)
    cleaned = []
    for n in numbers:
        digits_only = re.sub(r"\D", "", n)
        if len(digits_only) >= 7:
            cleaned.append(n.strip())
    return cleaned


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message or update.channel_post
    if not message or not message.text:
        return

    phone_numbers = extract_phone_numbers(message.text)
    if not phone_numbers:
        return

    admin_id = int(os.environ["ADMIN_TELEGRAM_ID"])
    sender = message.from_user
    sender_name = (
        f"{sender.first_name or ''} {sender.last_name or ''}".strip()
        if sender
        else "Unknown"
    )
    username = f"@{sender.username}" if sender and sender.username else "—"
    chat_name = message.chat.title or message.chat.username or str(message.chat.id)
    timestamp = datetime.utcnow().strftime("%d.%m.%Y")

    for phone in phone_numbers:
        callback_id = str(uuid.uuid4())[:8]

        pending[callback_id] = {
            "phone": phone,
            "sender_name": sender_name,
            "username": username,
            "chat_name": chat_name,
            "message": message.text[:500],
            "timestamp": timestamp,
        }

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm:{callback_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{callback_id}"),
            ]
        ])

        text = (
            f"📱 *Обнаружен номер телефона*\n\n"
            f"*Номер:* `{phone}`\n"
            f"*Отправитель:* {sender_name} ({username})\n"
            f"*Группа:* {chat_name}\n"
            f"*Время:* {timestamp}\n\n"
            f"*Сообщение:*\n_{message.text[:300]}_\n\n"
            f"Добавить этот номер в таблицу?"
        )

        await context.bot.send_message(
            chat_id=admin_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        logger.info("Sent confirmation request to admin for number: %s", phone)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    action, callback_id = query.data.split(":", 1)
    data = pending.pop(callback_id, None)

    if data is None:
        await query.edit_message_text("⚠️ Запрос уже обработан или устарел.")
        return

    if action == "confirm":
        try:
            def write_to_sheet():
                client = get_sheets_client()
                sheet = get_or_create_sheet(client)
                sheet.insert_row(
                    ["", data["timestamp"], "", data["phone"]],
                    index=6,
                    value_input_option="USER_ENTERED",
                )

            await asyncio.to_thread(write_to_sheet)
            await query.edit_message_text(
                f"✅ *Номер сохранён в таблицу*\n\n"
                f"📱 `{data['phone']}`\n"
                f"👤 {data['sender_name']} ({data['username']})\n"
                f"💬 {data['chat_name']}\n"
                f"🕐 {data['timestamp']}",
                parse_mode="Markdown",
            )
            logger.info("Saved confirmed number to sheet: %s", data["phone"])
        except Exception as e:
            logger.error("Failed to save to Google Sheets: %s", e)
            await query.edit_message_text(f"❌ Ошибка при сохранении: {e}")

    elif action == "reject":
        await query.edit_message_text(
            f"❌ *Номер отклонён*\n\n📱 `{data['phone']}`",
            parse_mode="Markdown",
        )
        logger.info("Admin rejected number: %s", data["phone"])


async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message or update.channel_post
    if not message or not message.contact:
        return

    contact = message.contact
    phone = contact.phone_number
    if not phone:
        return

    # Ensure phone starts with +
    if not phone.startswith("+"):
        phone = "+" + phone

    admin_id = int(os.environ["ADMIN_TELEGRAM_ID"])
    sender = message.from_user
    sender_name = (
        f"{sender.first_name or ''} {sender.last_name or ''}".strip()
        if sender
        else "Unknown"
    )
    username = f"@{sender.username}" if sender and sender.username else "—"
    chat_name = message.chat.title or message.chat.username or str(message.chat.id)
    timestamp = datetime.utcnow().strftime("%d.%m.%Y")

    # Contact owner name
    contact_name = f"{contact.first_name or ''} {contact.last_name or ''}".strip() or "—"

    callback_id = str(uuid.uuid4())[:8]
    pending[callback_id] = {
        "phone": phone,
        "sender_name": sender_name,
        "username": username,
        "chat_name": chat_name,
        "message": f"[Контакт] {contact_name}: {phone}",
        "timestamp": timestamp,
    }

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm:{callback_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{callback_id}"),
        ]
    ])

    text = (
        f"📱 *Получен контакт*\n\n"
        f"*Номер:* `{phone}`\n"
        f"*Имя контакта:* {contact_name}\n"
        f"*Отправитель:* {sender_name} ({username})\n"
        f"*Группа:* {chat_name}\n"
        f"*Дата:* {timestamp}\n\n"
        f"Добавить этот номер в таблицу?"
    )

    await context.bot.send_message(
        chat_id=admin_id,
        text=text,
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    logger.info("Received contact from %s in '%s': %s", sender_name, chat_name, phone)


def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]

    application = Application.builder().token(token).build()

    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    application.add_handler(
        MessageHandler(filters.CONTACT, handle_contact)
    )
    application.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("Bot started. Waiting for phone numbers and contacts to confirm...")
    # Explicitly list update types so Railway/webhook mode also receives contacts
    application.run_polling(
        allowed_updates=[
            "message",
            "channel_post",
            "callback_query",
        ],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
