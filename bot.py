import asyncio
import base64
import json
import logging
import sqlite3
from datetime import datetime
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ─────────────────────────────────────────────
#  Конфиг
# ─────────────────────────────────────────────
TOKEN = "8500266882:AAHTGpChTbUZ-CJ-GydZAWmlGBlshiK5UNk"
ADMINS = {"asd123dad", "venter8"}
DEFAULT_EMOJI_ID = "5285430309720966085"
DEFAULT_EMOJI_NAME = "Стандартный"
MAX_ADDITIONS = 5
NO_EMOJI = "__NO_EMOJI__"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  База данных
# ─────────────────────────────────────────────
con = sqlite3.connect(":memory:", check_same_thread=False)
con.row_factory = sqlite3.Row

def db_init() -> None:
    cur = con.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS emoji_catalog (
            emoji_id   TEXT PRIMARY KEY,
            name       TEXT NOT NULL,
            added_by   TEXT,
            added_at   TEXT
        );
        CREATE TABLE IF NOT EXISTS approvers (
            user_id    INTEGER PRIMARY KEY,
            username   TEXT,
            added_by   TEXT,
            added_at   TEXT
        );
    """)

    default_emojis = [
        ("5285430309720966085", "Стандартный #1"),
        ("5310169226856644648", "Стандартный #2"),
        ("5310076249404621168", "Стандартный #3"),
        ("5285032475490273112", "Стандартный #4"),
    ]

    for emoji_id, name in default_emojis:
        cur.execute(
            "INSERT OR IGNORE INTO emoji_catalog VALUES (?,?,?,?)",
            (emoji_id, name, "system", datetime.utcnow().isoformat()),
        )

    con.commit()

def db_get_catalog() -> list:
    return con.execute("SELECT * FROM emoji_catalog ORDER BY added_at").fetchall()

def db_add_emoji(emoji_id: str, name: str, added_by: str) -> None:
    con.execute(
        "INSERT OR REPLACE INTO emoji_catalog VALUES (?,?,?,?)",
        (emoji_id, name, added_by, datetime.utcnow().isoformat()),
    )
    con.commit()

def db_get_approvers() -> list:
    return con.execute("SELECT * FROM approvers ORDER BY added_at").fetchall()

def db_add_approver(user_id: int, username: str, added_by: str) -> None:
    con.execute(
        "INSERT OR REPLACE INTO approvers VALUES (?,?,?,?)",
        (user_id, username, added_by, datetime.utcnow().isoformat()),
    )
    con.commit()

def db_remove_approver(user_id: int) -> bool:
    cur = con.execute("DELETE FROM approvers WHERE user_id=?", (user_id,))
    con.commit()
    return cur.rowcount > 0

def db_is_approver(user_id: int) -> bool:
    row = con.execute("SELECT 1 FROM approvers WHERE user_id=?", (user_id,)).fetchone()
    return row is not None

def db_export() -> str:
    data = {
        "emoji_catalog": [dict(r) for r in db_get_catalog()],
        "approvers":     [dict(r) for r in db_get_approvers()],
    }
    return "EMOJI_BACKUP:" + base64.b64encode(json.dumps(data).encode()).decode()

def db_import(raw: str) -> bool:
    try:
        if not raw.startswith("EMOJI_BACKUP:"):
            return False
        data = json.loads(base64.b64decode(raw[13:]).decode())
        cur = con.cursor()
        for row in data.get("emoji_catalog", []):
            cur.execute(
                "INSERT OR REPLACE INTO emoji_catalog VALUES (?,?,?,?)",
                (row["emoji_id"], row["name"], row["added_by"], row["added_at"]),
            )
        for row in data.get("approvers", []):
            cur.execute(
                "INSERT OR REPLACE INTO approvers VALUES (?,?,?,?)",
                (row["user_id"], row["username"], row["added_by"], row["added_at"]),
            )
        con.commit()
        return True
    except Exception as e:
        log.error("import error: %s", e)
        return False

# ─────────────────────────────────────────────
#  Сессии
# ─────────────────────────────────────────────
class Part:
    __slots__ = ("text", "emoji_id")
    def __init__(self, text: str, emoji_id: str = DEFAULT_EMOJI_ID):
        self.text = text
        self.emoji_id = emoji_id

class Session:
    def __init__(self):
        self.parts: list[Part] = []
        self.waiting_for_input: bool = False
        self.current_part_index: int = 0
        self.selecting_emoji: bool = False
        self.emoji_page: int = 0
        self.waiting_for_emoji_name: bool = False
        self.pending_emoji_id: Optional[str] = None
        self.last_message_id: Optional[int] = None
        self.picker_message_id: Optional[int] = None

sessions: dict[int, Session] = {}

def get_session(user_id: int) -> Session:
    if user_id not in sessions:
        sessions[user_id] = Session()
    return sessions[user_id]

# ─────────────────────────────────────────────
#  Глобальные состояния admin
# ─────────────────────────────────────────────
admin_waiting_add:    set[int] = set()
admin_waiting_remove: set[int] = set()
admin_waiting_down:   set[int] = set()

# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────
def is_admin(username: Optional[str]) -> bool:
    if not username:
        return False
    clean = username.lstrip("@").lower()
    return clean in {a.lower() for a in ADMINS}

def tg_emoji_tag(emoji_id: str, placeholder: str = "⭐") -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{placeholder}</tg-emoji>'

def build_final_text(session: Session) -> str:
    chunks: list[str] = []
    for part in session.parts:
        chunks.append(part.text)
        if part.emoji_id != NO_EMOJI:
            chunks.append(tg_emoji_tag(part.emoji_id))
    return " ".join(chunks) if chunks else "..."

# ─────────────────────────────────────────────
#  Клавиатуры
# ─────────────────────────────────────────────
def build_editor_keyboard(session: Session) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i, part in enumerate(session.parts):
        label = "Основной" if i == 0 else f"Добавка {i}"
        has_emoji = part.emoji_id != NO_EMOJI
        emoji_icon = "✅" if has_emoji else "❌"
        rows.append([
            InlineKeyboardButton(
                text=f"{emoji_icon} {label}",
                callback_data=f"toggle_{i}",
            ),
            InlineKeyboardButton(
                text="🎭 Сменить",
                callback_data=f"pick_emoji_{i}",
            ),
        ])
    extras = len(session.parts) - 1
    if extras < MAX_ADDITIONS and not session.waiting_for_input:
        rows.append([
            InlineKeyboardButton(
                text=f"➕ Добавить ({extras}/{MAX_ADDITIONS})",
                callback_data="add",
            )
        ])
    if session.waiting_for_input:
        rows.append([
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def build_picker_keyboard(page: int) -> InlineKeyboardMarkup:
    catalog = db_get_catalog()
    total = len(catalog)
    per_page = 10
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    items = catalog[start: start + per_page]
    rows: list[list[InlineKeyboardButton]] = []
    rows.append([InlineKeyboardButton(text="❌ Без эмодзи", callback_data="ep_none")])
    pair: list[InlineKeyboardButton] = []
    for local_idx, _ in enumerate(items):
        num = local_idx + 1
        btn = InlineKeyboardButton(
            text=f"Выбрать {num}",
            callback_data=f"ep_sel_{start + local_idx}",
        )
        pair.append(btn)
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    nav: list[InlineKeyboardButton] = []
    nav.append(
        InlineKeyboardButton(
            text="←" if page > 0 else "·",
            callback_data=f"ep_page_{page - 1}" if page > 0 else "ep_noop",
        )
    )
    nav.append(
        InlineKeyboardButton(
            text=f"{page + 1}/{total_pages}",
            callback_data="ep_noop",
        )
    )
    nav.append(
        InlineKeyboardButton(
            text="→" if page < total_pages - 1 else "·",
            callback_data=f"ep_page_{page + 1}" if page < total_pages - 1 else "ep_noop",
        )
    )
    rows.append(nav)
    rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="ep_close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def build_picker_text(page: int) -> str:
    catalog = db_get_catalog()
    per_page = 10
    total_pages = max(1, (len(catalog) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    items = catalog[start: start + per_page]
    lines = ["🎭 <b>Выберите эмодзи:</b>\n"]
    for local_idx, row in enumerate(items):
        num = local_idx + 1
        preview = tg_emoji_tag(row["emoji_id"])
        lines.append(f"{num}. {preview} {row['name']}")
    return "\n".join(lines)

def build_upuser_text() -> str:
    approvers = db_get_approvers()
    lines = ["👑 <b>Панель администратора</b>\n", "<b>Аппруверы:</b>"]
    if approvers:
        for a in approvers:
            uname = f"@{a['username']}" if a["username"] else "—"
            lines.append(f"• {uname} (ID: <code>{a['user_id']}</code>)")
    else:
        lines.append("• Список пуст")
    return "\n".join(lines)

def build_upuser_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить аппрувера", callback_data="adm_add")],
        [InlineKeyboardButton(text="➖ Удалить аппрувера",  callback_data="adm_remove")],
    ])

# ─────────────────────────────────────────────
#  Bot + Dispatcher
# ─────────────────────────────────────────────
bot = Bot(token=TOKEN)
dp  = Dispatcher()

# ─────────────────────────────────────────────
#  Вспомогательная: обновление редактора
# ─────────────────────────────────────────────
async def _refresh_editor(chat_id: int, session: Session) -> None:
    text   = build_final_text(session)
    markup = build_editor_keyboard(session)
    if session.waiting_for_input:
        text += "\n\n✏️ <i>Введи текст для добавки:</i>"
    if session.last_message_id:
        try:
            await bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=session.last_message_id,
                reply_markup=markup,
                parse_mode="HTML",
            )
            return
        except Exception:
            pass
    sent = await bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")
    session.last_message_id = sent.message_id

# ─────────────────────────────────────────────
#  Команды
# ─────────────────────────────────────────────
@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 Привет! Отправь любой текст — и я склею его с премиум-эмодзи.\n"
        "Можно добавлять до 5 дополнительных частей.",
        parse_mode="HTML",
    )

@dp.message(Command("upuser"))
async def cmd_upuser(message: Message) -> None:
    log.info(
        "upuser: user=%s username=%r check=%s",
        message.from_user.id,
        message.from_user.username,
        is_admin(message.from_user.username),
    )
    if not is_admin(message.from_user.username):
        await message.answer("⛔ Нет доступа.")
        return
    await message.answer(
        build_upuser_text(),
        reply_markup=build_upuser_keyboard(),
        parse_mode="HTML",
    )

@dp.message(Command("up"))
async def cmd_up(message: Message) -> None:
    if not is_admin(message.from_user.username):
        await message.answer("⛔ Нет доступа.")
        return
    backup = db_export()
    await message.answer(f"📦 <b>Бэкап:</b>\n<code>{backup}</code>", parse_mode="HTML")

@dp.message(Command("down"))
async def cmd_down(message: Message) -> None:
    if not is_admin(message.from_user.username):
        await message.answer("⛔ Нет доступа.")
        return
    admin_waiting_down.add(message.from_user.id)
    await message.answer(
        "📥 Отправь строку бэкапа (начинается с <code>EMOJI_BACKUP:</code>):",
        parse_mode="HTML",
    )

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    uid = message.from_user.id
    admin_waiting_add.discard(uid)
    admin_waiting_remove.discard(uid)
    admin_waiting_down.discard(uid)
    session = get_session(uid)
    if session.waiting_for_input:
        session.waiting_for_input = False
        if session.parts:
            await _refresh_editor(message.chat.id, session)
    if session.waiting_for_emoji_name:
        session.waiting_for_emoji_name = False
        session.pending_emoji_id = None
    await message.answer("❌ Отменено.")

# ─────────────────────────────────────────────
#  Хендлер: сообщения с premium emoji
# ─────────────────────────────────────────────
def _has_custom_emoji(message: Message) -> bool:
    if not message.entities:
        return False
    return any(e.type == "custom_emoji" for e in message.entities)

@dp.message(F.text, F.func(_has_custom_emoji))
async def on_premium_emoji(message: Message) -> None:
    uid = message.from_user.id
    session = get_session(uid)

    custom_ids = [
        e.custom_emoji_id
        for e in message.entities
        if e.type == "custom_emoji" and e.custom_emoji_id
    ]
    if not custom_ids:
        return

    emoji_id = custom_ids[0]

    if db_is_approver(uid):
        if session.waiting_for_emoji_name:
            await message.answer("⏳ Сначала введи название для предыдущего эмодзи.")
            return
        session.pending_emoji_id = emoji_id
        session.waiting_for_emoji_name = True
        await message.answer(
            f"🎭 Эмодзи получен: {tg_emoji_tag(emoji_id)}\n"
            f"ID: <code>{emoji_id}</code>\n\n"
            f"Введи название для каталога:",
            parse_mode="HTML",
        )
        return

    await message.answer(f"ID: <code>{emoji_id}</code>", parse_mode="HTML")

# ─────────────────────────────────────────────
#  Хендлер: обычный текст
# ─────────────────────────────────────────────
@dp.message(F.text)
async def on_text(message: Message) -> None:
    uid      = message.from_user.id
    text     = message.text.strip()
    username = message.from_user.username or ""
    session  = get_session(uid)

    # 1. Ожидание строки бэкапа
    if uid in admin_waiting_down:
        admin_waiting_down.discard(uid)
        if db_import(text):
            await message.answer("✅ Бэкап восстановлен!")
        else:
            await message.answer(
                "❌ Ошибка формата. Начало должно быть <code>EMOJI_BACKUP:</code>",
                parse_mode="HTML",
            )
        return

    # 2. Ожидание добавления аппрувера
    if uid in admin_waiting_add:
        parts_input = text.strip().split()
        try:
            target_id = int(parts_input[0])
        except (ValueError, IndexError):
            await message.answer(
                "❌ Неверный формат.\n\n"
                "Введи ID и username через пробел:\n"
                "<code>123456789 username</code>\n\n"
                "Или только ID:\n"
                "<code>123456789</code>",
                parse_mode="HTML",
            )
            return
        target_username = parts_input[1].lstrip("@") if len(parts_input) > 1 else ""
        admin_waiting_add.discard(uid)
        db_add_approver(target_id, target_username, username or str(uid))
        uname_display = f"@{target_username}" if target_username else "без username"
        await message.answer(
            f"✅ Аппрувер добавлен!\n"
            f"ID: <code>{target_id}</code>\n"
            f"Username: {uname_display}",
            parse_mode="HTML",
        )
        await message.answer(
            build_upuser_text(),
            reply_markup=build_upuser_keyboard(),
            parse_mode="HTML",
        )
        return

    # 3. Ожидание ID для удаления
    if uid in admin_waiting_remove:
        try:
            target_id = int(text.strip())
        except ValueError:
            await message.answer(
                "❌ Нужен числовой ID.\n"
                "Пример: <code>123456789</code>",
                parse_mode="HTML",
            )
            return
        admin_waiting_remove.discard(uid)
        if db_remove_approver(target_id):
            await message.answer(
                f"✅ Аппрувер <code>{target_id}</code> удалён.",
                parse_mode="HTML",
            )
        else:
            await message.answer(
                f"❌ Аппрувер с ID <code>{target_id}</code> не найден.",
                parse_mode="HTML",
            )
        await message.answer(
            build_upuser_text(),
            reply_markup=build_upuser_keyboard(),
            parse_mode="HTML",
        )
        return

    # 4. Ожидание названия эмодзи (аппрувер)
    if session.waiting_for_emoji_name and session.pending_emoji_id:
        session.waiting_for_emoji_name = False
        emoji_id = session.pending_emoji_id
        session.pending_emoji_id = None
        db_add_emoji(emoji_id, text, username)
        await message.answer(
            f"✅ Эмодзи {tg_emoji_tag(emoji_id)} <b>{text}</b> добавлен в каталог!",
            parse_mode="HTML",
        )
        return

    # 5. Ожидание текста добавки
    if session.waiting_for_input:
        session.parts.append(Part(text=text))
        session.waiting_for_input = False
        await _refresh_editor(message.chat.id, session)
        return

    # 6. Новое сообщение → новый редактор
    session.parts = [Part(text=text)]
    session.waiting_for_input = False
    session.selecting_emoji   = False
    session.last_message_id   = None
    session.picker_message_id = None

    sent = await message.answer(
        build_final_text(session),
        reply_markup=build_editor_keyboard(session),
        parse_mode="HTML",
    )
    session.last_message_id = sent.message_id

# ─────────────────────────────────────────────
#  Callbacks
# ─────────────────────────────────────────────
@dp.callback_query()
async def on_callback(query: CallbackQuery) -> None:
    uid     = query.from_user.id
    data    = query.data
    chat_id = query.message.chat.id
    session = get_session(uid)

    # ── Админ-панель ─────────────────────────────────────────────────────────
    if data == "adm_add":
        if not is_admin(query.from_user.username):
            await query.answer("⛔ Нет доступа.")
            return
        admin_waiting_add.add(uid)
        await query.answer()
        await query.message.answer(
            "👤 Введи ID аппрувера и username через пробел:\n"
            "<code>123456789 username</code>\n\n"
            "Или только ID:\n"
            "<code>123456789</code>",
            parse_mode="HTML",
        )
        return

    if data == "adm_remove":
        if not is_admin(query.from_user.username):
            await query.answer("⛔ Нет доступа.")
            return
        admin_waiting_remove.add(uid)
        await query.answer()
        await query.message.answer(
            "🗑 Введи числовой <b>user_id</b> аппрувера для удаления:\n"
            "Пример: <code>123456789</code>",
            parse_mode="HTML",
        )
        return

    # ── Пикер: навигация ─────────────────────────────────────────────────────
    if data.startswith("ep_page_"):
        new_page = int(data.split("_")[-1])
        session.emoji_page = new_page
        await query.answer()
        await query.message.edit_text(
            build_picker_text(new_page),
            reply_markup=build_picker_keyboard(new_page),
            parse_mode="HTML",
        )
        return

    if data == "ep_noop":
        await query.answer()
        return

    if data == "ep_none":
        idx = session.current_part_index
        if 0 <= idx < len(session.parts):
            session.parts[idx].emoji_id = NO_EMOJI
        session.selecting_emoji = False
        await query.answer("❌ Эмодзи убран")
        await query.message.delete()
        session.picker_message_id = None
        await _refresh_editor(chat_id, session)
        return

    if data.startswith("ep_sel_"):
        catalog = db_get_catalog()
        cat_idx = int(data.split("_")[-1])
        if 0 <= cat_idx < len(catalog):
            emoji_id = catalog[cat_idx]["emoji_id"]
            name     = catalog[cat_idx]["name"]
            idx = session.current_part_index
            if 0 <= idx < len(session.parts):
                session.parts[idx].emoji_id = emoji_id
            session.selecting_emoji = False
            await query.answer(f"✅ Выбран: {name}")
        else:
            await query.answer("❌ Не найдено")
        await query.message.delete()
        session.picker_message_id = None
        await _refresh_editor(chat_id, session)
        return

    if data == "ep_close":
        session.selecting_emoji = False
        await query.answer("Закрыто")
        await query.message.delete()
        session.picker_message_id = None
        await _refresh_editor(chat_id, session)
        return

    # ── Редактор ─────────────────────────────────────────────────────────────
    if data == "add":
        extras = len(session.parts) - 1
        if extras >= MAX_ADDITIONS:
            await query.answer("⚠️ Лимит 5 добавок!")
            return
        session.waiting_for_input = True
        await query.answer("✏️ Введи текст (или /cancel)")
        await _refresh_editor(chat_id, session)
        return

    if data == "cancel":
        session.waiting_for_input = False
        await query.answer("❌ Отменено")
        await _refresh_editor(chat_id, session)
        return

    if data.startswith("toggle_"):
        idx = int(data.split("_")[1])
        if 0 <= idx < len(session.parts):
            if session.parts[idx].emoji_id == NO_EMOJI:
                session.parts[idx].emoji_id = DEFAULT_EMOJI_ID
                await query.answer("✅ Эмодзи включён")
            else:
                session.parts[idx].emoji_id = NO_EMOJI
                await query.answer("❌ Эмодзи выключен")
        await _refresh_editor(chat_id, session)
        return

    if data.startswith("pick_emoji_"):
        idx = int(data.split("_")[-1])
        session.current_part_index = idx
        session.selecting_emoji    = True
        session.emoji_page         = 0
        await query.answer()
        sent = await bot.send_message(
            chat_id,
            build_picker_text(0),
            reply_markup=build_picker_keyboard(0),
            parse_mode="HTML",
        )
        session.picker_message_id = sent.message_id
        return

    await query.answer()

# ─────────────────────────────────────────────
#  Точка входа
# ─────────────────────────────────────────────
async def main() -> None:
    db_init()
    log.info("Bot started")
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
