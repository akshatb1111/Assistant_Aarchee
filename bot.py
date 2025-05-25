import os
import json
import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from keep_alive import keep_alive

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
MASTER_ID = int(os.getenv("MASTER_ID"))
DATA_FILE = "client_data.json"

QUESTION = "✅ Did you complete your goal today? (yes/no)"
EXPECTED_ANSWER = "yes"

# --- Load/save clients ---
def load_data():
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except:
        return {}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

clients = load_data()

# --- Commands ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Hello! I'm your daily check-in bot.")

async def addclient(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MASTER_ID:
        return

    if not context.args:
        await update.message.reply_text("Usage: /addclient @username")
        return

    username = context.args[0].lstrip("@")
    try:
        user_chat = await context.bot.get_chat(f"@{username}")
        client_id = str(user_chat.id)
        clients[client_id] = {"username": username, "answers": {}}
        save_data(clients)
        await update.message.reply_text(f"✅ Added client @{username}")
        await context.bot.send_message(chat_id=client_id, text="👋 You've been added for daily check-ins!")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Could not add @{username}. Make sure they’ve started the bot.")
        print(e)

# --- Daily question ---
async def ask_questions(context: ContextTypes.DEFAULT_TYPE):
    today = str(datetime.date.today())
    for client_id, info in clients.items():
        already_answered = today in info["answers"]
        if not already_answered:
            await context.bot.send_message(chat_id=int(client_id), text=QUESTION)

# --- Message Handler ---
async def handle_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id not in clients:
        return

    response = update.message.text.strip().lower()
    today = str(datetime.date.today())

    clients[user_id]["answers"][today] = response
    save_data(clients)

    if response != EXPECTED_ANSWER:
        await context.bot.send_message(chat_id=MASTER_ID,
            text=f"⚠️ Deviation: @{clients[user_id]['username']} answered '{response}' on {today}")

# --- Scheduler ---
scheduler = BackgroundScheduler()
scheduler.add_job(lambda: app.bot.loop.create_task(ask_questions(app.bot)), "cron", hour=9)
scheduler.start()

# --- Main App Setup ---
app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("addclient", addclient))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_response))

keep_alive()
print("✅ Bot is running...")
app.run_polling()
