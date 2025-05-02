# budget_bot.py — полная версия с фиксами (≈330 строк)
# --------------------------------------------------
# 1.  ENV & CONFIG
# --------------------------------------------------
import os
import re
import sqlite3
import logging
import unicodedata
from datetime import datetime, date

from dotenv import load_dotenv
import telebot
from telebot import types

load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise RuntimeError("В .env нет TELEGRAM_TOKEN")
DB_FILE = "budget.db"
VALID_CATEGORIES = {
    c.strip().lower() for c in os.getenv("VALID_CATEGORIES", "").split(",") if c.strip()
} or {"другое"}

# --------------------------------------------------
# 2.  LOGGING
# --------------------------------------------------
logging.basicConfig(
    filename="unparsed.log",
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    encoding="utf-8",
)

# --------------------------------------------------
# 3.  TELEGRAM BOT
# --------------------------------------------------
bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

# --------------------------------------------------
# 4.  DATABASE
# --------------------------------------------------

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            date     DATE    NOT NULL,
            type     TEXT    CHECK(type IN ('income','expense')),
            amount   INTEGER NOT NULL,   -- хранится в КОПЕЙКАХ
            category TEXT,
            comment  TEXT
        )
        """
    )
    conn.commit(); conn.close()


def add_transactions(trans_date: date, tx_list: list[tuple]):
    if not tx_list:
        return
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.executemany(
        "INSERT INTO transactions(date,type,amount,category,comment) VALUES (?,?,?,?,?)",
        [(trans_date, *tx) for tx in tx_list],
    )
    conn.commit(); conn.close()

# --------------------------------------------------
# 5.  PARSER
# --------------------------------------------------
MONTHS_RU = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
MINUS_CHARS = r"\-−–—"  # дефис + все популярные минусы/длины тире
INCOME_RE  = re.compile(rf"^\s*\+\s*([\d\s\u00A0\u202F.,]+)р?\s*(.*)$", re.I)
EXPENSE_RE = re.compile(rf"^\s*[{MINUS_CHARS}]\s*([\d\s\u00A0\u202F.,]+)р?\s*(.*)$", re.I)
SUMMARY_RE = re.compile(rf"^\s*[{MINUS_CHARS}]?\s*\d[\d\s\u00A0\u202F.,]*р?\s*$", re.I)
SPACE_RE   = re.compile(r"[\s\u00A0\u202F\u2009\u2007]")  # все виды пробелов


def _normalize_num(raw: str) -> int:
    """→ int в КОПЕЙКАХ."""
    clean = SPACE_RE.sub("", raw).replace(",", ".")
    if "." in clean:
        rub, kop = clean.split(".")
        kop = (kop + "0")[:2]
    else:
        rub, kop = clean, "00"
    return int(rub) * 100 + int(kop)


def _clean_invis(s: str) -> str:
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Cf")
    s = "".join(" " if unicodedata.category(ch).startswith("Z") else ch for ch in s)
    return s.strip()


def _parse_date_from_line(line: str) -> date | None:
    """Любой формат даты в одной строке."""
    # dd.mm.yy
    m = re.match(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})", line)
    if m:
        d, mth, y = map(int, m.groups()); y += 2000 if y < 100 else 0
        try: return date(y, mth, d)
        except ValueError: return None
    # «7 апреля» (:+‑–— optional) + optional year
    m = re.match(r"(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?:?", line, re.I)
    if m:
        d, mon_name, y = m.groups(); mon = MONTHS_RU.get(mon_name.lower())
        if not mon: return None
        y = int(y) if y else datetime.now().year
        try: return date(y, mon, int(d))
        except ValueError: return None
    return None


def parse_transaction(text: str):
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if not lines:
        return None, []
    first_date = _parse_date_from_line(_clean_invis(lines[0]))
    if not first_date:
        logging.info("NO_DATE_HEADER | %s", lines[0][:80])
        return None, []
    txs = []
    for line in lines[1:]:
        line = line.strip()
        if not line or "=" in line or SUMMARY_RE.match(line):
            continue
        if (m := INCOME_RE.match(line)):
            amt = _normalize_num(m.group(1))
            txs.append(("income", amt, None, m.group(2).strip()))
            continue
        if (m := EXPENSE_RE.match(line)):
            amt = _normalize_num(m.group(1))
            cats = re.findall(r"\[([^\]]+)\]", line) or ["другое"]
            comment = m.group(2).strip()
            for cat in cats:
                cat = cat.strip().lower(); cat = cat if cat in VALID_CATEGORIES else "другое"
                txs.append(("expense", amt, cat, comment))
            continue
        logging.info("UNPARSED | %s | %s", first_date.isoformat(), line[:100])
    return first_date, txs

# --------------------------------------------------
# 6.  REPORTS
# --------------------------------------------------

def _month_bounds(y,m):
    start = date(y,m,1); ey,em = (y+1,1) if m==12 else (y,m+1)
    return start, date(ey,em,1)


def get_summary(y,m):
    s,e=_month_bounds(y,m)
    conn=sqlite3.connect(DB_FILE); c=conn.cursor()
    c.execute("SELECT SUM(amount) FROM transactions WHERE type='income' AND date>=? AND date<?",(s,e))
    inc=(c.fetchone()[0] or 0)/100
    c.execute("SELECT SUM(amount) FROM transactions WHERE type='expense' AND date>=? AND date<?",(s,e))
    exp=(c.fetchone()[0] or 0)/100
    bal=inc-exp
    c.execute("""SELECT category, SUM(amount) FROM transactions WHERE type='expense' AND date>=? AND date<? GROUP BY category ORDER BY SUM(amount) DESC""",(s,e))
    cats=[(cat,-amt/100) for cat,amt in c.fetchall()]
    conn.close(); return inc,exp,bal,cats

# --------------------------------------------------
# 7.  TELEGRAM HANDLERS
# --------------------------------------------------
@bot.message_handler(commands=["start","help"])
def _help(m):
    bot.reply_to(m, "<b>Бот учёта финансов</b> — пришлите сообщение вида:\n<pre>7 апреля:\n-250р хлеб [еда]\n+50 000р зарплата</pre>\nФото с подписью тоже учитываются.\nОтчёт: /summary <месяц-числом> (например /summary 4).")

@bot.message_handler(commands=["summary"])
def _summary(m):
    try: _,mn=m.text.split(); mn=int(mn)
    except: bot.reply_to(m,"/summary <месяц>"); return
    y=datetime.now().year
    inc,exp,bal,cats=get_summary(y,mn)
    if not (inc or exp):
        bot.reply_to(m,"Нет данных за этот месяц."); return
    mon_name=[k for k,v in MONTHS_RU.items() if v==mn][0]
    resp=[f"<b>Отчёт за {mon_name} {y}</b>",f"Доход:  <b>{inc:,.0f}</b>р",f"Расход: <b>{exp:,.0f}</b>р",f"Итог:   <b>{bal:,.0f}</b>р\n"]
    resp+=["<b>Расходы по категориям:</b>"]+[f"{c}: <b>{a:,.0f}</b>р" for c,a in cats]
    bot.reply_to(m,"\n".join(resp))

@bot.message_handler(content_types=["text","photo"])
def _incoming(m):
    txt = m.text or m.caption or ""
    d,tx=parse_transaction(txt)
    if not tx: return
    add_transactions(d,tx)
    bot.reply_to(m,f"Записал {len(tx)} транзакций на {d.strftime('%d.%m.%Y')}")

    
# --- DAILY REPORT ----------------------------------------------------
@bot.message_handler(commands=['daily'])
def cmd_daily(msg):
    try:
        _, m = msg.text.split()
        month = int(m)
    except ValueError:
        bot.reply_to(msg, 'Используй: /daily <месяц-числом>')
        return

    year = datetime.now().year
    start, end = _month_bounds(year, month)

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT date,
               SUM(CASE WHEN type='income'  THEN amount ELSE 0 END)/100 AS inc,
               SUM(CASE WHEN type='expense' THEN -amount ELSE 0 END)/100 AS exp
        FROM transactions
        WHERE date>=? AND date<? GROUP BY date ORDER BY date
    """, (start, end))
    rows = c.fetchall()
    conn.close()

    if not rows:
        bot.reply_to(msg, 'Нет данных.')
        return

    lines = [f'<b>Дневные итоги за {month:02}.{year}</b>']
    for d, inc, exp in rows:
        d_str = datetime.strptime(d, '%Y-%m-%d').strftime('%d.%m')
        bal = inc - exp
        lines.append(f'{d_str}: +{inc:,.0f}р  /  -{exp:,.0f}р  ⇒  {bal:,.0f}р')
    bot.reply_to(msg, '\n'.join(lines))
# ---------------------------------------------------------------------

# --------------------------------------------------
# 8.  MAIN LOOP
# --------------------------------------------------
if __name__ == "__main__":
    init_db(); print("Bot is running… (Ctrl+C to stop)")
    while True:
        try:        
            bot.infinity_polling(long_polling_timeout=30, timeout=90)
        except Exception as e:
            logging.error("POLLING CRASH: %s", e, exc_info=True)
            continue
