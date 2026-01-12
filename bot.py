import asyncio
import json
import sqlite3
import base64
from datetime import date
import os
import json
from json.decoder import JSONDecodeError

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from mistralai import Mistral
from dotenv import load_dotenv

# ================== CONFIG ==================

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
if not BOT_TOKEN or not MISTRAL_API_KEY:
    raise RuntimeError("Нужно указать BOT_TOKEN и MISTRAL_API_KEY в .env")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
mistral = Mistral(api_key=MISTRAL_API_KEY)

# ================== DATABASE ==================

conn = sqlite3.connect("calories.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    daily_limit INTEGER NOT NULL,
    calories_today REAL NOT NULL,
    last_reset TEXT NOT NULL
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS meals (
    user_id INTEGER,
    calories REAL,
    protein REAL,
    fat REAL,
    carbs REAL,
    active INTEGER
)
""")

conn.commit()

# ================== FSM ==================

class Setup(StatesGroup):
    waiting_limit = State()
    waiting_photo = State()

# ================== KEYBOARDS ==================

main_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="➕ Добавить приём пищи")]],
    resize_keyboard=True
)

meal_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📷 Добавить ещё фото")],
        [KeyboardButton(text="✅ Закончить подсчёт")]
    ],
    resize_keyboard=True
)

# ================== HELPERS ==================

def get_user(user_id: int):
    cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    return cursor.fetchone()

def reset_if_new_day(user_id: int):
    user = get_user(user_id)
    today = date.today().isoformat()
    if user and user[3] != today:
        cursor.execute(
            "UPDATE users SET calories_today=0, last_reset=? WHERE user_id=?",
            (today, user_id)
        )
        conn.commit()

def start_meal(user_id: int):
    cursor.execute("DELETE FROM meals WHERE user_id=?", (user_id,))
    cursor.execute(
        "INSERT INTO meals VALUES (?, 0, 0, 0, 0, 1)",
        (user_id,)
    )
    conn.commit()

def add_to_meal(user_id: int, data: dict):
    cursor.execute("""
        UPDATE meals
        SET calories = calories + ?,
            protein = protein + ?,
            fat = fat + ?,
            carbs = carbs + ?
        WHERE user_id=? AND active=1
    """, (
        float(data["calories"]),
        float(data["protein"]),
        float(data["fat"]),
        float(data["carbs"]),
        user_id
    ))
    conn.commit()

def finish_meal(user_id: int):
    cursor.execute("""
        SELECT calories, protein, fat, carbs
        FROM meals WHERE user_id=? AND active=1
    """, (user_id,))
    meal = cursor.fetchone()

    if not meal or meal[0] <= 0:
        return None

    cursor.execute("""
        UPDATE users
        SET calories_today = calories_today + ?
        WHERE user_id=?
    """, (meal[0], user_id))

    cursor.execute("DELETE FROM meals WHERE user_id=?", (user_id,))
    conn.commit()

    return meal

# ================== HANDLERS ==================

@dp.message(CommandStart())
async def start(msg: Message, state: FSMContext):
    user = get_user(msg.from_user.id)
    if not user:
        await msg.answer(
            "Привет! 👋\nЯ считаю калории по фото еды 📷\n"
            "Введи дневной лимит калорий (например: 2000)."
        )
        await state.set_state(Setup.waiting_limit)
    else:
        reset_if_new_day(msg.from_user.id)
        await msg.answer("С возвращением 👌", reply_markup=main_kb)

@dp.message(Setup.waiting_limit)
async def set_limit(msg: Message, state: FSMContext):
    if not msg.text or not msg.text.isdigit():
        await msg.answer("Введите число, например 2000.")
        return

    cursor.execute(
        "INSERT INTO users VALUES (?, ?, 0, ?)",
        (msg.from_user.id, int(msg.text), date.today().isoformat())
    )
    conn.commit()

    await msg.answer("🎯 Лимит сохранён!", reply_markup=main_kb)
    await state.clear()

@dp.message(F.text == "➕ Добавить приём пищи")
async def add_meal(msg: Message, state: FSMContext):
    reset_if_new_day(msg.from_user.id)
    start_meal(msg.from_user.id)
    await msg.answer("Отправь фото еды 📷")
    await state.set_state(Setup.waiting_photo)

@dp.message(F.text == "📷 Добавить ещё фото")
async def more_photo(msg: Message):
    await msg.answer("Отправь ещё одно фото 📷")

@dp.message(Setup.waiting_photo, F.photo)
async def analyze(msg: Message):
    photo = msg.photo[-1]
    file = await bot.get_file(photo.file_id)
    image = await bot.download_file(file.file_path)

    image_b64 = base64.b64encode(image.read()).decode("utf-8")

    response = mistral.chat.complete(
        model="pixtral-large-latest",
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Определи еду на фото и оцени КБЖУ.\n"
                            "Ответь строго в формате JSON БЕЗ лишнего текста, без комментариев:\n"
                        "{\"calories\": число, \"protein\": число, \"fat\": число, \"carbs\": число}"
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": f"data:image/jpeg;base64,{image_b64}",
                    },
                ],
            }
        ],
    )

    msg_obj = response.choices[0].message
    raw_content = msg_obj.content

    if isinstance(raw_content, list):
        parts = []
        for part in raw_content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text", ""))
        content_str = "\n".join(parts).strip()
    else:
        content_str = str(raw_content).strip()

    # 5. Пытаемся распарсить как JSON
    try:
        data = json.loads(content_str)
    except JSONDecodeError:
        # На всякий случай пробуем вытащить JSON по простому регекспу
        import re

        match = re.search(r"\{.*\}", content_str, re.S)
        if not match:
            await msg.answer(
                "Не удалось распознать ответ модели как JSON. "
                "Попробуй ещё раз прислать фото."
            )
            return

        try:
            data = json.loads(match.group(0))
        except JSONDecodeError:
            await msg.answer(
                "Модель ответила в неожиданном формате. "
                "Попробуй ещё раз или другое фото."
            )
            return

    # 6. Записываем в БД / хранилище
    add_to_meal(msg.from_user.id, data)

    await msg.answer(
        f"Добавлено:\n🔥 {data['calories']} ккал\n"
        f"Б {data['protein']} г | Ж {data['fat']} г | У {data['carbs']} г",
        reply_markup=meal_kb
    )
@dp.message(F.text == "✅ Закончить подсчёт")
async def finish(msg: Message, state: FSMContext):
    meal = finish_meal(msg.from_user.id)
    if not meal:
        await msg.answer("Ты ещё не добавил фото 🙂")
        return

    user = get_user(msg.from_user.id)

    await msg.answer(
        f"🍽 Приём пищи завершён\n"
        f"Калории: {meal[0]:.0f} ккал\n"
        f"Б: {meal[1]:.1f} г | Ж: {meal[2]:.1f} г | У: {meal[3]:.1f} г\n"
        f"За день: {user[2]:.0f}/{user[1]} ккал",
        reply_markup=main_kb
    )
    await state.clear()

# ================== RUN ==================

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
