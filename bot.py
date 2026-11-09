import asyncio
import logging
import os
import signal
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, ChatMemberUpdated, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, User

# ─── Конфигурация ────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
GROUP_ID = os.getenv("GROUP_ID")

if GROUP_ID:
    try:
        GROUP_ID = int(GROUP_ID)
    except ValueError:
        raise RuntimeError("GROUP_ID must be an integer (e.g., -1001234567890)")

POLLING_LIMIT_SECONDS = 5 * 60 * 60 + 50 * 60  # 5 часов 50 минут

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "bot.db"
OFFSET_PATH = DATA_DIR / "offset.txt"

# ─── Логирование ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("referral_bot")

# ─── Работа с БД ─────────────────────────────────────────────────────────────
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id      INTEGER PRIMARY KEY,
                referrer_id  INTEGER,
                nft_username TEXT,
                state        TEXT,
                first_seen   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS invite_links (
                invite_link  TEXT PRIMARY KEY,
                referrer_id  INTEGER
            );
        """)
        await db.commit()

async def get_referral_count(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (user_id,))
        row = await cursor.fetchone()
        return row[0] if row else 0

async def save_user_if_new(user_id: int, referrer_id: int | None = None) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
        if await cursor.fetchone():
            return False
        await db.execute("INSERT INTO users (user_id, referrer_id) VALUES (?, ?)", (user_id, referrer_id))
        await db.commit()
        return True

async def save_invite_link(link: str, referrer_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO invite_links (invite_link, referrer_id) VALUES (?, ?)", (link, referrer_id))
        await db.commit()

async def get_referrer_by_link(link: str) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT referrer_id FROM invite_links WHERE invite_link = ?", (link,))
        row = await cursor.fetchone()
        return row[0] if row else None

async def get_user_nft_username(user_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT nft_username FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return row[0] if row else None

async def set_nft_username_and_state(user_id: int, username: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET nft_username = ?, state = NULL WHERE user_id = ?", (username, user_id))
        await db.commit()

async def set_user_state(user_id: int, state: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET state = ? WHERE user_id = ?", (state, user_id))
        await db.commit()

async def get_user_state(user_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT state FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return row[0] if row else None

# ─── Offset ──────────────────────────────────────────────────────────────────
def load_offset() -> int | None:
    if OFFSET_PATH.exists():
        try:
            return int(OFFSET_PATH.read_text().strip())
        except (ValueError, OSError):
            return None
    return None

def save_offset(offset: int):
    OFFSET_PATH.write_text(str(offset))

# ─── Роутер и Клавиатура ─────────────────────────────────────────────────────
router = Router()

def get_ref_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Создать реф ссылку", callback_data="create_ref_link")]
    ])

async def _generate_group_invite(bot: Bot, user: User) -> str:
    if not GROUP_ID:
        return "Ошибка: GROUP_ID не настроен в секретах."
    
    display_name = user.username or user.first_name or str(user.id)
    safe_name = "".join(c for c in display_name if c.isalnum() or c in " _-")[:32].strip()
    if not safe_name:
        safe_name = f"user_{user.id}"

    try:
        link_obj = await bot.create_chat_invite_link(
            chat_id=GROUP_ID,
            name=safe_name,
            creates_join_request=False
        )
        await save_invite_link(link_obj.invite_link, user.id)
        return link_obj.invite_link
    except Exception as e:
        logger.error(f"Ошибка создания ссылки: {e}")
        return "Не удалось создать ссылку. Убедитесь, что бот админ в группе."

# ─── Обработчики команд (ТОЛЬКО В ЛС) ────────────────────────────────────────

@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message, bot: Bot):
    user_id = message.from_user.id
    await save_user_if_new(user_id)
    
    text = (
        f"👋 Привет! Я бот для создания реферальных ссылок в нашу группу.\n\n"
        f"Нажми на кнопку ниже, чтобы получить свою личную ссылку.\n\n"
        f"📊 /stats — количество приглашенных рефералов"
    )
    await message.answer(text, reply_markup=get_ref_keyboard())

@router.message(Command("link"), F.chat.type == "private")
async def cmd_link(message: Message, bot: Bot):
    await save_user_if_new(message.from_user.id)
    await message.answer("Нажми на кнопку, чтобы создать свою реферальную ссылку:", reply_markup=get_ref_keyboard())

@router.callback_query(F.data == "create_ref_link", F.chat.type == "private")
async def process_create_link(callback: CallbackQuery, bot: Bot):
    await callback.answer("Создаю ссылку...")
    user = callback.from_user
    await save_user_if_new(user.id)
    
    link = await _generate_group_invite(bot, user)
    
    text = (
        f"✅ Твоя персональная ссылка для вступления в группу:\n\n"
        f"`{link}`\n\n"
        f"⚠️ **Важно:** не выходи из группы, иначе реферал не засчитается."
    )
    await callback.message.answer(text, parse_mode=ParseMode.MARKDOWN)

@router.message(Command("stats", "myrefs"), F.chat.type == "private")
async def cmd_stats(message: Message, bot: Bot):
    user_id = message.from_user.id
    await save_user_if_new(user_id)
    ref_count = await get_referral_count(user_id)
    
    text = f"📊 Ты пригласил **{ref_count}** уникальных реферал(ов).\n"
    
    if ref_count < 10:
        text += f"🎯 До 10 рефералов осталось: {10 - ref_count}"
    else:
        text += "✅ Порог в 10 рефералов достигнут!"
        nft_username = await get_user_nft_username(user_id)
        if not nft_username:
            text += "\n\n⚠️ Укажи юзернейм (без @), куда будут приходить NFT:"
            await set_user_state(user_id, "waiting_for_username")
            
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)

# ─── Отслеживание вступлений ─────────────────────────────────────────────────

@router.chat_member()
async def on_chat_member(event: ChatMemberUpdated, bot: Bot):
    if event.chat.id != GROUP_ID:
        return
        
    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status
    
    if old_status in ("left", "kicked") and new_status in ("member", "administrator", "creator"):
        new_user_id = event.new_chat_member.user.id
        
        invite_link = event.invite_link.invite_link if event.invite_link else None
        if not invite_link:
            return

        referrer_id = await get_referrer_by_link(invite_link)
        if referrer_id and referrer_id != new_user_id:
            created = await save_user_if_new(new_user_id, referrer_id)
            if created:
                try:
                    count = await get_referral_count(referrer_id)
                    await bot.send_message(
                        referrer_id,
                        f"👤 По вашей ссылке в группу пришёл новый пользователь!\n"
                        f"Теперь у вас {count} реферал(ов)."
                    )
                except Exception:
                    pass 

# ─── Ввод юзернейма ──────────────────────────────────────────────────────────

@router.message(F.text, ~F.text.startswith("/"), F.chat.type == "private")
async def handle_nft_username(message: Message, bot: Bot):
    user_id = message.from_user.id
    state = await get_user_state(user_id)
    
    if state != "waiting_for_username":
        await message.answer("Используй /start или /link, чтобы получить ссылку.")
        return

    raw = message.text.strip()
    username = raw.lstrip("@").strip()

    if not (5 <= len(username) <= 32) or not username.replace("_", "").isalnum():
        await message.answer("❌ Некорректный юзернейм. Используй только латиницу, цифры и _ (5–32 символа).")
        return

    await set_nft_username_and_state(user_id, username)
    await message.answer(f"✅ Готово! Твой юзернейм `@{username}` сохранен.", parse_mode=ParseMode.MARKDOWN)
    
    if ADMIN_ID:
        try:
            admin_msg = f"🔔 **Новый получатель NFT!**\n\nID: `{user_id}`\nЮзернейм: `@{username}`"
            await bot.send_message(ADMIN_ID, admin_msg, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass

# ─── Polling и Обработка сигналов (SIGTERM) ──────────────────────────────────

async def run_polling(bot: Bot, dp: Dispatcher, limit_seconds: int, stop_event: asyncio.Event):
    offset = load_offset()
    end_time = datetime.now() + timedelta(seconds=limit_seconds)
    logger.info("Polling started. Offset=%s, limit=%ss", offset, limit_seconds)

    # Работаем пока не вышло время ИЛИ пока GitHub не прислал сигнал на убийство (stop_event)
    while datetime.now() < end_time and not stop_event.is_set():
        try:
            updates = await bot.get_updates(offset=offset, timeout=30, allowed_updates=["message", "chat_member", "callback_query"])
            if not updates:
                await asyncio.sleep(1)
                continue

            for update in updates:
                try:
                    await dp.feed_update(bot, update)
                except Exception:
                    logger.exception("Error processing update %s", update.update_id)
                finally:
                    offset = update.update_id + 1
                    save_offset(offset)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Polling error, retrying in 5s")
            await asyncio.sleep(5)
            
    logger.info("Polling stopped gracefully.")

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    await init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    # Создаем событие для отслеживания остановки
    stop_event = asyncio.Event()
    
    # Функция, которая сработает, когда GitHub Actions убьет процесс
    def signal_handler():
        logger.info("Received termination signal! Stopping polling...")
        stop_event.set()

    # Перехватываем SIGTERM и SIGINT
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    try:
        await run_polling(bot, dp, POLLING_LIMIT_SECONDS, stop_event)
    finally:
        # Обязательно закрываем сессию, чтобы Telegram отпустил lock мгновенно
        await bot.session.close()
        logger.info("Bot session closed. Safe to start new one.")

if __name__ == "__main__":
    asyncio.run(main())
