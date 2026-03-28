#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram-бот для прийому квитанцій з підтримкою кількох дітей на одного користувача.
Исправления и улучшения:
- Надёжная обработка ссылок: ищем ссылку в любом месте текста, не требуем, чтобы сообщение было только URL.
- Сохраняем ссылки как квитанции (в БД) и всегда создаём receipt_id (fallback при ошибках).
- Восстановлен handler выбора ребёнка для ссылок (choosechild_link).
- Упрощён и стабилизирован код работы с pending_links/pending_photos/pending_files (ключи — int user_id).
- Кнопки подтверждения/отклонения всегда содержат реальный receipt_id.
- Логи добавлены в ключевые места для отладки.
"""

import os
import logging
import sqlite3
import threading
import re
import shutil
from datetime import datetime, timezone
from typing import Optional, Tuple, List

import telebot
from telebot import types

# ========== Настройки ==========
BOT_TOKEN = os.getenv("BOT_TOKEN", "8379731527:AAElElXb0VwzxcCpRQYycz4Ji1VgAbBNwOM")
ADMIN_ID = int(os.getenv("ADMIN_ID", "425693500"))

PHOTOS_DIR = "photos"
FILES_DIR = "files"
DB_PATH = "receipts.db"
MAX_RETURN = 50

# ========== Логирование ==========
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

if not BOT_TOKEN or BOT_TOKEN == "ВАШ_BOT_TOKEN_ТУТ":
    logger.error("BOT_TOKEN не заданий. Встановіть BOT_TOKEN у змінних оточення або в коді.")
    raise SystemExit("BOT_TOKEN не заданий")

os.makedirs(PHOTOS_DIR, exist_ok=True)
os.makedirs(FILES_DIR, exist_ok=True)

# ========== Подключение к БД и миграция ==========
def backup_db_if_exists(db_path: str) -> None:
    if os.path.exists(db_path):
        bak = f"{db_path}.bak.{datetime.now().strftime('%Y%m%d%H%M%S')}"
        shutil.copy2(db_path, bak)
        logger.info("Створено резервну копію БД: %s", bak)

backup_db_if_exists(DB_PATH)
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
db_lock = threading.Lock()

def table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in cur.fetchall()]

def migrate_children_table_if_needed(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='children'")
    if not cur.fetchone():
        cur.execute("""
        CREATE TABLE children (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            child_name TEXT NOT NULL,
            payment_type TEXT,
            note TEXT,
            created_at TEXT NOT NULL
        )
        """)
        conn.commit()
        logger.info("Створено таблицю children (нова схема).")
        return

    cols = table_columns(conn, "children")
    logger.info("Колонки children: %s", cols)
    if "id" in cols and "child_name" in cols and "user_id" in cols:
        return

    logger.info("Починаємо міграцію children...")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS children_new (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        child_name TEXT NOT NULL,
        payment_type TEXT,
        note TEXT,
        created_at TEXT NOT NULL
    )
    """)
    conn.commit()

    cur.execute("PRAGMA table_info(children)")
    info = cur.fetchall()
    old_cols = [c[1] for c in info]
    cur.execute("SELECT * FROM children")
    rows = cur.fetchall()
    for row in rows:
        row_map = dict(zip(old_cols, row))
        user_id = row_map.get("user_id")
        child_name = row_map.get("child_name") or row_map.get("name") or row_map.get("full_name") or ""
        payment_type = row_map.get("payment_type") if "payment_type" in row_map else None
        note = row_map.get("note") if "note" in row_map else None
        created_at = row_map.get("created_at") or datetime.now(timezone.utc).isoformat()
        if user_id is None:
            continue
        cur.execute(
            "INSERT INTO children_new (user_id, child_name, payment_type, note, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, child_name, payment_type, note, created_at)
        )
    conn.commit()

    cur.execute("ALTER TABLE children RENAME TO children_old")
    cur.execute("ALTER TABLE children_new RENAME TO children")
    conn.commit()
    logger.info("Міграція children завершена. Стара таблиця збережена як children_old.")

def ensure_receipts_columns(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(receipts)")
    cols = [r[1] for r in cur.fetchall()]
    if not cols:
        cur.execute("""
        CREATE TABLE receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            child_id INTEGER,
            file_path TEXT,
            file_id TEXT,
            link TEXT,
            timestamp TEXT NOT NULL,
            month TEXT,
            payment_type TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            admin_id INTEGER,
            admin_comment TEXT
        )
        """)
        conn.commit()
        logger.info("Створено таблицю receipts (нова схема).")
        return
    # Добавляем недостающие колонки, если нужно
    if "child_id" not in cols:
        cur.execute("ALTER TABLE receipts ADD COLUMN child_id INTEGER")
    if "file_path" not in cols:
        cur.execute("ALTER TABLE receipts ADD COLUMN file_path TEXT")
    if "file_id" not in cols:
        cur.execute("ALTER TABLE receipts ADD COLUMN file_id TEXT")
    if "link" not in cols:
        cur.execute("ALTER TABLE receipts ADD COLUMN link TEXT")
    if "month" not in cols:
        cur.execute("ALTER TABLE receipts ADD COLUMN month TEXT")
    if "payment_type" not in cols:
        cur.execute("ALTER TABLE receipts ADD COLUMN payment_type TEXT")
    conn.commit()

with db_lock:
    migrate_children_table_if_needed(conn)
    ensure_receipts_columns(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)")
    conn.execute("INSERT OR IGNORE INTO admins(user_id) VALUES (?)", (ADMIN_ID,))
    conn.commit()

# ========== Инициализация бота ==========
bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

# ========== Вспомогательные функции ==========
def is_admin(user_id: int) -> bool:
    with db_lock:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,))
        return cur.fetchone() is not None

def month_year_uk_from_iso(iso_ts: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_ts)
    except Exception:
        try:
            dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        except Exception:
            return iso_ts
    months = {
        1: "січень", 2: "лютий", 3: "березень", 4: "квітень",
        5: "травень", 6: "червень", 7: "липень", 8: "серпень",
        9: "вересень", 10: "жовтень", 11: "листопад", 12: "грудень"
    }
    m = months.get(dt.month, dt.strftime("%B"))
    return f"{m.capitalize()} {dt.year}"

def save_receipt(user_id: int, child_id: Optional[int], file_path: Optional[str], file_id: Optional[str], link: Optional[str], payment_type: Optional[str]) -> int:
    ts = datetime.now(timezone.utc)
    month_str = month_year_uk_from_iso(ts.isoformat())
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO receipts (user_id, child_id, file_path, file_id, link, timestamp, month, payment_type, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
            (user_id, child_id, file_path, file_id, link, ts.isoformat(), month_str, payment_type)
        )
        conn.commit()
        return cur.lastrowid

def get_receipt_by_id(receipt_id: int):
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, user_id, child_id, file_path, file_id, link, timestamp, month, payment_type, status, admin_id, admin_comment FROM receipts WHERE id=?",
            (receipt_id,)
        )
        return cur.fetchone()

def update_receipt_status(receipt_id: int, status: str, admin_id: int, comment: Optional[str] = None) -> None:
    with db_lock:
        conn.execute("UPDATE receipts SET status=?, admin_id=?, admin_comment=? WHERE id=?", (status, admin_id, comment, receipt_id))
        conn.commit()

def list_children(user_id: int) -> List[Tuple[int, str, Optional[str], Optional[str], str]]:
    with db_lock:
        cur = conn.cursor()
        cur.execute("SELECT id, child_name, payment_type, note, created_at FROM children WHERE user_id=? ORDER BY id", (user_id,))
        return cur.fetchall()

def add_child(user_id: int, child_name: str, payment_type: Optional[str] = None, note: Optional[str] = None) -> int:
    ts = datetime.now(timezone.utc).isoformat()
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO children(user_id, child_name, payment_type, note, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, child_name, payment_type, note, ts)
        )
        conn.commit()
        new_id = cur.lastrowid
        logger.info("Додано дитину: user=%s id=%s name=%s", user_id, new_id, child_name)
        return new_id

def update_child(child_id: int, child_name: Optional[str] = None, payment_type: Optional[str] = None, note: Optional[str] = None) -> bool:
    with db_lock:
        cur = conn.cursor()
        parts = []
        params = []
        if child_name is not None:
            parts.append("child_name=?"); params.append(child_name)
        if payment_type is not None:
            parts.append("payment_type=?"); params.append(payment_type)
        if note is not None:
            parts.append("note=?"); params.append(note)
        if not parts:
            return False
        params.append(child_id)
        cur.execute(f"UPDATE children SET {', '.join(parts)} WHERE id=?", params)
        conn.commit()
        logger.info("Оновлено дитину id=%s fields=%s", child_id, parts)
        return True

def delete_child(child_id: int) -> bool:
    with db_lock:
        cur = conn.cursor()
        cur.execute("DELETE FROM children WHERE id=?", (child_id,))
        conn.commit()
        logger.info("Видалено дитину id=%s", child_id)
        return True

def get_child_by_id(child_id: int):
    if child_id is None:
        return None
    with db_lock:
        cur = conn.cursor()
        cur.execute("SELECT id, user_id, child_name, payment_type, note, created_at FROM children WHERE id=?", (child_id,))
        return cur.fetchone()

def delete_user_and_children(user_id: int) -> bool:
    with db_lock:
        cur = conn.cursor()
        cur.execute("SELECT file_path FROM receipts WHERE user_id=?", (user_id,))
        rows = cur.fetchall()
        for (path,) in rows:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except Exception as e:
                logger.warning("Не вдалося видалити файл %s: %s", path, e)
        cur.execute("DELETE FROM receipts WHERE user_id=?", (user_id,))
        cur.execute("DELETE FROM children WHERE user_id=?", (user_id,))
        conn.commit()
        return True

# ========== Временные хранилища состояний ==========
pending_edits = {}   # user_id -> {"action": "editname"/"editnote", "child_id": id}
pending_photos = {}  # user_id -> {"file_id": ..., "message_id": ..., "chat_id": ...}
pending_files = {}   # user_id -> {"file_id": ..., "file_name": ..., "message_id": ..., "chat_id": ...}
pending_links = {}   # user_id -> {"link": ...}

# ========== UI helpers ==========
def start_keyboard() -> types.ReplyKeyboardMarkup:
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    markup.add(types.KeyboardButton("Керувати дітьми"))
    return markup

def build_manage_markup(user_id: int) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(text="➕ Додати дитину", callback_data="manage:add_child"))
    children = list_children(user_id)
    for ch in children:
        cid, name, ptype, note, created = ch
        label = f"{name}" + (f" — {ptype}" if ptype else "")
        markup.add(types.InlineKeyboardButton(text=f"✏️ {label}", callback_data=f"manage:edit:{cid}"))
        markup.add(types.InlineKeyboardButton(text=f"🗑️ Видалити {name}", callback_data=f"manage:delete:{cid}"))
    return markup

# Простая проверка URL: поддерживаем http(s) и www.
# Ищем URL в любом месте строки; не захватываем завершающие запятые/точки/скобки
URL_RE = re.compile(r'(https?://[^\s\)\]\}\,;]+|www\.[^\s\)\]\}\,;]+|\b[^\s]+\.(com|net|org|ua|ru|io|gov|edu|biz|info)\b)', re.IGNORECASE)

# ========== Команды и обработчики ==========
@bot.message_handler(commands=['start', 'help'])
def cmd_start(message: types.Message) -> None:
    text = (
        "Привіт! Я бот для прийому квитанцій🎨.\n\n"
        "Щоб додати квитанцію, напишіть ім'я та прізвище дитини😎.\n"
        "Після цього оберіть тип оплати💵: Позанятійне або Абонемент, і надішліть фото квитанції📷.\n"
        "Також можна надіслати файл📁 (PDF, JPG, DOCX...) бот обробить їх як квитанцію🌸.\n\n"
        "Щоб керувати дітьми (додати/змінити/видалити), натисніть кнопку🔘 «Керувати дітьми».\n"
        "‼️Якщо вже додали дитину знову писати ії ім'я та прізвище не треба, дитина буде збережена в функції «Керувати дітьми» нижче⤵️."
    )
    bot.reply_to(message, text, reply_markup=start_keyboard())

@bot.message_handler(func=lambda m: m.content_type == 'text')
def handle_text(message: types.Message) -> None:
    user_id = int(message.from_user.id)
    text = message.text.strip()
    logger.info("handle_text user=%s text=%r", user_id, text)

    # =========================
    # =========================
    # Простая обработка ссылок: сохраняем весь текст как квитанцію и пересилаем адмінам
    # =========================
    # Если в тексте явно есть ссылка (http, www или домен вида example.com)
    if ("http" in text.lower()) or ("www." in text.lower()) or re.search(r'\.\w{2,4}(\b|/)', text):
        raw_text = text  # сохраняем весь текст как пришёл
        logger.info("Simple link handler triggered for user=%s text=%r", user_id, raw_text)

        # Сохраняем запись в БД (child_id оставляем NULL)
        try:
            receipt_id = save_receipt(user_id, None, None, None, raw_text, None)
        except Exception as e:
            logger.exception("Error saving link-as-text receipt (fallback insert): %s", e)
            with db_lock:
                cur = conn.cursor()
                ts = datetime.now(timezone.utc).isoformat()
                month_str = month_year_uk_from_iso(ts)
                cur.execute(
                    "INSERT INTO receipts (user_id, child_id, file_path, file_id, link, timestamp, month, payment_type, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                    (user_id, None, None, None, raw_text, ts, month_str, None)
                )
                conn.commit()
                receipt_id = cur.lastrowid

        # Формируем подпись для админов — показываем весь текст
        month_year = month_year_uk_from_iso(datetime.now(timezone.utc).isoformat())
        caption = (
            f"Нова квитанція (текст/посилання)\n"
            f"ID: {receipt_id}\n"
            f"user_id: {user_id}\n"
            f"Місяць: {month_year}\n\n"
            f"{raw_text}"
        )

        # Если в тексте есть явная ссылка — добавляем кнопку открытия (берём первую найденную)
        url_match = re.search(
            r'(https?://[^\s\)\]\}\,;]+|www\.[^\s\)\]\}\,;]+|\b[^\s]+\.(com|net|org|ua|ru|io|gov|edu|biz|info)\b)',
            raw_text, re.IGNORECASE)
        markup = types.InlineKeyboardMarkup()
        if url_match:
            found = url_match.group(0).rstrip('.,;:!?) ]}')
            button_url = found if found.startswith("http://") or found.startswith("https://") else ("http://" + found)
            try:
                markup.add(types.InlineKeyboardButton(text="Відкрити посилання", url=button_url))
            except Exception:
                logger.debug("Не удалось добавить кнопку открытия ссылки для: %s", button_url)

        # Кнопки подтверждения/отклонения с реальным id
        markup.add(
            types.InlineKeyboardButton(text="✅ Підтвердити", callback_data=f"confirm:{receipt_id}"),
            types.InlineKeyboardButton(text="❌ Відхилити", callback_data=f"reject:{receipt_id}")
        )

        # Отправляем админам (как текстовое сообщение — ссылка видна в тексте, кнопка открывает её)
        with db_lock:
            cur = conn.cursor()
            cur.execute("SELECT user_id FROM admins")
            admins = [row[0] for row in cur.fetchall()]
        for admin in admins:
            try:
                bot.send_message(chat_id=admin, text=caption, reply_markup=markup)
            except Exception as e:
                logger.warning("Не вдалося повідомити адміна %s: %s", admin, e)

        # Подтверждение пользователю
        bot.reply_to(message, "Посилання/текст отримано і надіслано на перевірку. Дякуємо.")
        return
    # === Конец простого блока ссылок ===

    # === Конец простого блока ссылок ===

    # Завершение редактирования (если есть pending)
    if user_id in pending_edits:
        data = pending_edits.pop(user_id)
        action = data.get("action")
        child_id = data.get("child_id")
        if action == "editname":
            if not (2 <= len(text) <= 100 and re.match(r"^[A-Za-zА-Яа-яЁёЇїІіЄєҐґ\-\s']+$", text)):
                bot.reply_to(message, "Невірний формат імені. Спробуйте ще раз.")
                return
            update_child(child_id, child_name=text)
            bot.reply_to(message, f"Ім'я дитини оновлено: {text}")
            return
        if action == "editnote":
            update_child(child_id, note=text)
            bot.reply_to(message, "Примітка збережена.")
            return

    # Кнопка "Керувати дітьми"
    if text == "Керувати дітьми":
        markup = build_manage_markup(user_id)
        bot.reply_to(message, "Керування дітьми:", reply_markup=markup)
        return

    # Иначе — считаем, что это имя новой дитини
    if not (2 <= len(text) <= 100 and re.match(r"^[A-Za-zА-Яа-яЁёЇїІіЄєҐґ\-\s']+$", text)):
        bot.reply_to(message, "Невірний формат імені. Введіть ім'я та прізвище дитини (тільки літери) або надішліть посилання/файл.")
        return

    # Добавляем ребенка и подтверждаем пользователю
    child_name = text
    try:
        child_id = add_child(user_id, child_name)
    except Exception as e:
        logger.exception("Error adding child: %s", e)
        bot.reply_to(message, "Не вдалося зберегти дитину. Спробуйте ще раз.")
        return

    # Подтверждение и немедленный выбор типа оплаты
    try:
        bot.send_message(user_id, f"Добре — дитину збережено: {child_name}.")
        pay_markup = types.InlineKeyboardMarkup()
        pay_markup.add(
            types.InlineKeyboardButton(text="Позанятійне", callback_data=f"setpay:{child_id}:pozanyat"),
            types.InlineKeyboardButton(text="Абонемент", callback_data=f"setpay:{child_id}:abon")
        )
        bot.send_message(user_id, "Оберіть тип оплати для цієї дитини:", reply_markup=pay_markup)
    except Exception:
        bot.reply_to(message, f"Дитину збережено: {child_name}. Надішліть фото/файл/посилання квитанції.")

    # Отправляем обновленное меню управления, чтобы пользователь видел нового ребенка
    try:
        bot.send_message(user_id, "Оновлений список дітей:", reply_markup=build_manage_markup(user_id))
    except Exception:
        pass

@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("setpay:"))
def handle_setpay(call: types.CallbackQuery) -> None:
    try:
        _, child_id_str, p = call.data.split(":", 2)
        child_id = int(child_id_str)
        payment_type = "Позанятійне" if p == "pozanyat" else "Абонемент"
        update_child(child_id, payment_type=payment_type)
        bot.answer_callback_query(call.id, f"Тип оплати збережено: {payment_type}")
        bot.send_message(call.from_user.id, f"Тип оплати збережено: {payment_type}. Тепер надішліть фото/файл квитанції.")
    except Exception as e:
        logger.exception("handle_setpay error: %s", e)
        try:
            bot.answer_callback_query(call.id, "Сталася помилка.")
        except Exception:
            pass

@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("manage:"))
def handle_manage(call: types.CallbackQuery) -> None:
    try:
        parts = call.data.split(":")
        action = parts[1]
        user_id = call.from_user.id

        if action == "add_child":
            try:
                bot.answer_callback_query(call.id)
            except Exception:
                pass
            bot.send_message(user_id, "Напишіть ім'я та прізвище дитини:")
            return

        if action == "edit":
            child_id = int(parts[2])
            child = get_child_by_id(child_id)
            if not child:
                bot.answer_callback_query(call.id, "Дитину не знайдено.")
                return
            cid, uid, name, ptype, note, created = child
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton(text="Ім'я та прізвище", callback_data=f"editname:{cid}"),
                       types.InlineKeyboardButton(text="Tип оплати", callback_data=f"editpay:{cid}"),
                       types.InlineKeyboardButton(text="Додати примітку", callback_data=f"editnote:{cid}"))
            bot.answer_callback_query(call.id)
            bot.send_message(user_id, f"Дитина: {name}\nТип оплати: {ptype or '-'}\nПримітка: {note or '-'}", reply_markup=markup)
            return

        if action == "delete":
            child_id = int(parts[2])
            child = get_child_by_id(child_id)
            if not child:
                bot.answer_callback_query(call.id, "Дитину не знайдено.")
                return
            delete_child(child_id)
            bot.answer_callback_query(call.id, "Дитину видалено.")
            bot.send_message(user_id, "Дитину видалено. Щоб додати нове ім'я та прізвище, напишіть та відправте його.")
            return

    except Exception as e:
        logger.exception("handle_manage error: %s", e)
        try:
            bot.answer_callback_query(call.id, "Сталася помилка.")
        except Exception:
            pass

@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith(("editname:", "editpay:", "editnote:")))
def handle_edit_prompts(call: types.CallbackQuery) -> None:
    try:
        parts = call.data.split(":")
        action = parts[0]
        child_id = int(parts[1])
        user_id = call.from_user.id
        if action == "editname":
            pending_edits[user_id] = {"action": "editname", "child_id": child_id}
            bot.answer_callback_query(call.id, "Надішліть нове ім'я та прізвище дитини.")
            bot.send_message(user_id, "Введіть нове ім'я та прізвище дитини:")
            return
        if action == "editpay":
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton(text="Позанятійне", callback_data=f"setpay:{child_id}:pozanyat"),
                       types.InlineKeyboardButton(text="Абонемент", callback_data=f"setpay:{child_id}:abon"))
            bot.answer_callback_query(call.id, "Оберіть новий тип оплати.")
            bot.send_message(user_id, "Оберіть новий тип оплати:", reply_markup=markup)
            return
        if action == "editnote":
            pending_edits[user_id] = {"action": "editnote", "child_id": child_id}
            bot.answer_callback_query(call.id, "Надішліть примітку текстом.")
            bot.send_message(user_id, "Введіть примітку для дитини (наприклад, клас або дата народження):")
            return
    except Exception as e:
        logger.exception("handle_edit_prompts error: %s", e)
        try:
            bot.answer_callback_query(call.id, "Сталася помилка.")
        except Exception:
            pass

# ========== Обработчик фото ==========
@bot.message_handler(content_types=['photo'])
def handle_photo(message: types.Message) -> None:
    try:
        user_id = int(message.from_user.id)
        children = list_children(user_id)
        if not children:
            bot.reply_to(message, "Спочатку додайте дитину: надішліть ім'я та прізвище текстом.")
            return

        # Если больше одного ребенка — просим выбрать
        if len(children) > 1:
            photo = message.photo[-1]
            file_id = photo.file_id
            pending_photos[user_id] = {"file_id": file_id, "message_id": message.message_id, "chat_id": message.chat.id}
            markup = types.InlineKeyboardMarkup()
            for ch in children:
                cid, name, ptype, note, created = ch
                label = f"{name}" + (f" — {ptype}" if ptype else "")
                markup.add(types.InlineKeyboardButton(text=label, callback_data=f"choosechild:{cid}"))
            bot.reply_to(message, "Оберіть, за яку дитину ця квитанція:", reply_markup=markup)
            return

        # Если один ребенок — используем его
        child = children[0]
        child_id, name, payment_type, note, created = child
        if not payment_type:
            pay_markup = types.InlineKeyboardMarkup()
            pay_markup.add(types.InlineKeyboardButton(text="Позанятійне", callback_data=f"setpay:{child_id}:pozanyat"),
                           types.InlineKeyboardButton(text="Абонемент", callback_data=f"setpay:{child_id}:abon"))
            bot.reply_to(message, "Для цієї дитини не вказано тип оплати. Оберіть тип оплати:", reply_markup=pay_markup)
            return

        # Сохраняем фото локально и в БД
        photo = message.photo[-1]
        file_id = photo.file_id
        try:
            file_info = bot.get_file(file_id)
            downloaded = bot.download_file(file_info.file_path)
            safe_user = re.sub(r'[^0-9A-Za-z_-]', '_', str(user_id))
            ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            filename = os.path.join(PHOTOS_DIR, f"receipt_{safe_user}_{ts}.jpg")
            with open(filename, 'wb') as f:
                f.write(downloaded)
            receipt_id = save_receipt(user_id, child_id, filename, file_id, None, payment_type)
            sent_via_forward = False
        except Exception as e:
            logger.exception("Error downloading/saving photo (fallback to file_id + forward): %s", e)
            # fallback: save record with file_id and forward original message to admins
            with db_lock:
                cur = conn.cursor()
                ts = datetime.now(timezone.utc).isoformat()
                month_str = month_year_uk_from_iso(ts)
                cur.execute(
                    "INSERT INTO receipts (user_id, child_id, file_path, file_id, link, timestamp, month, payment_type, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                    (user_id, child_id, None, file_id, None, ts, month_str, payment_type)
                )
                conn.commit()
                receipt_id = cur.lastrowid
            with db_lock:
                cur = conn.cursor()
                cur.execute("SELECT user_id FROM admins")
                admins = [row[0] for row in cur.fetchall()]
            for admin in admins:
                try:
                    bot.forward_message(chat_id=admin, from_chat_id=message.chat.id, message_id=message.message_id)
                except Exception:
                    logger.warning("Не вдалося переслати фото адміну %s", admin)
            sent_via_forward = True

        rec = get_receipt_by_id(receipt_id)
        month_year = rec[7] if rec else month_year_uk_from_iso(datetime.now(timezone.utc).isoformat())
        caption = (
            f"Нова квитанція\nID: {receipt_id}\nuser_id: {user_id}\n"
            f"Дитина: {name}\nТип оплати: {payment_type}\nМісяць: {month_year}"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(text="✅ Підтвердити", callback_data=f"confirm:{receipt_id}"),
                   types.InlineKeyboardButton(text="❌ Відхилити", callback_data=f"reject:{receipt_id}"))

        with db_lock:
            cur = conn.cursor()
            cur.execute("SELECT user_id FROM admins")
            admins = [row[0] for row in cur.fetchall()]

        for admin in admins:
            try:
                if not sent_via_forward and os.path.exists(filename):
                    with open(filename, 'rb') as f:
                        bot.send_photo(chat_id=admin, photo=f, caption=caption, reply_markup=markup)
                else:
                    # Если пересылали, отправим подпись отдельно (admins уже получили forward)
                    bot.send_message(admin, caption, reply_markup=markup)
            except Exception:
                try:
                    bot.send_message(admin, caption, reply_markup=markup)
                except Exception:
                    logger.warning("Не вдалося повідомити адміна %s", admin)

        bot.reply_to(message, "Квитанцію отримано і надіслано на перевірку. Можете додати ще квитанцію.")
    except Exception as e:
        logger.exception("handle_photo error: %s", e)
        bot.reply_to(message, "Сталася помилка при обробці квитанції. Спробуйте ще раз пізніше.")

# ========== Обработчик документов ==========
@bot.message_handler(content_types=['document'])
def handle_document(message: types.Message) -> None:
    try:
        user_id = int(message.from_user.id)
        doc = message.document
        if not doc:
            bot.reply_to(message, "Файл не знайдено. Спробуйте ще раз.")
            return
        children = list_children(user_id)
        if not children:
            bot.reply_to(message, "Спочатку додайте дитину: надішліть ім'я та прізвище текстом.")
            return

        file_id = doc.file_id
        file_name = doc.file_name or f"document_{file_id}"
        # Если несколько детей — просим выбрать
        if len(children) > 1:
            # сохраняем контекст сообщения, чтобы при выборе можно было forward
            pending_files[user_id] = {"file_id": file_id, "file_name": file_name, "message_id": message.message_id, "chat_id": message.chat.id}
            markup = types.InlineKeyboardMarkup()
            for ch in children:
                cid, name, ptype, note, created = ch
                label = f"{name}" + (f" — {ptype}" if ptype else "")
                markup.add(types.InlineKeyboardButton(text=label, callback_data=f"choosechild_file:{cid}"))
            bot.reply_to(message, "Оберіть, за яку дитину ця квитанція (файл):", reply_markup=markup)
            return

        # Один ребенок
        child = children[0]
        child_id, name, payment_type, note, created = child
        if not payment_type:
            pay_markup = types.InlineKeyboardMarkup()
            pay_markup.add(types.InlineKeyboardButton(text="Позанятійне", callback_data=f"setpay:{child_id}:pozanyat"),
                           types.InlineKeyboardButton(text="Абонемент", callback_data=f"setpay:{child_id}:abon"))
            bot.reply_to(message, "Для цієї дитини не вказано тип оплати. Оберіть тип оплати:", reply_markup=pay_markup)
            return

        # Скачиваем файл и сохраняем — с fallback: если не удалось скачать/сохранить, сохраняем запись с file_id и forward админам
        sent_via_forward = False
        try:
            file_info = bot.get_file(file_id)
            downloaded = bot.download_file(file_info.file_path)
            safe_user = re.sub(r'[^0-9A-Za-z_-]', '_', str(user_id))
            ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            filename = os.path.join(FILES_DIR, f"{safe_user}_{ts}_{file_name}")
            with open(filename, 'wb') as f:
                f.write(downloaded)
            # Успешно сохранили локально
            receipt_id = save_receipt(user_id, child_id, filename, file_id, None, payment_type)
        except Exception as e:
            logger.exception("Error downloading/saving document (fallback to file_id + forward): %s", e)
            # Сохраняем запись с file_id (без локального файла)
            with db_lock:
                cur = conn.cursor()
                ts = datetime.now(timezone.utc).isoformat()
                month_str = month_year_uk_from_iso(ts)
                cur.execute(
                    "INSERT INTO receipts (user_id, child_id, file_path, file_id, link, timestamp, month, payment_type, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                    (user_id, child_id, None, file_id, None, ts, month_str, payment_type)
                )
                conn.commit()
                receipt_id = cur.lastrowid
            # Пересылаем оригинальное сообщение админам (forward) — чтобы они могли увидеть файл
            with db_lock:
                cur = conn.cursor()
                cur.execute("SELECT user_id FROM admins")
                admins = [row[0] for row in cur.fetchall()]
            for admin in admins:
                try:
                    bot.forward_message(chat_id=admin, from_chat_id=message.chat.id, message_id=message.message_id)
                except Exception as ex:
                    logger.warning("Не вдалося переслати повідомлення адміну %s: %s", admin, ex)
            sent_via_forward = True

        rec = get_receipt_by_id(receipt_id)
        month_year = rec[7] if rec else month_year_uk_from_iso(datetime.now(timezone.utc).isoformat())
        caption = (
            f"Нова квитанція (файл)\nID: {receipt_id}\nuser_id: {user_id}\n"
            f"Дитина: {name}\nТип оплати: {payment_type}\nМісяць: {month_year}\nФайл: {file_name}"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(text="✅ Підтвердити", callback_data=f"confirm:{receipt_id}"),
                   types.InlineKeyboardButton(text="❌ Відхилити", callback_data=f"reject:{receipt_id}"))

        with db_lock:
            cur = conn.cursor()
            cur.execute("SELECT user_id FROM admins")
            admins = [row[0] for row in cur.fetchall()]

        for admin in admins:
            try:
                if not sent_via_forward and os.path.exists(filename):
                    with open(filename, 'rb') as f:
                        bot.send_document(chat_id=admin, document=f, caption=caption, reply_markup=markup)
                else:
                    # Если пересылали, отправим подпись отдельно (admins уже получили forward)
                    bot.send_message(admin, caption, reply_markup=markup)
            except Exception:
                try:
                    bot.send_message(admin, caption, reply_markup=markup)
                except Exception:
                    logger.warning("Не вдалося повідомити адміна %s", admin)

        bot.reply_to(message, "Файл отримано і надіслано на перевірку. Можете додати ще квитанцію.")
    except Exception as e:
        logger.exception("handle_document error: %s", e)
        bot.reply_to(message, "Сталася помилка при обробці файлу. Спробуйте ще раз пізніше.")

# ========== Выбор ребенка после фото/file/link ==========
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("choosechild:"))
def handle_choose_child(call: types.CallbackQuery) -> None:
    try:
        user_id = int(call.from_user.id)
        child_id = int(call.data.split(":", 1)[1])
        if user_id not in pending_photos:
            bot.answer_callback_query(call.id, "Фотографія не знайдена. Надішліть фото ще раз.")
            return
        data = pending_photos.pop(user_id)
        file_id = data.get("file_id")
        chat_id = data.get("chat_id")
        message_id = data.get("message_id")
        # Попытка скачать и сохранить; если не удалось — fallback: сохранить с file_id и forward
        sent_via_forward = False
        try:
            file_info = bot.get_file(file_id)
            downloaded = bot.download_file(file_info.file_path)
            safe_user = re.sub(r'[^0-9A-Za-z_-]', '_', str(user_id))
            ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            filename = os.path.join(PHOTOS_DIR, f"receipt_{safe_user}_{ts}.jpg")
            with open(filename, 'wb') as f:
                f.write(downloaded)
            # Сохраняем
            receipt_id = save_receipt(user_id, child_id, filename, file_id, None, None)
        except Exception as e:
            logger.exception("Error downloading chosen photo (fallback): %s", e)
            with db_lock:
                cur = conn.cursor()
                ts = datetime.now(timezone.utc).isoformat()
                month_str = month_year_uk_from_iso(ts)
                cur.execute(
                    "INSERT INTO receipts (user_id, child_id, file_path, file_id, link, timestamp, month, payment_type, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                    (user_id, child_id, None, file_id, None, ts, month_str, None)
                )
                conn.commit()
                receipt_id = cur.lastrowid
            # forward original
            try:
                with db_lock:
                    cur = conn.cursor()
                    cur.execute("SELECT user_id FROM admins")
                    admins = [row[0] for row in cur.fetchall()]
                for admin in admins:
                    bot.forward_message(chat_id=admin, from_chat_id=chat_id, message_id=message_id)
            except Exception:
                logger.warning("Не вдалося переслати фото адміну")
            sent_via_forward = True

        child = get_child_by_id(child_id)
        if not child:
            bot.answer_callback_query(call.id, "Дитину не знайдено.")
            return
        cid, uid, name, payment_type, note, created = child
        if not payment_type:
            bot.answer_callback_query(call.id, "Для цієї дитини не вказано тип оплати. Оновіть його, будь ласка.")
            bot.send_message(user_id, "Оберіть тип оплати для дитини:", reply_markup=types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton(text="Позанятійне", callback_data=f"setpay:{cid}:pozanyat"),
                types.InlineKeyboardButton(text="Абонемент", callback_data=f"setpay:{cid}:abon")
            ))
            return

        rec = get_receipt_by_id(receipt_id)
        month_year = rec[7] if rec else month_year_uk_from_iso(datetime.now(timezone.utc).isoformat())
        caption = (
            f"Нова квитанція\nID: {receipt_id}\nuser_id: {user_id}\n"
            f"Дитина: {name}\nТип оплати: {payment_type or '-'}\nМісяць: {month_year}"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(text="✅ Підтвердити", callback_data=f"confirm:{receipt_id}"),
                   types.InlineKeyboardButton(text="❌ Відхилити", callback_data=f"reject:{receipt_id}"))
        with db_lock:
            cur = conn.cursor()
            cur.execute("SELECT user_id FROM admins")
            admins = [row[0] for row in cur.fetchall()]
        for admin in admins:
            try:
                if not sent_via_forward and os.path.exists(filename):
                    with open(filename, 'rb') as f:
                        bot.send_photo(chat_id=admin, photo=f, caption=caption, reply_markup=markup)
                else:
                    bot.send_message(admin, caption, reply_markup=markup)
            except Exception:
                try:
                    bot.send_message(admin, caption, reply_markup=markup)
                except Exception:
                    logger.warning("Не вдалося повідомити адміна %s", admin)
        bot.answer_callback_query(call.id, "Квитанцію надіслано на перевірку.")
        bot.send_message(user_id, "Квитанцію отримано і надіслано на перевірку. Можете додати ще квитанцію.")
    except Exception as e:
        logger.exception("handle_choose_child error: %s", e)
        try:
            bot.answer_callback_query(call.id, "Сталася помилка.")
        except Exception:
            pass

@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("choosechild_file:"))
def handle_choose_child_file(call: types.CallbackQuery) -> None:
    try:
        user_id = int(call.from_user.id)
        child_id = int(call.data.split(":", 1)[1])
        if user_id not in pending_files:
            bot.answer_callback_query(call.id, "Файл не знайдено. Надішліть файл ще раз.")
            return
        data = pending_files.pop(user_id)
        file_id = data.get("file_id")
        file_name = data.get("file_name")
        chat_id = data.get("chat_id")
        message_id = data.get("message_id")

        child = get_child_by_id(child_id)
        if not child:
            bot.answer_callback_query(call.id, "Дитину не знайдено.")
            return
        cid, uid, name, payment_type, note, created = child
        if not payment_type:
            bot.answer_callback_query(call.id, "Для цієї дитини не вказано тип оплати. Оновіть його, будь ласка.")
            bot.send_message(user_id, "Оберіть тип оплати для дитини:", reply_markup=types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton(text="Позанятійне", callback_data=f"setpay:{cid}:pozanyat"),
                types.InlineKeyboardButton(text="Абонемент", callback_data=f"setpay:{cid}:abon")
            ))
            return

        # Попытка скачать и сохранить; если не удалось — fallback: сохранить с file_id и forward
        sent_via_forward = False
        try:
            file_info = bot.get_file(file_id)
            downloaded = bot.download_file(file_info.file_path)
            safe_user = re.sub(r'[^0-9A-Za-z_-]', '_', str(user_id))
            ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            filename = os.path.join(FILES_DIR, f"{safe_user}_{ts}_{file_name}")
            with open(filename, 'wb') as f:
                f.write(downloaded)
            receipt_id = save_receipt(user_id, cid, filename, file_id, None, payment_type)
        except Exception as e:
            logger.exception("Error downloading chosen file (fallback): %s", e)
            with db_lock:
                cur = conn.cursor()
                ts = datetime.now(timezone.utc).isoformat()
                month_str = month_year_uk_from_iso(ts)
                cur.execute(
                    "INSERT INTO receipts (user_id, child_id, file_path, file_id, link, timestamp, month, payment_type, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                    (user_id, cid, None, file_id, None, ts, month_str, payment_type)
                )
                conn.commit()
                receipt_id = cur.lastrowid
            # forward original
            try:
                with db_lock:
                    cur = conn.cursor()
                    cur.execute("SELECT user_id FROM admins")
                    admins = [row[0] for row in cur.fetchall()]
                for admin in admins:
                    bot.forward_message(chat_id=admin, from_chat_id=chat_id, message_id=message_id)
            except Exception:
                logger.warning("Не вдалося переслати файл адміну")
            sent_via_forward = True

        rec = get_receipt_by_id(receipt_id)
        month_year = rec[7] if rec else month_year_uk_from_iso(datetime.now(timezone.utc).isoformat())
        caption = (
            f"Нова квитанція (файл)\nID: {receipt_id}\nuser_id: {user_id}\n"
            f"Дитина: {name}\nТип оплати: {payment_type}\nМісяць: {month_year}\nФайл: {file_name}"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(text="✅ Підтвердити", callback_data=f"confirm:{receipt_id}"),
                   types.InlineKeyboardButton(text="❌ Відхилити", callback_data=f"reject:{receipt_id}"))
        with db_lock:
            cur = conn.cursor()
            cur.execute("SELECT user_id FROM admins")
            admins = [row[0] for row in cur.fetchall()]
        for admin in admins:
            try:
                if not sent_via_forward and os.path.exists(filename):
                    with open(filename, 'rb') as f:
                        bot.send_document(chat_id=admin, document=f, caption=caption, reply_markup=markup)
                else:
                    bot.send_message(admin, caption, reply_markup=markup)
            except Exception:
                try:
                    bot.send_message(admin, caption, reply_markup=markup)
                except Exception:
                    logger.warning("Не вдалося повідомити адміна %s", admin)
        bot.answer_callback_query(call.id, "Квитанцію надіслано на перевірку.")
        bot.send_message(user_id, "Квитанцію отримано і надіслано на перевірку. Можете додати ще квитанцію.")
    except Exception as e:
        logger.exception("handle_choose_child_file error: %s", e)
        try:
            bot.answer_callback_query(call.id, "Сталася помилка.")
        except Exception:
            pass

# ========== Выбор ребенка для ссылок (восстановленный) ==========
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("choosechild_link:"))
def handle_choose_child_link(call: types.CallbackQuery) -> None:
    try:
        user_id = int(call.from_user.id)
        parts = call.data.split(":", 1)
        if len(parts) < 2:
            bot.answer_callback_query(call.id, "Некоректні дані.")
            return
        child_id = int(parts[1])

        logger.debug("handle_choose_child_link called: user=%s child_id=%s pending_keys=%r", user_id, child_id, list(pending_links.keys()))

        if user_id not in pending_links:
            bot.answer_callback_query(call.id, "Посилання не знайдено. Надішліть посилання ще раз.", show_alert=True)
            return

        data = pending_links.pop(user_id)
        raw_text = data.get("link")
        if not raw_text:
            bot.answer_callback_query(call.id, "Посилання порожнє. Надішліть ще раз.")
            return

        # Найдём первую ссылку для кнопки (не меняем raw_text в БД)
        url_match = re.search(r'(https?://[^\s\)\]\}\,;]+|www\.[^\s\)\]\}\,;]+|\b[^\s]+\.(com|net|org|ua|ru|io|gov|edu|biz|info)\b)', raw_text, re.IGNORECASE)
        found = url_match.group(0).rstrip('.,;:!?) ]}') if url_match else None
        button_url = found if found and (found.startswith("http://") or found.startswith("https://")) else (("http://" + found) if found else None)

        child = get_child_by_id(child_id)
        if not child:
            bot.answer_callback_query(call.id, "Дитину не знайдено.")
            return
        cid, uid, name, payment_type, note, created = child
        if not payment_type:
            bot.answer_callback_query(call.id, "Для цієї дитини не вказано тип оплати. Оновіть його, будь ласка.")
            bot.send_message(user_id, "Оберіть тип оплати для дитини:", reply_markup=types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton(text="Позанятійне", callback_data=f"setpay:{cid}:pozanyat"),
                types.InlineKeyboardButton(text="Абонемент", callback_data=f"setpay:{cid}:abon")
            ))
            return

        # Сохраняем запись; при ошибке делаем fallback-вставку
        try:
            receipt_id = save_receipt(user_id, cid, None, None, raw_text, payment_type)
        except Exception as e:
            logger.exception("Error saving link from choosechild_link (will insert fallback): %s", e)
            with db_lock:
                cur = conn.cursor()
                ts = datetime.now(timezone.utc).isoformat()
                month_str = month_year_uk_from_iso(ts)
                cur.execute(
                    "INSERT INTO receipts (user_id, child_id, file_path, file_id, link, timestamp, month, payment_type, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                    (user_id, cid, None, None, raw_text, ts, month_str, payment_type)
                )
                conn.commit()
                receipt_id = cur.lastrowid

        month_year = month_year_uk_from_iso(datetime.now(timezone.utc).isoformat())
        caption = (
            f"Нова квитанція (посилання)\n"
            f"ID: {receipt_id}\n"
            f"user_id: {user_id}\n"
            f"Дитина: {name}\nТип оплати: {payment_type}\nМісяць: {month_year}\nLink: {found or raw_text}"
        )

        markup = types.InlineKeyboardMarkup()
        if button_url:
            try:
                markup.add(types.InlineKeyboardButton(text="Відкрити посилання", url=button_url))
            except Exception:
                logger.debug("Не удалось добавить кнопку открытия ссылки для: %s", button_url)
        markup.add(types.InlineKeyboardButton(text="✅ Підтвердити", callback_data=f"confirm:{receipt_id}"),
                   types.InlineKeyboardButton(text="❌ Відхилити", callback_data=f"reject:{receipt_id}"))

        with db_lock:
            cur = conn.cursor()
            cur.execute("SELECT user_id FROM admins")
            admins = [row[0] for row in cur.fetchall()]

        for admin in admins:
            try:
                bot.send_message(chat_id=admin, text=caption, reply_markup=markup)
            except Exception as e:
                logger.warning("Не вдалося повідомити адміна %s: %s", admin, e)

        bot.answer_callback_query(call.id, "Квитанцію надіслано на перевірку.")
        bot.send_message(user_id, "Квитанцію отримано і надіслано на перевірку. Можете додати ще квитанцію.")
    except Exception as e:
        logger.exception("handle_choose_child_link error: %s", e)
        try:
            bot.answer_callback_query(call.id, "Сталася помилка при обробці. Ми вже отримали лог і перевіримо.", show_alert=True)
        except Exception:
            pass
        return

# ========== Подтверждение/отклонение админом ==========
@bot.callback_query_handler(func=lambda call: call.data and (call.data.startswith("confirm:") or call.data.startswith("reject:")))
def handle_confirm_reject(call: types.CallbackQuery) -> None:
    try:
        action, rid_str = call.data.split(":", 1)
        try:
            rid = int(rid_str)
        except ValueError:
            bot.answer_callback_query(call.id, "Некоректний ID.")
            return

        # Защита от 0
        if rid == 0:
            bot.answer_callback_query(call.id, "Некоректний ID квитанції.", show_alert=True)
            return

        if not is_admin(call.from_user.id):
            bot.answer_callback_query(call.id, "У вас немає прав для цієї дії.")
            return

        rec = get_receipt_by_id(rid)
        if not rec:
            bot.answer_callback_query(call.id, "Квитанцію не знайдено.")
            return

        # rec: id, user_id, child_id, file_path, file_id, link, timestamp, month, payment_type, status, admin_id, admin_comment
        _id, user_id, child_id, path, fid, link, ts, month, payment_type, status, admin_id, admin_comment = rec

        # Попытка получить данные ребенка
        child_rec = get_child_by_id(child_id) if child_id else None
        child_name = child_rec[2] if child_rec else None
        # Если payment_type пустой в квитанции, берем из записи ребенка
        if not payment_type and child_rec and child_rec[3]:
            payment_type = child_rec[3]

        payment_label = payment_type or "не вказано"

        new_status = "confirmed" if action == "confirm" else "rejected"
        update_receipt_status(rid, new_status, call.from_user.id, admin_comment)

        # Формируем сообщение пользователю с именем ребенка и типом оплаты
        try:
            if new_status == "confirmed":
                msg_parts = [f"Оплата зарахована за {month}."]
                if child_name:
                    msg_parts.append(f"{child_name} — {payment_label}.")
                else:
                    msg_parts.append(f"Тип оплати: {payment_label}.")
                msg = " ".join(msg_parts)
                bot.send_message(user_id, f"{msg} (ID квитанції: {rid})")
            else:
                if child_name:
                    bot.send_message(user_id, f"Квитанцію за {month} для {child_name} ({payment_label}) відхилено. (ID квитанції: {rid})")
                else:
                    bot.send_message(user_id, f"Квитанцію за {month} відхилено. Тип оплати: {payment_label}. (ID квитанції: {rid})")
        except Exception:
            logger.warning("Не вдалося повідомити користувача %s про зміну статусу квитанції %s", user_id, rid)

        # Обновляем подпись/текст сообщения админу
        ts_now = datetime.now(timezone.utc).isoformat()
        status_label = "Прийнято" if new_status == "confirmed" else "Відхилено"
        new_caption = f"ID: {rid}\nuser_id: {user_id}\nДитина: {child_name or '-'}\nМісяць: {month}\nТип оплати: {payment_label}\nСтатус: {status_label}\nЧас (UTC): {ts_now}"
        try:
            if call.message:
                # Если сообщение содержит media (photo/document) и у него есть caption — редактируем caption
                if (getattr(call.message, "photo", None) or getattr(call.message, "document", None) or getattr(call.message, "video", None)) and getattr(call.message, "caption", None) is not None:
                    bot.edit_message_caption(chat_id=call.message.chat.id, message_id=call.message.message_id, caption=new_caption)
                else:
                    # Иначе редактируем текст
                    bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=new_caption)
        except Exception as e:
            logger.warning("Не вдалося оновити повідомлення адміну: %s", e)

        # Ответ админу в виде всплывашки: теперь конкретно "Прийнято" или "Відхилено"
        answer_text = "Прийнято" if new_status == "confirmed" else "Відхилено"
        bot.answer_callback_query(call.id, answer_text)
    except Exception as e:
        logger.exception("handle_confirm_reject error: %s", e)
        try:
            bot.answer_callback_query(call.id, "Сталася помилка при обробці дії.", show_alert=True)
        except Exception:
            pass

# ========== Админские команды ==========
@bot.message_handler(commands=['receipts'])
def cmd_receipts(message: types.Message) -> None:
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "У вас немає прав для цієї команди.")
        return
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, user_id, child_id, file_path, file_id, link, timestamp, month, payment_type, status FROM receipts ORDER BY id DESC LIMIT ?",
            (MAX_RETURN,)
        )
        rows = cur.fetchall()
    if not rows:
        bot.reply_to(message, "Поки немає квитанцій.")
        return
    for row in rows:
        rid, user_id, child_id, file_path, file_id, link, ts, month, payment_type, status = row
        child = get_child_by_id(child_id) if child_id else None
        child_name = child[2] if child else "-"
        caption = f"ID: {rid}\nuser_id: {user_id}\nДитина: {child_name}\nМісяць: {month}\nТип оплати: {payment_type}\nСтатус: {status}"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(text="✅ Підтвердити", callback_data=f"confirm:{rid}"),
                   types.InlineKeyboardButton(text="❌ Відхилити", callback_data=f"reject:{rid}"))
        if file_path and os.path.exists(file_path):
            try:
                with open(file_path, 'rb') as f:
                    bot.send_document(chat_id=message.chat.id, document=f, caption=caption, reply_markup=markup)
            except Exception:
                try:
                    bot.send_message(message.chat.id, caption, reply_markup=markup)
                except Exception:
                    pass
        elif link:
            try:
                # Отправляем текст с ссылкой; Telegram сделает ссылку кликабельной
                bot.send_message(message.chat.id, caption + f"\nLink: {link}", reply_markup=markup)
            except Exception:
                bot.send_message(message.chat.id, caption, reply_markup=markup)
        else:
            try:
                if file_id:
                    try:
                        file_info = bot.get_file(file_id)
                        downloaded = bot.download_file(file_info.file_path)
                        bot.send_photo(chat_id=message.chat.id, photo=downloaded, caption=caption, reply_markup=markup)
                    except Exception:
                        bot.send_message(message.chat.id, caption, reply_markup=markup)
                else:
                    bot.send_message(message.chat.id, caption, reply_markup=markup)
            except Exception:
                bot.send_message(message.chat.id, caption, reply_markup=markup)

@bot.message_handler(commands=['deleteuser'])
def cmd_deleteuser(message: types.Message) -> None:
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "У вас немає прав для цієї команди.")
        return
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "Вкажіть user_id: /deleteuser <user_id>")
        return
    try:
        uid = int(args[1])
    except ValueError:
        bot.reply_to(message, "Некоректний user_id.")
        return
    delete_user_and_children(uid)
    bot.reply_to(message, f"Дані користувача {uid} видалено.")

# ========== Запуск ==========
if __name__ == "__main__":
    logger.info("Bot started")
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.exception("Bot stopped with error: %s", e)
    finally:
        try:
            conn.close()
        except Exception:
            pass

