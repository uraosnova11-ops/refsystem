import asyncio
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import Message
from aiogram.utils.deep_linking import create_start_link

# ─── Конфигурация ────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")  # Сюда приходят уведомления

# Ограничение времени работы (~5 ч 50 мин)
POLLING_LIMIT_SECONDS = 5 * 60 * 60 + 50 * 60

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
        """)
        await db.commit()

async def get_referral_count(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM users WHERE referrer_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

async def save_user_if_new(user_id: int, referrer_id: int | None = None) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
        if await cursor.fetchone():
            return False
        await db.execute(
            "INSERT INTO users (user_id, referrer_id) VALUES (?, ?)",
            (user_id, referrer_id),
        )
        await db.commit()
        return True

async def get_user_nft_username(user_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT nft_username FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None

async def set_nft_username_and_state(user_id: int, username: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET nft_username = ?, state = NULL WHERE user_id = ?",
            (username, user_id),
        )
        await db.commit()

async def set_user_state(user_id: int, state: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET state = ? WHERE user_id = ?", (state, user_id)
        )
        await db.commit()

async def get_user_state(user_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT state FROM users WHERE user_id = ?", (user_id,)
        )
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

# ─── Роутер и обработчики ────────────────────────────────────────────────────
router = Router()

async def _generate_ref_link(bot: Bot, user_id: int) -> str:
    return await create_start_link(bot, f"ref_{user_id}")

async def _maybe_ask_for_nft(message: Message, user_id: int):
    ref_count = await get_referral_count(user_id)
    nft_username = await get_user_nft_username(user_id)

    if ref_count >= 10 and not nft_username:
        current_state = await get_user_state(user_id)
        if current_state != "waiting_for_username":
            await set_user_state(user_id, "waiting_for_username")
            await message.answer(
                "🎉 Поздравляем! Вы пригласили 10 рефералов.\n\n"
                "Укажи юзернейм (без @), куда будут приходить NFT:"
            )
            return True
    return False

@router.message(CommandStart(deep_link=True))
async def start_with_deep_link(message: Message, command: CommandObject, bot: Bot):
    user_id = message.from_user.id
    args = command.args

    referrer_id = None
    if args and args.startswith("ref_"):
        try:
            referrer_id = int(args.split("_", 1)[1])
        except (ValueError, IndexError):
            logger.warning("Invalid deep-link payload: %s", args)

    if referrer_id == user_id:
        referrer_id = None

    created = await save_user_if_new(user_id, referrer_id)

    if created and referrer_id:
        try:
            await bot.send_message(
                referrer_id,
                f"👤 По вашей ссылке пришёл новый пользователь! Теперь у вас "
                f"{await get_referral_count(referrer_id)} реферал(ов).",
            )
        except Exception:
            pass

    if await _maybe_ask_for_nft(message, user_id):
        return

    link = await _generate_ref_link(bot, user_id)
    await message.answer(
        f"👋 Привет! Твоя персональная реферальная ссылка:\n\n"
        f"`{link}`\n\n"
        f"📊 /stats — количество рефералов\n"
        f"🔗 /link — получить ссылку повторно",
        parse_mode=ParseMode.MARKDOWN,
    )

@router.message(CommandStart())
async def start_plain(message: Message, bot: Bot):
    user_id = message.from_user.id
    await save_user_if_new(user_id)

    if await _maybe_ask_for_nft(message, user_id):
        return

    link = await _generate_ref_link(bot, user_id)
    await message.answer(
        f"👋 Привет! Твоя персональная реферальная ссылка:\n\n"
        f"`{link}`\n\n"
        f"📊 /stats — количество рефералов\n"
        f"🔗 /link — получить ссылку повторно",
        parse_mode=ParseMode.MARKDOWN,
    )

@router.message(Command("link"))
async def cmd_link(message: Message, bot: Bot):
    user_id = message.from_user.id
    await save_user_if_new(user_id)
    link = await _generate_ref_link(bot, user_id)
    await message.answer(f"🔗 Твоя реферальная ссылка:\n\n`{link}`", parse_mode=ParseMode.MARKDOWN)

@router.message(Command("stats", "myrefs"))
async def cmd_stats(message: Message):
    user_id = message.from_user.id
    await save_user_if_new(user_id)
    ref_count = await get_referral_count(user_id)
    
    text = f"📊 Ты пригласил **{ref_count}** уникальных реферал(ов).\n"
    if ref_count < 10:
        text += f"🎯 До 10 рефералов осталось: {10 - ref_count}"
    else:
        text += "✅ Порог в 10 рефералов достигнут!"
        
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)

@router.message(F.text, ~F.text.startswith("/"))
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

    ref_count = await get_referral_count(user_id)
    if ref_count < 10:
        await set_user_state(user_id, None)
        await message.answer(f"⚠️ У тебя пока только {ref_count} реферал(ов). Нужно минимум 10.")
        return

    # Сохраняем в БД
    await set_nft_username_and_state(user_id, username)
    await message.answer(f"✅ Готово! Твой юзернейм `@{username}` сохранен.", parse_mode=ParseMode.MARKDOWN)
    
    # Отправляем уведомление АДМИНУ
    if ADMIN_ID:
        try:
            admin_msg = (
                f"🔔 **Новый получатель NFT!**\n\n"
                f"Пользователь пригласил 10+ рефералов.\n"
                f"Его ID: `{user_id}`\n"
                f"Юзернейм для NFT: `@{username}`"
            )
            await bot.send_message(ADMIN_ID, admin_msg, parse_mode=ParseMode.MARKDOWN)
            logger.info(f"Admin notified about user {user_id} and username {username}")
        except Exception as e:
            logger.error(f"Failed to notify admin: {e}")

# ─── Polling ─────────────────────────────────────────────────────────────────
async def run_polling(bot: Bot, dp: Dispatcher, limit_seconds: int):
    offset = load_offset()
    end_time = datetime.now() + timedelta(seconds=limit_seconds)
    logger.info("Polling started. Offset=%s, limit=%ss", offset, limit_seconds)

    while datetime.now() < end_time:
        try:
            updates = await bot.get_updates(offset=offset, timeout=30, allowed_updates=["message"])
            if not updates:
                await asyncio.sleep(2)
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

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    if not ADMIN_ID:
        logger.warning("ADMIN_ID is not set. Notifications will not be sent.")

    await init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    try:
        await run_polling(bot, dp, POLLING_LIMIT_SECONDS)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
