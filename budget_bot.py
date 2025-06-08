# -----------------------------------------------------------------------------
# 0. DEPENDENCIES --------------------------------------------------------------
import os
import re
import sqlite3
import logging
import unicodedata
from datetime import datetime, date

from dotenv import load_dotenv
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton


# 1. CONFIG --------------------------------------------------------------------
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise RuntimeError("В .env нет TELEGRAM_TOKEN")
DB_FILE = "budget.db"
VALID_CATEGORIES = {c.strip().lower() for c in os.getenv("VALID_CATEGORIES", "").split(",") if c.strip()} or {"другое"}


# 2. LOGGING -------------------------------------------------------------------
log_unparsed = logging.getLogger("unparsed")
log_unparsed.setLevel(logging.INFO)

# создаём файловый хендлер только для нераспознанных
handler = logging.FileHandler("unparsed.log", mode="w", encoding="utf-8")
handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
log_unparsed.addHandler(handler)

# Очищаем файл логов при каждом запуске
with open("unparsed.log", "w", encoding="utf-8") as f:
    f.write("")  # просто затираем


# 3. TELEGRAM BOT --------------------------------------------------------------
bot = telebot.TeleBot(TOKEN, parse_mode="HTML")


# 4. DATABASE ------------------------------------------------------------------
def init_db():
    """Создание / проверка таблицы с учётом поля primary_flag."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # если таблицы нет — создаём сразу с нужными колонками
    c.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            date         DATE    NOT NULL,
            type         TEXT    CHECK(type IN ('income','expense')),
            amount       INTEGER NOT NULL,   -- в копейках
            category     TEXT,
            comment      TEXT,
            primary_flag INTEGER NOT NULL DEFAULT 1
        )
    """)

    # если таблица уже была, убеждаемся, что колонка primary_flag есть
    c.execute("PRAGMA table_info(transactions)")
    cols = {row[1] for row in c.fetchall()}
    if 'primary_flag' not in cols:
        c.execute("ALTER TABLE transactions ADD COLUMN primary_flag INTEGER NOT NULL DEFAULT 1")

    conn.commit()
    conn.close()


def add_transactions(d: date, rows: list[tuple]):
    """rows: (type, amount, category, comment, primary_flag)"""
    if not rows:
        return
    conn = sqlite3.connect(DB_FILE)
    conn.executemany(
        "INSERT INTO transactions(date,type,amount,category,comment,primary_flag)"
        " VALUES (?,?,?,?,?,?)",
        [(d, *r) for r in rows],
    )
    conn.commit()
    conn.close()


# 5. PARSER --------------------------------------------------------------------
MONTHS_RU = {"января":1,"февраля":2,"марта":3,"апреля":4,"мая":5,"июня":6,"июля":7,"августа":8,"сентября":9,"октября":10,"ноября":11,"декабря":12}
MONTHS_NOM = {
    1: "январь",  2: "февраль", 3: "март",     4: "апрель",
    5: "май",     6: "июнь",    7: "июль",     8: "август",
    9: "сентябрь",10: "октябрь",11: "ноябрь", 12: "декабрь"
}
MINUS_CHARS = r"\-−–—"
INCOME_RE  = re.compile(rf"^\s*\+\s*([\d\s\u00A0\u202F.,]+)р?\s*(.*)$", re.I)
EXPENSE_RE = re.compile(rf"^\s*[{MINUS_CHARS}]\s*([\d\s\u00A0\u202F.,]+)р?\s*(.*)$", re.I)
SUMMARY_RE = re.compile(rf"^\s*[{MINUS_CHARS}]?\s*\d[\d\s\u00A0\u202F.,]*р?\s*$", re.I)
SPACE_RE   = re.compile(r"[\s\u00A0\u202F\u2009\u2007]")


def _normalize(raw: str) -> int:
    clean = SPACE_RE.sub("", raw).replace(",", ".")
    if "." in clean:
        rub, kop = clean.split("."); kop = (kop+"0")[:2]
    else:
        rub, kop = clean, "00"
    return int(rub)*100 + int(kop)


def _clean(s: str) -> str:
    s = "".join(ch for ch in s if unicodedata.category(ch) != 'Cf')
    s = "".join(' ' if unicodedata.category(ch).startswith('Z') else ch for ch in s)
    return s.strip()


def _parse_date(line: str) -> date | None:
    if m:=re.match(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})", line):
        d,mn,y = map(int,m.groups()); y += 2000 if y<100 else 0
        try: return date(y,mn,d)
        except: return None
    m = re.match(r"(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?:?", line, re.I)
    if not m: return None
    d, mname, y = m.groups(); mn = MONTHS_RU.get(mname.lower())
    if not mn: return None
    y = int(y) if y else datetime.now().year
    try: return date(y,mn,int(d))
    except: return None


def parse_transaction(txt: str):
    """Возвращает (date, [ (type, amount, category, comment) … ]).
       amount хранится в копейках (всегда положительное число)."""
    lines = [ln for ln in txt.split('\n') if ln.strip()]
    if not lines:
        return None, []

    d = _parse_date(_clean(lines[0]))
    if not d:
        log_unparsed.info("NO_DATE_HEADER | %s", lines[0][:90])
        return None, []

    res = []
    for ln in lines[1:]:
        ln = ln.strip()

        # пропускаем пустые, промежуточные «=» и строку‑итог дня
        if not ln or '=' in ln or SUMMARY_RE.match(ln):
            continue

        # ---------- ДОХОД ----------
        if (m := INCOME_RE.match(ln)):
            res.append((
                "income",
                _normalize(m.group(1)),
                None,
                m.group(2).strip(),
                1                      # <‑‑ добавляем primary_flag = 1
            ))
            continue

        # ---------- РАСХОД ----------
        if (m := EXPENSE_RE.match(ln)):
            amt     = _normalize(m.group(1))          # положительное!
            comment = m.group(2).strip()
            cats    = re.findall(r"\[([^\]]+)\]", ln) or ["другое"]

            for idx, cat in enumerate(cats):
                cat = cat.strip().lower()
                if cat not in VALID_CATEGORIES:
                    cat = "другое"

                is_primary = 1 if idx == 0 else 0     # ← для общего расхода
                res.append(("expense", amt, cat, comment, is_primary))
            continue
        # если строка не распознана — логируем отдельно
        log_unparsed.info("UNPARSED | %s | %s", d.isoformat(), ln[:80])

        # если НИ ОДНА строка не распарсилась — логируем заголовок
        if not res:
            with open("unparsed.log", "a", encoding="utf-8"):
                flat = ' '.join(lines).replace('\n', ' ')
                log_unparsed.info("%s | %s", d or "NO_DATE", flat[:120])

    return d, res


# 6. REPORTS -------------------------------------------------------------------
def _bounds(y, m):
    st = date(y, m, 1)
    ey, em = (y + 1, 1) if m == 12 else (y, m + 1)
    return st, date(ey, em, 1)


def get_summary(y: int, m: int):
    """Возвращает inc, exp, cats, transactions   (4 значения)"""
    st, en = _bounds(y, m)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # ДОХОД
    c.execute("SELECT SUM(amount) FROM transactions "
              "WHERE type='income' AND date>=? AND date<?", (st, en))
    inc = (c.fetchone()[0] or 0) / 100

    # РАСХОД (только primary_flag = 1)
    c.execute("SELECT SUM(amount) FROM transactions "
              "WHERE type='expense' AND primary_flag=1 "
              "AND date>=? AND date<?", (st, en))
    exp = (c.fetchone()[0] or 0) / 100

    # КАТЕГОРИИ — сумма ВСЕХ строк, без фильтра primary_flag
    c.execute("SELECT category, SUM(amount) "
              "FROM transactions "
              "WHERE type='expense' AND date>=? AND date<? "
              "GROUP BY category", (st, en))
    cats = [(cat, -amt / 100) for cat, amt in c.fetchall()]

    # ПОЛНЫЙ СПИСОК ТРАНЗАКЦИЙ ПО КАТЕГОРИЯМ
    c.execute("""
        SELECT category, date, amount, comment
        FROM transactions
        WHERE type='expense' AND date>=? AND date<? 
        ORDER BY date
    """, (st, en))
    transactions = {}
    for cat, d, amt, comment in c.fetchall():
        amt = -amt / 100  # Инвертируем для отрицательного значения
        transactions.setdefault(cat, []).append((d, amt, comment))

    conn.close()
    return inc, exp, cats, transactions


def get_daily(y: int, m: int):
    st, en = _bounds(y, m)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("""
        SELECT date,
               SUM(CASE WHEN type='income' THEN amount ELSE 0 END)/100  AS inc,
               SUM(CASE WHEN type='expense' AND primary_flag = 1 THEN amount ELSE 0 END)/100  AS exp
        FROM transactions
        WHERE date >= ? AND date < ?
        GROUP BY date
        ORDER BY date
    """, (st, en))

    rows = c.fetchall()
    conn.close()
    return rows


def pretty_money(v: float) -> str:
    return f'{v:,.0f}р'.replace(',', ' ')        # узкий не‑перенос пробел


def render_summary(month_name: str, y: int, m: int, inc: float, exp: float, bal: float, cats: list[tuple[str, float]], transactions: dict) -> tuple[str, InlineKeyboardMarkup]:
    emoji = {'доход': '💰', 'расход': '💸', 'итог': '🟢' if bal >= 0 else '🔴'}
    cat_emoji = {
        'еда': '🍲', 'сладкое': '🍭', 'другое': '📦', 'нужное': '🛠️',
        'жилье': '🏠', 'лекарства': '💊', 'проезд': '🚌', 'даня': '🧒'
    }

    lines = [
        '📊 <b>Отчёт за {}</b>\n'.format(month_name.capitalize() + f' {y}'),
        f'{emoji["доход"]} <b>Доход:</b>  <b>{pretty_money(inc)}</b>',
        f'{emoji["расход"]} <b>Расход:</b> <b>{pretty_money(exp)}</b>',
        f'{emoji["итог"]} <b>Итог:</b>   <b>{pretty_money(bal)}</b>',
        '',
        '📂 <b>Расходы по категориям</b>',
    ]
    kb = InlineKeyboardMarkup()
    for cat, val in cats:
        ico = cat_emoji.get(cat, '•')
        lines.append(f'{ico} <blockquote><b>{cat}:</b> {pretty_money(val)}</blockquote>')
        kb.add(InlineKeyboardButton(f"Подробности {cat}", callback_data=f"details:{y}:{m}:{cat}"))

    return '\n'.join(lines), kb


def render_daily(rows):
    out = ['🗓️ <b>Сводка по дням</b>']
    for d, inc, exp in rows:
        date_s = datetime.strptime(d, '%Y-%m-%d').strftime('%d.%m')
        bal = inc - exp
        out.append(f'{date_s}: +{pretty_money(inc)} / -{pretty_money(exp)} ⇒ {pretty_money(bal)}')
    return '\n'.join(out)


def render_details(category: str, transactions: dict, y: int, m: int):
    lines = [f'📋 <b>Подробности по категории "{category}" за {MONTHS_NOM[m]} {y}</b>']
    if category in transactions:
        for d, amt, comment in transactions[category]:
            date_s = datetime.strptime(d, '%Y-%m-%d').strftime('%d.%m')
            lines.append(f'{date_s}: {pretty_money(amt)} — {comment}')
    else:
        lines.append("Нет транзакций для этой категории.")
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("Назад к категориям", callback_data=f"back:{y}:{m}"))
    return '\n'.join(lines), kb


# 7. HANDLERS -----------------------------------------------------------------
@bot.message_handler(commands=['menu'])
def send_menu(msg):
    kb = InlineKeyboardMarkup()
    y = datetime.now().year
    for yy in range(y, y - 4, -1):
        kb.add(InlineKeyboardButton(str(yy), callback_data=f"year:{yy}"))
    bot.send_message(msg.chat.id, "Выбери год", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("year:"))
def pick_year(call):
    y = int(call.data.split(":")[1])
    kb = InlineKeyboardMarkup()
    for i in range(1, 13):
        kb.add(InlineKeyboardButton(MONTHS_NOM[i], callback_data=f"summary:{y}:{i}"))
    bot.edit_message_text(f"Год {y}. Теперь выбери месяц:", call.message.chat.id, call.message.message_id, reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("summary:"))
def pick_summary(call):
    _, y, m = call.data.split(":")
    y, m = int(y), int(m)

    inc, exp, cats, transactions = get_summary(y, m)
    bal = inc - exp

    daily_rows = get_daily(y, m)

    daily_txt = render_daily(daily_rows)
    summary_txt, kb = render_summary(MONTHS_NOM[m], y, m, inc, exp, bal, cats, transactions)

    bot.send_message(call.message.chat.id, daily_txt)
    msg = bot.send_message(call.message.chat.id, summary_txt, reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("details:"))
def show_details(call):
    _, y, m, category = call.data.split(":")
    y, m = int(y), int(m)
    inc, exp, cats, transactions = get_summary(y, m)
    details_txt, kb = render_details(category, transactions, y, m)
    bot.edit_message_text(details_txt, call.message.chat.id, call.message.message_id, reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("back:"))
def go_back(call):
    y, m = map(int, call.data.split(":")[1:])
    inc, exp, cats, transactions = get_summary(y, m)
    bal = inc - exp
    summary_txt, kb = render_summary(MONTHS_NOM[m], y, m, inc, exp, bal, cats, transactions)
    bot.edit_message_text(summary_txt, call.message.chat.id, call.message.message_id, reply_markup=kb)


# сохраним id всех входящих msg и id ответов бота
GC_BUFFER = []


@bot.message_handler(commands=['start','help'])
def _help(msg):
    bot.reply_to(msg,
        '<b>Фин‑бот</b>. Пересылай записи вида:\n<pre>7 апреля:\n-250р хлеб [еда]\n+50 000р зарплата</pre>'
        '\nФото с подписью тоже считаются. Отчёт: /summary 4, дневной: /daily 4.')


@bot.message_handler(commands=['summary'])
def _summary(msg):
    try:
        _, mm = msg.text.split(); mm = int(mm)
    except ValueError:
        bot.reply_to(msg, '/summary <месяц>'); return

    y = datetime.now().year
    inc, exp, cats, transactions = get_summary(y, mm)
    bal = inc - exp

    if not (inc or exp):
        bot.reply_to(msg, 'Нет данных'); return

    name = [k for k, v in MONTHS_RU.items() if v == mm][0]

    # Показываем суммы по дням
    daily_rows = get_daily(y, mm)
    daily_text = render_daily(daily_rows)
    daily_msg = bot.reply_to(msg, daily_text)

    # Показываем итог
    summary_txt, kb = render_summary(name, y, mm, inc, exp, bal, cats, transactions)
    summary_msg = bot.reply_to(msg, summary_txt, reply_markup=kb)
    daily_id = daily_msg.message_id
    summary_id = summary_msg.message_id

    # --- очистка ---
    to_delete = GC_BUFFER.copy()
    to_delete.append(msg.id)  # удалить только команду /summary

    for mid in to_delete:
        try:
            bot.delete_message(msg.chat.id, mid)
        except:
            pass
    GC_BUFFER.clear()


@bot.message_handler(commands=['daily'])
def _daily(msg):
    try:
        _, mm = msg.text.split(); mm = int(mm)
    except ValueError:
        bot.reply_to(msg, '/daily <месяц>'); return
    y = datetime.now().year
    rows = get_daily(y, mm)
    if not rows:
        bot.reply_to(msg, 'Нет данных'); return
    mon_name = [k for k, v in MONTHS_RU.items() if v == mm][0]
    out = [f'<b>Дневные итоги за {mon_name} {y}</b>']
    for d, inc, exp in rows:
        d_fmt = datetime.strptime(d, '%Y-%m-%d').strftime('%d.%m')
        bal = inc - exp
        out.append(f'{d_fmt}: +{pretty_money(inc)}р / -{pretty_money(exp)}р ⇒ {pretty_money(bal)}р')
    bot.reply_to(msg, ''.join(out))


@bot.message_handler(content_types=['text', 'photo'])
def _incoming(msg):
    # ⬅️ Всегда добавляем исходное сообщение (и текст, и фото)
    GC_BUFFER.append(msg.message_id)

    text = msg.text or msg.caption or ''
    d, rows = parse_transaction(text)
    if not rows:
        return

    add_transactions(d, rows)
    bot_msg = bot.reply_to(msg, f'Записал {len(rows)} транзакций на {d.strftime("%d.%m.%Y")}')
    GC_BUFFER.append(bot_msg.message_id)



# 8. MAIN LOOP ---------------------------------------------------------------
if __name__ == '__main__':
    init_db()
    print('Bot is running… (Ctrl+C to stop)')
    while True:
        try:
            bot.infinity_polling(long_polling_timeout=30, timeout=90)
        except Exception as e:
            logging.error('Polling crash: %s', e, exc_info=True)
            continue