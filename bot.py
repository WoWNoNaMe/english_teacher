import os
import json
import random
import asyncio
import logging
from datetime import datetime, time
from pathlib import Path
 
import google.generativeai as genai
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
 
# --- Config ---
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
CHAT_ID = int(os.environ["CHAT_ID"])  # A te Telegram chat ID-d
DATA_FILE = "progress.json"
 
# Napközben mikor jöhetnek kérdések (UTC időben!)
QUESTION_WINDOW_START = time(7, 40)   # 09:40 Budapest = 07:40 UTC
QUESTION_WINDOW_END = time(18, 0)     # 20:00 Budapest = 18:00 UTC
DAILY_SEND_HOUR = 5    # 07:40 Budapest = 05:40 UTC
DAILY_SEND_MINUTE = 40
 
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
 
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")
 
 
# --- Adatkezelés ---
 
def load_data() -> dict:
    if Path(DATA_FILE).exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "words": [],          # Összes tanult szó/kifejezés
        "learned_today": False,
        "today_words": [],    # Mai 5 szó
        "pending_reviews": [], # Szavak amiket még kérdezni kell ma
        "last_date": None,
    }
 
def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
 
 
# --- Gemini API hívások ---
 
def generate_daily_words(previous_words: list[str]) -> str:
    """5 új szakmai angol szó/kifejezés generálása Model Validator témában."""
    prev_text = ", ".join(previous_words[-30:]) if previous_words else "nincs még"
 
    prompt = f"""Te egy szakmai angol oktató vagy. A tanuló Junior Model Validator pozícióban dolgozik – pénzügyi modellek validálásával, kvantitatív kockázatelemzéssel, stresszteszteléssel, model governance-szel foglalkozik.
 
Generálj pontosan 5 szakmai angol szót vagy kifejezést, amiket még NEM tanult (előző szavak: {prev_text}).
 
Formátum (pontosan így, semmi más):
1. **szó/kifejezés** – magyar jelentés – Példamondat angolul.
2. **szó/kifejezés** – magyar jelentés – Példamondat angolul.
3. **szó/kifejezés** – magyar jelentés – Példamondat angolul.
4. **szó/kifejezés** – magyar jelentés – Példamondat angolul.
5. **szó/kifejezés** – magyar jelentés – Példamondat angolul.
 
Csak a listát add vissza, semmi bevezető szöveg."""
 
    response = model.generate_content(prompt)
    return response.text
 
 
def generate_review_question(word_entry: str, all_today_words: list[str]) -> str:
    """Egy kérdést generál egy adott szóról."""
    prompt = f"""Egy Junior Model Validator angolt tanul. Ez az egyik szava:
{word_entry}
 
Generálj EGY rövid kérdést magyarul, ami teszteli hogy emlékszik-e erre a szóra/kifejezésre. 
Például kérdezheted a jelentést, vagy hogy töltse ki a hiányzó szót egy mondatban angolul.
Csak a kérdést írd, semmi más."""
 
    response = model.generate_content(prompt)
    return response.text
 
 
def check_answer(question: str, word_entry: str, user_answer: str) -> str:
    """Értékeli a választ és visszajelez."""
    prompt = f"""Kérdés volt: {question}
A helyes szó/kifejezés: {word_entry}
A tanuló válasza: {user_answer}
 
Értékeld röviden magyarul: helyes volt-e, és ha nem, mi lett volna a helyes válasz. Max 2 mondat."""
 
    response = model.generate_content(prompt)
    return response.text
 
 
# --- Bot logika ---
 
async def send_daily_words(bot: Bot, data: dict):
    """Napi 5 új szó küldése."""
    today = datetime.now().strftime("%Y-%m-%d")
    
    # Ha már küldtük ma, ne küldjük újra
    if data.get("last_date") == today:
        return
 
    logger.info("Napi szavak küldése...")
    
    previous_words_flat = [w["term"] for w in data["words"]]
    words_text = generate_daily_words(previous_words_flat)
    
    # Parsoljuk a szavakat (egyszerű split soronként)
    lines = [l.strip() for l in words_text.strip().split("\n") if l.strip()]
    today_words = []
    for line in lines:
        # Kinyerjük a szót a **...** közül
        if "**" in line:
            term = line.split("**")[1]
            today_words.append({"term": term, "full": line, "reviewed": 0})
 
    # Előző napok szavaiból is hozzáadunk ismétlőket
    review_pool = []
    for w in data["words"]:
        review_pool.append(w)
 
    data["today_words"] = today_words
    data["words"].extend(today_words)
    data["learned_today"] = False
    data["last_date"] = today
    data["pending_reviews"] = []
    data["current_question"] = None
    save_data(data)
 
    # Üzenet összerakása
    day_num = len(set(w.get("date", today) for w in data["words"]))
    review_count = len(review_pool)
    
    msg = f"☀️ *{today} – Napi szakmai angol*\n\n"
    msg += f"*Mai 5 új szó:*\n\n{words_text}\n\n"
    if review_count > 0:
        msg += f"📚 _{review_count} korábbi szó is vár ismétlésre a nap folyamán._\n\n"
    msg += "Ha megtanultad, írd: *megtanultam*"
 
    await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
    logger.info("Napi szavak elküldve.")
 
 
async def send_review_question(bot: Bot, data: dict):
    """Küld egy ismétlő kérdést a nap folyamán."""
    if not data.get("learned_today"):
        return  # Még nem mondta h megtanulta
 
    now = datetime.now().time()
    if not (QUESTION_WINDOW_START <= now <= QUESTION_WINDOW_END):
        return
 
    # Pool: mai szavak + korábbi szavak
    all_words = data.get("words", [])
    if not all_words:
        return
 
    # Véletlenszerűen választunk egyet
    word = random.choice(all_words[-20:])  # Az utóbbi 20 szóból
    question = generate_review_question(word["full"], [w["full"] for w in data["today_words"]])
    
    data["current_question"] = {
        "question": question,
        "word": word
    }
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
 
    # "megtanultam" feldolgozása
    if "megtanultam" in text:
        if data.get("learned_today"):
            await update.message.reply_text("✅ Már jelezted korábban! A nap folyamán jönnek a kérdések.")
            return
        
        data["learned_today"] = True
        save_data(data)
        
        today_count = len(data.get("today_words", []))
        total_count = len(data.get("words", []))
        await update.message.reply_text(
            f"💪 Klassz! A mai {today_count} szót rögzítettem.\n"
            f"Összesen már {total_count} szót ismersz.\n\n"
            f"A nap folyamán 20:00-ig kérdezek néhányat – de nem most rögtön. 😏"
        )
        return
 
    # Ha van függőben lévő kérdés → ez a válasz rá
    if data.get("current_question"):
        q_data = data["current_question"]
        feedback = check_answer(q_data["question"], q_data["word"]["full"], text)
        data["current_question"] = None
        save_data(data)
        await update.message.reply_text(f"📝 {feedback}")
        return
 
    # Egyéb üzenet
    await update.message.reply_text(
        "Szia! Ha megtanultad a mai szavakat, írd: *megtanultam*\n"
        "A nap folyamán kérdezek tőled néhányat. 📖",
        parse_mode="Markdown"
    )
 
 
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Szia! Ez a Model Validator angol tanuló botod.\n\n"
        "Minden reggel 8:00-kor kapsz 5 új szakmai szót.\n"
        "Ha megtanultad, írd: *megtanultam*\n"
        "Utána a nap folyamán kérdezek tőled néhányat.\n\n"
        "Holnap reggelig várok! 💪",
        parse_mode="Markdown"
    )
 
 
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    total = len(data.get("words", []))
    learned = "✅ Igen" if data.get("learned_today") else "❌ Még nem"
    today_words = data.get("today_words", [])
    
    msg = f"📊 *Státusz*\n\n"
    msg += f"Összes tanult szó: *{total}*\n"
    msg += f"Mai szavak megtanulva: {learned}\n"
    if today_words:
        msg += f"\n*Mai szavak:*\n"
        for w in today_words:
            msg += f"• {w['term']}\n"
    
    await update.message.reply_text(msg, parse_mode="Markdown")
 
 
# --- Scheduler ---
 
async def scheduler(bot: Bot):
    """Háttérben futó ütemező."""
    while True:
        now = datetime.utcnow()
        data = load_data()
 
        # Napi szavak küldése 08:00 Budapest = 06:00 UTC
        if now.hour == DAILY_SEND_HOUR and now.minute == DAILY_SEND_MINUTE:
            await send_daily_words(bot, data)
            await asyncio.sleep(60)  # Ne küldje kétszer
            continue
 
        # Napközbeni kérdések: ha megtanulta, random 20-60 percenként kérdez
        if data.get("learned_today"):
            local_hour = (now.hour + 2) % 24  # UTC+2
            if 9 <= local_hour < 18:
                # 30% eséllyel kérdez minden 10 percben → kb. 3-4 kérdés naponta
                if random.random() < 0.30 and not data.get("current_question"):
                    await send_review_question(bot, data)
 
        await asyncio.sleep(600)  # 10 percenként ellenőriz
 
 
async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
 
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
 
    logger.info("Bot indul...")
    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        await scheduler(app.bot)  # végtelen ciklus, ez tartja életben a botot
        await app.updater.stop()
        await app.stop()
 
 
if __name__ == "__main__":
    asyncio.run(main())
 
