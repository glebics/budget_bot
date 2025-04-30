import re
import sqlite3
import logging
import os
from telegram.ext import Updater, MessageHandler, Filters, CommandHandler
from telegram import Update
import schedule
import time
import threading
from datetime import datetime, timedelta
from dateutil import relativedelta
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()
TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('CHANNEL_CHAT_ID')
VALID_CATEGORIES = os.getenv('VALID_CATEGORIES').split(',') if os.getenv('VALID_CATEGORIES') else []

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация базы данных SQLite
def init_db():
    conn = sqlite3.connect('budget.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS transactions
                 (date TEXT, amount REAL, category TEXT, description TEXT, type TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS summaries
                 (summary_id TEXT, month TEXT, year TEXT, UNIQUE(month, year))''')
    conn.commit()
    conn.close()

# Парсинг транзакции из сообщения
def parse_transaction(text):
    lines = text.strip().split('\n')
    if not lines:
        return None
    # Проверяем, начинается ли сообщение с даты
    date_pattern = r'^\d{1,2}\s+[а-яА-Я]+\s*:$'
    if not re.match(date_pattern, lines[0]):
        return None
    
    transactions = []
    for line in lines[1:]:
        # Парсинг доходов (+)
        income_pattern = r'^\+([\d.]+)р\s+(.+)$'
        income_match = re.match(income_pattern, line.strip())
        if income_match:
            amount = float(income_match.group(1))
            description = income_match.group(2).strip()
            transactions.append(('income', amount, None, description))
            continue
        
        # Парсинг расходов (-, –, тире)
        expense_pattern = r'^[-–]([\d.]+)р\s+([^\[]+)\s*((?:\[[^\]]+\]\s*)+)$'
        expense_match = re.match(expense_pattern, line.strip())
        if expense_match:
            amount = -float(expense_match.group(1))
            description = expense_match.group(2).strip()
            categories_str = expense_match.group(3)
            categories = re.findall(r'\[([^\]]+)\]', categories_str)
            # Проверка валидности категорий
            valid_categories = [cat for cat in categories if cat in VALID_CATEGORIES]
            if valid_categories:
                for category in valid_categories:
                    transactions.append(('expense', amount, category, description))
    
    return transactions if transactions else None

# Сохранение транзакции в базу данных
def store_transaction(date, amount, category, description, type_):
    conn = sqlite3.connect('budget.db')
    c = conn.cursor()
    c.execute('INSERT INTO transactions (date, amount, category, description, type) VALUES (?, ?, ?, ?, ?)',
              (date, amount, category, description, type_))
    conn.commit()
    conn.close()

# Проверка, обработан ли месяц
def is_month_processed(month, year):
    conn = sqlite3.connect('budget.db')
    c = conn.cursor()
    c.execute('SELECT 1 FROM summaries WHERE month = ? AND year = ?', (month, year))
    result = c.fetchone()
    conn.close()
    return result is not None

# Отметка месяца как обработанного
def mark_month_processed(month, year, summary_id):
    conn = sqlite3.connect('budget.db')
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO summaries (summary_id, month, year) VALUES (?, ?, ?)',
              (summary_id, month, year))
    conn.commit()
    conn.close()

# Расчет сводки за месяц
def get_monthly_summary(year, month):
    conn = sqlite3.connect('budget.db')
    c = conn.cursor()
    start_date = f'{year}-{month:02d}-01'
    end_date = (datetime.strptime(start_date, '%Y-%m-%d') + relativedelta.months(1)).strftime('%Y-%m-%d')
    
    # Доходы
    c.execute('SELECT SUM(amount) FROM transactions WHERE type = ? AND date >= ? AND date < ?',
              ('income', start_date, end_date))
    total_income = c.fetchone()[0] or 0.0
    
    # Расходы по категориям
    c.execute('SELECT category, SUM(amount) as total FROM transactions WHERE type = ? AND date >= ? AND date < ? GROUP BY category',
              ('expense', start_date, end_date))
    expense_results = c.fetchall()
    
    # Общий расход
    c.execute('SELECT SUM(amount) FROM transactions WHERE type = ? AND date >= ? AND date < ?',
              ('expense', start_date, end_date))
    total_expense = c.fetchone()[0] or 0.0
    
    conn.close()
    
    if not total_income and not expense_results and not total_expense:
        return None
    
    summary = f"📊 Сводка за {month:02d}/{year}:\n"
    summary += f"Доход: {total_income:.2f}р\n"
    summary += "Расходы по категориям:\n"
    for category, total in expense_results:
        summary += f"{category}: {total:.2f}р\n"
    summary += f"Общий расход: {total_expense:.2f}р"
    return summary

# Обработка входящих сообщений
def handle_message(update: Update, context):
    text = update.message.text
    transactions = parse_transaction(text)
    if transactions:
        date = datetime.now().strftime('%Y-%m-%d')
        for type_, amount, category, description in transactions:
            store_transaction(date, amount, category, description, type_)
        # Не отправляем ответ, чтобы не спамить

# Обработка команды /summary
def summary_command(update: Update, context):
    current_date = datetime.now()
    prev_month = current_date - relativedelta.months(1)
    summary = get_monthly_summary(prev_month.year, prev_month.month)
    if summary:
        update.message.reply_text(summary)
    else:
        update.message.reply_text("Нет данных за прошлый месяц.")

# Анализ исторических данных
def process_historical_messages(context):
    months_to_process = [(2025, 3), (2025, 4)]  # Март, Апрель 2025
    for year, month in months_to_process:
        if not is_month_processed(month, year):
            summary = get_monthly_summary(year, month)
            if summary:
                context.bot.send_message(chat_id=CHAT_ID, text=summary)
                summary_id = f"{year}-{month:02d}"
                mark_month_processed(month, year, summary_id)

# Планирование месячной сводки
def schedule_monthly_summary(context):
    current_date = datetime.now()
    if current_date.day == 1:
        prev_month = current_date - relativedelta.months(1)
        if not is_month_processed(prev_month.month, prev_month.year):
            summary = get_monthly_summary(prev_month.year, prev_month.month)
            if summary:
                context.bot.send_message(chat_id=CHAT_ID, text=summary)
                summary_id = f"{prev_month.year}-{prev_month.month:02d}"
                mark_month_processed(prev_month.month, prev_month.year, summary_id)

# Запуск планировщика
def run_scheduler(updater):
    schedule.every().day.at("00:01").do(schedule_monthly_summary, updater)
    while True:
        schedule.run_pending()
        time.sleep(60)

# Обработка ошибок
def error(update, context):
    logger.warning('Update "%s" caused error "%s"', update, context.error)

# Основная функция
def main():
    if not TOKEN or not CHAT_ID or not VALID_CATEGORIES:
        logger.error("TELEGRAM_TOKEN, CHANNEL_CHAT_ID или VALID_CATEGORIES не установлены в .env")
        return
    
    init_db()
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher
    
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    dp.add_handler(CommandHandler("summary", summary_command))
    dp.add_error_handler(error)
    
    process_historical_messages(updater)
    
    scheduler_thread = threading.Thread(target=run_scheduler, args=(updater,))
    scheduler_thread.daemon = True
    scheduler_thread.start()
    
    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
