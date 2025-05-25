import logging
import pytz
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import os
import asyncio
import telegram.error

# ========== CONFIG ===========
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ALLOWED_MASTER_IDS = os.environ.get("ALLOWED_MASTER_IDS", "")
ALLOWED_MASTERS = set(int(mid.strip()) for mid in ALLOWED_MASTER_IDS.split(",") if mid.strip())
IST = pytz.timezone("Asia/Kolkata")

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

registered_groups = {}

QUESTIONS = [
    {
        "time": {"hour": 10, "minute": 0},
        "text": "Did you have your breakfast as per the diet plan?",
        "expected": "Yes",
        "next_action_yes": "Please share a picture of your breakfast.",
        "next_action_no": "Please provide an explanation for missing breakfast.",
    },
    {
        "time": {"hour": 14, "minute": 30},
        "text": "Did you have your lunch as per the diet plan?",
        "expected": "Yes",
        "next_action_yes": "Please share a picture of your lunch.",
        "next_action_no": "Please provide an explanation for missing lunch.",
    },
    {
        "time": {"hour": 21, "minute": 0},
        "text": "Did you have your dinner as per the diet plan?",
        "expected": "Yes",
        "next_action_yes": "Please share a picture of your dinner.",
        "next_action_no": "Please provide an explanation for missing dinner.",
    },
]

scheduler = AsyncIOScheduler(timezone=IST)

async def send_with_retry(bot, *args, max_retries=3, delay=2, **kwargs):
    for attempt in range(max_retries):
        try:
            return await bot.send_message(*args, **kwargs)
        except (telegram.error.TimedOut, telegram.error.RetryAfter, telegram.error.NetworkError) as e:
            if attempt < max_retries - 1:
                await asyncio.sleep(delay)
                delay *= 2
            else:
                logger.error(f"Message failed after {max_retries} attempts: {e}")
        except telegram.error.TelegramError as e:
            logger.error(f"Telegram error (no retry): {e}")
            break

async def register_group_by_mention(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    group_name = update.effective_chat.title or str(chat_id)

    if user_id not in ALLOWED_MASTERS:
        await update.message.reply_text("Sorry, you are not authorized to register this bot in groups.")
        return

    if chat_id in registered_groups:
        await update.message.reply_text("This group is already registered.")
        return

    registered_groups[chat_id] = {
        "master_id": user_id,
        "state": None,
        "question_idx": None,
        "group_name": group_name
    }
    await update.message.reply_text("Hello Aruna, group registered successfully! I will now take over the conversation to perform daily follow-ups with the client.")

    schedule_questions(chat_id, user_id)

async def mention_register_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat = update.effective_chat

    if chat.type not in ("group", "supergroup"):
        return

    if not message or not message.entities:
        return

    for entity in message.entities:
        if entity.type in ("mention", "text_mention"):
            if entity.type == "mention":
                mentioned_username = message.text[entity.offset:entity.offset + entity.length]
                if mentioned_username.lower() == f"@{context.bot.username.lower()}":
                    await register_group_by_mention(update, context)
                    return
            elif entity.type == "text_mention":
                if entity.user and entity.user.id == context.bot.id:
                    await register_group_by_mention(update, context)
                    return

def schedule_questions(chat_id, master_id):
    for idx in range(len(QUESTIONS)):
        job_id = f"{chat_id}_q{idx}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)

    for idx, q in enumerate(QUESTIONS):
        scheduler.add_job(
            ask_question,
            CronTrigger(hour=q["time"]["hour"], minute=q["time"]["minute"], timezone=IST),
            args=[chat_id, master_id, idx],
            id=f"{chat_id}_q{idx}",
            replace_existing=True,
        )

async def ask_question(chat_id, master_id, question_index):
    question = QUESTIONS[question_index]
    keyboard = [[
        InlineKeyboardButton("Yes", callback_data=f"answer_{question_index}_yes"),
        InlineKeyboardButton("No", callback_data=f"answer_{question_index}_no"),
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        global application_instance
        await send_with_retry(application_instance.bot, chat_id=chat_id, text=question["text"], reply_markup=reply_markup)

        if chat_id in registered_groups:
            registered_groups[chat_id]["state"] = None
            registered_groups[chat_id]["question_idx"] = question_index
    except Exception as e:
        logger.error(f"Failed to send question to {chat_id}: {e}")

async def answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, q_idx_str, ans = query.data.split("_")
        q_idx = int(q_idx_str)
    except Exception:
        await query.edit_message_text(text="Invalid response.")
        return

    chat_id = query.message.chat_id
    if chat_id not in registered_groups:
        await query.edit_message_text(text="This group is not registered. Please mention me in this group to register.")
        return

    master_id = registered_groups[chat_id]["master_id"]
    group_name = registered_groups[chat_id].get("group_name", str(chat_id))
    question = QUESTIONS[q_idx]

    if ans == "yes":
        await query.edit_message_text(text=question["next_action_yes"])
        registered_groups[chat_id]["state"] = "awaiting_photo"
        registered_groups[chat_id]["question_idx"] = q_idx
    else:
        await query.edit_message_text(text=question["next_action_no"])
        registered_groups[chat_id]["state"] = "awaiting_explanation"
        registered_groups[chat_id]["question_idx"] = q_idx
        await send_with_retry(context.bot, chat_id=master_id, text=(f"\u26a0\ufe0f Deviation alert from group '{group_name}':\nQuestion: {question['text']}\nClient answered NO."))

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if chat_id not in registered_groups:
        return

    state = registered_groups[chat_id]["state"]
    q_idx = registered_groups[chat_id]["question_idx"]
    group_name = registered_groups[chat_id].get("group_name", str(chat_id))

    if state == "awaiting_photo":
        if update.message.photo:
            await update.message.reply_text("\u2705 Photo received, thank you!")
            registered_groups[chat_id]["state"] = None
            registered_groups[chat_id]["question_idx"] = None
        else:
            await update.message.reply_text("Please send a photo as requested.")
    elif state == "awaiting_explanation":
        if update.message.text:
            explanation = update.message.text
            master_id = registered_groups[chat_id]["master_id"]
            question = QUESTIONS[q_idx]
            await update.message.reply_text("\u2705 Explanation received, thank you!")
            await send_with_retry(context.bot, chat_id=master_id, text=(f"\u26a0\ufe0f Explanation from group '{group_name}':\nQuestion: {question['text']}\nExplanation: {explanation}"))
            registered_groups[chat_id]["state"] = None
            registered_groups[chat_id]["question_idx"] = None
        else:
            await update.message.reply_text("Please send a text explanation.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hello Dt Aruna! \nCreate a group with your client and add me (@assistant_aarchee_bot).\nThen mention/tag me in the group chat to activate the bot."
    )

async def main():
    global application_instance
    application_instance = ApplicationBuilder().token(BOT_TOKEN).build()

    application_instance.add_handler(CommandHandler("start", start))
    application_instance.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, mention_register_handler))
    application_instance.add_handler(CallbackQueryHandler(answer_callback))
    application_instance.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))

    scheduler.start()
    await application_instance.run_polling()

if __name__ == "__main__":
    import nest_asyncio
    nest_asyncio.apply()
    asyncio.get_event_loop().run_until_complete(main())
