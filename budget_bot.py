# budget_bot_full.py  —  complete version with /daily command (~340 lines)
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
from telebot import types

# 1. CONFIG --------------------------------------------------------------------
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise RuntimeError("В .env нет TELEGRAM_TOKEN")
DB_FILE = "budget.db"
VALID_CATEGORIES = {c.strip().lower() for c in os.getenv("VALID_CATEGORIES", "").split(",") if c.strip()} or {"другое"}

# 2. LOGGING -------------------------------------------------------------------
logging.basicConfig(
    filename="unparsed.log",
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    encoding="utf-8",
)

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
        logging.info("NO_DATE_HEADER | %s", lines[0][:90])
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


        # если строка не распознана — логируем для отладки
        logging.info("UNPARSED | %s | %s", d.isoformat(), ln[:80])

    return d, res


# 6. REPORTS -------------------------------------------------------------------

def _bounds(y,m):
    st=date(y,m,1); ey,em=(y+1,1) if m==12 else (y,m+1)
    return st, date(ey,em,1)


def get_summary(y: int, m: int):
    """Возвращает inc, exp, cats   (3 значения)"""
    st, en = _bounds(y, m)
    conn   = sqlite3.connect(DB_FILE)
    c      = conn.cursor()

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

    conn.close()
    return inc, exp, cats



def get_daily(y: int, m: int):
    st, en = _bounds(y, m)
    conn   = sqlite3.connect(DB_FILE)
    c      = conn.cursor()

    c.execute("""
        SELECT date,
               SUM(CASE WHEN type='income'
                        THEN  amount                       ELSE 0 END)/100  AS inc,
               SUM(CASE WHEN type='expense' AND primary_flag = 1
                        THEN -amount                       ELSE 0 END)/100  AS exp
        FROM transactions
        WHERE date >= ? AND date < ?
        GROUP BY date
        ORDER BY date
    """, (st, en))

    rows = c.fetchall()
    conn.close()
    return rows


# 7. HANDLERS -----------------------------------------------------------------
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

    # get_summary возвращает 3 значения — inc, exp, cats
    inc, exp, cats = get_summary(datetime.now().year, mm)
    bal = inc - exp

    if not (inc or exp):
        bot.reply_to(msg, 'Нет данных'); return

    name = [k for k, v in MONTHS_RU.items() if v == mm][0]
    out  = [f'<b>Отчёт за {name} {datetime.now().year}</b>',
            f'Доход:  <b>{inc:,.0f}</b>р',
            f'Расход: <b>{exp:,.0f}</b>р',
            f'Итог:   <b>{bal:,.0f}</b>р',
            '<b>Расходы по категориям:</b>']
    out += [f'{c}: <b>{a:,.0f}</b>р' for c, a in cats]
    bot.reply_to(msg, '\n'.join(out))


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
        out.append(f'{d_fmt}: +{inc:,.0f}р / -{exp:,.0f}р ⇒ {bal:,.0f}р')
    bot.reply_to(msg, ''.join(out))

@bot.message_handler(content_types=['text', 'photo'])
def _incoming(msg):
    text = msg.text or msg.caption or ''
    d, txs = parse_transaction(text)
    if not txs:
        return
    add_transactions(d, txs)
    bot.reply_to(msg, f'Записал {len(txs)} транзакций на {d.strftime("%d.%m.%Y")}')

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

