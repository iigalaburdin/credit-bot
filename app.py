import os
import json
import calendar
import requests
from datetime import datetime, date, timedelta
from flask import Flask, request, jsonify

import turso_db

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН_ОТ_BOTFATHER")
REMINDER_SECRET = os.environ.get("REMINDER_SECRET", "PRIDUMAI_SVOI_SEKRET_123")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
MONTHS_RU = [
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]

app = Flask(__name__)

# ================== БАЗА ДАННЫХ (Turso) ==================

def init_db():
    turso_db.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            title TEXT,
            amount REAL NOT NULL,
            due_date TEXT NOT NULL,
            paid INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    turso_db.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_state (
            chat_id INTEGER PRIMARY KEY,
            step TEXT,
            data TEXT
        )
        """
    )


_DB_READY = False


def ensure_db():
    global _DB_READY
    if not _DB_READY:
        init_db()
        _DB_READY = True


def set_state(chat_id, step, data=None):
    turso_db.execute("DELETE FROM chat_state WHERE chat_id = ?", [chat_id])
    turso_db.execute(
        "INSERT INTO chat_state (chat_id, step, data) VALUES (?, ?, ?)",
        [chat_id, step, json.dumps(data or {})],
    )


def get_state(chat_id):
    rows = turso_db.execute("SELECT step, data FROM chat_state WHERE chat_id = ?", [chat_id])
    if not rows:
        return None, {}
    step, data = rows[0]
    return step, json.loads(data or "{}")


def clear_state(chat_id):
    turso_db.execute("DELETE FROM chat_state WHERE chat_id = ?", [chat_id])


def add_payment(chat_id, title, amount, due_date):
    turso_db.execute(
        "INSERT INTO payments (chat_id, title, amount, due_date, paid) VALUES (?, ?, ?, ?, 0)",
        [chat_id, title, amount, due_date],
    )


def get_unpaid(chat_id):
    return turso_db.execute(
        "SELECT id, title, amount, due_date FROM payments WHERE chat_id=? AND paid=0 ORDER BY due_date ASC",
        [chat_id],
    )


def get_all_unpaid():
    return turso_db.execute(
        "SELECT id, chat_id, title, amount, due_date FROM payments WHERE paid=0"
    )


def mark_paid(payment_id):
    turso_db.execute("UPDATE payments SET paid=1 WHERE id=?", [payment_id])


def delete_payment(payment_id):
    turso_db.execute("DELETE FROM payments WHERE id=?", [payment_id])


# ================== TELEGRAM API ==================

def send_message(chat_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "text": text}
    if keyboard:
        payload["reply_markup"] = json.dumps(keyboard)
    requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=10)


def edit_message(chat_id, message_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if keyboard:
        payload["reply_markup"] = json.dumps(keyboard)
    requests.post(f"{TELEGRAM_API}/editMessageText", json=payload, timeout=10)


def answer_callback(callback_id, text=None):
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    requests.post(f"{TELEGRAM_API}/answerCallbackQuery", json=payload, timeout=10)


def set_bot_commands():
    commands = [
        {"command": "add", "description": "Добавить платёж"},
        {"command": "list", "description": "Список платежей"},
        {"command": "cancel", "description": "Отменить текущее действие"},
    ]
    requests.post(f"{TELEGRAM_API}/setMyCommands", json={"commands": commands}, timeout=10)


# ================== ГЛАВНОЕ МЕНЮ (кнопки) ==================

def main_menu_keyboard():
    return {
        "keyboard": [
            [{"text": "➕ Добавить платёж"}, {"text": "📋 Список платежей"}],
        ],
        "resize_keyboard": True,
    }


def build_list_text_and_keyboard(chat_id):
    rows = get_unpaid(chat_id)
    if not rows:
        return "Список пуст — все платежи оплачены 🎉", None

    lines = ["Твои платежи:\n"]
    buttons = []
    for payment_id, title, amount, due_date in rows:
        d = datetime.strptime(due_date, "%Y-%m-%d").date().strftime("%d.%m.%Y")
        label = f"{title}: " if title else ""
        lines.append(f"• {label}{amount:.2f} руб. — до {d}")
        buttons.append([
            {"text": f"✅ {label}{amount:.2f} до {d}", "callback_data": f"paid:{payment_id}"},
            {"text": "🗑", "callback_data": f"del:{payment_id}"},
        ])

    return "\n".join(lines), {"inline_keyboard": buttons}


# ================== INLINE-КАЛЕНДАРЬ ==================

def build_calendar(year, month):
    """Строит inline-клавиатуру календаря на месяц."""
    buttons = []

    buttons.append([{"text": f"{MONTHS_RU[month]} {year}", "callback_data": "cal:ignore"}])

    week_days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    buttons.append([{"text": d, "callback_data": "cal:ignore"} for d in week_days])

    month_calendar = calendar.monthcalendar(year, month)
    for week in month_calendar:
        row = []
        for day in week:
            if day == 0:
                row.append({"text": " ", "callback_data": "cal:ignore"})
            else:
                row.append({"text": str(day), "callback_data": f"cal:pick:{year}-{month:02d}-{day:02d}"})
        buttons.append(row)

    prev_month = month - 1
    prev_year = year
    if prev_month == 0:
        prev_month = 12
        prev_year -= 1

    next_month = month + 1
    next_year = year
    if next_month == 13:
        next_month = 1
        next_year += 1

    buttons.append([
        {"text": "« Пред.", "callback_data": f"cal:nav:{prev_year}-{prev_month:02d}"},
        {"text": "След. »", "callback_data": f"cal:nav:{next_year}-{next_month:02d}"},
    ])

    return {"inline_keyboard": buttons}


# ================== ЛОГИКА ДИАЛОГА ==================

def start_add_flow(chat_id):
    set_state(chat_id, "ask_bank_amount")
    send_message(
        chat_id,
        "Введи банк и сумму одной строкой, например:\nТинькофф 5000\n\n"
        "Если название не важно — просто отправь сумму: 5000",
    )


def parse_bank_amount(text):
    """Разбирает строку вида 'Тинькофф 5000' или '5000' на (название, сумма).
    Возвращает (title, amount) или (None, None), если сумму распознать не удалось."""
    parts = text.strip().split()
    if not parts:
        return None, None

    last = parts[-1].replace(",", ".")
    try:
        amount = float(last)
    except ValueError:
        return None, None

    title = " ".join(parts[:-1]).strip()
    return title, amount


def handle_text(chat_id, text):
    text = text.strip()

    if text == "/start":
        clear_state(chat_id)
        set_bot_commands()
        send_message(
            chat_id,
            "Привет! Я помогу не забывать про платежи по кредитке.\n\n"
            "Жми кнопки внизу экрана или используй команды /add и /list.\n"
            "За день до платежа и в день платежа я сам пришлю напоминание.",
            keyboard=main_menu_keyboard(),
        )
        return

    if text in ("/add", "➕ Добавить платёж"):
        start_add_flow(chat_id)
        return

    if text in ("/list", "📋 Список платежей"):
        clear_state(chat_id)
        list_text, keyboard = build_list_text_and_keyboard(chat_id)
        send_message(chat_id, list_text, keyboard)
        return

    if text == "/cancel":
        clear_state(chat_id)
        send_message(chat_id, "Отменил.", keyboard=main_menu_keyboard())
        return

    step, data = get_state(chat_id)

    if step == "ask_bank_amount":
        title, amount = parse_bank_amount(text)
        if amount is None:
            send_message(
                chat_id,
                "Не разобрал сумму. Напиши так: Тинькофф 5000 (или просто 5000, если без названия).",
            )
            return
        data["title"] = title
        data["amount"] = amount
        set_state(chat_id, "ask_date", data)
        today = date.today()
        send_message(chat_id, "Выбери дату платежа:", keyboard=build_calendar(today.year, today.month))
        return

    # На шаге даты пользователь должен нажимать календарь, а не писать текст
    if step == "ask_date":
        send_message(chat_id, "Выбери дату кнопками в календаре выше 👆")
        return

    send_message(chat_id, "Не понял. Используй кнопки внизу или команды /add, /list", keyboard=main_menu_keyboard())


def finish_add_with_date(chat_id, message_id, due_date_str):
    step, data = get_state(chat_id)
    if step != "ask_date":
        return

    due = datetime.strptime(due_date_str, "%Y-%m-%d").date()
    title = data.get("title", "")
    amount = data["amount"]
    add_payment(chat_id, title, amount, due.isoformat())
    clear_state(chat_id)

    label = f"{title} — " if title else ""
    edit_message(chat_id, message_id, f"Готово ✅\nЗаписал: {label}{amount:.2f} руб. до {due.strftime('%d.%m.%Y')}")


# ================== МАРШРУТЫ FLASK ==================

@app.route("/webhook", methods=["POST"])
def webhook():
    ensure_db()
    update = request.get_json(force=True, silent=True) or {}

    if "message" in update:
        chat_id = update["message"]["chat"]["id"]
        text = update["message"].get("text", "")
        if text:
            handle_text(chat_id, text)

    elif "callback_query" in update:
        cq = update["callback_query"]
        chat_id = cq["message"]["chat"]["id"]
        message_id = cq["message"]["message_id"]
        data = cq.get("data", "")

        if data.startswith("paid:"):
            answer_callback(cq["id"])
            payment_id = int(data.split(":")[1])
            mark_paid(payment_id)
            list_text, keyboard = build_list_text_and_keyboard(chat_id)
            edit_message(chat_id, message_id, list_text, keyboard)

        elif data.startswith("del:"):
            answer_callback(cq["id"], "Удалено")
            payment_id = int(data.split(":")[1])
            delete_payment(payment_id)
            list_text, keyboard = build_list_text_and_keyboard(chat_id)
            edit_message(chat_id, message_id, list_text, keyboard)

        elif data == "cal:ignore":
            answer_callback(cq["id"])

        elif data.startswith("cal:nav:"):
            answer_callback(cq["id"])
            year_month = data.split(":")[2]
            year, month = map(int, year_month.split("-"))
            edit_message(chat_id, message_id, "Выбери дату платежа:", keyboard=build_calendar(year, month))

        elif data.startswith("cal:pick:"):
            answer_callback(cq["id"])
            picked_date = data.split(":")[2]
            finish_add_with_date(chat_id, message_id, picked_date)

    return jsonify({"ok": True})


@app.route("/check_reminders")
def check_reminders():
    ensure_db()
    secret = request.args.get("secret", "")
    if secret != REMINDER_SECRET:
        return "forbidden", 403

    today = date.today()
    tomorrow = today + timedelta(days=1)
    sent = 0
    for payment_id, chat_id, title, amount, due_date in get_all_unpaid():
        due = datetime.strptime(due_date, "%Y-%m-%d").date()
        label = f"{title}: " if title else ""
        if due == today:
            text = f"⚠️ Сегодня срок платежа!\n{label}{amount:.2f} руб."
        elif due == tomorrow:
            text = f"🔔 Завтра срок платежа.\n{label}{amount:.2f} руб."
        else:
            continue
        send_message(chat_id, text)
        sent += 1

    return jsonify({"ok": True, "reminders_sent": sent})


@app.route("/")
def index():
    ensure_db()
    return "Бот работает."


if __name__ == "__main__":
    app.run(debug=True)
