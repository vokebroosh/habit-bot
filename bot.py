# bot.py — полностью исправленный бот для aiogram 3.22 + APScheduler
import logging
import sqlite3
import re
from datetime import datetime
import pytz
import asyncio

from aiogram import Bot, Dispatcher, types
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.filters import Command

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# -----------------------------
# Настройки (вставлен твой ключ по запросу)
# -----------------------------
API_TOKEN = "8288617895:AAH6uoqtii48Y8BPd9_Y0yVIb1ktg105E0U"
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# используем часовой пояс Бишкек (GMT+6)
timezone = pytz.timezone('Asia/Bishkek')

bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
scheduler = AsyncIOScheduler()

# -----------------------------
# БД (SQLite)
# -----------------------------
conn = sqlite3.connect("habits.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS habits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    name TEXT,
    time TEXT,
    timezone TEXT,
    created_at TEXT,
    completed_count INTEGER DEFAULT 0
)
""")
conn.commit()

# -----------------------------
# Регулярка времени
# -----------------------------
TIME_RE = re.compile(r'^\d{1,2}:[0-5]\d$')

# -----------------------------
# Утилиты
# -----------------------------
def format_age(created_iso: str) -> str:
    """Вернуть 'X дней Y часов' от created_iso до now в timezone."""
    try:
        created = datetime.fromisoformat(created_iso)
    except Exception:
        # fallback: без таймзоны
        created = datetime.fromisoformat(created_iso)
    now = datetime.now(timezone)
    # если created — naive, делаем aware (предполагаем timezone)
    if created.tzinfo is None:
        created = timezone.localize(created)
    delta = now - created.astimezone(timezone)
    days = delta.days
    hours = delta.seconds // 3600
    return f"{days} дней {hours} часов"

def build_reply_keyboard() -> ReplyKeyboardMarkup:
    # Формируем ReplyKeyboardMarkup через keyword-аргумент (требует pydantic)
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="/add_habit")],
            [KeyboardButton(text="/list_habits")]
        ],
        resize_keyboard=True
    )

def build_inline_for_habit(habit_id: int) -> InlineKeyboardMarkup:
    # Корректно создаём InlineKeyboardMarkup через inline_keyboard kwarg
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Выполнено", callback_data=f"done_{habit_id}"),
                InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"edit_{habit_id}"),
                InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"delete_{habit_id}")
            ]
        ]
    )

# -----------------------------
# Планирование напоминаний
# -----------------------------
def schedule_reminder(habit_id: int):
    """Запланировать (или пересоздать) ежедневное напоминание для habit_id."""
    cursor.execute("SELECT user_id, time FROM habits WHERE id=?", (habit_id,))
    row = cursor.fetchone()
    if not row:
        return
    user_id, time_str = row
    # валидируем формат
    if not TIME_RE.match(time_str):
        log.warning("Некорректный формат времени в БД для habit %s: %s", habit_id, time_str)
        return
    hour, minute = map(int, time_str.split(":"))
    job_id = f"reminder_{habit_id}"
    # удаляем старую задачу, если была
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass

    async def send_reminder():
        try:
            cursor.execute("SELECT user_id, name FROM habits WHERE id=?", (habit_id,))
            rr = cursor.fetchone()
            if not rr:
                try:
                    scheduler.remove_job(job_id)
                except Exception:
                    pass
                return
            u_id, name = rr
            await bot.send_message(u_id, f"Напоминание: {name}", reply_markup=build_inline_for_habit(habit_id))
        except Exception as e:
            log.exception("Ошибка в задаче напоминания для %s: %s", habit_id, e)

    # CronTrigger — ежедневно в указанное время (в timezone)
    scheduler.add_job(send_reminder, trigger=CronTrigger(hour=hour, minute=minute, timezone=timezone), id=job_id)
    log.info("Scheduled reminder %s at %02d:%02d %s", job_id, hour, minute, timezone)

def reschedule_all_from_db():
    """При старте пересоздать все напоминания из базы."""
    cursor.execute("SELECT id FROM habits")
    for (hid,) in cursor.fetchall():
        try:
            schedule_reminder(hid)
        except Exception:
            log.exception("Не удалось пересоздать напоминание для habit %s", hid)

# -----------------------------
# Ежедневный обзор
# -----------------------------
async def send_daily_overview():
    now = datetime.now(timezone)
    cursor.execute("SELECT DISTINCT user_id FROM habits")
    users = cursor.fetchall()
    for (user_id,) in users:
        cursor.execute("SELECT id, name, created_at, completed_count, time FROM habits WHERE user_id=?", (user_id,))
        habits = cursor.fetchall()
        if not habits:
            continue
        for habit_id, name, created_at_str, completed_count, time_str in habits:
            age = format_age(created_at_str)
            try:
                await bot.send_message(
                    user_id,
                    f"{name}\n"
                    f"Время с создания: {age}\n"
                    f"Выполнено: {completed_count} раз\n"
                    f"Следующее напоминание: каждый день в {time_str}",
                    reply_markup=build_inline_for_habit(habit_id)
                )
            except Exception:
                log.exception("Не удалось отправить daily overview пользователю %s", user_id)

# добавляем job (будет в jobstore до старта scheduler)
scheduler.add_job(send_daily_overview, trigger=CronTrigger(hour=8, minute=0, timezone=timezone), id="daily_overview")

# -----------------------------
# Хендлеры (в порядке: спец. обработчики редактирования ДО общего обработчика текста)
# -----------------------------

# словари состояния для простого режима редактирования (пользователь -> habit_id)
EDIT_NAME_USERS: dict[int, int] = {}
EDIT_TIME_USERS: dict[int, int] = {}

@dp.message(Command(commands=["start"]))
async def cmd_start(message: types.Message):
    markup = build_reply_keyboard()
    await message.answer("Привет! Я помогу тебе формировать привычки. Добавь привычку: /add_habit", reply_markup=markup)

@dp.message(Command(commands=["add_habit"]))
async def cmd_add_habit(message: types.Message):
    await message.answer("Напиши привычку и время через запятую (пример: Чтение, 21:30).")

@dp.message(lambda m: m.from_user.id in EDIT_NAME_USERS)
async def process_new_name(message: types.Message):
    user_id = message.from_user.id
    habit_id = EDIT_NAME_USERS.pop(user_id, None)
    if habit_id is None:
        return
    new_name = message.text.strip()
    cursor.execute("UPDATE habits SET name=? WHERE id=?", (new_name, habit_id))
    conn.commit()
    # Пересоздать напоминание (чтобы кнопки/текст актуальны)
    schedule_reminder(habit_id)
    await message.answer(f"Название привычки обновлено: {new_name}")

@dp.message(lambda m: m.from_user.id in EDIT_TIME_USERS)
async def process_new_time(message: types.Message):
    user_id = message.from_user.id
    habit_id = EDIT_TIME_USERS.pop(user_id, None)
    if habit_id is None:
        return
    new_time = message.text.strip()
    if not TIME_RE.match(new_time):
        await message.answer("Неверный формат времени. Используй HH:MM (например 21:30).")
        return
    cursor.execute("UPDATE habits SET time=? WHERE id=?", (new_time, habit_id))
    conn.commit()
    schedule_reminder(habit_id)
    await message.answer(f"Время привычки обновлено: {new_time}")

# ОБРАБОТЧИК: добавление привычки (только если текст с запятой и не в режиме редактирования)
@dp.message(lambda m: ("," in m.text) and (m.from_user.id not in EDIT_NAME_USERS) and (m.from_user.id not in EDIT_TIME_USERS))
async def save_habit(message: types.Message):
    try:
        name, time_str = [x.strip() for x in message.text.split(",", 1)]
        if not TIME_RE.match(time_str):
            await message.answer("Неверный формат времени. Используй HH:MM (например 21:30).")
            return
        created_at = datetime.now(timezone).isoformat()
        cursor.execute(
            "INSERT INTO habits (user_id, name, time, timezone, created_at) VALUES (?, ?, ?, ?, ?)",
            (message.from_user.id, name, time_str, "Asia/Bishkek", created_at)
        )
        conn.commit()
        habit_id = cursor.lastrowid
        schedule_reminder(habit_id)
        await message.answer(f"Привычка '{name}' добавлена. Напоминания каждый день в {time_str}.")
    except Exception as e:
        log.exception("Ошибка при сохранении привычки: %s", e)
        await message.answer("Произошла ошибка при добавлении привычки.")

@dp.message(Command(commands=["list_habits"]))
async def cmd_list_habits(message: types.Message):
    cursor.execute("SELECT id, name, created_at, completed_count, time FROM habits WHERE user_id=?", (message.from_user.id,))
    habits = cursor.fetchall()
    if not habits:
        await message.answer("У тебя пока нет привычек.")
        return
    for habit_id, name, created_at_str, completed_count, time_str in habits:
        age = format_age(created_at_str)
        await message.answer(
            f"{name}\n"
            f"Время с создания: {age}\n"
            f"Выполнено: {completed_count} раз\n"
            f"Напоминание: каждый день в {time_str}",
            reply_markup=build_inline_for_habit(habit_id)
        )

# -----------------------------
# Callback handlers (inline buttons)
# -----------------------------
@dp.callback_query(lambda c: c.data and c.data.startswith("done_"))
async def callback_done(cb: types.CallbackQuery):
    try:
        habit_id = int(cb.data.split("_", 1)[1])
        cursor.execute("SELECT completed_count, name, time, user_id, created_at FROM habits WHERE id=?", (habit_id,))
        row = cursor.fetchone()
        if not row:
            await cb.answer("Привычка не найдена.")
            return
        completed_count, name, time_str, user_id, created_at_str = row
        completed_count += 1
        cursor.execute("UPDATE habits SET completed_count=? WHERE id=?", (completed_count, habit_id))
        conn.commit()
        age = format_age(created_at_str)
        await cb.message.edit_text(
            f"{name}\n"
            f"Время с создания: {age}\n"
            f"Выполнено: {completed_count} раз\n"
            f"Напоминание: каждый день в {time_str}",
            reply_markup=build_inline_for_habit(habit_id)
        )
        schedule_reminder(habit_id)
        await cb.answer("Отмечено!")
    except Exception:
        log.exception("Ошибка в callback_done")
        await cb.answer("Ошибка.")

@dp.callback_query(lambda c: c.data and c.data.startswith("delete_"))
async def callback_delete(cb: types.CallbackQuery):
    try:
        habit_id = int(cb.data.split("_", 1)[1])
        cursor.execute("SELECT name FROM habits WHERE id=?", (habit_id,))
        row = cursor.fetchone()
        if not row:
            await cb.answer("Привычка не найдена.")
            return
        name = row[0]
        cursor.execute("DELETE FROM habits WHERE id=?", (habit_id,))
        conn.commit()
        try:
            scheduler.remove_job(f"reminder_{habit_id}")
        except Exception:
            pass
        await cb.message.edit_text(f"Привычка '{name}' удалена.")
        await cb.answer("Удалено")
    except Exception:
        log.exception("Ошибка в callback_delete")
        await cb.answer("Ошибка при удалении.")

@dp.callback_query(lambda c: c.data and c.data.startswith("edit_"))
async def callback_edit(cb: types.CallbackQuery):
    try:
        habit_id = int(cb.data.split("_", 1)[1])
        cursor.execute("SELECT name, time FROM habits WHERE id=?", (habit_id,))
        row = cursor.fetchone()
        if not row:
            await cb.answer("Привычка не найдена.")
            return
        name, time_str = row
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✏️ Изменить название", callback_data=f"edit_name_{habit_id}")],
                [InlineKeyboardButton(text="⏰ Изменить время", callback_data=f"edit_time_{habit_id}")]
            ]
        )
        await cb.message.answer(f"Выберите, что редактировать для '{name}':", reply_markup=keyboard)
        await cb.answer()
    except Exception:
        log.exception("Ошибка в callback_edit")
        await cb.answer("Ошибка при редактировании.")

@dp.callback_query(lambda c: c.data and c.data.startswith("edit_name_"))
async def callback_edit_name(cb: types.CallbackQuery):
    try:
        habit_id = int(cb.data.split("_", 2)[2])
        EDIT_NAME_USERS[cb.from_user.id] = habit_id
        await cb.message.answer("Напиши новое название привычки:")
        await cb.answer()
    except Exception:
        log.exception("Ошибка в callback_edit_name")
        await cb.answer("Ошибка.")

@dp.callback_query(lambda c: c.data and c.data.startswith("edit_time_"))
async def callback_edit_time(cb: types.CallbackQuery):
    try:
        habit_id = int(cb.data.split("_", 2)[2])
        EDIT_TIME_USERS[cb.from_user.id] = habit_id
        await cb.message.answer("Напиши новое время в формате HH:MM:")
        await cb.answer()
    except Exception:
        log.exception("Ошибка в callback_edit_time")
        await cb.answer("Ошибка.")

# -----------------------------
# Запуск (рескейджул из БД, запуск планировщика и polling)
# -----------------------------
async def main():
    # Пересоздать напоминания для всех привычек (на случай рестарта)
    try:
        reschedule_all_from_db()
    except Exception:
        log.exception("Не удалось пересоздать напоминания при старте.")
    scheduler.start()
    log.info("Scheduler started")
    # старт polling
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
