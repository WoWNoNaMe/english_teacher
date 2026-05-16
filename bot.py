import os
import json
import random
import asyncio
import logging
import requests
from datetime import datetime, time
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

    
# --- Config ---
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
CHAT_ID = int(os.environ["CHAT_ID"])
DATA_FILE = "progress.json"

DAILY_SEND_HOUR = 9      # 07:40 Budapest = 05:40 UTC
DAILY_SEND_MINUTE = 33
QUESTION_START_HOUR = 7  # 09:00 Budapest = 07:00 UTC
QUESTION_END_HOUR = 18   # 20:00 Budapest = 18:00 UTC

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# --- Gemini API (direkt requests, nem SDK) ---

def gemini(prompt: str) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    r = requests.post(url, json=body, timeout=30)
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


# --- Adatkezelés ---

def load_data() -> dict:
    if Path(DATA_FILE).exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "words": [],
        "today_words": [],
        "learned_today": False,
        "current_question": None,
        "last_date": None,
    }

def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# --- AI funkciók ---

def get_daily_words(previous: list) -> str:
    prev_text = ", ".join(previous[-30:]) if previous else "nincs még"
    return gemini(f"""Te egy szakmai angol oktató vagy. A tanuló Junior Model Validator – pénzügyi modellek validálása, kockázatelemzés, stressztesztelés, model governance.

Generálj pontosan 5 szakmai angol szót/kifejezést, amiket még NEM tanult (előzők: {prev_text}).

Formátum (pontosan, semmi más):
1. **szó** – magyar jelentés – Példamondat angolul.
2. **szó** – magyar jelentés – Példamondat angolul.
3. **szó** – magyar jelentés – Példamondat angolul.
4. **szó** – magyar jelentés – Példamondat angolul.
5. **szó** – magyar jelentés – Példamondat angolul.""")

def get_question(word: str) -> str:
    return gemini(f"""Egy Junior Model Validator angolt tanul. Az egyik szava:
{word}

Írj EGY rövid kérdést magyarul ami teszteli hogy emlékszik-e rá. Csak a kérdést írd.""")

def check_answer(question: str, word: str, answer: str) -> str:
    return gemini(f"""Kérdés: {question}
Helyes szó: {word}
Tanuló válasza: {answer}

Értékeld röviden magyarul (max 2 mondat): helyes volt-e, és ha nem, mi lett volna a helyes válasz.""")


# --- Napi szavak küldése ---

async def send_daily_words(bot, data: dict):
    today = datetime.now().strftime("%Y-%m-%d")
    if data.get("last_date") == today:
        return

    logger.info("Napi szavak generálása...")
    previous = [w["term"] for w in data["words"]]
    words_text = get_daily_words(previous)

    today_words = []
    for line in words_text.strip().split("\n"):
        if "**" in line:
            term = line.split("**")[1]
            today_words.append({"term": term, "full": line})

    data["today_words"] = today_words
    data["words"].extend(today_words)
    data["learned_today"] = False
    data["last_date"] = today
    data["current_question"] = None
    save_data(data)

    total = len(data["words"])
    msg = f"☀️ *{today} – Napi szakmai angol*\n\n{words_text}\n\n"
    if total > len(today_words):
        msg += f"📚 _{total - len(today_words)} korábbi szó is vár ismétlésre._\n\n"
    msg += "Ha megtanultad, írd: *megtanultam*"

    await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
    logger.info("Napi szavak elküldve.")


# --- Kérdés küldése ---

async def send_question(bot, data: dict, force: bool = False):
    if not data.get("learned_today"):
        return

    now_utc = datetime.utcnow()
    if not force and not (QUESTION_START_HOUR <= now_utc.hour < QUESTION_END_HOUR):
        return

    if data.get("current_question"):
        return

    all_words = data.get("words", [])
    if not all_words:
        return

    word = random.choice(all_words[-20:])
    question = get_question(word["full"])

    data["current_question"] = {"question": question, "word": word}
    save_data(data)

    await bot.send_message(
        chat_id=CHAT_ID,
        text=f"🧠 *Gyors ismétlés:*\n\n{question}",
        parse_mode="Markdown"
    )


# --- Telegram handlers ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    data = load_data()

    if "megtanultam" in text:
        if data.get("learned_today"):
            await update.message.reply_text("✅ Már jelezted! Kérdések jönnek a nap folyamán.")
            return
        data["learned_today"] = True
        save_data(data)
        total = len(data.get("words", []))
        await update.message.reply_text(
            f"💪 Klassz! Összesen már *{total}* szót ismersz.\n\n"
            f"A nap folyamán kérdezek – de nem most rögtön. 😏",
            parse_mode="Markdown"
        )
        return

    if data.get("current_question"):
        q = data["current_question"]
        feedback = check_answer(q["question"], q["word"]["full"], text)
        data["current_question"] = None
        save_data(data)
        await update.message.reply_text(f"📝 {feedback}")
        return

    await update.message.reply_text(
        "Ha megtanultad a mai szavakat, írd: *megtanultam* 📖",
        parse_mode="Markdown"
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Szia! Ez a Model Validator angol tanuló botod.\n\n"
        "Minden reggel 7:40-kor kapsz 5 új szakmai szót.\n"
        "Ha megtanultad, írd: *megtanultam*\n"
        "Utána a nap folyamán kérdezek tőled néhányat. 💪",
        parse_mode="Markdown"
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    total = len(data.get("words", []))
    learned = "✅ Igen" if data.get("learned_today") else "❌ Még nem"
    msg = f"📊 *Státusz*\n\nÖsszes tanult szó: *{total}*\nMai szavak megtanulva: {learned}"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_force(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    data["last_date"] = None
    save_data(data)
    await update.message.reply_text("⏳ Generálás...")
    await send_daily_words(context.bot, data)

async def cmd_forcequestion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    if not data.get("today_words"):
        await update.message.reply_text("Előbb küldj /force-t!")
        return
    await send_question(context.bot, data, force=True)
    if not load_data().get("current_question"):
        await update.message.reply_text("Hiba, próbáld újra.")


# --- Scheduler ---

async def scheduler(bot):
    while True:
        now = datetime.utcnow()
        logger.info(f"Scheduler tick: {now.strftime('%H:%M:%S')} UTC")
        data = load_data()
        today = now.strftime("%Y-%m-%d")

        if now.hour == DAILY_SEND_HOUR and now.minute >= DAILY_SEND_MINUTE:
            if data.get("last_date") != today:
                await send_daily_words(bot, data)

        if data.get("learned_today") and not data.get("current_question"):
            if QUESTION_START_HOUR <= now.hour < QUESTION_END_HOUR:
                if random.random() < 0.25:
                    await send_question(bot, data)

        await asyncio.sleep(600)

# --- Main ---

async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("force", cmd_force))
    app.add_handler(CommandHandler("forcequestion", cmd_forcequestion))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot indul...")
    
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True, poll_interval=1.0)

    logger.info("Scheduler indul...")
    asyncio.ensure_future(scheduler(app.bot))
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
