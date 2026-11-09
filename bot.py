import asyncio
import logging
import os
import signal
import html
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, ChatMemberUpdated, User
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

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
        
        # Добавляем недостающие колонки безопасно
        try:
            await db.execute("ALTER TABLE users ADD COLUMN username TEXT")
        except aiosqlite.OperationalError:
            pass 
            
        try:
            await db.execute("ALTER TABLE users ADD COLUMN first_name TEXT")
        except aiosqlite.OperationalError:
            pass 

        # НОВАЯ КОЛОНКА: сохраняем ссылку прямо в юзера
        try:
            await db.execute("ALTER TABLE users ADD COLUMN invite_link TEXT")
        except aiosqlite.OperationalError:
            pass 
            
        await db.commit()

async def get_referral_count(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (user_id,))
        row = await cursor.fetchone()
        return row[0] if row else 0

async def save_user_if_new(user_id: int, referrer_id: int | None = None, username: str | None = None, first_name: str | None = None) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
        if await cursor.fetchone():
            await db.execute("UPDATE users SET username = COALESCE(?, username), first_name = COALESCE(?, first_name) WHERE user_id = ?", (username, first_name, user_id))
            await db.commit()
            return False
        
        await db.execute(
            "INSERT INTO users (user_id, referrer_id, username, first_name) VALUES (?, ?, ?, ?)", 
            (user_id, referrer_id, username, first_name)
        )
        await db.commit()
        return True

async def get_referrer_by_link(link: str) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        # Ищем в новой колонке users
        cursor = await db.execute("SELECT user_id FROM users WHERE invite_link = ?", (link,))
        row = await cursor.fetchone()
        if row:
            return row[0]
        
        # Фоллбэк на старую таблицу invite_links
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

# ─── Роутер ──────────────────────────────────────────────────────────────────
router = Router()

async def _generate_group_invite(bot: Bot, user: User) -> tuple[bool, str]:
    if not GROUP_ID:
        return False, "Ошибка сервера: GROUP_ID не настроен в секретах."

    # 1. Ищем ссылку в профиле юзера
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT invite_link FROM users WHERE user_id = ?", (user.id,))
        row = await cursor.fetchone()
        if row and row[0]:
            return True, row[0] # Если ссылка есть, отдаем её НАВСЕГДА

    # 2. Если нет, генерируем новую
    display_name = user.username or user.first_name or str(user.id)
    safe_name = "".join(c for c in display_name if c.isalnum() or c in " _-")[:32].strip()
    if not safe_name:
        safe_name = f"user_{user.id}"

    try:
        link_obj = await asyncio.wait_for(
            bot.create_chat_invite_link(
                chat_id=GROUP_ID,
                name=safe_name,
                creates_join_request=False
            ),
            timeout=10.0
        )
        new_link = link_obj.invite_link

        # 3. Жестко сохраняем ссылку в профиль, чтобы больше никогда её не менять
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET invite_link = ? WHERE user_id = ?", (new_link, user.id))
            # Дублируем в старую таблицу для обратной совместимости
            await db.execute("INSERT OR REPLACE INTO invite_links (invite_link, referrer_id) VALUES (?, ?)", (new_link, user.id))
            await db.commit()

        return True, new_link

    except asyncio.TimeoutError:
        logger.error("Timeout creating invite link for user %s", user.id)
        return False, "⚠️ Telegram не отвечает. Попробуйте написать /start еще раз через минуту."
    except TelegramBadRequest as e:
        logger.error(f"BadRequest for user {user.id}: {e}")
        if "not enough rights" in str(e).lower():
            return False, "❌ Ошибка: у бота нет прав. Сделайте бота администратором группы с правом 'Приглашать пользователей'."
        if "chat not found" in str(e).lower():
            return False, "❌ Ошибка: бот не добавлен в группу или указан неверный GROUP_ID."
        return False, f"❌ Ошибка Telegram: {e}"
    except TelegramRetryAfter as e:
        logger.error(f"Flood control for user {user.id}: {e}")
        return False, f"⏳ Слишком много запросов. Подождите {e.retry_after} секунд."
    except Exception as e:
        logger.exception("Unexpected error creating invite link")
        return False, "❌ Неизвестная ошибка при создании ссылки. Проверьте логи."

# ─── Обработчики команд (ТОЛЬКО В ЛС) ────────────────────────────────────────

@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message, bot: Bot):
    user = message.from_user
    await save_user_if_new(user.id, username=user.username, first_name=user.first_name)
    
    success, result_text = await _generate_group_invite(bot, user)
    
    if success:
        text = (
            f"👋 Привет! Твоя персональная ссылка для вступления в группу:\n\n"
            f"`{result_text}`\n\n"
            f"📊 /stats — количество приглашенных рефералов\n\n"
            f"⚠️ **Важно:** не выходи из группы, иначе реферал не засчитается."
        )
    else:
        text = f"👋 Привет!\n{result_text}"
        
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)

@router.message(Command("link"), F.chat.type == "private")
async def cmd_link(message: Message, bot: Bot):
    user = message.from_user
    await save_user_if_new(user.id, username=user.username, first_name=user.first_name)
    
    success, result_text = await _generate_group_invite(bot, user)
    
    if success:
        await message.answer(f"🔗 Твоя ссылка для входа в группу:\n\n`{result_text}`", parse_mode=ParseMode.MARKDOWN)
    else:
        await message.answer(result_text)

@router.message(Command("stats", "myrefs"), F.chat.type == "private")
async def cmd_stats(message: Message, bot: Bot):
    user = message.from_user
    await save_user_if_new(user.id, username=user.username, first_name=user.first_name)
    ref_count = await get_referral_count(user.id)
    
    text = f"📊 Ты пригласил: **{ref_count}** реферал(ов).\n"
    
    if ref_count >= 10:
        nft_username = await get_user_nft_username(user.id)
        if not nft_username:
            text += "\n⚠️ Укажи юзернейм (без @), куда будут приходить NFT:"
            await set_user_state(user.id, "waiting_for_username")
            
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)

# ─── КОМАНДА ДЛЯ АДМИНА: /users ──────────────────────────────────────────────

@router.message(Command("users"), F.chat.type == "private")
async def cmd_users(message: Message):
    if str(message.from_user.id) != str(ADMIN_ID):
        await message.answer("❌ У вас нет прав для этой команды.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT u1.user_id, u1.username, u1.first_name, u1.nft_username, COUNT(u2.user_id) as ref_count
            FROM users u1
            LEFT JOIN users u2 ON u1.user_id = u2.referrer_id
            GROUP BY u1.user_id
            ORDER BY ref_count DESC, u1.user_id ASC
        """)
        rows = await cursor.fetchall()

    if not rows:
        await message.answer("В базе пока нет пользователей.")
        return

    text = "👥 <b>Список пользователей:</b>\n\n"
    for row in rows:
        user_id, username, first_name, nft_username, ref_count = row
        
        safe_name = html.escape(first_name) if first_name else None
        
        if username:
            display_name = f"{safe_name} (@{username})" if safe_name else f"@{username}"
        elif safe_name:
            display_name = safe_name
        else:
            display_name = f"ID {user_id}"
            
        user_link = f'<a href="tg://user?id={user_id}">{display_name}</a>'
        
        nft_str = f"@{nft_username}" if nft_username else "—"
        
        text += f"{user_link} | NFT: {nft_str} | <b>{ref_count}</b> реф.\n"
        
        if len(text) > 3500:
            await message.answer(text, parse_mode=ParseMode.HTML)
            text = "👥 <b>Продолжение списка:</b>\n\n"

    if text not in ("👥 <b>Список пользователей:</b>\n\n", "👥 <b>Продолжение списка:</b>\n\n"):
        await message.answer(text, parse_mode=ParseMode.HTML)

# ─── Отслеживание вступлений ─────────────────────────────────────────────────

@router.chat_member()
async def on_chat_member(event: ChatMemberUpdated, bot: Bot):
    if event.chat.id != GROUP_ID:
        return
        
    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status
    
    if old_status in ("left", "kicked") and new_status in ("member", "administrator", "creator"):
        new_user = event.new_chat_member.user
        new_user_id = new_user.id
        
        invite_link = event.invite_link.invite_link if event.invite_link else None
        if not invite_link:
            return

        referrer_id = await get_referrer_by_link(invite_link)
        if referrer_id and referrer_id != new_user_id:
            created = await save_user_if_new(
                new_user_id, 
                referrer_id=referrer_id, 
                username=new_user.username, 
                first_name=new_user.first_name
            )
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

# ─── Polling и Обработка сигналов ────────────────────────────────────────────

async def run_polling(bot: Bot, dp: Dispatcher, limit_seconds: int, stop_event: asyncio.Event):
    offset = load_offset()
    end_time = datetime.now() + timedelta(seconds=limit_seconds)
    logger.info("Polling started. Offset=%s, limit=%ss", offset, limit_seconds)

    while datetime.now() < end_time and not stop_event.is_set():
        try:
            updates = await bot.get_updates(offset=offset, timeout=30, allowed_updates=["message", "chat_member"])
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

    stop_event = asyncio.Event()
    
    def signal_handler():
        logger.info("Received termination signal! Stopping polling...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    try:
        await run_polling(bot, dp, POLLING_LIMIT_SECONDS, stop_event)
    finally:
        await bot.session.close()
        logger.info("Bot session closed. Safe to start new one.")

if __name__ == "__main__":
    asyncio.run(main())
