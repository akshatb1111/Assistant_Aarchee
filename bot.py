import logging
import pytz
from keep_alive import *
from datetime import datetime
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
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

# ========== CONFIG ===========
BOT_TOKEN = os.environ.get("BOT_TOKEN")  # Injected via Replit Secrets
# Allowed masters IDs as comma-separated string, e.g. "12345678,87654321"
ALLOWED_MASTER_IDS = os.environ.get("ALLOWED_MASTER_IDS", "")
ALLOWED_MASTERS = set(int(mid.strip()) for mid in ALLOWED_MASTER_IDS.split(",") if mid.strip())

IST = pytz.timezone("Asia/Kolkata")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO)
logger = logging.getLogger(__name__)

# Store group chat info: chat_id -> {"master_id": int, "state": None/"awaiting_photo"/"awaiting_explanation", "question_idx": int}
registered_groups = {}

# Questions & schedules
QUESTIONS = [
    {
        "time": {
            "hour": 10,
            "minute": 0
        },
        "text": "Did you have your breakfast as per the diet plan?",
        "expected": "Yes",
        "next_action_yes": "Please share a picture of your breakfast.",
        "next_action_no":
        "Please provide an explanation for missing breakfast.",
    },
    {
        "time": {
            "hour": 14,
            "minute": 30
        },
        "text": "Did you have your lunch as per the diet plan?",
        "expected": "Yes",
        "next_action_yes": "Please share a picture of your lunch.",
        "next_action_no": "Please provide an explanation for missing lunch.",
    },
    {
        "time": {
            "hour": 21,
            "minute": 0
        },
        "text": "Did you have your dinner as per the diet plan?",
        "expected": "Yes",
        "next_action_yes": "Please share a picture of your dinner.",
        "next_action_no": "Please provide an explanation for missing dinner.",
    },
]

scheduler = AsyncIOScheduler(timezone=IST)


async def register_group_by_mention(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if user_id not in ALLOWED_MASTERS:
        await update.message.reply_text("Sorry, you are not authorized to register this bot in groups.")
        return

    if chat_id in registered_groups:
        await update.message.reply_text("This group is already registered.")
        return

    registered_groups[chat_id] = {
        "master_id": user_id,
        "state": None,
        "question_idx": None
    }
    await update.message.reply_text("Group registered successfully! The bot will now start asking scheduled questions.")

    schedule_questions(chat_id, user_id)


async def mention_register_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat = update.effective_chat

    if chat.type not in ("group", "supergroup"):
        return  # Only react in groups

    # Check if the bot was mentioned/tagged in this message
    if not message or not message.entities:
        return

    for entity in message.entities:
        if entity.type in ("mention", "text_mention"):
            # For mention type, get the username mentioned, compare with bot username
            # For text_mention, entity.user contains the user object
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
    # Remove old jobs for this chat first
    for idx in range(len(QUESTIONS)):
        job_id = f"{chat_id}_q{idx}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)

    # Schedule new jobs for this chat
    for idx, q in enumerate(QUESTIONS):
        scheduler.add_job(
            ask_question,
            CronTrigger(hour=q["time"]["hour"],
                        minute=q["time"]["minute"],
                        timezone=IST),
            args=[chat_id, master_id, idx],
            id=f"{chat_id}_q{idx}",
            replace_existing=True,
        )


async def ask_question(chat_id, master_id, question_index):
    question = QUESTIONS[question_index]

    keyboard = [[
        InlineKeyboardButton("Yes",
                             callback_data=f"answer_{question_index}_yes"),
        InlineKeyboardButton("No",
                             callback_data=f"answer_{question_index}_no"),
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        global application_instance
        await application_instance.bot.send_message(chat_id=chat_id,
                                                    text=question["text"],
                                                    reply_markup=reply_markup)

        # Reset client state
        if chat_id in registered_groups:
            registered_groups[chat_id]["state"] = None
            registered_groups[chat_id]["question_idx"] = question_index
    except Exception as e:
        logger.error(f"Failed to send question to {chat_id}: {e}")


async def answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    try:
        _, q_idx_str, ans = data.split("_")
        q_idx = int(q_idx_str)
    except Exception:
        await query.edit_message_text(text="Invalid response.")
        return

    chat_id = query.message.chat_id
    if chat_id not in registered_groups:
        await query.edit_message_text(
            text=
            "This group is not registered. Please mention me in this group to register."
        )
        return

    master_id = registered_groups[chat_id]["master_id"]
    question = QUESTIONS[q_idx]

    if ans == "yes":
        await query.edit_message_text(text=question["next_action_yes"])
        registered_groups[chat_id]["state"] = "awaiting_photo"
        registered_groups[chat_id]["question_idx"] = q_idx
    else:
        await query.edit_message_text(text=question["next_action_no"])
        registered_groups[chat_id]["state"] = "awaiting_explanation"
        registered_groups[chat_id]["question_idx"] = q_idx
        # Notify master privately
        await context.bot.send_message(
            chat_id=master_id,
            text=(
                f"⚠️ Deviation alert from group {chat_id}:\n"
                f"Question: {question['text']}\nClient answered NO."
            ),
        )


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if chat_id not in registered_groups:
        return

    state = registered_groups[chat_id]["state"]
    q_idx = registered_groups[chat_id]["question_idx"]

    if state == "awaiting_photo":
        if update.message.photo:
            await update.message.reply_text("✅ Photo received, thank you!")
            registered_groups[chat_id]["state"] = None
            registered_groups[chat_id]["question_idx"] = None
        else:
            await update.message.reply_text("Please send a photo as requested.")
    elif state == "awaiting_explanation":
        if update.message.text:
            explanation = update.message.text
            master_id = registered_groups[chat_id]["master_id"]
            question = QUESTIONS[q_idx]

            await update.message.reply_text("✅ Explanation received, thank you!")

            await context.bot.send_message(
                chat_id=master_id,
                text=(
                    f"⚠️ Explanation from group {chat_id}:\n"
                    f"Question: {question['text']}\nExplanation: {explanation}"
                ),
            )
            registered_groups[chat_id]["state"] = None
            registered_groups[chat_id]["question_idx"] = None
        else:
            await update.message.reply_text("Please send a text explanation.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hello Master! \n"
        "Create a group with your client and add me (the bot).\n"
        "Then mention/tag me in the group chat to activate the bot."
    )


async def main():
    global application_instance
    application_instance = ApplicationBuilder().token(BOT_TOKEN).build()

    application_instance.add_handler(CommandHandler("start", start))
    # You may keep /register command or remove it if you want.
    # application_instance.add_handler(CommandHandler("register", register))

    # New handler to register group when bot is mentioned
    application_instance.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, mention_register_handler))

    # Callback query handler for answers
    application_instance.add_handler(CallbackQueryHandler(answer_callback))

    # Message handler for photos/explanations
    application_instance.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))

    scheduler.start()

    keep_alive()

    await application_instance.run_polling()


import asyncio

if __name__ == "__main__":
    import nest_asyncio
    nest_asyncio.apply()
    asyncio.get_event_loop().run_until_complete(main())
