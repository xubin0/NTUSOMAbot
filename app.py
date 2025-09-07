import os, json, uuid, datetime as dt, re
from dotenv import load_dotenv

# Telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, CallbackQueryHandler, filters
)

# Google Sheets
import gspread
from google.oauth2.service_account import Credentials

# Webhook server (used in production)
from flask import Flask, request

# ---------- ENV & CONFIG ----------
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SHEET_ID = os.getenv("SHEET_ID")
KEY_FILE = os.getenv("KEY_FILE", "somantu-2c5c352bcad8.json")  # local dev fallback
MODE = os.getenv("MODE", "polling")  # "polling" (local) or "webhook" (Render/cloud)

# Price list (unit prices) — edit here if your prices change
PRICE_MAP = {
    "Cedar Veil": 79,
    "Musk Reverie": 79,
    "Mythos Blanc": 79,
}

# ---------- GOOGLE SHEETS CLIENT (dual loader) ----------
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def get_worksheet(sheet_id: str, worksheet_name: str = "Orders"):
    """
    On Render: uses GOOGLE_SERVICE_ACCOUNT_JSON env var.
    Locally: uses service account JSON file via KEY_FILE.
    """
    sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if sa_json:
        info = json.loads(sa_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(KEY_FILE, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)
    return sh.worksheet(worksheet_name)

# ---------- HELPERS ----------
def now_utc_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

PHONE_RE = re.compile(r"^\+?\d[\d\s\-]{6,}$")  # simple, permissive phone validator

def valid_phone(s: str) -> bool:
    return bool(PHONE_RE.match(s.strip()))

def clean_int(txt: str):
    try:
        return True, int(txt)
    except:
        return False, None

# Conversation states
ASK_NAME, ASK_PHONE, ASK_ITEM, ASK_QTY, CONFIRM = range(5)

# ---------- TELEGRAM HANDLERS ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to SOMA orders.\nUse /order to place an order.\nUse /cancel anytime to stop."
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/order – start an order\n/cancel – cancel current order"
    )

async def order_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Initialize the order
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

    # NOTE: Your sheet currently has 'address' column; we store phone there for now.
    context.user_data["order"]["address"] = phone

    # Ask for perfume using buttons
    keyboard = [
        [InlineKeyboardButton("Cedar Veil", callback_data="Cedar Veil")],
        [InlineKeyboardButton("Musk Reverie", callback_data="Musk Reverie")],
        [InlineKeyboardButton("Mythos Blanc", callback_data="Mythos Blanc")],
    ]
    await update.message.reply_text(
        "Choose your perfume:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ASK_ITEM

async def item_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    item = query.data

    context.user_data["order"]["item"] = item
    context.user_data["order"]["price"] = PRICE_MAP.get(item, 0.0)

    await query.edit_message_text(
        f"Selected: {item}\nUnit price: {context.user_data['order']['price']:.2f}\n\nQuantity? (e.g., 1)"
    )
    return ASK_QTY

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

    # Save to Google Sheets
    o = context.user_data["order"]
    try:
        ws = get_worksheet(SHEET_ID, "Orders")
        # Append in the EXACT column order:
        # order_id | timestamp_utc | telegram_username | customer_name | address | item | price | quantity | status
        ws.append_row([
            o["order_id"],
            o["timestamp_utc"],
            o["telegram_username"],
            o["customer_name"],
            o["address"],         # currently storing PHONE here
            o["item"],
            o["price"],           # unit price
            o["quantity"],
            "NEW"
        ], value_input_option="USER_ENTERED")
        await update.message.reply_text(f"✅ Order placed! ID: {o['order_id']}")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to save your order. Please try again later.\nError: {e}")

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled. You can start again with /order.")
    return ConversationHandler.END

# ---------- WIRING ----------
def build_app():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("order", order_start)],
        states={
            ASK_NAME:    [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone)],
            ASK_PHONE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_item)],
            ASK_ITEM:    [CallbackQueryHandler(item_chosen)],
            ASK_QTY:     [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm)],
            CONFIRM:     [MessageHandler(filters.TEXT & ~filters.COMMAND, finalize)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(conv)
    return app

telegram_app = build_app()

# Flask app for webhook deployments
flask_app = Flask(__name__)

@flask_app.post("/webhook")
def webhook():
    update = Update.de_json(request.get_json(force=True), telegram_app.bot)
    telegram_app.update_queue.put_nowait(update)
    return "OK", 200

@flask_app.get("/")
def health():
    return "OK", 200

if __name__ == "__main__":
    if MODE == "webhook":
        # In production, run with: gunicorn app:flask_app
        # Webhook will be set separately via Telegram API or post_init hook.
        flask_app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
    else:
        # Local development: polling
        telegram_app.run_polling(drop_pending_updates=True)