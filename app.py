import os
import re
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
            paid INTEGER NOT NULL DEFAULT 0,
            series_id INTEGER,
            series_index INTEGER,
            series_total INTEGER
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
    for ddl in (
        "ALTER TABLE payments ADD COLUMN series_id INTEGER",
        "ALTER TABLE payments ADD COLUMN series_index INTEGER",
        "ALTER TABLE payments ADD COLUMN series_total INTEGER",
    ):
        try:
            turso_db.execute(ddl)
        except Exception:
            pass


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


def add_payment(chat_id, title, amount, due_date, series_id=None, series_index=None, series_total=None):
    rows = turso_db.execute(
        "INSERT INTO payments (chat_id, title, amount, due_date, paid, series_id, series_index, series_total) "
        "VALUES (?, ?, ?, ?, 0, ?, ?, ?) RETURNING id",
        [chat_id, title, amount, due_date, series_id, series_index, series_total],
    )
    return rows[0][0] if rows else None


def add_months(d: date, months: int) -> date:
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def add_series(chat_id, title, amount, start_date: date, count: int):
    first_id = add_payment(chat_id, title, amount, start_date.isoformat(), series_total=count, series_index=1)
    turso_db.execute("UPDATE payments SET series_id=? WHERE id=?", [first_id, first_id])
    for i in range(1, count):
        next_date = add_months(start_date, i)
        add_payment(chat_id, title, amount, next_date.isoformat(), series_id=first_id, series_index=i + 1, series_total=count)
    return first_id


PAYMENT_FIELDS = "id, title, amount, due_date, series_id, series_index, series_total"


def get_unpaid(chat_id):
    return turso_db.execute(
        f"SELECT {PAYMENT_FIELDS} FROM payments WHERE chat_id=? AND paid=0 ORDER BY due_date ASC",
        [chat_id],
    )


def get_nearest_unpaid_per_series(chat_id):
    """Список неоплаченных платежей, но по каждой серии — только ближайший."""
    rows = get_unpaid(chat_id)
    seen = set()
    result = []
    for row in rows:
        payment_id, title, amount, due_date, series_id, series_index, series_total = row
        group_key = series_id if series_id else f"single-{payment_id}"
        if group_key in seen:
            continue
        seen.add(group_key)
        result.append(row)
    return result


def get_payment(payment_id):
    rows = turso_db.execute(f"SELECT {PAYMENT_FIELDS} FROM payments WHERE id=?", [payment_id])
    return rows[0] if rows else None


def get_next_unpaid_in_series(series_id):
    rows = turso_db.execute(
        f"SELECT {PAYMENT_FIELDS} FROM payments WHERE series_id=? AND paid=0 ORDER BY due_date ASC LIMIT 1",
        [series_id],
    )
    return rows[0] if rows else None


def get_all_unpaid():
    return turso_db.execute(
        "SELECT id, chat_id, title, amount, due_date FROM payments WHERE paid=0"
    )


def mark_paid(payment_id):
    turso_db.execute("UPDATE payments SET paid=1 WHERE id=?", [payment_id])


def delete_payment(payment_id):
    turso_db.execute("DELETE FROM payments WHERE id=?", [payment_id])


def get_series_id(payment_id):
    rows = turso_db.execute("SELECT series_id FROM payments WHERE id=?", [payment_id])
    return rows[0][0] if rows else None


def update_amount(payment_id, new_amount):
    series_id = get_series_id(payment_id)
    if series_id:
        turso_db.execute("UPDATE payments SET amount=? WHERE series_id=? AND paid=0", [new_amount, series_id])
    else:
        turso_db.execute("UPDATE payments SET amount=? WHERE id=?", [new_amount, payment_id])


# ================== TELEGRAM API ==================

def send_message(chat_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "text": text}
    if keyboard:
        payload["reply_markup"] = json.dumps(keyboard)
    resp = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=10)
    try:
        return resp.json().get("result", {}).get("message_id")
    except Exception:
        return None


def edit_message(chat_id, message_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if keyboard is not None:
        payload["reply_markup"] = json.dumps(keyboard)
    requests.post(f"{TELEGRAM_API}/editMessageText", json=payload, timeout=10)


def delete_message(chat_id, message_id):
    requests.post(f"{TELEGRAM_API}/deleteMessage", json={"chat_id": chat_id, "message_id": message_id}, timeout=10)


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


def fmt_date(iso_date: str) -> str:
    return datetime.strptime(iso_date, "%Y-%m-%d").date().strftime("%d.%m.%y")


def format_payment_line(row) -> str:
    payment_id, title, amount, due_date, series_id, series_index, series_total = row
    d = fmt_date(due_date)
    label = f"{title}\n" if title else ""
    suffix = f"  🔁{series_index}/{series_total}" if series_total and series_total > 1 else ""
    return f"{label}{amount:.2f} руб. — до {d}{suffix}"


def payment_keyboard(payment_id):
    return {
        "inline_keyboard": [[
            {"text": "✅ Оплачено", "callback_data": f"paid:{payment_id}"},
            {"text": "✏️ Сумма", "callback_data": f"edit:{payment_id}"},
            {"text": "🗑 Удалить", "callback_data": f"del:{payment_id}"},
        ]]
    }


def send_list(chat_id):
    rows = get_nearest_unpaid_per_series(chat_id)
    if not rows:
        send_message(chat_id, "Список пуст — все платежи оплачены 🎉")
        return

    send_message(chat_id, "📋 Твои платежи:")
    for row in rows:
        payment_id = row[0]
        send_message(chat_id, format_payment_line(row), keyboard=payment_keyboard(payment_id))


# ================== INLINE-КАЛЕНДАРЬ ==================

def build_calendar(year, month):
    buttons = []
    buttons.append([{"text": f"{MONTHS_RU[month]} {year % 100:02d}", "callback_data": "cal:ignore"}])

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

    prev_month, prev_year = month - 1, year
    if prev_month == 0:
        prev_month, prev_year = 12, year - 1

    next_month, next_year = month + 1, year
    if next_month == 13:
        next_month, next_year = 1, year + 1

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
        "Введи платёж одной строкой:\nБанк Сумма [Дата]\n\n"
        "Например:\nТинькофф 5000\nТинькофф 5000 25.08.26\n5000\n\n"
        "Дату можно не указывать — тогда предложу выбрать её в календаре.",
    )


DATE_RE = re.compile(r"^\d{1,2}\.\d{1,2}\.(\d{4}|\d{2})$")


def parse_bank_amount_date(text):
    parts = text.strip().split()
    if not parts:
        return None, None, None, "empty"

    due_date = None
    if DATE_RE.match(parts[-1]):
        date_token = parts.pop()
        year_part = date_token.split(".")[-1]
        fmt = "%d.%m.%Y" if len(year_part) == 4 else "%d.%m.%y"
        try:
            due_date = datetime.strptime(date_token, fmt).date().isoformat()
        except ValueError:
            return None, None, None, "bad_date"
        if not parts:
            return None, None, None, "bad_date"

    last = parts[-1].replace(",", ".")
    try:
        amount = float(last)
    except ValueError:
        return None, None, None, "bad_amount"

    title = " ".join(parts[:-1]).strip()
    return title, amount, due_date, None


def ask_count(chat_id, message_id=None):
    text = (
        "Сколько раз повторить платёж?\n"
        "Введи 1, если платёж разовый, или число месяцев подряд (например 6)."
    )
    if message_id:
        edit_message(chat_id, message_id, text)
    else:
        send_message(chat_id, text)


def finalize_payment(chat_id, title, amount, due_date_iso, count):
    due = date.fromisoformat(due_date_iso)
    d = fmt_date(due_date_iso)
    label = f"{title} — " if title else ""

    if count <= 1:
        add_payment(chat_id, title, amount, due_date_iso)
        send_message(chat_id, f"Готово ✅\nЗаписал: {label}{amount:.2f} руб. до {d}", keyboard=main_menu_keyboard())
    else:
        add_series(chat_id, title, amount, due, count)
        send_message(
            chat_id,
            f"Готово ✅\nСоздал регулярный платёж на {count} мес.\n"
            f"Ближайший: {label}{amount:.2f} руб. до {d}",
            keyboard=main_menu_keyboard(),
        )


def handle_text(chat_id, text):
    text = text.strip()

    if text == "/start":
        clear_state(chat_id)
        set_bot_commands()
        send_message(
            chat_id,
            "Привет! Я помогу не забывать про платежи по кредитке.\n\n"
            "Жми кнопки внизу экрана или используй команды /add и /list.\n"
            "Могу вести и регулярные (ежемесячные) платежи.\n"
            "За день до платежа и в день платежа я сам пришлю напоминание.",
            keyboard=main_menu_keyboard(),
        )
        return

    if text in ("/add", "➕ Добавить платёж"):
        start_add_flow(chat_id)
        return

    if text in ("/list", "📋 Список платежей"):
        clear_state(chat_id)
        send_list(chat_id)
        return

    if text == "/cancel":
        clear_state(chat_id)
        send_message(chat_id, "Отменил.", keyboard=main_menu_keyboard())
        return

    step, data = get_state(chat_id)

    if step == "ask_bank_amount":
        title, amount, due_date, error = parse_bank_amount_date(text)

        if error == "bad_date":
            send_message(chat_id, "Дата некорректна. Формат: ДД.ММ.ГГ, например 25.08.26. Попробуй ещё раз.")
            return
        if error in ("bad_amount", "empty"):
            send_message(
                chat_id,
                "Не разобрал сумму. Напиши так: Тинькофф 5000 (можно добавить дату: Тинькофф 5000 25.08.26).",
            )
            return

        data["title"] = title
        data["amount"] = amount

        if due_date:
            data["due_date"] = due_date
            set_state(chat_id, "ask_count", data)
            ask_count(chat_id)
        else:
            set_state(chat_id, "ask_date", data)
            today = date.today()
            send_message(chat_id, "Дату не указал — выбери в календаре:", keyboard=build_calendar(today.year, today.month))
        return

    if step == "ask_date":
        send_message(chat_id, "Выбери дату кнопками в календаре выше 👆")
        return

    if step == "ask_count":
        try:
            count = int(text.strip())
        except ValueError:
            send_message(chat_id, "Нужно число. Введи 1 для разового платежа или сколько месяцев повторить.")
            return
        if count < 1:
            send_message(chat_id, "Число должно быть 1 или больше. Попробуй ещё раз.")
            return

        title = data.get("title", "")
        amount = data["amount"]
        due_date_iso = data["due_date"]
        clear_state(chat_id)
        finalize_payment(chat_id, title, amount, due_date_iso, count)
        return

    if step == "edit_amount":
        try:
            new_amount = float(text.replace(",", "."))
        except ValueError:
            send_message(chat_id, "Не похоже на число. Введи сумму ещё раз (например: 5000):")
            return

        payment_id = data.get("edit_payment_id")
        edit_message_id = data.get("edit_message_id")
        clear_state(chat_id)
        update_amount(payment_id, new_amount)

        row = get_payment(payment_id)
        if row and edit_message_id:
            edit_message(chat_id, edit_message_id, format_payment_line(row), keyboard=payment_keyboard(payment_id))
        send_message(chat_id, "Сумма обновлена ✅")
        return

    send_message(chat_id, "Не понял. Используй кнопки внизу или команды /add, /list", keyboard=main_menu_keyboard())


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
            payment_id = int(data.split(":")[1])
            row = get_payment(payment_id)
            if not row or row[0] is None:
                answer_callback(cq["id"], "Уже обработано")
            else:
                answer_callback(cq["id"], "Оплачено")
                mark_paid(payment_id)
                edit_message(chat_id, message_id, f"✅ {format_payment_line(row)}", keyboard={"inline_keyboard": []})

                series_id = row[4]
                if series_id:
                    next_row = get_next_unpaid_in_series(series_id)
                    if next_row:
                        next_id = next_row[0]
                        send_message(chat_id, format_payment_line(next_row), keyboard=payment_keyboard(next_id))

        elif data.startswith("del:"):
            answer_callback(cq["id"], "Удалено")
            payment_id = int(data.split(":")[1])
            delete_payment(payment_id)
            delete_message(chat_id, message_id)

        elif data.startswith("edit:"):
            answer_callback(cq["id"])
            payment_id = int(data.split(":")[1])
            set_state(chat_id, "edit_amount", {"edit_payment_id": payment_id, "edit_message_id": message_id})
            send_message(chat_id, "Введи новую сумму:")

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
            step, sdata = get_state(chat_id)
            if step == "ask_date":
                sdata["due_date"] = picked_date
                set_state(chat_id, "ask_count", sdata)
                ask_count(chat_id, message_id)

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
