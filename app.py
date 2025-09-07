import os, json, uuid, datetime as dt, re, threading, asyncio, logging
from dotenv import load_dotenv

# ===================== LOGGING =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("soma-bot")

# Telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, CallbackQueryHandler, filters
)

# Google Sheets
import gspread
from google.oauth2.service_account import Credentials

# Flask (webhook)
from flask import Flask, request

# ===================== ENV & CONFIG =====================
load_dotenv()  # harmless on Render
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SHEET_ID = os.getenv("SHEET_ID")

PRICE_MAP = {"Cedar Veil": 79, "Musk Reverie": 79, "Mythos Blanc": 79}
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ===================== SHEETS CLIENT =====================
def get_worksheet(sheet_id: str, worksheet_name: str = "Orders"):
    sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not sa_json:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON not set in environment")
    info = json.loads(sa_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)
    return sh.worksheet(worksheet_name)

# ===================== HELPERS =====================
def now_utc_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

PHONE_RE = re.compile(r"^\+?\d[\d\s\-]{6,}$")
def valid_phone(s: str) -> bool:
    return bool(PHONE_RE.match(s.strip()))

def clean_int(txt: str):
    try:
        return True, int(txt)
    except Exception:
        return False, None

# Conversation states
ASK_NAME, ASK_PHONE, ASK_ITEM, ASK_QTY, CONFIRM = range(5)

# ===================== HANDLERS =====================
async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.info("/start from %s", update.effective_user.id)
    await update.message.reply_text(
        "Welcome to SOMA orders.\nUse /order to place an order.\nUse /cancel anytime to stop."
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/order – start an order\n/cancel – cancel current order")

async def order_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.info("order_start by %s", update.effective_user.id)
    context.user_data["order"] = {
        "order_id": str(uuid.uuid4())[:8],
        "timestamp_utc": now_utc_iso(),
        "telegram_username": update.effective_user.username or update.effective_user.full_name,
    }
    await update.message.reply_text("Customer name?")
    return ASK_NAME

async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["order"]["customer_name"] = update.message.text.strip()
    await update.message.reply_text("Phone number? (e.g., +65 9123 4567)")
    return ASK_PHONE

async def ask_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    if not valid_phone(phone):
        await update.message.reply_text("Please enter a valid phone number (e.g., +65 9123 4567).")
        return ASK_PHONE

    context.user_data["order"]["address"] = phone  # storing phone in 'address' column
    keyboard = [
        [InlineKeyboardButton("Cedar Veil", callback_data="Cedar Veil")],
        [InlineKeyboardButton("Musk Reverie", callback_data="Musk Reverie")],
        [InlineKeyboardButton("Mythos Blanc", callback_data="Mythos Blanc")],
    ]
    await update.message.reply_text("Choose your perfume:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ASK_ITEM

async def item_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
        item = query.data
        log.info("item_chosen: %s by %s", item, query.from_user.id)

        context.user_data["order"]["item"] = item
        context.user_data["order"]["price"] = PRICE_MAP.get(item, 0.0)

        await query.edit_message_text(
            f"Selected: {item}\nUnit price: {context.user_data['order']['price']:.2f}\n\nQuantity? (e.g., 1)"
        )
        return ASK_QTY
    except Exception as e:
        log.exception("Error in item_chosen: %s", e)
        await query.message.reply_text("Sorry, something went wrong selecting the item. Please send /order to try again.")
        return ConversationHandler.END

async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ok, qty = clean_int(update.message.text.strip())
    if not ok or qty <= 0:
        await update.message.reply_text("Please enter a valid quantity (whole number, e.g., 1).")
        return ASK_QTY

    context.user_data["order"]["quantity"] = qty
    o = context.user_data["order"]
    total = o["price"] * o["quantity"]
    summary = (
        "Please confirm your order:\n"
        f"• Name: {o['customer_name']}\n"
        f"• Phone: {o['address']}\n"
        f"• Perfume: {o['item']}\n"
        f"• Unit Price: {o['price']:.2f}\n"
        f"• Quantity: {o['quantity']}\n"
        f"• Estimated Total: {total:.2f}\n\n"
        "Reply YES to confirm or NO to cancel."
    )
    await update.message.reply_text(summary)
    return CONFIRM

async def finalize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = update.message.text.strip().lower()
    if reply not in ("yes", "y", "no", "n"):
        await update.message.reply_text("Please reply YES to confirm or NO to cancel.")
        return CONFIRM
    if reply in ("no", "n"):
        await update.message.reply_text("Order cancelled.")
        return ConversationHandler.END

    o = context.user_data["order"]
    try:
        ws = get_worksheet(SHEET_ID, "Orders")
        ws.append_row([
            o["order_id"], o["timestamp_utc"], o["telegram_username"],
            o["customer_name"], o["address"], o["item"],
            o["price"], o["quantity"], "NEW"
        ], value_input_option="USER_ENTERED")
        await update.message.reply_text(f"✅ Order placed! ID: {o['order_id']}")
    except Exception as e:
        log.exception("Sheet append failed")
        await update.message.reply_text(f"❌ Failed to save your order. Error: {e}")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled. You can start again with /order.")
    return ConversationHandler.END

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled exception: %s", context.error)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await context.bot.send_message(update.effective_chat.id, "⚠️ An error occurred. Please try again.")
    except Exception:
        pass

# ===================== APP WIRING =====================
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start","Begin"),
        BotCommand("order","Place an order"),
        BotCommand("help","Help"),
        BotCommand("cancel","Cancel current order"),
        BotCommand("ping","Health check"),
    ])

def build_telegram_app() -> Application:
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[CommandHandler("order", order_start)],
        states={
            ASK_NAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone)],
            ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_item)],
            ASK_ITEM:  [CallbackQueryHandler(item_chosen)],
            ASK_QTY:   [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm)],
            CONFIRM:   [MessageHandler(filters.TEXT & ~filters.COMMAND, finalize)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(conv)
    app.add_error_handler(on_error)
    return app

telegram_app = build_telegram_app()

# Background PTB runner so Flask/Gunicorn can serve webhook
def _run_ptb():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(telegram_app.initialize())
    loop.run_until_complete(telegram_app.start())
    log.info("PTB application started")
    loop.run_forever()

threading.Thread(target=_run_ptb, daemon=True).start()

# ===================== FLASK (WSGI) =====================
flask_app = Flask(__name__)

@flask_app.post("/webhook")
def webhook():
    try:
        data = request.get_json(force=True, silent=False)
        update = Update.de_json(data, telegram_app.bot)
        telegram_app.update_queue.put_nowait(update)
        return "OK", 200
    except Exception as e:
        log.exception("Webhook error: %s", e)
        return "BAD", 200  # still 200 so Telegram doesn't retry storm

@flask_app.get("/")
def health():
    return "OK", 200