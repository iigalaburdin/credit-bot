import os
import json
import requests
from datetime import datetime, date, timedelta
from flask import Flask, request, jsonify

import turso_db

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН_ОТ_BOTFATHER")
REMINDER_SECRET = os.environ.get("REMINDER_SECRET", "PRIDUMAI_SVOI_SEKRET_123")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

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


def answer_callback(callback_id):
    requests.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": callback_id}, timeout=10)


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
        buttons.append([{"text": f"✅ Оплачено: {label}{amount:.2f} до {d}", "callback_data": f"paid:{payment_id}"}])

    return "\n".join(lines), {"inline_keyboard": buttons}


# ================== ЛОГИКА ДИАЛОГА ==================

def handle_text(chat_id, text):
    text = text.strip()

    if text == "/start":
        clear_state(chat_id)
        send_message(
            chat_id,
            "Привет! Я помогу не забывать про платежи по кредитке.\n\n"
            "Команды:\n"
            "/add — добавить платёж\n"
            "/list — показать список неоплаченных платежей\n\n"
            "За день до платежа и в день платежа я сам пришлю напоминание.",
        )
        return

    if text == "/add":
        set_state(chat_id, "ask_title")
        send_message(chat_id, "Как назвать платёж? (например «Тинькофф»). Если не важно — отправь «-».")
        return

    if text == "/list":
        clear_state(chat_id)
        list_text, keyboard = build_list_text_and_keyboard(chat_id)
        send_message(chat_id, list_text, keyboard)
        return

    if text == "/cancel":
        clear_state(chat_id)
        send_message(chat_id, "Отменил.")
        return

    step, data = get_state(chat_id)

    if step == "ask_title":
        data["title"] = "" if text == "-" else text
        set_state(chat_id, "ask_amount", data)
        send_message(chat_id, "Введи сумму платежа (например: 5000):")
        return

    if step == "ask_amount":
        try:
            amount = float(text.replace(",", "."))
        except ValueError:
            send_message(chat_id, "Не похоже на число. Введи сумму ещё раз (например: 5000):")
            return
        data["amount"] = amount
        set_state(chat_id, "ask_date", data)
        send_message(chat_id, "Введи дату платежа в формате ДД.ММ.ГГГГ (например: 25.08.2026):")
        return

    if step == "ask_date":
        try:
            due = datetime.strptime(text, "%d.%m.%Y").date()
        except ValueError:
            send_message(chat_id, "Не похоже на дату. Формат: ДД.ММ.ГГГГ (например: 25.08.2026):")
            return
        title = data.get("title", "")
        amount = data["amount"]
        add_payment(chat_id, title, amount, due.isoformat())
        clear_state(chat_id)
        label = f"{title} — " if title else ""
        send_message(chat_id, f"Готово ✅\nЗаписал: {label}{amount:.2f} руб. до {due.strftime('%d.%m.%Y')}")
        return

    send_message(chat_id, "Не понял. Доступные команды: /add, /list")


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
        answer_callback(cq["id"])
        chat_id = cq["message"]["chat"]["id"]
        message_id = cq["message"]["message_id"]
        data = cq.get("data", "")
        if data.startswith("paid:"):
            payment_id = int(data.split(":")[1])
            mark_paid(payment_id)
            list_text, keyboard = build_list_text_and_keyboard(chat_id)
            edit_message(chat_id, message_id, list_text, keyboard)

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
