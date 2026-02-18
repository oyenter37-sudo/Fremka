import asyncio
import json
import base64
import sqlite3
from typing import Dict, Optional, List
from dataclasses import dataclass, asdict
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.enums import ParseMode

# ============ КОНФИГ ============
TOKEN = '8500266882:AAHTGpChTbUZ-CJ-GydZAWmlGBlshiK5UNk'
ADMIN_USERNAMES = {'asd123dad', 'venter8'}
DEFAULT_EMOJI_ID = '5285430309720966085'
MAX_ADDITIONS = 5
EMOJI_PER_PAGE = 10

# ============ БАЗА ДАННЫХ (SQLite в памяти) ============
class Database:
    def __init__(self):
        self.conn = sqlite3.connect(':memory:', check_same_thread=False)
        self.cursor = self.conn.cursor()
        self.init_tables()
        self.init_default_data()
    
    def init_tables(self):
        self.cursor.execute('''
            CREATE TABLE emoji_catalog (
                emoji_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                added_by INTEGER,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        self.cursor.execute('''
            CREATE TABLE approvers (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                added_by INTEGER,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        self.conn.commit()
    
    def init_default_data(self):
        # Начальный каталог - только один эмодзи
        self.cursor.execute(
            'INSERT OR IGNORE INTO emoji_catalog (emoji_id, name, added_by) VALUES (?, ?, ?)',
            (DEFAULT_EMOJI_ID, 'Стандартный 👍', 0)
        )
        self.conn.commit()
    
    def get_catalog(self) -> List[dict]:
        self.cursor.execute('SELECT emoji_id, name FROM emoji_catalog ORDER BY added_at')
        return [{'emoji_id': row[0], 'name': row[1]} for row in self.cursor.fetchall()]
    
    def add_emoji(self, emoji_id: str, name: str, user_id: int):
        self.cursor.execute(
            'INSERT OR REPLACE INTO emoji_catalog (emoji_id, name, added_by) VALUES (?, ?, ?)',
            (emoji_id, name, user_id)
        )
        self.conn.commit()
    
    def remove_emoji(self, emoji_id: str):
        self.cursor.execute('DELETE FROM emoji_catalog WHERE emoji_id = ?', (emoji_id,))
        self.conn.commit()
    
    def is_approver(self, user_id: int) -> bool:
        self.cursor.execute('SELECT 1 FROM approvers WHERE user_id = ?', (user_id,))
        return self.cursor.fetchone() is not None
    
    def add_approver(self, user_id: int, username: str, added_by: int):
        self.cursor.execute(
            'INSERT OR REPLACE INTO approvers (user_id, username, added_by) VALUES (?, ?, ?)',
            (user_id, username, added_by)
        )
        self.conn.commit()
    
    def remove_approver(self, user_id: int):
        self.cursor.execute('DELETE FROM approvers WHERE user_id = ?', (user_id,))
        self.conn.commit()
    
    def get_approvers(self) -> List[dict]:
        self.cursor.execute('SELECT user_id, username FROM approvers')
        return [{'user_id': row[0], 'username': row[1]} for row in self.cursor.fetchall()]
    
    def export_data(self) -> str:
        catalog = self.get_catalog()
        approvers = self.get_approvers()
        data = {'catalog': catalog, 'approvers': approvers}
        json_str = json.dumps(data, ensure_ascii=False)
        return f"EMOJI_BACKUP:{base64.b64encode(json_str.encode()).decode()}"
    
    def import_data(self, data_str: str) -> bool:
        try:
            if not data_str.startswith('EMOJI_BACKUP:'):
                return False
            encoded = data_str.split(':', 1)[1]
            decoded = base64.b64decode(encoded).decode()
            data = json.loads(decoded)
            
            # Очищаем и восстанавливаем
            self.cursor.execute('DELETE FROM emoji_catalog')
            self.cursor.execute('DELETE FROM approvers')
            
            for item in data.get('catalog', []):
                self.cursor.execute(
                    'INSERT INTO emoji_catalog (emoji_id, name, added_by) VALUES (?, ?, ?)',
                    (item['emoji_id'], item['name'], 0)
                )
            
            for appr in data.get('approvers', []):
                self.cursor.execute(
                    'INSERT INTO approvers (user_id, username, added_by) VALUES (?, ?, ?)',
                    (appr['user_id'], appr['username'], 0)
                )
            
            self.conn.commit()
            return True
        except Exception:
            return False

db = Database()

# ============ СЕССИИ ============
@dataclass
class Part:
    text: str
    emoji_id: str  # ID премиум эмодзи для этой части

@dataclass
class Session:
    parts: List[Part]
    waiting_for_input: bool = False
    current_part_index: int = 0  # Для какой части выбираем эмодзи
    selecting_emoji: bool = False
    emoji_page: int = 0
    waiting_for_emoji_name: bool = False
    pending_emoji_id: Optional[str] = None

sessions: Dict[int, Session] = {}

def get_session(user_id: int) -> Session:
    if user_id not in sessions:
        sessions[user_id] = Session(parts=[])
    return sessions[user_id]

def clear_session(user_id: int):
    if user_id in sessions:
        del sessions[user_id]

# ============ БОТ ============
bot = Bot(token=TOKEN)
dp = Dispatcher()

def is_admin(username: Optional[str]) -> bool:
    return username is not None and username.lower() in ADMIN_USERNAMES

def format_emoji_html(emoji_id: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">👍</tg-emoji>'

def build_final_text(session: Session) -> str:
    if not session.parts:
        return '...'
    result = []
    for part in session.parts:
        result.append(part.text)
        result.append(format_emoji_html(part.emoji_id))
    return ' '.join(result)

def build_main_keyboard(session: Session) -> InlineKeyboardMarkup:
    buttons = []
    
    # Кнопки для каждой части (вкл/выкл эмодзи + сменить эмодзи)
    for i, part in enumerate(session.parts):
        label = 'Основной' if i == 0 else f'Добавка {i}'
        # Кнопка вкл/выкл (всегда вкл в новой версии, но оставим для совместимости)
        buttons.append([InlineKeyboardButton(
            text=f'🎭 Сменить эмодзи {label}',
            callback_data=f'change_emoji_{i}'
        )])
    
    # Кнопка добавить
    if len(session.parts) <= MAX_ADDITIONS and not session.waiting_for_input:
        buttons.append([InlineKeyboardButton(
            text=f'➕ Добавить ({len(session.parts) - 1}/{MAX_ADDITIONS})',
            callback_data='add_part'
        )])
    
    # Отмена если ждём ввод
    if session.waiting_for_input:
        buttons.append([InlineKeyboardButton(
            text='❌ Отмена',
            callback_data='cancel_input'
        )])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def build_emoji_keyboard(page: int, total_pages: int) -> InlineKeyboardMarkup:
    catalog = db.get_catalog()
    start = page * EMOJI_PER_PAGE
    end = start + EMOJI_PER_PAGE
    page_items = catalog[start:end]
    
    buttons = []
    
    # Кнопки выбора (5 рядов по 2)
    row = []
    for i, item in enumerate(page_items):
        row.append(InlineKeyboardButton(
            text=f'Выбрать {i+1}',
            callback_data=f'select_emoji_{start + i}'
        ))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    
    # Навигация
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text='←', callback_data=f'emoji_page_{page-1}'))
    else:
        nav_buttons.append(InlineKeyboardButton(text='•', callback_data='noop'))
    
    nav_buttons.append(InlineKeyboardButton(text=f'{page+1}/{total_pages}', callback_data='noop'))
    
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text='→', callback_data=f'emoji_page_{page+1}'))
    else:
        nav_buttons.append(InlineKeyboardButton(text='•', callback_data='noop'))
    
    buttons.append(nav_buttons)
    buttons.append([InlineKeyboardButton(text='❌ Закрыть', callback_data='close_emoji_selector')])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def build_emoji_preview_text(page: int) -> str:
    catalog = db.get_catalog()
    start = page * EMOJI_PER_PAGE
    end = start + EMOJI_PER_PAGE
    page_items = catalog[start:end]
    
    lines = ['<b>Выберите эмодзи:</b>\n']
    for i, item in enumerate(page_items, 1):
        emoji_html = format_emoji_html(item['emoji_id'])
        lines.append(f'{i}. {emoji_html} {item["name"]}')
    
    return '\n'.join(lines)

# ============ ХЕНДЛЕРЫ ============

@dp.message(Command('start'))
async def cmd_start(message: Message):
    await message.answer(
        '👋 Отправьте текст, чтобы создать сообщение с премиум-эмодзи.\n\n'
        'Используйте кнопки для добавления частей и смены эмодзи.'
    )

@dp.message(Command('upuser'))
async def cmd_upuser(message: Message):
    if not is_admin(message.from_user.username):
        return
    
    approvers = db.get_approvers()
    text = '<b>👑 Панель администратора</b>\n\n<b>Аппруверы:</b>\n'
    
    if not approvers:
        text += 'Нет аппруверов\n'
    else:
        for appr in approvers:
            text += f'• @{appr["username"]} (ID: {appr["user_id"]})\n'
    
    buttons = [
        [InlineKeyboardButton(text='➕ Добавить аппрувера', callback_data='admin_add_approver')],
        [InlineKeyboardButton(text='➖ Удалить аппрувера', callback_data='admin_remove_approver')]
    ]
    
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.message(Command('up'))
async def cmd_up(message: Message):
    if not is_admin(message.from_user.username):
        return
    backup = db.export_data()
    await message.answer(f'<code>{backup}</code>\n\nСкопируйте эту строку для резервного копирования.')

@dp.message(Command('down'))
async def cmd_down(message: Message):
    if not is_admin(message.from_user.username):
        return
    await message.answer('Отправьте строку бэкапа для восстановления.')

@dp.message(F.text.startswith('EMOJI_BACKUP:'))
async def handle_backup_import(message: Message):
    if not is_admin(message.from_user.username):
        return
    if db.import_data(message.text):
        await message.answer('✅ Каталог и аппруверы восстановлены!')
    else:
        await message.answer('❌ Ошибка импорта. Проверьте строку.')

# ============ ОСНОВНАЯ ЛОГИКА ============

@dp.message(F.text & ~F.text.startswith('/'))
async def handle_text(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username
    text = message.text
    
    session = get_session(user_id)
    
    # Если ждём название для нового эмодзи (аппрувер)
    if session.waiting_for_emoji_name and session.pending_emoji_id:
        db.add_emoji(session.pending_emoji_id, text, user_id)
        session.waiting_for_emoji_name = False
        session.pending_emoji_id = None
        await message.answer(f'✅ Эмодзи добавлен в каталог как "{text}"')
        return
    
    # Если ждём ввод для добавки
    if session.waiting_for_input:
        session.parts.append(Part(text=text, emoji_id=DEFAULT_EMOJI_ID))
        session.waiting_for_input = False
        
        result = await message.answer(
            build_final_text(session),
            reply_markup=build_main_keyboard(session),
            parse_mode=ParseMode.HTML
        )
        session.last_message_id = result.message_id
        return
    
    # Новое сообщение - сбрасываем всё
    session.parts = [Part(text=text, emoji_id=DEFAULT_EMOJI_ID)]
    session.waiting_for_input = False
    session.selecting_emoji = False
    
    result = await message.answer(
        build_final_text(session),
        reply_markup=build_main_keyboard(session),
        parse_mode=ParseMode.HTML
    )
    session.last_message_id = result.message_id

@dp.message(F.entities)
async def handle_entities(message: Message):
    """Обработка премиум-эмодзи"""
    user_id = message.from_user.id
    username = message.from_user.username
    
    # Ищем кастомные эмодзи в сообщении
    custom_emojis = []
    if message.entities:
        for entity in message.entities:
            if entity.type == 'custom_emoji':
                custom_emojis.append(entity.custom_emoji_id)
    
    if not custom_emojis:
        return
    
    emoji_id = custom_emojis[0]  # Берём первый
    
    # Если аппрувер - предлагаем добавить в каталог
    if db.is_approver(user_id) or is_admin(username):
        session = get_session(user_id)
        session.pending_emoji_id = emoji_id
        session.waiting_for_emoji_name = True
        
        await message.answer(
            f'🎭 <b>Премиум-эмодзи</b>\n'
            f'ID: <code>{emoji_id}</code>\n\n'
            f'Вы можете добавить его в каталог. Отправьте название для этого эмодзи (или /cancel для отмены):'
        )
    else:
        # Обычный пользователь - просто показываем ID
        await message.answer(f'🎭 ID премиум-эмодзи: <code>{emoji_id}</code>')

@dp.callback_query(F.data == 'noop')
async def noop(callback: CallbackQuery):
    await callback.answer()

@dp.callback_query(F.data == 'add_part')
async def add_part(callback: CallbackQuery):
    user_id = callback.from_user.id
    session = get_session(user_id)
    
    if len(session.parts) > MAX_ADDITIONS:
        await callback.answer('Лимит добавок!')
        return
    
    session.waiting_for_input = True
    
    await callback.message.edit_text(
        build_final_text(session) + '\n\n✏️ Введите текст для добавки:',
        reply_markup=build_main_keyboard(session),
        parse_mode=ParseMode.HTML
    )
    await callback.answer('Введите текст')

@dp.callback_query(F.data == 'cancel_input')
async def cancel_input(callback: CallbackQuery):
    user_id = callback.from_user.id
    session = get_session(user_id)
    
    session.waiting_for_input = False
    
    await callback.message.edit_text(
        build_final_text(session),
        reply_markup=build_main_keyboard(session),
        parse_mode=ParseMode.HTML
    )
    await callback.answer('Отменено')

@dp.callback_query(F.data.startswith('change_emoji_'))
async def change_emoji(callback: CallbackQuery):
    user_id = callback.from_user.id
    session = get_session(user_id)
    
    part_idx = int(callback.data.split('_')[2])
    session.current_part_index = part_idx
    session.selecting_emoji = True
    session.emoji_page = 0
    
    catalog = db.get_catalog()
    total_pages = (len(catalog) + EMOJI_PER_PAGE - 1) // EMOJI_PER_PAGE
    
    await callback.message.edit_text(
        build_emoji_preview_text(0),
        reply_markup=build_emoji_keyboard(0, total_pages),
        parse_mode=ParseMode.HTML
    )
    await callback.answer()

@dp.callback_query(F.data.startswith('emoji_page_'))
async def emoji_page(callback: CallbackQuery):
    user_id = callback.from_user.id
    session = get_session(user_id)
    
    page = int(callback.data.split('_')[2])
    session.emoji_page = page
    
    catalog = db.get_catalog()
    total_pages = (len(catalog) + EMOJI_PER_PAGE - 1) // EMOJI_PER_PAGE
    
    await callback.message.edit_text(
        build_emoji_preview_text(page),
        reply_markup=build_emoji_keyboard(page, total_pages),
        parse_mode=ParseMode.HTML
    )
    await callback.answer()

@dp.callback_query(F.data.startswith('select_emoji_'))
async def select_emoji(callback: CallbackQuery):
    user_id = callback.from_user.id
    session = get_session(user_id)
    
    emoji_idx = int(callback.data.split('_')[2])
    catalog = db.get_catalog()
    
    if emoji_idx >= len(catalog):
        await callback.answer('Ошибка!')
        return
    
    selected = catalog[emoji_idx]
    part_idx = session.current_part_index
    
    if part_idx < len(session.parts):
        session.parts[part_idx].emoji_id = selected['emoji_id']
    
    session.selecting_emoji = False
    
    await callback.message.edit_text(
        build_final_text(session),
        reply_markup=build_main_keyboard(session),
        parse_mode=ParseMode.HTML
    )
    await callback.answer(f'Выбрано: {selected["name"]}')

@dp.callback_query(F.data == 'close_emoji_selector')
async def close_emoji_selector(callback: CallbackQuery):
    user_id = callback.from_user.id
    session = get_session(user_id)
    
    session.selecting_emoji = False
    
    await callback.message.edit_text(
        build_final_text(session),
        reply_markup=build_main_keyboard(session),
        parse_mode=ParseMode.HTML
    )
    await callback.answer()

# ============ АДМИН-ПАНЕЛЬ ============

@dp.callback_query(F.data == 'admin_add_approver')
async def admin_add_approver(callback: CallbackQuery):
    if not is_admin(callback.from_user.username):
        return
    await callback.message.answer('Отправьте username пользователя (без @) для добавления как аппрувера:')
    await callback.answer()

@dp.callback_query(F.data == 'admin_remove_approver')
async def admin_remove_approver(callback: CallbackQuery):
    if not is_admin(callback.from_user.username):
        return
    await callback.message.answer('Отправьте ID пользователя для удаления:')
    await callback.answer()

async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
