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


# Perfume descriptions (edit freely)
PERFUME_DESC = {
    "Cedar Veil": (
        "Cool cedar meets warm amber, a forest breeze slipping through a city window. Calm, composed, and quietly powerful.\n "
        "It doesn’t shout, but it’s impossible to ignore.\n"
        "keynotes: Cedarwood, Rose, Pink Pepper, Vetiver."
    ),
    "Musk Reverie": (
        "Creamy fig, cut with the edge of clean musk. Soft at first, then unforgettable.\n"
        "Sweet enough to draw them in, dangerous enough to make them stay. \n "
        "It lingers like a half-remembered dream, equal parts innocence and intrigue.\n"
        "keynotes: Ambrette Seed, Fig Leaf, Musk."
    ),
    "Mythos Blanc": (
        "Coconut milk swirling with jasmine, grounded by incense and soft vanilla, a quiet escape in a bottle.\n "
        "Creamy but never sweet, airy but grounding.\n"
        "Like stepping into a sunlit temple by the sea, lifting your mood without asking for attention.\n"
        "keynotes: Milk, Jasmine, Incense, Vanilla."
    ),
}


PRICE_MAP = {"Cedar Veil": 79, "Musk Reverie": 79, "Mythos Blanc": 79}
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ===================== SHEETS CLIENT =====================
def get_worksheet(sheet_id: str, worksheet_name: str = "Orders"):
    sa_json = os.getenv("KEY_FILE")
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
ASK_NAME, ASK_PHONE, ASK_ITEM, ASK_QTY, ASK_MORE, CONFIRM, ASK_DELIVERY_METHOD, ASK_DELIVERY_ADDRESS = range(8)

# ===================== HANDLERS =====================
async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.info("/start from %s", update.effective_user.id)
    await update.message.reply_text(
        "Welcome to SOMA orders.\nUse /order to place an order.\nUse /cancel anytime to stop."
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/order - start an order\n"
        "/perfume_list - view perfume descriptions\n"
        "/cancel - cancel current order\n"
        "/ping - health check"
    )

async def cmd_perfume_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Cedar Veil(50ml)", callback_data="INFO|Cedar Veil")],
        [InlineKeyboardButton("Musk Reverie(50ml)", callback_data="INFO|Musk Reverie")],
        [InlineKeyboardButton("Mythos Blanc(50ml)", callback_data="INFO|Mythos Blanc")],
    ]
    await update.message.reply_text(
        "Tap a perfume to see its description:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def perfume_info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, name = query.data.split("|", 1)
    except Exception:
        await query.edit_message_text("Sorry, I didn’t recognise that perfume.")
        return

    desc = PERFUME_DESC.get(name, "No description available yet.")
    # Show the description and keep the list as buttons below
    keyboard = [
        [InlineKeyboardButton("Cedar Veil(50ml)", callback_data="INFO|Cedar Veil")],
        [InlineKeyboardButton("Musk Reverie(50ml)", callback_data="INFO|Musk Reverie")],
        [InlineKeyboardButton("Mythos Blanc(50ml)", callback_data="INFO|Mythos Blanc")],
    ]
    await query.edit_message_text(
        f"**{name}**\n{desc}\n\nSelect another:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def order_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.info("order_start by %s", update.effective_user.id)
    context.user_data["order"] = {
        "order_id": str(uuid.uuid4())[:8],
        "timestamp_utc": now_utc_iso(),
        "telegram_username": update.effective_user.username or update.effective_user.full_name,
        "items": [],  # NEW: hold multiple items
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

    context.user_data["order"]["phone"] = phone 
    keyboard = [
        [InlineKeyboardButton("Cedar Veil(50ml)", callback_data="Cedar Veil")],
        [InlineKeyboardButton("Musk Reverie(50ml)", callback_data="Musk Reverie")],
        [InlineKeyboardButton("Mythos Blanc(50ml)", callback_data="Mythos Blanc")],
    ]
    await update.message.reply_text("Choose your perfume:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ASK_ITEM

async def item_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
        item = query.data
        log.info("item_chosen: %s by %s", item, query.from_user.id)

        # NEW: hold current item until we get qty
        context.user_data["current_item"] = {
            "name": item,
            "price": float(PRICE_MAP.get(item, 0.0)),
        }

        await query.edit_message_text(
            f"Selected: {item}\nUnit price: {context.user_data['current_item']['price']:.2f}\n\nQuantity? (e.g., 1)"
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

    cur = context.user_data.get("current_item")
    if not cur:  # safety
        await update.message.reply_text("Please choose a perfume first.")
        return ASK_ITEM

    cur = {**cur, "quantity": qty}
    context.user_data["order"]["items"].append(cur)
    context.user_data["current_item"] = None  # reset

    # Running total
    total = sum(i["price"] * i["quantity"] for i in context.user_data["order"]["items"])

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add another perfume", callback_data="more_yes")],
        [InlineKeyboardButton("✅ Checkout", callback_data="more_no")],
    ])
    await update.message.reply_text(
        f"Added: {cur['name']} × {cur['quantity']} = {cur['price']*cur['quantity']:.2f}\n"
        f"Current total: {total:.2f}\n\nAdd another perfume?",
        reply_markup=kb
    )
    return ASK_MORE

async def ask_more(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data

    if choice == "more_yes":
        # show perfume keyboard again
        keyboard = [
            [InlineKeyboardButton("Cedar Veil(50ml)", callback_data="Cedar Veil")],
            [InlineKeyboardButton("Musk Reverie(50ml)", callback_data="Musk Reverie")],
            [InlineKeyboardButton("Mythos Blanc(50ml)", callback_data="Mythos Blanc")],
        ]
        await query.edit_message_text("Choose your perfume:", reply_markup=InlineKeyboardMarkup(keyboard))
        return ASK_ITEM

    # choice == "more_no" → build final summary
    o = context.user_data["order"]
    lines = []
    total = 0.0
    for i in o["items"]:
        sub = i["price"] * i["quantity"]
        total += sub
        lines.append(f"• {i['name']} × {i['quantity']} @ {i['price']:.2f} = {sub:.2f}")

    summary = (
        "Please confirm your order:\n"
        f"• Name: {o['customer_name']}\n"
        f"• Phone: {o['phone']}\n"
        + "\n".join(lines) +
        f"\n\nTotal: {total:.2f}\n\nReply YES to confirm or NO to cancel."
    )
    await query.edit_message_text(summary)
    return CONFIRM

async def finalize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = update.message.text.strip().lower()
    if reply not in ("yes", "y", "no", "n"):
        await update.message.reply_text("Please reply YES to confirm or NO to cancel.")
        return CONFIRM
    if reply in ("no", "n"):
        await update.message.reply_text("Order cancelled.")
        return ConversationHandler.END

    # YES → ask delivery method
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Self collect", callback_data="DELIVERY_SELF")],
        [InlineKeyboardButton("Deliver", callback_data="DELIVERY_SHIP")],
    ])
    await update.message.reply_text(
        "How would you like to receive your order?",
        reply_markup=kb
    )
    return ASK_DELIVERY_METHOD


async def delivery_method_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "DELIVERY_SELF":
        context.user_data["order"]["delivery_method"] = "SELF"
        context.user_data["order"]["delivery_address"] = ""
        return await _save_and_finish(query.message, context)

    if query.data == "DELIVERY_SHIP":
        context.user_data["order"]["delivery_method"] = "DELIVER"
        await query.edit_message_text("Please enter your delivery address:")
        return ASK_DELIVERY_ADDRESS

    await query.edit_message_text("Please choose a delivery option.")
    return ASK_DELIVERY_METHOD


async def delivery_address_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    addr = (update.message.text or "").strip()
    if not addr:
        await update.message.reply_text("Please enter a valid delivery address:")
        return ASK_DELIVERY_ADDRESS

    context.user_data["order"]["delivery_address"] = addr
    return await _save_and_finish(update.message, context)


async def _save_and_finish(msg, context: ContextTypes.DEFAULT_TYPE):
    """Write one row per item to the sheet and end the conversation."""
    o = context.user_data["order"]
    try:
        ws = get_worksheet(SHEET_ID, "Orders")
        for i in o["items"]:
            ws.append_row([
                o["order_id"],              # Order ID
                o["timestamp_utc"],         # Timestamp
                o["telegram_username"],     # Telegram username
                o["customer_name"],         # Customer name
                o.get("phone", ""),         # Phone
                o.get("delivery_method",""),# Delivery method
                o.get("delivery_address",""),# Delivery address (optional)
                i["name"],                  # Item name
                i["price"],                 # Unit price
                i["quantity"],              # Quantity
                "NEW"                       # Status
            ], value_input_option="USER_ENTERED")
        await msg.reply_text(f"✅ Order placed! ID: {o['order_id']}")
    except Exception as e:
        log.exception("Sheet append failed")
        await msg.reply_text(f"❌ Failed to save your order. Error: {e}")
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
        BotCommand("perfume_list","View perfume descriptions"),
        BotCommand("help","Help"),
        BotCommand("cancel","Cancel current order"),
        BotCommand("ping","Health check"),
    ])

    me = await app.bot.get_me()
    log.info("Bot started as @%s (id=%s)", me.username, me.id)

async def dbg_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.info("DBG saw command: %r from chat %s", update.message.text, update.effective_chat.id)
    await update.message.reply_text(f"debug got {update.message.text}")

async def dbg_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # log what PTB sees after process_update
    if update.message:
        log.info("DBG all: text=%r chat=%s", update.message.text, update.effective_chat.id)
    elif update.callback_query:
        log.info("DBG all: callback data=%r from=%s", update.callback_query.data, update.effective_user.id)
    else:
        log.info("DBG all: update type=%s", update.to_dict().keys())



def build_telegram_app() -> Application:
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    # 1) Register simple commands in group 0 (highest priority)# register it BEFORE others, group=0
    app.add_handler(CommandHandler("ping", cmd_ping), group=0)
    app.add_handler(CommandHandler("start", cmd_start), group=0)
    app.add_handler(CommandHandler("help", cmd_help), group=0)
    app.add_handler(CommandHandler("cancel", cancel), group=0)
    app.add_handler(CommandHandler("perfume_list", cmd_perfume_list), group=0)
    app.add_handler(CallbackQueryHandler(perfume_info_callback, pattern=r"^INFO\|"), group=0)

    # 2) Conversation goes in group 1 so it won't block top-level commands
    conv = ConversationHandler(
        entry_points=[CommandHandler("order", order_start)],
        states={
            ASK_NAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone)],
            ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_item)],
            ASK_ITEM:  [CallbackQueryHandler(item_chosen)],
            ASK_QTY:   [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm)],
            ASK_MORE:  [CallbackQueryHandler(ask_more)],
            CONFIRM:   [MessageHandler(filters.TEXT & ~filters.COMMAND, finalize)],
            ASK_DELIVERY_METHOD: [CallbackQueryHandler(delivery_method_chosen)],
            ASK_DELIVERY_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, delivery_address_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv, group=1)
    # lowest priority so it never interferes
    app.add_handler(MessageHandler(filters.ALL, dbg_all), group=99)

    # Optional: super-verbose debug to confirm pipeline (remove later)
    # from telegram.ext import MessageHandler
    # app.add_handler(MessageHandler(filters.COMMAND, dbg_commands), group=0)

    app.add_error_handler(on_error)
    return app

telegram_app = build_telegram_app()

# Background PTB runner so Flask/Gunicorn can serve webhook
# global
PTB_LOOP = None

def _run_ptb():
    global PTB_LOOP
    PTB_LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(PTB_LOOP)

    async def _init():
        await telegram_app.initialize()
        await telegram_app.start()
        log.info("PTB application started (webhook mode, update_queue consumer running)")
        # <-- this keeps processing the queue
        asyncio.create_task(telegram_app.updater.start_polling())  

    PTB_LOOP.run_until_complete(_init())
    PTB_LOOP.run_forever()

threading.Thread(target=_run_ptb, daemon=True).start()

# ===================== FLASK (WSGI) =====================
flask_app = Flask(__name__)

@flask_app.post("/webhook")
def webhook():
    try:
        data = request.get_json(force=True, silent=False)
        update = Update.de_json(data, telegram_app.bot)

        if PTB_LOOP is None:
            log.error("PTB loop not ready yet")
            return "NOT READY", 503

        # Enqueue update into PTB’s queue safely
        PTB_LOOP.call_soon_threadsafe(
            telegram_app.update_queue.put_nowait,
            update
        )
        log.info("Update enqueued: %s", data.keys())
        return "OK", 200

    except Exception as e:
        log.exception("Webhook error: %s", e)
        return "BAD", 200
        
@flask_app.get("/")
def health():
    return "OK", 200