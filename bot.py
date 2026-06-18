import os
import re
import json
import base64
import sqlite3
import logging
import shutil
import zipfile
from PIL import Image, ImageDraw, ImageFont, ImageOps
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    Update,
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.error import TelegramError, TimedOut


load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


def get_int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip())
    except (TypeError, ValueError):
        return default


def get_float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip())
    except (TypeError, ValueError):
        return default


ADMIN_USER_IDS_RAW = os.getenv("ADMIN_USER_IDS", "")
ADMIN_USER_ID_PARTS = [x.strip() for x in ADMIN_USER_IDS_RAW.split(",") if x.strip()]
ADMIN_USER_IDS = (
    {int(x) for x in ADMIN_USER_ID_PARTS}
    if ADMIN_USER_ID_PARTS and all(x.isdigit() for x in ADMIN_USER_ID_PARTS)
    else set()
)

MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
MAX_TELEGRAM_SEND_SIZE_MB = int(os.getenv("MAX_TELEGRAM_SEND_SIZE_MB", "49"))
MAX_TELEGRAM_SEND_SIZE_BYTES = MAX_TELEGRAM_SEND_SIZE_MB * 1024 * 1024
TELEGRAM_SEND_TIMEOUT_SECONDS = int(os.getenv("TELEGRAM_SEND_TIMEOUT_SECONDS", "180"))
APP_TIMEZONE = ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Bangkok"))
WORK_SHIFT_START_HOUR = int(os.getenv("WORK_SHIFT_START_HOUR", "9"))
BOT_VERSION = os.getenv("BOT_VERSION", "0.5.0")
BOT_BUILD_NAME = os.getenv("BOT_BUILD_NAME", "work-packages-private-control-contact-sheets")
BOT_BUILD_DATE = os.getenv("BOT_BUILD_DATE", "2026-06-12")
CONTACT_SHEET_IMAGES_PER_PAGE = int(os.getenv("CONTACT_SHEET_IMAGES_PER_PAGE", "4"))
CONTACT_SHEET_PAGE_WIDTH = int(os.getenv("CONTACT_SHEET_PAGE_WIDTH", "3000"))
CONTACT_SHEET_THUMB_WIDTH = int(os.getenv("CONTACT_SHEET_THUMB_WIDTH", "1350"))
CONTACT_SHEET_JPEG_QUALITY = int(os.getenv("CONTACT_SHEET_JPEG_QUALITY", "92"))
CONTACT_SHEET_FONT_PATH = os.getenv("CONTACT_SHEET_FONT_PATH", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TRANSCRIBE_VOICE = os.getenv("TRANSCRIBE_VOICE", "false").strip().lower() in ("1", "true", "yes", "on")
TRANSCRIBE_MODEL = os.getenv("TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
TRANSCRIBE_MAX_AUDIO_MB = int(os.getenv("TRANSCRIBE_MAX_AUDIO_MB", "25"))
TRANSCRIBE_MAX_AUDIO_BYTES = TRANSCRIBE_MAX_AUDIO_MB * 1024 * 1024
TRANSCRIBE_TIMEOUT_SECONDS = get_int_env("TRANSCRIBE_TIMEOUT_SECONDS", 120)
OPENAI_REPORTS_ENABLED = os.getenv("OPENAI_REPORTS_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
OPENAI_REPORT_MODEL = os.getenv("OPENAI_REPORT_MODEL", "gpt-5-mini")
OPENAI_REPORT_TIMEOUT_SECONDS = get_int_env("OPENAI_REPORT_TIMEOUT_SECONDS", 180)
OPENAI_REPORT_MAX_INPUT_CHARS = get_int_env("OPENAI_REPORT_MAX_INPUT_CHARS", 250000)
OPENAI_REPORT_TEMPERATURE = get_float_env("OPENAI_REPORT_TEMPERATURE", 0.2)
OPENAI_REPORT_INCLUDE_IMAGES = os.getenv("OPENAI_REPORT_INCLUDE_IMAGES", "true").strip().lower() in ("1", "true", "yes", "on")
OPENAI_REPORT_MAX_IMAGES = get_int_env("OPENAI_REPORT_MAX_IMAGES", 6)
AI_REPORT_LOGO_PATH = Path(os.getenv("AI_REPORT_LOGO_PATH", "assets/bs_logo.png"))

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
EXPORT_DIR = DATA_DIR / "exports"
FILES_DIR = DATA_DIR / "files"
TRANSCRIPTS_DIR = DATA_DIR / "transcripts"
PROMPTS_DIR = BASE_DIR / "prompts"
DEFAULT_WORK_ANALYSIS_PROMPT_PATH = DATA_DIR / "config" / "work_analysis_prompt.txt"
WORK_ANALYSIS_PROMPT_PATH = Path(os.getenv("WORK_ANALYSIS_PROMPT_PATH", str(DEFAULT_WORK_ANALYSIS_PROMPT_PATH)))
WORK_ANALYSIS_PROMPT_EXAMPLE_PATH = PROMPTS_DIR / "work_analysis_prompt.example.txt"
DB_PATH = DATA_DIR / "messages.db"
RETENTION_DAYS = 14

DATA_DIR.mkdir(exist_ok=True)
EXPORT_DIR.mkdir(exist_ok=True)
FILES_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.WARNING,
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

if not ADMIN_USER_IDS:
    logging.warning(
        "ADMIN_USER_IDS is missing, empty, or invalid. "
        "All administrator-only actions are disabled."
    )


def is_admin(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False

    return user.id in ADMIN_USER_IDS


async def admin_only(update: Update) -> bool:
    if is_admin(update):
        return True

    await update.message.reply_text("Эта команда доступна только администратору бота.")
    return False


def safe_filename(name: str) -> str:
    if not name:
        return "file"

    name = name.strip()
    name = re.sub(r"[^\w\-.а-яА-ЯёЁ ]+", "_", name)
    name = re.sub(r"\s+", "_", name)

    return name[:150] or "file"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            chat_title TEXT,
            message_id INTEGER NOT NULL,
            date_utc TEXT NOT NULL,
            user_id INTEGER,
            username TEXT,
            full_name TEXT,
            message_type TEXT,
            text TEXT,
            caption TEXT,
            reply_to_message_id INTEGER,
            file_id TEXT,
            file_unique_id TEXT,
            file_name TEXT,
            file_mime_type TEXT,
            file_size INTEGER,
            file_path TEXT,
            file_skipped INTEGER DEFAULT 0,
            file_skip_reason TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(chat_id, message_id)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY,
            chat_title TEXT,
            chat_type TEXT,
            status TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            approved_at TEXT,
            approved_by_user_id INTEGER,
            last_seen_at TEXT
        )
        """
    )

    # Lightweight migrations for existing SQLite databases
    cur.execute("PRAGMA table_info(messages)")
    existing_columns = {row[1] for row in cur.fetchall()}

    if "file_skipped" not in existing_columns:
        cur.execute("ALTER TABLE messages ADD COLUMN file_skipped INTEGER DEFAULT 0")

    if "file_skip_reason" not in existing_columns:
        cur.execute("ALTER TABLE messages ADD COLUMN file_skip_reason TEXT")

    conn.commit()
    conn.close()


def detect_message_type(message):
    if message.text:
        return "text"
    if message.photo:
        return "photo"
    if message.document:
        return "document"
    if message.video:
        return "video"
    if message.voice:
        return "voice"
    if message.audio:
        return "audio"
    if message.sticker:
        return "sticker"
    if message.animation:
        return "animation"
    return "other"


async def download_attachment(message, context: ContextTypes.DEFAULT_TYPE):
    file_id = None
    file_unique_id = None
    original_file_name = None
    mime_type = None
    file_size = None
    extension = ""

    if message.document:
        doc = message.document
        file_id = doc.file_id
        file_unique_id = doc.file_unique_id
        original_file_name = doc.file_name or "document"
        mime_type = doc.mime_type
        file_size = doc.file_size
        extension = Path(original_file_name).suffix

    elif message.photo:
        photo = message.photo[-1]
        file_id = photo.file_id
        file_unique_id = photo.file_unique_id
        original_file_name = f"photo_{message.message_id}.jpg"
        mime_type = "image/jpeg"
        file_size = photo.file_size
        extension = ".jpg"

    elif message.video:
        video = message.video
        file_id = video.file_id
        file_unique_id = video.file_unique_id
        original_file_name = video.file_name or f"video_{message.message_id}.mp4"
        mime_type = video.mime_type
        file_size = video.file_size
        extension = Path(original_file_name).suffix or ".mp4"

    elif message.voice:
        voice = message.voice
        file_id = voice.file_id
        file_unique_id = voice.file_unique_id
        original_file_name = f"voice_{message.message_id}.ogg"
        mime_type = voice.mime_type
        file_size = voice.file_size
        extension = ".ogg"

    elif message.audio:
        audio = message.audio
        file_id = audio.file_id
        file_unique_id = audio.file_unique_id
        original_file_name = audio.file_name or f"audio_{message.message_id}.mp3"
        mime_type = audio.mime_type
        file_size = audio.file_size
        extension = Path(original_file_name).suffix or ".mp3"

    elif message.animation:
        animation = message.animation
        file_id = animation.file_id
        file_unique_id = animation.file_unique_id
        original_file_name = animation.file_name or f"animation_{message.message_id}.mp4"
        mime_type = animation.mime_type
        file_size = animation.file_size
        extension = Path(original_file_name).suffix or ".mp4"

    else:
        return {
            "file_id": None,
            "file_unique_id": None,
            "file_name": None,
            "file_mime_type": None,
            "file_size": None,
            "file_path": None,
            "file_skipped": 0,
            "file_skip_reason": None,
        }

    if file_size is not None and file_size > MAX_FILE_SIZE_BYTES:
        return {
            "file_id": file_id,
            "file_unique_id": file_unique_id,
            "file_name": original_file_name,
            "file_mime_type": mime_type,
            "file_size": file_size,
            "file_path": None,
            "file_skipped": 1,
            "file_skip_reason": (
                f"File size {file_size} bytes exceeds "
                f"the configured limit of {MAX_FILE_SIZE_MB} MB"
            ),
        }

    date_folder = message.date.astimezone().strftime("%Y-%m-%d")
    target_dir = FILES_DIR / date_folder
    target_dir.mkdir(parents=True, exist_ok=True)

    base_name = safe_filename(original_file_name)

    if not Path(base_name).suffix and extension:
        base_name = base_name + extension

    local_file_name = f"{message.chat.id}_{message.message_id}_{base_name}"
    local_path = target_dir / local_file_name

    telegram_file = await context.bot.get_file(file_id)
    await telegram_file.download_to_drive(custom_path=local_path)

    relative_path = local_path.relative_to(BASE_DIR)

    return {
        "file_id": file_id,
        "file_unique_id": file_unique_id,
        "file_name": original_file_name,
        "file_mime_type": mime_type,
        "file_size": file_size,
        "file_path": str(relative_path),
        "file_skipped": 0,
        "file_skip_reason": None,
    }


def save_message(message, file_info):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    user = message.from_user

    text = message.text or ""
    caption = message.caption or ""
    message_type = detect_message_type(message)

    reply_to_message_id = None
    if message.reply_to_message:
        reply_to_message_id = message.reply_to_message.message_id

    cur.execute(
        """
        INSERT OR IGNORE INTO messages (
            chat_id,
            chat_title,
            message_id,
            date_utc,
            user_id,
            username,
            full_name,
            message_type,
            text,
            caption,
            reply_to_message_id,
            file_id,
            file_unique_id,
            file_name,
            file_mime_type,
            file_size,
            file_path,
            file_skipped,
            file_skip_reason,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message.chat.id,
            message.chat.title or message.chat.full_name or "",
            message.message_id,
            message.date.astimezone(timezone.utc).isoformat(),
            user.id if user else None,
            user.username if user else None,
            user.full_name if user else None,
            message_type,
            text,
            caption,
            reply_to_message_id,
            file_info.get("file_id"),
            file_info.get("file_unique_id"),
            file_info.get("file_name"),
            file_info.get("file_mime_type"),
            file_info.get("file_size"),
            file_info.get("file_path"),
            file_info.get("file_skipped", 0),
            file_info.get("file_skip_reason"),
            datetime.now(timezone.utc).isoformat(),
        ),
    )

    conn.commit()
    conn.close()




def get_chat_status(chat_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute(
        """
        SELECT status
        FROM chats
        WHERE chat_id = ?
        """,
        (chat_id,),
    )

    row = cur.fetchone()
    conn.close()

    return row[0] if row else None


def register_chat_if_needed(chat, status: str = "pending"):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    now = datetime.now(timezone.utc).isoformat()

    cur.execute(
        """
        INSERT OR IGNORE INTO chats (
            chat_id,
            chat_title,
            chat_type,
            status,
            requested_at,
            last_seen_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            chat.id,
            chat.title or chat.full_name or str(chat.id),
            chat.type,
            status,
            now,
            now,
        ),
    )

    cur.execute(
        """
        UPDATE chats
        SET chat_title = ?,
            chat_type = ?,
            last_seen_at = ?
        WHERE chat_id = ?
        """,
        (
            chat.title or chat.full_name or str(chat.id),
            chat.type,
            now,
            chat.id,
        ),
    )

    conn.commit()
    conn.close()



def delete_chat_record(chat_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute(
        """
        DELETE FROM chats
        WHERE chat_id = ?
        """,
        (chat_id,),
    )

    deleted = cur.rowcount
    conn.commit()
    conn.close()

    return deleted > 0


def set_chat_status(chat_id: int, status: str, approved_by_user_id=None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    now = datetime.now(timezone.utc).isoformat()

    cur.execute(
        """
        UPDATE chats
        SET status = ?,
            approved_at = ?,
            approved_by_user_id = ?
        WHERE chat_id = ?
        """,
        (
            status,
            now if status == "approved" else None,
            approved_by_user_id if status == "approved" else None,
            chat_id,
        ),
    )

    changed = cur.rowcount
    conn.commit()
    conn.close()

    return changed > 0


def list_chats_by_status(status: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute(
        """
        SELECT chat_id, chat_title, chat_type, requested_at, last_seen_at
        FROM chats
        WHERE status = ?
        ORDER BY last_seen_at DESC
        """,
        (status,),
    )

    rows = cur.fetchall()
    conn.close()

    return rows


async def notify_admins_about_pending_chat(context: ContextTypes.DEFAULT_TYPE, chat):
    chat_title = chat.title or chat.full_name or str(chat.id)

    message_text = (
        "Бота добавили в новый чат или он увидел новый чат.\n\n"
        f"Название: {chat_title}\n"
        f"Chat ID: {chat.id}\n"
        f"Тип: {chat.type}\n\n"
        "Пока чат не подтверждён, сообщения и файлы из него не сохраняются."
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Подтвердить",
                callback_data=f"approve_chat:{chat.id}"
            ),
            InlineKeyboardButton(
                "❌ Отклонить",
                callback_data=f"reject_chat:{chat.id}"
            ),
        ]
    ])

    for admin_id in ADMIN_USER_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=message_text,
                reply_markup=keyboard,
            )
        except Exception as e:
            print(f"Could not notify admin {admin_id} about chat {chat.id}: {e}")


async def ensure_chat_approved(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat

    if not chat:
        return False

    # Personal chat with admin is always allowed for commands/testing.
    if chat.type == "private" and is_admin(update):
        register_chat_if_needed(chat, status="approved")
        return True

    status = get_chat_status(chat.id)

    if status is None:
        register_chat_if_needed(chat, status="pending")
        await notify_admins_about_pending_chat(context, chat)

        if update.message:
            await update.message.reply_text(
                "Бот ожидает подтверждения администратора. "
                "Сообщения и файлы этого чата пока не сохраняются."
            )

        return False

    register_chat_if_needed(chat, status=status)

    if status == "approved":
        return True

    if update.message and status == "pending":
        await update.message.reply_text(
            "Бот ожидает подтверждения администратора. "
            "Сообщения и файлы этого чата пока не сохраняются."
        )

    return False


def cleanup_old_data(retention_days: int = RETENTION_DAYS):
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute(
        """
        SELECT file_path
        FROM messages
        WHERE date_utc < ? AND file_path IS NOT NULL
        """,
        (cutoff.isoformat(),),
    )

    old_files = [row[0] for row in cur.fetchall()]

    cur.execute(
        """
        DELETE FROM messages
        WHERE date_utc < ?
        """,
        (cutoff.isoformat(),),
    )

    deleted_messages = cur.rowcount

    conn.commit()
    conn.close()

    deleted_files = 0

    for relative_file_path in old_files:
        file_path = BASE_DIR / relative_file_path

        try:
            if file_path.exists() and file_path.is_file():
                file_path.unlink()
                deleted_files += 1
        except Exception as e:
            print(f"Could not delete old file {file_path}: {e}")

    # Remove empty date folders inside data/files
    if FILES_DIR.exists():
        for folder in sorted(FILES_DIR.iterdir(), reverse=True):
            if folder.is_dir():
                try:
                    if not any(folder.iterdir()):
                        folder.rmdir()
                except Exception:
                    pass

    if deleted_messages or deleted_files:
        print(
            f"Cleanup complete: deleted_messages={deleted_messages}, "
            f"deleted_files={deleted_files}, retention_days={retention_days}"
        )





def count_files_in_dir(root: Path) -> int:
    if not root.exists():
        return 0

    return sum(1 for item in root.rglob("*") if item.is_file())


def delete_file_from_message_path(file_path_value):
    if not file_path_value:
        return 0

    try:
        candidate = resolve_stored_file_path(file_path_value)
        if candidate and candidate.is_file():
            candidate.unlink()
            return 1
    except Exception as e:
        logging.warning("Could not delete file %s: %s", file_path_value, e)

    return 0


def cleanup_empty_dirs(root: Path):
    if not root.exists():
        return

    for path_item in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path_item.is_dir():
            try:
                path_item.rmdir()
            except OSError:
                pass


def purge_chat_archive_data(chat_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("SELECT file_path FROM messages WHERE chat_id = ?", (chat_id,))
    file_paths = [row[0] for row in cur.fetchall()]

    deleted_files = 0
    for fp in file_paths:
        deleted_files += delete_file_from_message_path(fp)

    cur.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat_id,))
    deleted_messages = cur.fetchone()[0]

    cur.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
    cur.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))

    conn.commit()
    conn.close()

    cleanup_empty_dirs(FILES_DIR)

    return deleted_messages, deleted_files


def purge_all_archive_data():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM messages")
    deleted_messages = cur.fetchone()[0]

    cur.execute("DELETE FROM messages")
    conn.commit()
    conn.close()

    files_before = count_files_in_dir(FILES_DIR)
    exports_before = count_files_in_dir(EXPORT_DIR)

    if FILES_DIR.exists():
        shutil.rmtree(FILES_DIR)
    FILES_DIR.mkdir(parents=True, exist_ok=True)

    if EXPORT_DIR.exists():
        shutil.rmtree(EXPORT_DIR)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    deleted_physical_files = files_before + exports_before

    return deleted_messages, deleted_physical_files


def force_retention_cleanup():
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute(
        "SELECT file_path FROM messages WHERE date_utc < ? AND file_path IS NOT NULL",
        (cutoff.isoformat(),),
    )
    file_paths = [row[0] for row in cur.fetchall()]

    deleted_files = 0
    for fp in file_paths:
        deleted_files += delete_file_from_message_path(fp)

    cur.execute("SELECT COUNT(*) FROM messages WHERE date_utc < ?", (cutoff.isoformat(),))
    deleted_messages = cur.fetchone()[0]

    cur.execute("DELETE FROM messages WHERE date_utc < ?", (cutoff.isoformat(),))

    conn.commit()
    conn.close()

    cleanup_empty_dirs(FILES_DIR)

    return deleted_messages, deleted_files



def get_dir_size_bytes(root: Path) -> int:
    if not root.exists():
        return 0

    total = 0

    for item in root.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                pass

    return total


def get_dir_file_count(root: Path) -> int:
    if not root.exists():
        return 0

    return sum(1 for item in root.rglob("*") if item.is_file())


def get_storage_status_data():
    db_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM messages")
    message_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM messages WHERE file_path IS NOT NULL")
    db_file_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM chats")
    chats_count = cur.fetchone()[0]

    conn.close()

    files_size = get_dir_size_bytes(FILES_DIR)
    exports_size = get_dir_size_bytes(EXPORT_DIR)
    data_size = get_dir_size_bytes(DATA_DIR)

    files_count = get_dir_file_count(FILES_DIR)
    exports_count = get_dir_file_count(EXPORT_DIR)
    data_count = get_dir_file_count(DATA_DIR)

    return {
        "db_size": db_size,
        "message_count": message_count,
        "db_file_count": db_file_count,
        "chats_count": chats_count,
        "files_size": files_size,
        "exports_size": exports_size,
        "data_size": data_size,
        "files_count": files_count,
        "exports_count": exports_count,
        "data_count": data_count,
    }


def format_storage_status_text(data) -> str:
    return (
        "Storage status\n\n"
        "Database:\n"
        f"messages.db: {human_file_size(data['db_size'])}\n"
        f"Messages rows: {data['message_count']}\n"
        f"DB file records: {data['db_file_count']}\n"
        f"Known chats: {data['chats_count']}\n\n"
        "Directories:\n"
        f"data/files: {human_file_size(data['files_size'])} / {data['files_count']} files\n"
        f"data/exports: {human_file_size(data['exports_size'])} / {data['exports_count']} files\n"
        f"data total: {human_file_size(data['data_size'])} / {data['data_count']} files\n\n"
        "If messages rows are low but messages.db is large, run /vacuum_db."
    )


def vacuum_database():
    before_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0

    conn = sqlite3.connect(DB_PATH)
    conn.execute("VACUUM")
    conn.close()

    after_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0

    return before_size, after_size


def get_health_data():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM messages")
    total_messages = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM messages WHERE file_path IS NOT NULL")
    saved_files = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM messages WHERE file_skipped = 1")
    skipped_files = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM chats WHERE status = 'approved'")
    approved_chats = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM chats WHERE status = 'pending'")
    pending_chats = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM chats WHERE status = 'rejected'")
    rejected_chats = cur.fetchone()[0]

    conn.close()

    db_exists = DB_PATH.exists()
    data_dir_exists = DATA_DIR.exists()
    files_dir_exists = FILES_DIR.exists()
    exports_dir_exists = EXPORT_DIR.exists()

    return {
        "db_exists": db_exists,
        "data_dir_exists": data_dir_exists,
        "files_dir_exists": files_dir_exists,
        "exports_dir_exists": exports_dir_exists,
        "total_messages": total_messages,
        "saved_files": saved_files,
        "skipped_files": skipped_files,
        "approved_chats": approved_chats,
        "pending_chats": pending_chats,
        "rejected_chats": rejected_chats,
        "base_dir": str(BASE_DIR),
        "data_dir": str(DATA_DIR),
        "files_dir": str(FILES_DIR),
        "exports_dir": str(EXPORT_DIR),
        "db_path": str(DB_PATH),
    }


def get_stats(chat_id=None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    if chat_id is None:
        cur.execute("SELECT COUNT(*) FROM messages")
        total_messages = cur.fetchone()[0]

        cur.execute(
            """
            SELECT COUNT(*)
            FROM messages
            WHERE date_utc >= ?
            """,
            ((datetime.now(timezone.utc) - timedelta(days=7)).isoformat(),),
        )
        week_messages = cur.fetchone()[0]

        cur.execute(
            """
            SELECT COUNT(*)
            FROM messages
            WHERE file_path IS NOT NULL
            """
        )
        total_files = cur.fetchone()[0]

    else:
        cur.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat_id,))
        total_messages = cur.fetchone()[0]

        cur.execute(
            """
            SELECT COUNT(*)
            FROM messages
            WHERE chat_id = ? AND date_utc >= ?
            """,
            (chat_id, (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()),
        )
        week_messages = cur.fetchone()[0]

        cur.execute(
            """
            SELECT COUNT(*)
            FROM messages
            WHERE chat_id = ? AND file_path IS NOT NULL
            """,
            (chat_id,),
        )
        total_files = cur.fetchone()[0]

    conn.close()

    return total_messages, week_messages, total_files





def is_supported_contact_sheet_image(file_name, mime_type, file_path_value):
    value = " ".join(str(x or "").lower() for x in [file_name, mime_type, file_path_value])

    if "image/" in value:
        return True

    return value.endswith((".jpg", ".jpeg", ".png", ".webp"))


def resolve_stored_file_path(file_path_value):
    if not file_path_value:
        return None

    candidate = DATA_DIR / file_path_value

    if candidate.exists():
        return candidate

    candidate = BASE_DIR / file_path_value

    if candidate.exists():
        return candidate

    return None


def get_transcript_cache_paths(chat_id, message_id, file_unique_id, date_utc=None, file_path_value=None):
    if not file_unique_id:
        return []

    base_name = safe_filename(f"{chat_id}_{message_id}_{file_unique_id}")
    date_folders = []

    if date_utc:
        try:
            parsed_dt = datetime.fromisoformat(date_utc)
            date_folders.append(parsed_dt.astimezone(APP_TIMEZONE).strftime("%Y-%m-%d"))
            date_folders.append(parsed_dt.astimezone(timezone.utc).strftime("%Y-%m-%d"))
        except Exception:
            date_folders.append(str(date_utc)[:10])

    if file_path_value:
        stored_path = Path(file_path_value)
        if len(stored_path.parts) >= 2:
            date_folders.append(stored_path.parts[-2])

    seen = set()
    paths = []

    for folder in date_folders:
        if not folder or folder in seen:
            continue
        seen.add(folder)
        transcript_dir = TRANSCRIPTS_DIR / folder
        paths.append(
            {
                "json": transcript_dir / f"{base_name}.transcript.json",
                "txt": transcript_dir / f"{base_name}.transcript.txt",
            }
        )

    return paths


def load_transcript_cache(chat_id, message_id, file_unique_id, date_utc=None, file_path_value=None):
    for cache_paths in get_transcript_cache_paths(chat_id, message_id, file_unique_id, date_utc, file_path_value):
        json_path = cache_paths["json"]
        txt_path = cache_paths["txt"]

        if not json_path.exists() and not txt_path.exists():
            continue

        if json_path.exists():
            try:
                cache = json.loads(json_path.read_text(encoding="utf-8"))
            except Exception as e:
                return {
                    "status": "failed",
                    "error": f"Could not read transcript cache: {e}",
                    "note": "Transcript unavailable / failed.",
                    "json_path": str(json_path),
                    "txt_path": str(txt_path) if txt_path.exists() else None,
                }

            if not isinstance(cache, dict):
                cache = {
                    "status": "failed",
                    "error": "Transcript cache JSON is not an object.",
                    "note": "Transcript unavailable / failed.",
                }

            cache["json_path"] = str(json_path)
            cache["txt_path"] = str(txt_path) if txt_path.exists() else None
            return cache

        if txt_path.exists():
            try:
                text = txt_path.read_text(encoding="utf-8").strip()
            except Exception as e:
                return {
                    "status": "failed",
                    "error": f"Could not read transcript text cache: {e}",
                    "note": "Transcript unavailable / failed.",
                    "txt_path": str(txt_path),
                }

            return {
                "status": "ok",
                "text": text,
                "note": "Automatic transcript. Verify if critical.",
                "txt_path": str(txt_path),
            }

    return None


def save_transcript_cache(chat_id, message_id, file_unique_id, cache_data, date_utc=None, file_path_value=None):
    cache_paths = get_transcript_cache_paths(chat_id, message_id, file_unique_id, date_utc, file_path_value)

    if not cache_paths:
        logging.warning("Could not save transcript cache: missing cache path for message %s", message_id)
        return False

    json_path = cache_paths[0]["json"]
    txt_path = cache_paths[0]["txt"]

    try:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(cache_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        if cache_data.get("status") == "ok":
            text = str(cache_data.get("text") or "").strip()
            if text:
                txt_path.write_text(text + "\n", encoding="utf-8")

        return True
    except Exception as e:
        logging.warning("Could not save transcript cache for message %s: %s", message_id, e)
        return False


def format_transcript_for_export(transcript_cache, style: str = "message"):
    if not transcript_cache:
        return None

    status = str(transcript_cache.get("status") or "").lower()
    note = transcript_cache.get("note") or "Automatic transcript. Verify if critical."

    if status == "ok":
        text = str(transcript_cache.get("text") or "").strip()
        if not text:
            status = "failed"
            transcript_cache = {
                **transcript_cache,
                "status": "failed",
                "error": "Transcript cache has status ok but empty text.",
                "note": "Transcript unavailable / failed.",
            }
        else:
            if style == "attachment":
                return (
                    "- Transcript status: `ok`\n"
                    f"- Transcript note: {note}\n\n"
                    "### Transcript\n\n"
                    f"{text}\n"
                )

            return (
                "Transcript status: ok\n"
                f"Transcript note: {note}\n\n"
                "Automatic transcript:\n"
                f"{text}"
            )

    reason = transcript_cache.get("error") or transcript_cache.get("reason") or "unknown reason"
    status_label = status or "unavailable"

    if style == "attachment":
        return (
            f"- Transcript status: `{status_label}`\n"
            "- Transcript: `unavailable`\n"
            f"- Transcript reason: {reason}\n"
            f"- Transcript note: {note}\n"
        )

    return (
        f"Transcript unavailable ({status_label}).\n"
        f"Reason: {reason}\n"
        f"Note: {note}"
    )


def get_transcript_text_file_for_zip(chat_id, message_id, file_unique_id, date_utc=None, file_path_value=None):
    cache = load_transcript_cache(chat_id, message_id, file_unique_id, date_utc, file_path_value)

    if cache and cache.get("txt_path"):
        txt_path = Path(cache["txt_path"])
        if txt_path.exists() and txt_path.is_file():
            return txt_path

    return None


def transcribe_audio_file(file_path: Path) -> str:
    from openai import OpenAI

    client = OpenAI(
        api_key=OPENAI_API_KEY,
        timeout=TRANSCRIBE_TIMEOUT_SECONDS,
    )

    with file_path.open("rb") as audio_file:
        transcription = client.audio.transcriptions.create(
            model=TRANSCRIBE_MODEL,
            file=audio_file,
        )

    text = getattr(transcription, "text", None)

    if text is None:
        text = str(transcription)

    return text.strip()


def prepare_transcripts_for_period(since_dt, until_dt, chat_id=None):
    if not TRANSCRIBE_VOICE:
        return {"processed": 0, "created": 0, "skipped": 0, "failed": 0, "cached": 0}

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    if chat_id is None:
        cur.execute(
            """
            SELECT chat_id, message_id, file_unique_id, date_utc, file_path, file_size
            FROM messages
            WHERE date_utc >= ?
              AND date_utc < ?
              AND message_type IN ('voice', 'audio')
            ORDER BY date_utc ASC
            """,
            (since_dt.isoformat(), until_dt.isoformat()),
        )
    else:
        cur.execute(
            """
            SELECT chat_id, message_id, file_unique_id, date_utc, file_path, file_size
            FROM messages
            WHERE chat_id = ?
              AND date_utc >= ?
              AND date_utc < ?
              AND message_type IN ('voice', 'audio')
            ORDER BY date_utc ASC
            """,
            (chat_id, since_dt.isoformat(), until_dt.isoformat()),
        )

    rows = cur.fetchall()
    conn.close()

    stats = {"processed": len(rows), "created": 0, "skipped": 0, "failed": 0, "cached": 0}

    for row_chat_id, message_id, file_unique_id, date_utc, file_path_value, file_size in rows:
        try:
            existing_cache = load_transcript_cache(
                row_chat_id,
                message_id,
                file_unique_id,
                date_utc,
                file_path_value,
            )

            if existing_cache and str(existing_cache.get("status") or "").lower() in ("ok", "failed", "skipped"):
                stats["cached"] += 1
                continue

            source_path = resolve_stored_file_path(file_path_value)
            base_cache = {
                "model": TRANSCRIBE_MODEL,
                "source_file_path": file_path_value,
                "generated_at": datetime.now(APP_TIMEZONE).isoformat(),
            }

            if not file_unique_id:
                save_transcript_cache(
                    row_chat_id,
                    message_id,
                    file_unique_id,
                    {
                        **base_cache,
                        "status": "failed",
                        "error": "Missing file_unique_id for transcript cache key.",
                        "note": "Transcript unavailable / failed.",
                    },
                    date_utc,
                    file_path_value,
                )
                stats["failed"] += 1
                continue

            if not source_path or not source_path.exists():
                save_transcript_cache(
                    row_chat_id,
                    message_id,
                    file_unique_id,
                    {
                        **base_cache,
                        "status": "failed",
                        "error": "Audio file not found on server.",
                        "note": "Transcript unavailable / failed.",
                    },
                    date_utc,
                    file_path_value,
                )
                stats["failed"] += 1
                continue

            actual_file_size = source_path.stat().st_size
            size_for_limit = file_size or actual_file_size

            if size_for_limit > TRANSCRIBE_MAX_AUDIO_BYTES:
                save_transcript_cache(
                    row_chat_id,
                    message_id,
                    file_unique_id,
                    {
                        **base_cache,
                        "status": "skipped",
                        "error": (
                            f"Audio file size {size_for_limit} bytes exceeds "
                            f"TRANSCRIBE_MAX_AUDIO_MB={TRANSCRIBE_MAX_AUDIO_MB}."
                        ),
                        "note": "Transcript unavailable / skipped.",
                    },
                    date_utc,
                    file_path_value,
                )
                stats["skipped"] += 1
                continue

            if not OPENAI_API_KEY:
                save_transcript_cache(
                    row_chat_id,
                    message_id,
                    file_unique_id,
                    {
                        **base_cache,
                        "status": "failed",
                        "error": "OPENAI_API_KEY is not configured.",
                        "note": "Transcript unavailable / failed.",
                    },
                    date_utc,
                    file_path_value,
                )
                stats["failed"] += 1
                continue

            try:
                transcript_text = transcribe_audio_file(source_path)
            except Exception as e:
                logging.exception("Could not transcribe audio message %s", message_id)
                save_transcript_cache(
                    row_chat_id,
                    message_id,
                    file_unique_id,
                    {
                        **base_cache,
                        "status": "failed",
                        "error": str(e),
                        "note": "Transcript unavailable / failed.",
                    },
                    date_utc,
                    file_path_value,
                )
                stats["failed"] += 1
                continue

            if not transcript_text:
                save_transcript_cache(
                    row_chat_id,
                    message_id,
                    file_unique_id,
                    {
                        **base_cache,
                        "status": "failed",
                        "error": "OpenAI transcription returned empty text.",
                        "note": "Transcript unavailable / failed.",
                    },
                    date_utc,
                    file_path_value,
                )
                stats["failed"] += 1
                continue

            save_transcript_cache(
                row_chat_id,
                message_id,
                file_unique_id,
                {
                    **base_cache,
                    "status": "ok",
                    "text": transcript_text,
                    "note": "Automatic transcript. Verify if critical.",
                },
                date_utc,
                file_path_value,
            )
            stats["created"] += 1

        except Exception as e:
            logging.exception("Unexpected transcript preparation error for message %s", message_id)
            stats["failed"] += 1

    if any(stats[key] for key in ("created", "skipped", "failed")):
        logging.warning("Transcript preparation completed: %s", stats)

    return stats


def collect_transcript_files_for_period(since_dt, until_dt=None, chat_id=None):
    if until_dt is None:
        until_dt = datetime.now(timezone.utc)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    if chat_id is None:
        cur.execute(
            """
            SELECT chat_id, message_id, file_unique_id, date_utc, file_path
            FROM messages
            WHERE date_utc >= ?
              AND date_utc < ?
              AND message_type IN ('voice', 'audio')
            ORDER BY date_utc ASC
            """,
            (since_dt.isoformat(), until_dt.isoformat()),
        )
    else:
        cur.execute(
            """
            SELECT chat_id, message_id, file_unique_id, date_utc, file_path
            FROM messages
            WHERE chat_id = ?
              AND date_utc >= ?
              AND date_utc < ?
              AND message_type IN ('voice', 'audio')
            ORDER BY date_utc ASC
            """,
            (chat_id, since_dt.isoformat(), until_dt.isoformat()),
        )

    rows = cur.fetchall()
    conn.close()

    transcript_files = []
    seen = set()

    for row_chat_id, message_id, file_unique_id, date_utc, file_path_value in rows:
        txt_path = get_transcript_text_file_for_zip(
            row_chat_id,
            message_id,
            file_unique_id,
            date_utc,
            file_path_value,
        )

        if not txt_path:
            continue

        path_key = str(txt_path)
        if path_key in seen:
            continue

        seen.add(path_key)
        transcript_files.append((row_chat_id, message_id, txt_path))

    return transcript_files


def load_contact_sheet_font(size: int):
    if CONTACT_SHEET_FONT_PATH:
        font_path = Path(CONTACT_SHEET_FONT_PATH)
        if font_path.exists():
            try:
                font = ImageFont.truetype(str(font_path), size)
                logging.info("Contact sheet font loaded: %s", font_path)
                return font
            except Exception as e:
                logging.warning("Could not load CONTACT_SHEET_FONT_PATH %s: %s", font_path, e)
        else:
            logging.warning("CONTACT_SHEET_FONT_PATH is set but file does not exist: %s", font_path)

    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]

    for candidate in candidates:
        try:
            font = ImageFont.truetype(candidate, size)
            logging.info("Contact sheet font loaded: %s", candidate)
            return font
        except Exception:
            pass

    logging.warning(
        "Could not find a Unicode TrueType font for contact sheets. "
        "Falling back to ImageFont.load_default(); Cyrillic captions may not render correctly."
    )
    return ImageFont.load_default()


def truncate_contact_sheet_text(draw, text: str, font, max_width: int) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text

    suffix = "..."
    low = 0
    high = len(text)

    while low < high:
        mid = (low + high) // 2
        candidate = text[:mid].rstrip() + suffix
        if draw.textlength(candidate, font=font) <= max_width:
            low = mid + 1
        else:
            high = mid

    return text[:max(0, low - 1)].rstrip() + suffix


def wrap_contact_sheet_text(draw, text: str, font, max_width: int, max_lines: int):
    normalized = " ".join((text or "").split())
    if not normalized or max_lines <= 0:
        return []

    words = normalized.split(" ")
    lines = []
    current = ""
    index = 0

    while index < len(words):
        word = words[index]
        candidate = f"{current} {word}".strip()

        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
            index += 1
            continue

        if current:
            lines.append(current)
            current = ""
        else:
            lines.append(truncate_contact_sheet_text(draw, word, font, max_width))
            index += 1

        if len(lines) == max_lines:
            remaining = " ".join(words[index:])
            if remaining:
                lines[-1] = truncate_contact_sheet_text(draw, f"{lines[-1]} {remaining}", font, max_width)
            return lines

    if current:
        lines.append(current)

    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = truncate_contact_sheet_text(draw, lines[-1], font, max_width)

    return lines


def create_contact_sheets(days: int = 7, chat_id=None, chat_title_for_filename=None, since_dt=None, until_dt=None, period_label=None):
    if since_dt is None:
        since_dt = datetime.now(timezone.utc) - timedelta(days=days)
    if until_dt is None:
        until_dt = datetime.now(timezone.utc)

    scope = "all_chats" if chat_id is None else safe_filename(chat_title_for_filename or str(chat_id))

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    base_where = """
        date_utc >= ?
        AND date_utc < ?
        AND file_path IS NOT NULL
        AND file_skipped = 0
    """

    select_sql = """
        SELECT
            chat_id,
            chat_title,
            message_id,
            date_utc,
            username,
            full_name,
            message_type,
            file_name,
            file_mime_type,
            file_path,
            caption
        FROM messages
    """

    if chat_id is None:
        query = f"""
            {select_sql}
            WHERE {base_where}
            ORDER BY date_utc ASC
        """
        params = (since_dt.isoformat(), until_dt.isoformat())
    else:
        query = f"""
            {select_sql}
            WHERE chat_id = ?
              AND {base_where}
            ORDER BY date_utc ASC
        """
        params = (chat_id, since_dt.isoformat(), until_dt.isoformat())

    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()

    image_items = []

    for row in rows:
        (
            row_chat_id,
            row_chat_title,
            message_id,
            date_utc,
            username,
            full_name,
            message_type,
            file_name,
            file_mime_type,
            file_path_value,
            caption,
        ) = row

        if not is_supported_contact_sheet_image(file_name, file_mime_type, file_path_value):
            continue

        source_path = resolve_stored_file_path(file_path_value)

        if not source_path or not source_path.exists():
            continue

        image_items.append(
            {
                "chat_title": row_chat_title or row_chat_id,
                "message_id": message_id,
                "date_utc": date_utc,
                "username": username,
                "full_name": full_name,
                "message_type": message_type,
                "file_name": file_name or source_path.name,
                "mime_type": file_mime_type,
                "file_path": source_path,
                "caption": caption,
            }
        )

    if not image_items:
        return []

    sheets = []
    font_title = load_contact_sheet_font(34)
    font_meta = load_contact_sheet_font(26)
    font_small = load_contact_sheet_font(22)

    page_width = CONTACT_SHEET_PAGE_WIDTH
    margin = 70
    gutter = 50
    columns = 2
    images_per_page = max(1, CONTACT_SHEET_IMAGES_PER_PAGE)
    tile_width = min(CONTACT_SHEET_THUMB_WIDTH, int((page_width - margin * 2 - gutter) / columns))
    image_max_height = 1500
    caption_height = 210
    tile_height = image_max_height + caption_height
    header_height = 170
    rows_per_page = max(1, (images_per_page + columns - 1) // columns)
    page_height = header_height + margin + rows_per_page * tile_height + (rows_per_page - 1) * gutter + margin

    total_pages = (len(image_items) + images_per_page - 1) // images_per_page

    for page_index in range(total_pages):
        chunk = image_items[page_index * images_per_page:(page_index + 1) * images_per_page]

        sheet = Image.new("RGB", (page_width, page_height), "white")
        draw = ImageDraw.Draw(sheet)

        title = f"Image contact sheet — {chat_title_for_filename or 'all chats'}"
        subtitle = f"Period: {period_label or f'last {days} days'} | Page {page_index + 1}/{total_pages}"

        draw.text((margin, 45), title, fill="black", font=font_title)
        draw.text((margin, 95), subtitle, fill="black", font=font_meta)

        for item_index, item in enumerate(chunk):
            global_index = page_index * images_per_page + item_index + 1

            col = item_index % columns
            row_num = item_index // columns

            x = margin + col * (tile_width + gutter)
            y = header_height + margin + row_num * (tile_height + gutter)

            try:
                with Image.open(item["file_path"]) as img:
                    img = ImageOps.exif_transpose(img)
                    img = img.convert("RGB")

                    original_width, original_height = img.size
                    scale = min(tile_width / original_width, image_max_height / original_height, 1.0)
                    new_width = max(1, int(original_width * scale))
                    new_height = max(1, int(original_height * scale))

                    img = img.resize((new_width, new_height), Image.LANCZOS)

                    image_x = x + int((tile_width - new_width) / 2)
                    image_y = y

                    sheet.paste(img, (image_x, image_y))

                    draw.rectangle(
                        [x, y, x + tile_width, y + image_max_height],
                        outline=(180, 180, 180),
                        width=2,
                    )

            except Exception as e:
                draw.rectangle(
                    [x, y, x + tile_width, y + image_max_height],
                    outline=(180, 180, 180),
                    width=2,
                )
                draw.text((x + 20, y + 20), f"Could not render image: {e}", fill="black", font=font_small)

            try:
                parsed_dt = datetime.fromisoformat(item["date_utc"])
                local_dt = parsed_dt.astimezone(APP_TIMEZONE)
                date_text = local_dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                date_text = item["date_utc"]

            sender = item["full_name"] or item["username"] or "Unknown"
            file_name = item["file_name"]
            caption = item["caption"] or ""

            caption_y = y + image_max_height + 18

            lines = [
                f"Attachment image {global_index}",
                f"{date_text} | {sender}",
                truncate_contact_sheet_text(draw, f"{file_name}", font_small, tile_width),
            ]

            if caption:
                lines.extend(
                    wrap_contact_sheet_text(
                        draw,
                        f"Caption: {caption}",
                        font_small,
                        tile_width,
                        max_lines=3,
                    )
                )

            for line_index, line in enumerate(lines):
                draw.text(
                    (x, caption_y + line_index * 34),
                    line,
                    fill="black",
                    font=font_small if line_index else font_meta,
                )

        sheet_path = EXPORT_DIR / f"04_image_contact_sheet_{scope}_{page_index + 1}.jpg"
        sheet.save(sheet_path, "JPEG", quality=CONTACT_SHEET_JPEG_QUALITY, optimize=True)
        sheets.append(sheet_path)

    return sheets


def create_attachments_index(days: int = 7, chat_id=None, chat_title_for_filename=None, since_dt=None, until_dt=None, period_label=None):
    if since_dt is None:
        since_dt = datetime.now(timezone.utc) - timedelta(days=days)
    if until_dt is None:
        until_dt = datetime.now(timezone.utc)

    scope = "all_chats" if chat_id is None else safe_filename(chat_title_for_filename or str(chat_id))
    export_path = EXPORT_DIR / f"02_attachments_index_{scope}.md"

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    base_where = """
        date_utc >= ?
        AND date_utc < ?
        AND (
            file_id IS NOT NULL
            OR file_path IS NOT NULL
            OR file_skipped = 1
            OR message_type IN ('photo', 'document', 'video', 'voice', 'audio', 'animation')
        )
    """

    if chat_id is None:
        query = f"""
            SELECT
                id, chat_id, chat_title, message_id, date_utc, username, full_name,
                message_type, text, caption, file_id, file_unique_id, file_name, file_mime_type,
                file_size, file_path, file_skipped, file_skip_reason
            FROM messages
            WHERE {base_where}
            ORDER BY date_utc ASC
        """
        params = (since_dt.isoformat(), until_dt.isoformat())
    else:
        query = f"""
            SELECT
                id, chat_id, chat_title, message_id, date_utc, username, full_name,
                message_type, text, caption, file_id, file_unique_id, file_name, file_mime_type,
                file_size, file_path, file_skipped, file_skip_reason
            FROM messages
            WHERE chat_id = ?
              AND {base_where}
            ORDER BY date_utc ASC
        """
        params = (chat_id, since_dt.isoformat(), until_dt.isoformat())

    cur.execute(query, params)
    rows = cur.fetchall()

    with export_path.open("w", encoding="utf-8") as f:
        f.write("# Attachments index\n\n")
        f.write(f"Period: {period_label or f'last {days} days'}\n\n")

        if chat_id is not None:
            f.write(f"Chat: {chat_title_for_filename or chat_id}\n\n")
        else:
            f.write("Scope: all chats\n\n")

        f.write(
            "This file lists attachments and file-like messages from the Telegram export. "
            "Use it together with 01_chat_export.md.\n\n"
        )

        if not rows:
            f.write("No attachments found for this period.\n")

        for number, row in enumerate(rows, start=1):
            (
                db_id, row_chat_id, row_chat_title, message_id, date_utc, username, full_name,
                message_type, text_value, caption, file_id, file_unique_id, file_name, file_mime_type,
                file_size, file_path, file_skipped, file_skip_reason
            ) = row

            display_name = full_name or username or "Unknown"

            f.write(f"## Attachment {number}\n\n")
            f.write(f"- Date UTC: `{date_utc}`\n")
            f.write(f"- Chat: `{row_chat_title or row_chat_id}`\n")
            f.write(f"- Message ID: `{message_id}`\n")
            f.write(f"- Sender: `{display_name}`\n")
            f.write(f"- Message type: `{message_type}`\n")

            if file_name:
                f.write(f"- File name: `{file_name}`\n")
            if file_mime_type:
                f.write(f"- MIME type: `{file_mime_type}`\n")
            if file_size:
                f.write(f"- File size: `{file_size}` bytes\n")
            if file_path:
                f.write(f"- Local path: `{file_path}`\n")
            if file_skipped:
                f.write(f"- File skipped: `yes`\n")
                f.write(f"- Skip reason: `{file_skip_reason}`\n")

            if message_type in ("voice", "audio"):
                transcript_cache = load_transcript_cache(
                    row_chat_id,
                    message_id,
                    file_unique_id,
                    date_utc,
                    file_path,
                )
                transcript_text = format_transcript_for_export(transcript_cache, style="attachment")

                if transcript_text:
                    f.write(transcript_text)
                else:
                    f.write("- Transcript: `not available`\n")
                    f.write("- Transcript note: ask user to paste Telegram Premium transcript if needed.\n")

            if caption:
                f.write(f"- Caption: {caption}\n")
            if text_value and text_value != caption:
                f.write(f"- Message text: {text_value}\n")

            cur.execute(
                """
                SELECT date_utc, full_name, username, message_type, text, caption, file_name
                FROM messages
                WHERE chat_id = ?
                  AND date_utc < ?
                ORDER BY date_utc DESC
                LIMIT 2
                """,
                (row_chat_id, date_utc),
            )
            previous_rows = list(reversed(cur.fetchall()))

            cur.execute(
                """
                SELECT date_utc, full_name, username, message_type, text, caption, file_name
                FROM messages
                WHERE chat_id = ?
                  AND date_utc > ?
                ORDER BY date_utc ASC
                LIMIT 2
                """,
                (row_chat_id, date_utc),
            )
            next_rows = cur.fetchall()

            if previous_rows:
                f.write("\n### Previous context\n\n")
                for ctx in previous_rows:
                    ctx_date, ctx_full_name, ctx_username, ctx_type, ctx_text, ctx_caption, ctx_file_name = ctx
                    ctx_sender = ctx_full_name or ctx_username or "Unknown"
                    ctx_body = ctx_text or ctx_caption or ctx_file_name or ""
                    f.write(f"- `{ctx_date}` {ctx_sender} [{ctx_type}]: {ctx_body}\n")

            if next_rows:
                f.write("\n### Next context\n\n")
                for ctx in next_rows:
                    ctx_date, ctx_full_name, ctx_username, ctx_type, ctx_text, ctx_caption, ctx_file_name = ctx
                    ctx_sender = ctx_full_name or ctx_username or "Unknown"
                    ctx_body = ctx_text or ctx_caption or ctx_file_name or ""
                    f.write(f"- `{ctx_date}` {ctx_sender} [{ctx_type}]: {ctx_body}\n")

            f.write("\n---\n\n")

    conn.close()
    return export_path, len(rows)


def create_prompt_file(prompt_text: str, chat_title_for_filename: str, prefix: str = "03_work_analysis_prompt"):
    scope = safe_filename(chat_title_for_filename or "chat")
    prompt_path = EXPORT_DIR / f"{prefix}_{scope}.txt"
    prompt_path.write_text(prompt_text, encoding="utf-8")
    return prompt_path


def create_zip_export(days: int = 7, chat_id=None, chat_title_for_filename=None):
    export_path, count = export_last_days(
        days,
        chat_id=chat_id,
        chat_title_for_filename=chat_title_for_filename,
    )

    scope = "all_chats" if chat_id is None else safe_filename(chat_title_for_filename or str(chat_id))
    zip_path = EXPORT_DIR / f"05_full_archive_{scope}_last_{days}_days.zip"

    if zip_path.exists():
        zip_path.unlink()

    since = datetime.now(timezone.utc) - timedelta(days=days)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    if chat_id is None:
        cur.execute(
            """
            SELECT file_path
            FROM messages
            WHERE date_utc >= ? AND file_path IS NOT NULL
            ORDER BY date_utc ASC
            """,
            (since.isoformat(),),
        )
    else:
        cur.execute(
            """
            SELECT file_path
            FROM messages
            WHERE chat_id = ? AND date_utc >= ? AND file_path IS NOT NULL
            ORDER BY date_utc ASC
            """,
            (chat_id, since.isoformat()),
        )

    file_rows = cur.fetchall()
    conn.close()

    temp_dir = EXPORT_DIR / f"zip_temp_{scope}_{days}"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)

    temp_dir.mkdir(parents=True, exist_ok=True)

    temp_markdown_path = temp_dir / export_path.name
    shutil.copy2(export_path, temp_markdown_path)

    temp_files_dir = temp_dir / "files"
    temp_files_dir.mkdir(exist_ok=True)

    copied_files = 0

    for (relative_file_path,) in file_rows:
        source_path = BASE_DIR / relative_file_path

        if not source_path.exists():
            continue

        target_name = safe_filename(source_path.name)
        target_path = temp_files_dir / target_name

        if target_path.exists():
            target_path = temp_files_dir / f"{copied_files + 1}_{target_name}"

        shutil.copy2(source_path, target_path)
        copied_files += 1

    transcript_rows = collect_transcript_files_for_period(since, chat_id=chat_id)

    if transcript_rows:
        temp_transcripts_dir = temp_dir / "transcripts"
        temp_transcripts_dir.mkdir(exist_ok=True)

        for transcript_chat_id, transcript_message_id, transcript_path in transcript_rows:
            target_name = safe_filename(f"{transcript_chat_id}_{transcript_message_id}_{transcript_path.name}")
            shutil.copy2(transcript_path, temp_transcripts_dir / target_name)

    shutil.make_archive(
        base_name=str(zip_path.with_suffix("")),
        format="zip",
        root_dir=temp_dir,
    )

    shutil.rmtree(temp_dir)

    return zip_path, count, copied_files



def human_file_size(size_bytes: int) -> str:
    if size_bytes is None:
        return "unknown"

    size = float(size_bytes)

    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024

    return f"{size_bytes} B"


async def send_document_safely(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path: Path, caption: str = None) -> bool:
    if not file_path.exists():
        await update.message.reply_text(
            f"Не удалось отправить файл: файл не найден на сервере.\n\n{file_path.name}"
        )
        return False

    file_size = file_path.stat().st_size

    if file_size > MAX_TELEGRAM_SEND_SIZE_BYTES:
        await update.message.reply_text(
            "Файл сформирован, но он слишком большой для стабильной отправки через Telegram.\n\n"
            f"Файл: {file_path.name}\n"
            f"Размер: {human_file_size(file_size)}\n"
            f"Лимит отправки: {MAX_TELEGRAM_SEND_SIZE_MB} MB\n\n"
            "Попробуй Markdown-экспорт командой /export_week или сделаем отдельный облегчённый ZIP без тяжёлых вложений."
        )
        return False

    try:
        with file_path.open("rb") as f:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=f,
                filename=file_path.name,
                caption=caption,
                reply_to_message_id=update.message.message_id if update.message else None,
                connect_timeout=TELEGRAM_SEND_TIMEOUT_SECONDS,
                read_timeout=TELEGRAM_SEND_TIMEOUT_SECONDS,
                write_timeout=TELEGRAM_SEND_TIMEOUT_SECONDS,
                pool_timeout=TELEGRAM_SEND_TIMEOUT_SECONDS,
            )

        return True

    except TimedOut:
        await update.message.reply_text(
            "Telegram не успел принять файл и оборвал отправку по таймауту.\n\n"
            f"Файл: {file_path.name}\n"
            f"Размер: {human_file_size(file_size)}\n\n"
            "Можно попробовать ещё раз позже или сделать облегчённый ZIP без тяжёлых вложений."
        )
        return False

    except TelegramError as e:
        await update.message.reply_text(
            "Telegram вернул ошибку при отправке файла.\n\n"
            f"Файл: {file_path.name}\n"
            f"Размер: {human_file_size(file_size)}\n"
            f"Ошибка: {e}"
        )
        return False

    except Exception as e:
        await update.message.reply_text(
            "Не удалось отправить файл из-за непредвиденной ошибки.\n\n"
            f"Файл: {file_path.name}\n"
            f"Размер: {human_file_size(file_size)}\n"
            f"Ошибка: {e}"
        )
        return False



def get_current_work_shift_interval():
    now_local = datetime.now(APP_TIMEZONE)
    shift_start_local = now_local.replace(
        hour=WORK_SHIFT_START_HOUR,
        minute=0,
        second=0,
        microsecond=0,
    )

    if now_local < shift_start_local:
        shift_start_local = shift_start_local - timedelta(days=1)

    shift_end_local = now_local

    since_utc = shift_start_local.astimezone(timezone.utc)
    until_utc = shift_end_local.astimezone(timezone.utc)

    period_label = (
        f"{shift_start_local.strftime('%Y-%m-%d %H:%M')} — "
        f"{shift_end_local.strftime('%Y-%m-%d %H:%M')} "
        f"{APP_TIMEZONE.key if hasattr(APP_TIMEZONE, 'key') else APP_TIMEZONE}"
    )

    return since_utc, until_utc, period_label


def export_interval(since_dt, until_dt, chat_id=None, chat_title_for_filename=None, period_label=None):
    scope = "all_chats" if chat_id is None else safe_filename(chat_title_for_filename or str(chat_id))
    export_path = EXPORT_DIR / f"01_chat_export_{scope}_work_shift.md"

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    if chat_id is None:
        cur.execute(
            """
            SELECT
                chat_id,
                chat_title,
                message_id,
                date_utc,
                username,
                full_name,
                message_type,
                text,
                caption,
                file_unique_id,
                file_name,
                file_mime_type,
                file_size,
                file_path,
                file_skipped,
                file_skip_reason
            FROM messages
            WHERE date_utc >= ?
              AND date_utc < ?
            ORDER BY date_utc ASC
            """,
            (since_dt.isoformat(), until_dt.isoformat()),
        )
    else:
        cur.execute(
            """
            SELECT
                chat_id,
                chat_title,
                message_id,
                date_utc,
                username,
                full_name,
                message_type,
                text,
                caption,
                file_unique_id,
                file_name,
                file_mime_type,
                file_size,
                file_path,
                file_skipped,
                file_skip_reason
            FROM messages
            WHERE chat_id = ?
              AND date_utc >= ?
              AND date_utc < ?
            ORDER BY date_utc ASC
            """,
            (chat_id, since_dt.isoformat(), until_dt.isoformat()),
        )

    rows = cur.fetchall()
    conn.close()

    with export_path.open("w", encoding="utf-8") as f:
        f.write("# Telegram work shift export\n\n")

        if chat_id is not None:
            f.write(f"Scope: {chat_title_for_filename or chat_id}\n\n")
        else:
            f.write("Scope: all chats\n\n")

        f.write(f"Period: {period_label or f'{since_dt.isoformat()} — {until_dt.isoformat()}'}\n\n")
        f.write(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n")
        f.write(f"Messages: {len(rows)}\n\n")
        f.write("---\n\n")

        current_date = None

        for row in rows:
            (
                row_chat_id,
                row_chat_title,
                message_id,
                date_utc,
                username,
                full_name,
                message_type,
                text_value,
                caption,
                file_unique_id,
                file_name,
                file_mime_type,
                file_size,
                file_path,
                file_skipped,
                file_skip_reason,
            ) = row

            try:
                parsed_dt = datetime.fromisoformat(date_utc)
                local_dt = parsed_dt.astimezone(APP_TIMEZONE)
                message_date = local_dt.strftime("%Y-%m-%d")
                message_time = local_dt.strftime("%H:%M")
            except Exception:
                message_date = date_utc[:10]
                message_time = date_utc[11:16]

            if message_date != current_date:
                current_date = message_date
                f.write(f"\n## {current_date}\n\n")

            sender = full_name or username or "Unknown"

            f.write(f"### {message_time} — {sender}")
            if username:
                f.write(f" (@{username})")
            f.write("\n\n")

            f.write(f"- Chat: `{row_chat_title or row_chat_id}`\n")
            f.write(f"- Message ID: `{message_id}`\n")
            f.write(f"- Type: `{message_type}`\n")

            if file_name:
                f.write(f"- File name: `{file_name}`\n")
            if file_mime_type:
                f.write(f"- File MIME type: `{file_mime_type}`\n")
            if file_size:
                f.write(f"- File size: `{file_size}` bytes\n")
            if file_path:
                f.write(f"- File path: `{file_path}`\n")
            if file_skipped:
                f.write("- File skipped: `yes`\n")
                f.write(f"- Skip reason: `{file_skip_reason}`\n")

            body_parts = []
            if text_value:
                body_parts.append(text_value)
            if caption and caption != text_value:
                body_parts.append(f"Caption: {caption}")

            if message_type in ("voice", "audio"):
                transcript_cache = load_transcript_cache(
                    row_chat_id,
                    message_id,
                    file_unique_id,
                    date_utc,
                    file_path,
                )
                transcript_text = format_transcript_for_export(transcript_cache)

                if transcript_text:
                    body_parts.append(transcript_text)

            if body_parts:
                f.write("\n")
                f.write("\n\n".join(body_parts))
                f.write("\n\n")
            else:
                f.write("\n_No text content_\n\n")

            f.write("---\n\n")

    return export_path, len(rows)


def create_zip_export_interval(since_dt, until_dt, chat_id=None, chat_title_for_filename=None):
    export_path, count = export_interval(
        since_dt,
        until_dt,
        chat_id=chat_id,
        chat_title_for_filename=chat_title_for_filename,
    )

    scope = "all_chats" if chat_id is None else safe_filename(chat_title_for_filename or str(chat_id))
    zip_path = EXPORT_DIR / f"05_full_archive_{scope}_work_shift.zip"

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    if chat_id is None:
        cur.execute(
            """
            SELECT file_path
            FROM messages
            WHERE date_utc >= ?
              AND date_utc < ?
              AND file_path IS NOT NULL
              AND file_skipped = 0
            ORDER BY date_utc ASC
            """,
            (since_dt.isoformat(), until_dt.isoformat()),
        )
    else:
        cur.execute(
            """
            SELECT file_path
            FROM messages
            WHERE chat_id = ?
              AND date_utc >= ?
              AND date_utc < ?
              AND file_path IS NOT NULL
              AND file_skipped = 0
            ORDER BY date_utc ASC
            """,
            (chat_id, since_dt.isoformat(), until_dt.isoformat()),
        )

    file_rows = cur.fetchall()
    conn.close()

    copied_files = 0

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(export_path, arcname=export_path.name)

        for (relative_file_path,) in file_rows:
            # file_path in DB may be stored either as:
            # - "files/2026-..."
            # - "data/files/2026-..."
            # so try both DATA_DIR-relative and BASE_DIR-relative paths.
            source_path = DATA_DIR / relative_file_path

            if not source_path.exists():
                source_path = BASE_DIR / relative_file_path

            if source_path.exists() and source_path.is_file():
                zf.write(source_path, arcname=f"files/{source_path.name}")
                copied_files += 1

        for transcript_chat_id, transcript_message_id, transcript_path in collect_transcript_files_for_period(
            since_dt,
            until_dt,
            chat_id=chat_id,
        ):
            arcname = safe_filename(f"{transcript_chat_id}_{transcript_message_id}_{transcript_path.name}")
            zf.write(transcript_path, arcname=f"transcripts/{arcname}")

    return zip_path, count, copied_files


async def send_document_safely_to_chat(context: ContextTypes.DEFAULT_TYPE, target_chat_id: int, file_path: Path, caption: str = None) -> bool:
    if not file_path.exists():
        await context.bot.send_message(
            chat_id=target_chat_id,
            text=f"Не удалось отправить файл: файл не найден на сервере.\n\n{file_path.name}",
        )
        return False

    file_size = file_path.stat().st_size

    if file_size > MAX_TELEGRAM_SEND_SIZE_BYTES:
        await context.bot.send_message(
            chat_id=target_chat_id,
            text=(
                "Файл сформирован, но он слишком большой для стабильной отправки через Telegram.\n\n"
                f"Файл: {file_path.name}\n"
                f"Размер: {human_file_size(file_size)}\n"
                f"Лимит отправки: {MAX_TELEGRAM_SEND_SIZE_MB} MB"
            ),
        )
        return False

    try:
        with file_path.open("rb") as f:
            await context.bot.send_document(
                chat_id=target_chat_id,
                document=f,
                filename=file_path.name,
                caption=caption,
                connect_timeout=TELEGRAM_SEND_TIMEOUT_SECONDS,
                read_timeout=TELEGRAM_SEND_TIMEOUT_SECONDS,
                write_timeout=TELEGRAM_SEND_TIMEOUT_SECONDS,
                pool_timeout=TELEGRAM_SEND_TIMEOUT_SECONDS,
            )

        return True

    except TimedOut:
        await context.bot.send_message(
            chat_id=target_chat_id,
            text=(
                "Telegram не успел принять файл и оборвал отправку по таймауту.\n\n"
                f"Файл: {file_path.name}\n"
                f"Размер: {human_file_size(file_size)}"
            ),
        )
        return False

    except TelegramError as e:
        await context.bot.send_message(
            chat_id=target_chat_id,
            text=(
                "Telegram вернул ошибку при отправке файла.\n\n"
                f"Файл: {file_path.name}\n"
                f"Размер: {human_file_size(file_size)}\n"
                f"Ошибка: {e}"
            ),
        )
        return False

    except Exception as e:
        await context.bot.send_message(
            chat_id=target_chat_id,
            text=(
                "Не удалось отправить файл из-за непредвиденной ошибки.\n\n"
                f"Файл: {file_path.name}\n"
                f"Размер: {human_file_size(file_size)}\n"
                f"Ошибка: {e}"
            ),
        )
        return False




def export_last_days(days: int = 7, chat_id=None, chat_title_for_filename=None):
    since = datetime.now(timezone.utc) - timedelta(days=days)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    if chat_id is None:
        cur.execute(
            """
            SELECT
                chat_id,
                date_utc,
                chat_title,
                message_id,
                full_name,
                username,
                message_type,
                text,
                caption,
                reply_to_message_id,
                file_unique_id,
                file_name,
                file_mime_type,
                file_size,
                file_path,
                file_skipped,
                file_skip_reason
            FROM messages
            WHERE date_utc >= ?
            ORDER BY date_utc ASC
            """,
            (since.isoformat(),),
        )
    else:
        cur.execute(
            """
            SELECT
                chat_id,
                date_utc,
                chat_title,
                message_id,
                full_name,
                username,
                message_type,
                text,
                caption,
                reply_to_message_id,
                file_unique_id,
                file_name,
                file_mime_type,
                file_size,
                file_path,
                file_skipped,
                file_skip_reason
            FROM messages
            WHERE chat_id = ? AND date_utc >= ?
            ORDER BY date_utc ASC
            """,
            (chat_id, since.isoformat()),
        )

    rows = cur.fetchall()
    conn.close()

    scope = "all_chats" if chat_id is None else safe_filename(chat_title_for_filename or str(chat_id))
    export_path = EXPORT_DIR / f"telegram_export_{scope}_last_{days}_days.md"

    with open(export_path, "w", encoding="utf-8") as f:
        f.write(f"# Telegram export — last {days} days\n\n")
        f.write(f"Scope: {scope}\n\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write(f"Messages: {len(rows)}\n\n")
        f.write("---\n\n")

        current_date = None

        for row in rows:
            (
                row_chat_id,
                date_utc,
                chat_title,
                message_id,
                full_name,
                username,
                message_type,
                text,
                caption,
                reply_to_message_id,
                file_unique_id,
                file_name,
                file_mime_type,
                file_size,
                file_path,
                file_skipped,
                file_skip_reason,
            ) = row

            dt = datetime.fromisoformat(date_utc)
            local_dt = dt.astimezone()
            date_label = local_dt.strftime("%Y-%m-%d")

            if date_label != current_date:
                current_date = date_label
                f.write(f"\n## {current_date}\n\n")

            time_label = local_dt.strftime("%H:%M")
            author = full_name or username or "Unknown"
            username_part = f" (@{username})" if username else ""

            f.write(f"### {time_label} — {author}{username_part}\n\n")
            f.write(f"- Chat: `{chat_title}`\n")
            f.write(f"- Message ID: `{message_id}`\n")
            f.write(f"- Type: `{message_type}`\n")

            if reply_to_message_id:
                f.write(f"- Reply to message ID: `{reply_to_message_id}`\n")

            if file_path:
                f.write(f"- File name: `{file_name}`\n")
                f.write(f"- File MIME type: `{file_mime_type}`\n")
                f.write(f"- File size: `{file_size}` bytes\n")
                f.write(f"- File path: `{file_path}`\n")
            elif file_skipped:
                f.write(f"- File name: `{file_name}`\n")
                f.write(f"- File MIME type: `{file_mime_type}`\n")
                f.write(f"- File size: `{file_size}` bytes\n")
                f.write(f"- File skipped: `yes`\n")
                f.write(f"- Skip reason: `{file_skip_reason}`\n")

            content_parts = []
            content = text or caption or ""

            if content:
                content_parts.append(content.strip())

            if message_type in ("voice", "audio"):
                transcript_cache = load_transcript_cache(
                    row_chat_id,
                    message_id,
                    file_unique_id,
                    date_utc,
                    file_path,
                )
                transcript_text = format_transcript_for_export(transcript_cache)

                if transcript_text:
                    content_parts.append(transcript_text)

            if content_parts:
                f.write("\n")
                f.write("\n\n".join(content_parts))
                f.write("\n\n")
            else:
                f.write("\n")
                f.write("_No text content_\n\n")

            f.write("---\n\n")

    return export_path, len(rows)



def get_gemini_summary_prompt(chat_title: str, days: int = 7) -> str:
    return f"""Ты — аналитик рабочей коммуникации и помощник по управлению задачами.

Я загрузил ZIP-архив с экспортом Telegram-чата "{chat_title}" за последние {days} дней.

Внутри архива есть:
- Markdown-файл с сообщениями чата;
- папка files с вложениями, которые были отправлены в чат.

Твоя задача — внимательно проанализировать весь архив: сообщения, подписи к файлам, названия файлов и сами вложения, если они доступны для чтения.

Сделай структурированную выжимку по переписке.

Нужный формат ответа:

1. Краткое резюме периода
В 5–10 предложениях опиши, что в целом обсуждали, какие были основные события, решения и рабочий контекст.

2. Основные темы обсуждения
Сгруппируй переписку по темам. Для каждой темы укажи:
- краткое описание;
- ключевые сообщения или выводы;
- связанные файлы, если они есть;
- текущий статус темы: закрыта / в работе / требует внимания / непонятно.

3. Задачи и поручения
Составь таблицу:
- задача;
- ответственный, если его можно определить;
- срок, если он был указан или подразумевается;
- источник/контекст из переписки;
- статус: новая / в работе / ожидает ответа / выполнена / риск просрочки.

Если ответственный или срок не указаны явно, не выдумывай. Напиши «не указан» или «можно предположить, но требуется подтверждение».

4. Открытые вопросы
Выдели вопросы, которые обсуждались, но не были закрыты. Для каждого укажи, кто должен ответить или что нужно сделать дальше.

5. Решения и договорённости
Отдельно перечисли все решения, договорённости и подтверждения, которые были явно зафиксированы в переписке.

6. Риски и проблемные места
Отметь возможные риски: задержки, неопределённость, отсутствие ответственного, финансовые вопросы, конфликтные моменты, срочные задачи, вопросы, которые могут быть забыты.

7. Файлы и вложения
Составь список всех важных файлов из архива. Для каждого укажи:
- название файла;
- к какой теме относится;
- что в нём содержится, если файл можно прочитать;
- какие действия с ним нужны, если это понятно из переписки.

8. Что нужно сделать дальше
Сформируй короткий список приоритетных следующих шагов на ближайшие дни.

9. Очень короткая executive summary
В конце дай краткую версию на 5–7 пунктов, которую можно быстро отправить руководителю или команде.

Важные правила:
- Не выдумывай факты, имена, сроки или решения.
- Если информация неочевидна, помечай её как предположение.
- Не пересказывай всю переписку подряд, а группируй информацию по смыслу.
- Сохраняй деловой, нейтральный стиль.
- Если в архиве есть персональные данные или чувствительная информация, не распространяй их без необходимости, а используй только для понимания контекста задач.
- Если какие-то вложения не удалось прочитать, явно укажи это в разделе файлов.
"""



def get_friendly_summary_prompt(chat_title: str, days: int = 7) -> str:
    return f"""Ты — внимательный и ироничный редактор дружеского Telegram-чата.

Я загрузил ZIP-архив с экспортом Telegram-чата "{chat_title}" за последние {days} дней.

Внутри архива есть:
- Markdown-файл с сообщениями чата;
- папка files с вложениями, которые были отправлены в чат.

Твоя задача — проанализировать переписку не как рабочий отчёт, а как живой дружеский чат: с шутками, мемами, планами, внезапными темами, внутренними приколами и общим настроением.

Сделай дружескую выжимку по чату.

Нужный формат ответа:

1. Краткое резюме периода
В 5–10 предложениях опиши, что происходило в чате: какие темы всплывали, кто что обсуждал, какой был общий вайб.

2. Главные темы недели
Сгруппируй обсуждения по темам. Для каждой темы укажи:
- о чём говорили;
- кто был наиболее активен;
- чем всё закончилось или осталось ли висеть в воздухе.

3. Планы и договорённости
Отдельно выпиши всё, что похоже на реальные планы:
- встретиться;
- куда-то сходить;
- что-то посмотреть;
- кому-то что-то прислать;
- что-то купить, принести или забронировать;
- созвониться;
- вернуться к теме позже.

Если дата, время или место не указаны — так и напиши.

4. Кто что обещал или должен сделать
Составь мягкий список “обещалки и хвосты”:
- кто;
- что обещал, планировал или предлагал;
- насколько это явно сказано;
- нужно ли напомнить.

Не превращай это в строгий таск-трекер. Стиль должен быть дружеский.

5. Лучшие шутки, мемы и фразы
Выдели самые смешные или характерные моменты переписки. Не цитируй огромные куски, только короткие фразы или пересказ. Если юмор завязан на контекст, кратко объясни.

6. Внутренние приколы и повторяющиеся мотивы
Отметь, какие темы, слова, персонажи, мемы или ситуации повторялись.

7. Файлы, фото и вложения
Составь список заметных вложений:
- что это за файл или фото;
- к какой теме относится;
- почему оно было отправлено;
- есть ли с ним какой-то смешной или важный контекст.

Если вложение нельзя прочитать или понять, честно укажи это.

8. Настроение чата
Опиши общий тон: спокойный, хаотичный, мемный, токсичный, заботливый, сонный, тревожный, праздничный и т.п. Можно с лёгкой иронией, но без грубости.

9. Что можно напомнить друзьям
Короткий список вещей, которые стоит вернуть в чат:
- незакрытые планы;
- забытые договорённости;
- хорошие темы, к которым можно вернуться;
- смешные моменты, которые можно обыграть.

10. Суперкороткая версия
В конце дай 5–7 пунктов “что было в чате, если читать лень”.

Важные правила:
- Не выдумывай события, обещания или отношения между людьми.
- Если что-то неясно, помечай как предположение.
- Не делай выводов о личной жизни, здоровье, финансах или чувствительных темах, если это прямо не обсуждалось и не нужно для понимания переписки.
- Не пересказывай весь чат подряд.
- Не пиши слишком официально.
- Стиль: живой, дружеский, с лёгкой иронией, но без злости и без оскорблений.
- Если в чате есть чувствительные или личные сообщения, обобщай аккуратно и не выноси лишние детали.
"""



WORK_ANALYSIS_PROMPT_FALLBACK = """Ты — аналитик рабочей коммуникации.

Проанализируй рабочий пакет Telegram-чата "{chat_title}" за период: {period_label}.

Используй chat_export.md, attachments_index.md, доступные вложения, скриншоты, видимое содержание изображений и transcripts voice/audio, если они есть.
Не выдумывай отсутствующую информацию. Если что-то неясно, пиши “не указано / требуется подтверждение”.

По умолчанию дай компактный отчёт на русском, пригодный для краткого PDF на 1–2 страницы:
1. Краткий вывод
2. Ключевые темы
3. Решения и статус
4. Задачи
5. Риски и открытые вопросы
6. Провайдеры и партнёры, если применимо
7. Следующие шаги
"""


def resolve_work_analysis_prompt_path(path: Path) -> Path:
    return path if path.is_absolute() else BASE_DIR / path


def load_work_analysis_prompt_template() -> str:
    custom_path = resolve_work_analysis_prompt_path(WORK_ANALYSIS_PROMPT_PATH)
    candidates = [
        ("custom", custom_path),
        ("example", WORK_ANALYSIS_PROMPT_EXAMPLE_PATH),
    ]

    for source_name, path in candidates:
        try:
            if path.exists():
                logging.info("Work analysis prompt template loaded from %s: %s", source_name, path)
                return path.read_text(encoding="utf-8")
        except Exception as e:
            logging.warning("Could not load work analysis prompt template from %s: %s", path, e)

    logging.warning(
        "Work analysis prompt template not found. "
        "Using built-in generic fallback prompt."
    )
    return WORK_ANALYSIS_PROMPT_FALLBACK


def render_work_analysis_prompt_template(template: str, chat_title: str, period_label: str) -> str:
    return (
        template
        .replace("{chat_title}", chat_title)
        .replace("{period_label}", period_label)
    )


def get_work_analysis_prompt_source():
    custom_path = resolve_work_analysis_prompt_path(WORK_ANALYSIS_PROMPT_PATH)
    if custom_path.exists():
        return "custom file", custom_path
    if WORK_ANALYSIS_PROMPT_EXAMPLE_PATH.exists():
        return "example file", WORK_ANALYSIS_PROMPT_EXAMPLE_PATH
    return "fallback", None


def get_prompt_backup_path(prompt_path: Path) -> Path:
    timestamp = datetime.now(APP_TIMEZONE).strftime("%Y%m%d-%H%M%S")
    return prompt_path.with_name(f"work_analysis_prompt.backup-{timestamp}.txt")


def get_chatgpt_work_prompt(chat_title: str, days: int = 7, period_label: str = None) -> str:
    period_text = period_label or f"последние {days} дней"
    template = load_work_analysis_prompt_template()
    return render_work_analysis_prompt_template(template, chat_title, period_text)


def limit_report_input_text(label: str, text: str, max_chars: int) -> str:
    text = text or ""
    if max_chars <= 0 or len(text) <= max_chars:
        return text

    head_chars = max_chars * 2 // 3
    tail_chars = max_chars - head_chars
    return (
        text[:head_chars]
        + f"\n\n[... {label} truncated to fit OPENAI_REPORT_MAX_INPUT_CHARS ...]\n\n"
        + text[-tail_chars:]
    )


def extract_openai_response_text(response) -> str:
    output_text = getattr(response, "output_text", None)
    if output_text:
        return output_text.strip()

    parts = []
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            text = getattr(content, "text", None)
            if text:
                parts.append(text)

    return "\n".join(parts).strip()


def get_image_mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    return "image/jpeg"


def build_openai_report_image_inputs(contact_sheet_paths):
    if not OPENAI_REPORT_INCLUDE_IMAGES:
        return []

    max_images = max(0, OPENAI_REPORT_MAX_IMAGES)
    if max_images <= 0:
        return []

    image_inputs = []
    for path in (contact_sheet_paths or [])[:max_images]:
        path = Path(path)
        if not path.exists() or not path.is_file():
            continue

        try:
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        except Exception as e:
            logging.warning("Could not read AI PDF contact sheet image %s: %s", path.name, e)
            continue

        mime_type = get_image_mime_type(path)
        image_inputs.append({
            "type": "input_image",
            "image_url": f"data:{mime_type};base64,{encoded}",
        })

    return image_inputs


def parse_ai_report_json(report_text: str):
    expected_keys = {
        "summary",
        "key_topics",
        "decisions",
        "tasks",
        "risks_open_questions",
        "providers_partners",
        "next_steps",
        "important_facts",
    }
    text = (report_text or "").strip()
    if not text:
        return None

    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if fence_match:
        text = fence_match.group(1).strip()

    candidates = [text]
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidates.append(text[first_brace:last_brace + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict) and any(key in parsed for key in expected_keys):
            return parsed

    return None


def compact_report_text(value, max_chars: int, default: str = "") -> str:
    text = report_text_value(value, default)
    text = re.sub(r"\s+", " ", text).strip()
    if max_chars and len(text) > max_chars:
        return text[:max(0, max_chars - 1)].rstrip(" ,.;:") + "…"
    return text


def normalize_report_dedupe_key(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").lower())
    text = re.sub(r"[^\wа-яё0-9]+", " ", text, flags=re.IGNORECASE)
    return text.strip()


def is_duplicate_report_item(text: str, seen_keys: set) -> bool:
    key = normalize_report_dedupe_key(text)
    if not key:
        return True

    prefix = key[:90]
    if key in seen_keys or prefix in seen_keys:
        return True

    for existing in seen_keys:
        if len(existing) >= 45 and len(prefix) >= 45:
            if existing.startswith(prefix[:45]) or prefix.startswith(existing[:45]):
                return True

    seen_keys.add(key)
    seen_keys.add(prefix)
    return False


def compact_string_list(items, limit: int, max_chars: int, seen_keys: set):
    compacted = []
    for item in report_list(items):
        text = compact_report_text(item, max_chars)
        if is_duplicate_report_item(text, seen_keys):
            continue
        compacted.append(text)
        if len(compacted) >= limit:
            break
    return compacted


def compact_pdf_short_value(value: str) -> str:
    text = report_text_value(value)
    lowered = text.lower()
    if "требуется назначить" in lowered:
        return "не назначен"
    if "требуется подтверждение" in lowered:
        return "не указан"
    if "не указан" in lowered:
        return "не указан"
    if "не назначен" in lowered:
        return "не назначен"
    return compact_report_text(text, 70)


def normalize_ai_report_data(report_data: dict) -> dict:
    seen = set()
    normalized = {
        "summary": compact_string_list(report_data.get("summary"), 5, 180, seen),
    }

    topics = []
    for topic in report_list(report_data.get("key_topics")):
        if isinstance(topic, dict):
            title = compact_report_text(topic.get("title"), 80)
            text = compact_report_text(topic.get("text"), 220)
            dedupe_text = f"{title} {text}"
        else:
            title = ""
            text = compact_report_text(topic, 220)
            dedupe_text = text
        if is_duplicate_report_item(dedupe_text, seen):
            continue
        topics.append({"title": title, "text": text})
        if len(topics) >= 4:
            break
    normalized["key_topics"] = topics
    normalized["decisions"] = compact_string_list(report_data.get("decisions"), 4, 180, seen)

    task_seen = set()
    tasks = []
    for task in report_list(report_data.get("tasks")):
        if not isinstance(task, dict):
            continue
        task_text = compact_report_text(task.get("task"), 180)
        if is_duplicate_report_item(task_text, task_seen):
            continue
        context = compact_report_text(task.get("context"), 130)
        if normalize_report_dedupe_key(context) == normalize_report_dedupe_key(task_text):
            context = ""
        tasks.append({
            "task": task_text,
            "owner": compact_report_text(task.get("owner"), 70, "не назначен"),
            "deadline": compact_report_text(task.get("deadline"), 70, "не указан"),
            "context": context,
            "status": compact_report_text(task.get("status"), 70, "не указан"),
        })
        if len(tasks) >= 7:
            break
    normalized["tasks"] = tasks

    providers = []
    provider_seen = set()
    for item in report_list(report_data.get("providers_partners")):
        if not isinstance(item, dict):
            continue
        name = compact_report_text(item.get("name"), 80)
        role_status = compact_report_text(item.get("role_status"), 110)
        key_issue = compact_report_text(item.get("key_issue"), 160)
        if is_duplicate_report_item(f"{name} {role_status} {key_issue}", provider_seen):
            continue
        providers.append({
            "name": name,
            "role_status": role_status,
            "key_issue": key_issue,
        })
        if len(providers) >= 4:
            break
    normalized["providers_partners"] = providers
    normalized["risks_open_questions"] = compact_string_list(report_data.get("risks_open_questions"), 5, 180, seen)
    normalized["next_steps"] = compact_string_list(report_data.get("next_steps"), 5, 160, seen)
    normalized["important_facts"] = normalize_important_facts(report_data.get("important_facts"))
    apply_important_facts_safety_net(normalized)

    return normalized


def normalize_important_facts(items):
    facts = []
    fact_seen = set()

    for item in report_list(items):
        if not isinstance(item, dict):
            continue
        fact = compact_report_text(item.get("fact"), 180)
        source_type = compact_report_text(item.get("source_type"), 40)
        action = compact_report_text(item.get("action"), 140)
        if not fact:
            continue
        if is_duplicate_report_item(f"{fact} {action}", fact_seen):
            continue
        facts.append({
            "fact": fact,
            "source_type": source_type,
            "action": action,
        })
        if len(facts) >= 6:
            break

    return facts


def apply_important_facts_safety_net(report_data: dict):
    content_seen = set()

    for item in report_data.get("summary", []):
        is_duplicate_report_item(item, content_seen)
    for item in report_data.get("key_topics", []):
        if isinstance(item, dict):
            is_duplicate_report_item(f"{item.get('title', '')} {item.get('text', '')}", content_seen)
    for item in report_data.get("decisions", []):
        is_duplicate_report_item(item, content_seen)
    for task in report_data.get("tasks", []):
        if isinstance(task, dict):
            is_duplicate_report_item(f"{task.get('task', '')} {task.get('context', '')}", content_seen)
    for item in report_data.get("risks_open_questions", []):
        is_duplicate_report_item(item, content_seen)
    for item in report_data.get("next_steps", []):
        is_duplicate_report_item(item, content_seen)

    for item in report_data.get("important_facts", []):
        fact = report_text_value(item.get("fact"))
        action = report_text_value(item.get("action"))
        fact_text = compact_report_text(f"{fact}. {action}" if action else fact, 220)
        if is_duplicate_report_item(fact_text, content_seen):
            continue

        if len(report_data.get("key_topics", [])) < 4:
            report_data.setdefault("key_topics", []).append({
                "title": "Важный факт",
                "text": fact_text,
            })
        elif len(report_data.get("risks_open_questions", [])) < 5:
            report_data.setdefault("risks_open_questions", []).append(compact_report_text(fact_text, 180))


def create_openai_report_response(client, response_input):
    try:
        return client.responses.create(
            model=OPENAI_REPORT_MODEL,
            temperature=OPENAI_REPORT_TEMPERATURE,
            input=response_input,
        )
    except Exception as e:
        error_text = str(e).lower()
        if "temperature" not in error_text and "unsupported" not in error_text:
            raise
        logging.warning("OpenAI report model rejected temperature; retrying without temperature.")
        return client.responses.create(
            model=OPENAI_REPORT_MODEL,
            input=response_input,
        )


def generate_ai_one_page_report(chat_title, period_label, chat_export_text, attachments_index_text, prompt_text, contact_sheet_paths=None):
    if not OPENAI_REPORTS_ENABLED:
        raise RuntimeError("AI PDF-отчёты отключены. Установите OPENAI_REPORTS_ENABLED=true.")

    if not OPENAI_API_KEY:
        raise RuntimeError("Не задан OPENAI_API_KEY.")

    from openai import OpenAI

    max_chars = max(1000, OPENAI_REPORT_MAX_INPUT_CHARS)
    prompt_budget = min(30000, max_chars // 5)
    remaining_budget = max_chars - prompt_budget
    chat_budget = remaining_budget * 2 // 3
    attachments_budget = remaining_budget - chat_budget

    prompt_text = limit_report_input_text("prompt", prompt_text, prompt_budget)
    chat_export_text = limit_report_input_text("chat_export", chat_export_text, chat_budget)
    attachments_index_text = limit_report_input_text("attachments_index", attachments_index_text, attachments_budget)

    system_prompt = """Ты готовишь JSON-данные для краткого PDF-отчёта на русском языке.

Главный источник правил анализа — блок WORK_ANALYSIS_PROMPT в пользовательском сообщении.
Ты должен строго следовать WORK_ANALYSIS_PROMPT и применить его к CHAT_EXPORT и ATTACHMENTS_INDEX.
Не игнорируй правила из WORK_ANALYSIS_PROMPT про изображения/скриншоты, voice/audio transcripts, summary встреч / AI-summary, провайдеров/партнёров, компактность и запрет технической “кухни”.
Если к запросу приложены image contact sheets, анализируй их как изображения: извлекай видимый текст, суммы, ставки, валюты, статусы, ошибки доступа и сообщения внешних партнёров, связывая их с captions/context из ATTACHMENTS_INDEX и CHAT_EXPORT.
Если есть конфликт между этой технической обвязкой и WORK_ANALYSIS_PROMPT, приоритет у WORK_ANALYSIS_PROMPT, кроме обязательного требования вернуть валидный JSON по OUTPUT_FORMAT.

Не повторяй одну и ту же мысль в нескольких JSON-разделах:
- summary — только самое главное;
- key_topics — контекст и смысловые направления;
- decisions — только решения/договорённости, не повторяй темы;
- tasks — только action items;
- risks_open_questions — только неопределённости/риски, не повторяй задачи;
- next_steps — короткий приоритетный список, не копируй таблицу задач.

Задачи должны быть операционными, а не общими темами:
- каждая задача содержит конкретное действие;
- если есть внешняя сторона, укажи её прямо в task: кому отправить, у кого запросить, с кем согласовать;
- формулируй task по смыслу “сделать X для/у Y, чтобы получить Z”;
- не создавай задачи вида “уточнить” без объекта: должно быть понятно, что уточнить, у кого и зачем;
- не превращай тему в задачу, если нет следующего действия;
- owner заполняй только если ответственный явно указан в чате, иначе “не назначен”;
- deadline заполняй только если срок явно указан, иначе “не указан”;
- status используй один из вариантов: “новая”, “в работе”, “ожидает ответа”, “требует подтверждения”.

Верни только валидный JSON без Markdown и пояснений.
"""

    output_format = """OUTPUT_FORMAT:
{
  "summary": ["до 5 коротких пунктов, каждый до 180 символов"],
  "key_topics": [
    {"title": "тема", "text": "краткое содержание и важные факты, до 220 символов"}
  ],
  "decisions": ["до 4 решений или текущих статусов, каждый до 180 символов"],
  "tasks": [
    {
      "task": "конкретное действие в формате “сделать X для/у Y, чтобы получить Z”, до 180 символов",
      "owner": "явно указанный ответственный или “не назначен”",
      "deadline": "дата/срок или “не указан”",
      "context": "короткая причина/источник задачи + важные цифры/условия, до 130 символов",
      "status": "новая / в работе / ожидает ответа / требует подтверждения"
    }
  ],
  "risks_open_questions": ["до 5 рисков или открытых вопросов, каждый до 180 символов"],
  "providers_partners": [
    {"name": "партнёр", "role_status": "роль / статус", "key_issue": "ключевой вопрос / следующий шаг, до 160 символов"}
  ],
  "next_steps": ["до 5 практических следующих шагов, каждый до 160 символов"],
  "important_facts": [
    {
      "fact": "важный факт из изображения / скриншота / voice transcript / документа / чата, до 180 символов",
      "source_type": "image / screenshot / voice / document / chat",
      "action": "какой вывод, риск или следующий шаг связан с фактом, до 140 символов"
    }
  ]
}

Лимиты: key_topics до 4 тем, tasks до 7 задач, providers_partners до 4 строк, important_facts до 6 фактов.
important_facts нужен как safety net, чтобы факты из изображений, скриншотов, voice transcripts и вложений не потерялись при сжатии.
Если важный факт уже отражён в summary/key_topics/tasks/risks/next_steps, не дублируй его ради заполнения important_facts.
Если скриншот подтверждает наличие опции, валюты, суммы или статуса, формулируй fact как “предварительно подтверждено по скриншоту”, а action — как проверку условий, комиссий, реквизитов или официального подтверждения.
Примеры:
- Если на банковском скриншоте видна нужная валюта: fact “нужная валюта предварительно доступна по скриншоту банковского интерфейса”; action “проверить комиссии, реквизиты и условия перевода”.
- Если на изображении видны ставки/тарифы по коммерческому предложению: fact “на скриншоте видны конкретные ставки X / Y / Z”; action “подтвердить актуальность конкретных ставок”.
- Если скриншот показывает переписку с клиникой/провайдером и условия работы: включи это как факт партнёра и связанный следующий шаг.
- Если скриншот показывает ошибку доступа к платформе видеоконференции: fact “ссылка не пускала из-за недостаточного уровня доступа”; action “на будущие встречи нужен тест доступа и резервный канал связи”.
Не используй примеры как факты. Не добавляй в отчёт компании, валюты, ставки, события или платформы из примеров, если они не присутствуют в конкретном chat_export, attachments_index, transcripts или image contact sheets.
Если данных мало — не заполняй искусственно. Если данных много — выбери главное.
"""

    user_input = f"""Чат: {chat_title}
Период: {period_label}

WORK_ANALYSIS_PROMPT:
{prompt_text}

CHAT_EXPORT:
{chat_export_text}

ATTACHMENTS_INDEX:
{attachments_index_text}

{output_format}
"""

    client = OpenAI(
        api_key=OPENAI_API_KEY,
        timeout=OPENAI_REPORT_TIMEOUT_SECONDS,
    )

    image_inputs = build_openai_report_image_inputs(contact_sheet_paths)
    user_content = [{"type": "input_text", "text": user_input}] + image_inputs

    response_input = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    try:
        response = create_openai_report_response(client, response_input)
    except Exception:
        if not image_inputs:
            raise
        logging.exception("OpenAI report image input failed; retrying AI PDF as text-only.")
        text_only_input = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input},
        ]
        response = create_openai_report_response(client, text_only_input)

    report_text = extract_openai_response_text(response)
    if not report_text:
        raise RuntimeError("OpenAI returned empty report text.")

    report_data = parse_ai_report_json(report_text)
    if report_data is not None:
        report_data = normalize_ai_report_data(report_data)

    return report_text, report_data


def get_report_pdf_font_name() -> str:
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
    except Exception:
        return "Helvetica"

    candidates = []
    if CONTACT_SHEET_FONT_PATH:
        candidates.append(CONTACT_SHEET_FONT_PATH)
    candidates.extend([
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ])

    for candidate in candidates:
        path = Path(candidate)
        if not path.exists():
            continue
        try:
            pdfmetrics.registerFont(TTFont("ReportSans", str(path)))
            return "ReportSans"
        except Exception as e:
            logging.warning("Could not load PDF font %s: %s", path, e)

    logging.warning("Could not find Unicode TTF font for PDF report. Falling back to Helvetica.")
    return "Helvetica"


def resolve_ai_report_logo_path() -> Path:
    path = AI_REPORT_LOGO_PATH
    return path if path.is_absolute() else BASE_DIR / path


def get_ai_report_logo_flowable(max_width: float, max_height: float):
    from reportlab.platypus import Image as ReportLabImage

    logo_path = resolve_ai_report_logo_path()
    if not logo_path.exists():
        return None

    if logo_path.suffix.lower() == ".svg":
        logging.warning(
            "AI_REPORT_LOGO_PATH points to SVG, but SVG logos are not rendered without extra dependencies: %s",
            logo_path,
        )
        return None

    if logo_path.suffix.lower() not in (".png", ".jpg", ".jpeg"):
        logging.warning("Unsupported AI report logo format: %s", logo_path)
        return None

    try:
        with Image.open(logo_path) as img:
            width, height = img.size
        scale = min(max_width / width, max_height / height, 1.0)
        return ReportLabImage(str(logo_path), width=width * scale, height=height * scale)
    except Exception as e:
        logging.warning("Could not load AI report logo %s: %s", logo_path, e)
        return None


def report_line_to_flowable(line: str, styles, font_name: str):
    from reportlab.platypus import Paragraph, Spacer
    from xml.sax.saxutils import escape

    stripped = line.strip()
    if not stripped:
        return Spacer(1, 4)

    heading_numbers = tuple(f"{number}. " for number in range(1, 11))
    if stripped.startswith(heading_numbers):
        return Paragraph(f"<b>{escape(stripped)}</b>", styles["ReportHeading"])

    if stripped.startswith(("- ", "* ", "• ")):
        return Paragraph(f'<font color="#75A63C">•</font> {escape(stripped[2:].strip())}', styles["ReportBody"])

    return Paragraph(escape(stripped), styles["ReportBody"])


def add_markdownish_report_text(story, report_text: str, styles, font_name: str, available_width: float):
    from reportlab.platypus import Table, TableStyle, Paragraph
    from reportlab.lib import colors
    from xml.sax.saxutils import escape

    lines = report_text.splitlines()
    index = 0

    while index < len(lines):
        line = lines[index]

        if "|" in line and index + 1 < len(lines) and set(lines[index + 1].replace("|", "").strip()) <= {"-", ":"}:
            headers = [cell.strip() for cell in line.strip().strip("|").split("|")]
            table_rows = [[Paragraph(f"<b>{escape(cell)}</b>", styles["TableCell"]) for cell in headers]]
            index += 2

            while index < len(lines) and "|" in lines[index]:
                cells = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
                table_rows.append([Paragraph(escape(cell), styles["TableCell"]) for cell in cells])
                index += 1

            column_count = max(1, len(table_rows[0]))
            table = Table(table_rows, colWidths=[available_width / column_count] * column_count, repeatRows=1)
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]))
            story.append(table)
            continue

        story.append(report_line_to_flowable(line, styles, font_name))
        index += 1


def report_list(value, limit=None):
    if not isinstance(value, list):
        return []

    items = [item for item in value if item]
    if limit:
        return items[:limit]
    return items


def report_text_value(value, default=""):
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def add_report_heading(story, title: str, styles):
    from reportlab.platypus import Paragraph
    from xml.sax.saxutils import escape

    story.append(Paragraph(escape(title), styles["ReportHeading"]))


def add_report_bullets(story, title: str, items, styles):
    from reportlab.platypus import Paragraph
    from xml.sax.saxutils import escape

    items = report_list(items)
    if not items:
        return

    add_report_heading(story, title, styles)
    for item in items:
        story.append(Paragraph(f'<font color="#75A63C">•</font> {escape(report_text_value(item))}', styles["ReportBody"]))


def add_key_topics(story, topics, styles):
    from reportlab.platypus import Paragraph
    from xml.sax.saxutils import escape

    rows = report_list(topics, 4)
    if not rows:
        return

    add_report_heading(story, "Ключевые темы", styles)
    for topic in rows:
        if isinstance(topic, dict):
            title = report_text_value(topic.get("title"))
            text = report_text_value(topic.get("text"))
            if title and text:
                story.append(Paragraph(f"<b>{escape(title)}.</b> {escape(text)}", styles["ReportBody"]))
            elif title or text:
                story.append(Paragraph(f'<font color="#75A63C">•</font> {escape(title or text)}', styles["ReportBody"]))
        else:
            story.append(Paragraph(f'<font color="#75A63C">•</font> {escape(report_text_value(topic))}', styles["ReportBody"]))


def add_tasks_table(story, tasks, styles, available_width: float):
    from reportlab.platypus import Table, TableStyle, Paragraph
    from reportlab.lib import colors
    from xml.sax.saxutils import escape

    rows = [task for task in report_list(tasks, 7) if isinstance(task, dict)]
    if not rows:
        return False

    add_report_heading(story, "Задачи", styles)

    table_rows = [[
        Paragraph("<b>Задача</b>", styles["TableHeader"]),
        Paragraph("<b>Ответственный</b>", styles["TableHeader"]),
        Paragraph("<b>Срок</b>", styles["TableHeader"]),
        Paragraph("<b>Статус</b>", styles["TableHeader"]),
    ]]

    for task in rows:
        task_text = compact_report_text(task.get("task"), 180, "не указано")
        context = compact_report_text(task.get("context"), 130)
        if normalize_report_dedupe_key(context) == normalize_report_dedupe_key(task_text):
            context = ""
        task_cell = f"<b>{escape(task_text)}</b>"
        if context:
            task_cell += f"<br/><font size=\"6.5\" color=\"#666666\">{escape(context)}</font>"

        table_rows.append([
            Paragraph(task_cell, styles["TableCell"]),
            Paragraph(escape(compact_pdf_short_value(task.get("owner")) or "не назначен"), styles["TableCell"]),
            Paragraph(escape(compact_pdf_short_value(task.get("deadline")) or "не указан"), styles["TableCell"]),
            Paragraph(escape(compact_pdf_short_value(task.get("status")) or "не указан"), styles["TableCell"]),
        ])

    table = Table(
        table_rows,
        colWidths=[
            available_width * 0.52,
            available_width * 0.17,
            available_width * 0.14,
            available_width * 0.17,
        ],
        repeatRows=1,
    )
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef4ea")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#224870")),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d7e1d1")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(table)
    return True


def add_providers_table(story, providers, styles, available_width: float):
    from reportlab.platypus import Table, TableStyle, Paragraph
    from reportlab.lib import colors
    from xml.sax.saxutils import escape

    rows = [item for item in report_list(providers, 4) if isinstance(item, dict)]
    if not rows:
        return False

    add_report_heading(story, "Провайдеры и партнёры", styles)

    table_rows = [[
        Paragraph("<b>Партнёр</b>", styles["TableHeader"]),
        Paragraph("<b>Роль / статус</b>", styles["TableHeader"]),
        Paragraph("<b>Ключевой вопрос / следующий шаг</b>", styles["TableHeader"]),
    ]]

    for item in rows:
        table_rows.append([
            Paragraph(escape(compact_report_text(item.get("name"), 80, "не указано")), styles["TableCell"]),
            Paragraph(escape(compact_report_text(item.get("role_status"), 110, "не указано")), styles["TableCell"]),
            Paragraph(escape(compact_report_text(item.get("key_issue"), 160, "не указано")), styles["TableCell"]),
        ])

    table = Table(
        table_rows,
        colWidths=[
            available_width * 0.25,
            available_width * 0.32,
            available_width * 0.43,
        ],
        repeatRows=1,
    )
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef4ea")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#224870")),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d7e1d1")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(table)
    return True


def add_structured_report_text(story, report_data: dict, styles, available_width: float):
    add_report_bullets(story, "Краткий вывод", report_data.get("summary"), styles)
    add_key_topics(story, report_data.get("key_topics"), styles)
    add_report_bullets(story, "Решения и статус", report_data.get("decisions"), styles)

    tasks_rendered = add_tasks_table(story, report_data.get("tasks"), styles, available_width)
    if not tasks_rendered:
        add_report_bullets(story, "Задачи", report_data.get("tasks"), styles)

    add_report_bullets(story, "Риски и открытые вопросы", report_data.get("risks_open_questions"), styles)
    add_providers_table(story, report_data.get("providers_partners"), styles, available_width)
    add_report_bullets(story, "Следующие шаги", report_data.get("next_steps"), styles)


def render_one_page_report_pdf(report_text, chat_title, period_label, output_path, report_data=None):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    font_name = get_report_pdf_font_name()
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="ReportTitle",
        parent=styles["Title"],
        fontName=font_name,
        fontSize=13,
        leading=14.5,
        spaceAfter=1,
        textColor="#224870",
    ))
    styles.add(ParagraphStyle(
        name="ReportMeta",
        parent=styles["Normal"],
        fontName=font_name,
        fontSize=7.6,
        leading=8.8,
        textColor="#555555",
        spaceAfter=1,
    ))
    styles.add(ParagraphStyle(
        name="ReportHeading",
        parent=styles["Heading2"],
        fontName=font_name,
        fontSize=9.2,
        leading=10.5,
        spaceBefore=3,
        spaceAfter=1,
        textColor="#224870",
    ))
    styles.add(ParagraphStyle(
        name="ReportBody",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=8,
        leading=9.2,
        spaceAfter=1,
    ))
    styles.add(ParagraphStyle(
        name="TableCell",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=7.5,
        leading=8.4,
    ))
    styles.add(ParagraphStyle(
        name="TableHeader",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=7.5,
        leading=8.4,
        textColor="#1f2937",
    ))

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=9 * mm,
        bottomMargin=11 * mm,
        title=f"AI PDF report — {chat_title}",
    )

    header_text = [
        Paragraph("Краткий рабочий отчёт", styles["ReportTitle"]),
        Paragraph(
            f"Чат: {escape(str(chat_title))}<br/>"
            f"Период: {escape(str(period_label))}<br/>"
            f"Дата подготовки: {datetime.now(APP_TIMEZONE).strftime('%Y-%m-%d %H:%M')}",
            styles["ReportMeta"],
        ),
    ]
    logo = get_ai_report_logo_flowable(34 * mm, 10 * mm)
    if logo:
        header_table = Table(
            [[logo, header_text]],
            colWidths=[37 * mm, doc.width - 37 * mm],
        )
    else:
        header_table = Table([[header_text]], colWidths=[doc.width])
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, -1), 0.8, colors.HexColor("#224870")),
    ]))

    story = [header_table, Spacer(1, 3)]

    if isinstance(report_data, dict):
        add_structured_report_text(story, report_data, styles, doc.width)
    else:
        add_markdownish_report_text(story, report_text, styles, font_name, doc.width)

    def draw_footer(canvas, doc_obj):
        canvas.saveState()
        canvas.setFont(font_name, 7)
        canvas.setFillColor(colors.HexColor("#777777"))
        footer = "Подготовлено на основе рабочего пакета Telegram"
        canvas.drawString(doc_obj.leftMargin, 7 * mm, footer)
        canvas.drawRightString(A4[0] - doc_obj.rightMargin, 7 * mm, f"· Стр. {doc_obj.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)
    return output_path


def get_ai_report_interval(mode: str):
    if mode == "shift":
        return get_current_work_shift_interval()

    until_dt = datetime.now(timezone.utc)
    since_dt = until_dt - timedelta(days=7)
    return since_dt, until_dt, "последние 7 дней"


def create_ai_pdf_output_path(chat_title: str, mode: str) -> Path:
    scope = safe_filename(chat_title)
    timestamp = datetime.now(APP_TIMEZONE).strftime("%Y%m%d-%H%M%S")
    return EXPORT_DIR / f"ai_short_report_{scope}_{mode}_{timestamp}.pdf"


async def send_ai_pdf_report_for_source_chat(context: ContextTypes.DEFAULT_TYPE, target_chat_id: int, source_chat_id: int, source_chat_title: str, mode: str):
    if not OPENAI_REPORTS_ENABLED:
        await context.bot.send_message(
            chat_id=target_chat_id,
            text="AI PDF-отчёты отключены. Установите OPENAI_REPORTS_ENABLED=true.",
        )
        return

    if not OPENAI_API_KEY:
        await context.bot.send_message(
            chat_id=target_chat_id,
            text="AI PDF-отчёт недоступен: не задан OPENAI_API_KEY.",
        )
        return

    since_dt, until_dt, period_label = get_ai_report_interval(mode)

    await context.bot.send_message(
        chat_id=target_chat_id,
        text=f"Готовлю краткий AI PDF-отчёт для чата: {source_chat_title}\nПериод: {period_label}",
    )

    prepare_transcripts_for_period(
        since_dt,
        until_dt,
        chat_id=source_chat_id,
    )

    export_path, _message_count = export_interval(
        since_dt,
        until_dt,
        chat_id=source_chat_id,
        chat_title_for_filename=source_chat_title,
        period_label=period_label,
    )

    attachments_index_path, _attachment_count = create_attachments_index(
        1,
        chat_id=source_chat_id,
        chat_title_for_filename=source_chat_title,
        since_dt=since_dt,
        until_dt=until_dt,
        period_label=period_label,
    )

    contact_sheet_paths = create_contact_sheets(
        1,
        chat_id=source_chat_id,
        chat_title_for_filename=source_chat_title,
        since_dt=since_dt,
        until_dt=until_dt,
        period_label=period_label,
    )

    prompt_text = get_chatgpt_work_prompt(source_chat_title, 1, period_label)
    chat_export_text = export_path.read_text(encoding="utf-8")
    attachments_index_text = attachments_index_path.read_text(encoding="utf-8")

    report_text, report_data = generate_ai_one_page_report(
        source_chat_title,
        period_label,
        chat_export_text,
        attachments_index_text,
        prompt_text,
        contact_sheet_paths=contact_sheet_paths,
    )

    pdf_path = create_ai_pdf_output_path(source_chat_title, mode)
    render_one_page_report_pdf(report_text, source_chat_title, period_label, pdf_path, report_data=report_data)

    if report_data is None:
        await context.bot.send_message(
            chat_id=target_chat_id,
            text="Структура AI-отчёта не распознана, PDF создан в fallback-режиме.",
        )

    await send_document_safely_to_chat(
        context,
        target_chat_id,
        pdf_path,
        caption=f"Краткий AI PDF-отчёт. {source_chat_title} — {period_label}",
    )



async def legacy_command_notice(update: Update, command_name: str):
    await update.message.reply_text(
        f"{command_name} — legacy-команда старого сценария.\n\n"
        "Основной рабочий сценарий теперь такой:\n"
        "1. Открой личный чат с ботом.\n"
        "2. Используй /report_chats.\n"
        "3. Выбери нужный чат кнопками.\n"
        "4. Собери пакет за смену 09:00–сейчас или за последние 7 дней.\n\n"
        "Новый пакет включает chat_export.md, attachments_index.md, work_analysis_prompt.txt, "
        "contact sheets и full archive ZIP."
    )


async def summary_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    await legacy_command_notice(update, "/summary_prompt")


async def weekly_package(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    await legacy_command_notice(update, "/weekly_package")


async def prompt_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    if update.effective_chat and update.effective_chat.type != "private":
        return

    custom_path = resolve_work_analysis_prompt_path(WORK_ANALYSIS_PROMPT_PATH)
    source_name, source_path = get_work_analysis_prompt_source()
    custom_exists = custom_path.exists()

    lines = [
        "Work analysis prompt status",
        "",
        f"Current source: {source_name}",
        f"Custom prompt path: {custom_path}",
        f"Custom file exists: {'yes' if custom_exists else 'no'}",
    ]

    if custom_exists:
        try:
            stat = custom_path.stat()
            modified_at = datetime.fromtimestamp(stat.st_mtime, APP_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %Z")
            lines.extend([
                f"Custom file size: {stat.st_size} bytes",
                f"Custom file modified: {modified_at}",
            ])
        except Exception as e:
            lines.append(f"Could not read custom file metadata: {e}")

    if source_path and source_path != custom_path:
        lines.append(f"Fallback source path: {source_path}")

    lines.extend([
        "",
        "Чтобы обновить prompt, отправьте .txt файл с подписью /set_work_prompt.",
    ])

    await update.message.reply_text("\n".join(lines))


async def reset_work_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    if update.effective_chat and update.effective_chat.type != "private":
        return

    custom_path = resolve_work_analysis_prompt_path(WORK_ANALYSIS_PROMPT_PATH)
    if not custom_path.exists():
        await update.message.reply_text(
            "Custom prompt уже отсутствует. Бот использует example/fallback prompt."
        )
        return

    backup_path = get_prompt_backup_path(custom_path)
    custom_path.rename(backup_path)

    await update.message.reply_text(
        "Custom prompt disabled/reset.\n"
        f"Backup created: {backup_path}\n"
        "Теперь бот будет использовать example/fallback prompt."
    )


async def handle_set_work_prompt_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    message = update.message
    if not message or not message.caption:
        return False

    caption_command = message.caption.strip().split()[0]
    if not caption_command.startswith("/set_work_prompt"):
        return False

    if not update.effective_chat or update.effective_chat.type != "private":
        return True

    if not is_admin(update):
        await message.reply_text("Эта команда доступна только администратору бота.")
        return True

    document = message.document
    if not document:
        await message.reply_text("Отправьте .txt файл с подписью /set_work_prompt.")
        return True

    file_name = document.file_name or ""
    mime_type = document.mime_type or ""
    is_text_file = file_name.lower().endswith(".txt") or mime_type == "text/plain"
    if not is_text_file:
        await message.reply_text("Prompt должен быть .txt файлом или MIME text/plain.")
        return True

    temp_path = Path("/tmp") / f"work_prompt_upload_{document.file_unique_id}.txt"

    try:
        telegram_file = await context.bot.get_file(document.file_id)
        await telegram_file.download_to_drive(custom_path=temp_path)
        prompt_text = temp_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        await message.reply_text("Не удалось прочитать файл как UTF-8. Сохраните prompt в UTF-8 и попробуйте снова.")
        return True
    except Exception as e:
        logging.exception("Could not download work analysis prompt")
        await message.reply_text(f"Не удалось загрузить prompt: {e}")
        return True
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass

    missing_placeholders = [
        placeholder
        for placeholder in ("{chat_title}", "{period_label}")
        if placeholder not in prompt_text
    ]
    if missing_placeholders:
        await message.reply_text(
            "Prompt не сохранён. В файле отсутствуют placeholders: "
            + ", ".join(missing_placeholders)
        )
        return True

    custom_path = resolve_work_analysis_prompt_path(WORK_ANALYSIS_PROMPT_PATH)
    custom_path.parent.mkdir(parents=True, exist_ok=True)

    backup_path = None
    if custom_path.exists():
        backup_path = get_prompt_backup_path(custom_path)
        custom_path.rename(backup_path)

    custom_path.write_text(prompt_text, encoding="utf-8")

    response = [
        "Prompt updated.",
        f"Path: {custom_path}",
        f"Size: {len(prompt_text.encode('utf-8'))} bytes",
    ]
    if backup_path:
        response.append(f"Backup created: {backup_path}")

    await message.reply_text("\n".join(response))
    return True


def get_private_admin_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["/help", "/health"],
            ["/global_status"],
            ["/report_chats"],
            ["/pending_chats", "/approved_chats"],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        selective=True,
    )


def get_private_admin_inline_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Собрать рабочий пакет", callback_data="admin:reports")],
        [InlineKeyboardButton("✅ Подтверждённые чаты", callback_data="admin:approved")],
        [InlineKeyboardButton("🕓 Ожидают подтверждения", callback_data="admin:pending")],
        [InlineKeyboardButton("💾 Статус хранилища", callback_data="admin:storage")],
        [InlineKeyboardButton("🆘 Помощь", callback_data="admin:help")],
    ])


def get_private_help_text() -> str:
    return """Личный кабинет бота

Основной рабочий сценарий:

/report_chats
Показать подтверждённые рабочие чаты и собрать пакет кнопками, не отправляя команды в сами чаты.

Через кнопки можно собрать:
- пакет за текущую смену 09:00–сейчас;
- пакет за последние 7 дней.

Рабочий пакет включает:
- 01_chat_export.md — переписка;
- 02_attachments_index.md — индекс вложений;
- 03_work_analysis_prompt.txt — промпт для ChatGPT;
- 04_image_contact_sheet_*.jpg — обзор изображений и скриншотов, если они есть;
- 05_full_archive.zip — полный архив с оригинальными вложениями.

Ручные команды пакетов:

/work_package
Legacy/manual: собрать пакет текущего чата за последние 7 дней.
Лучше использовать /report_chats в личке.

/work_shift_package
Legacy/manual: собрать пакет текущего чата за смену 09:00–сейчас.
Лучше использовать /report_chats в личке.

Обычно эти команды лучше не использовать в рабочих группах, чтобы не шуметь. Для рабочих чатов используй /report_chats в личке.

Статус и диагностика:

/version
Показать текущую версию и сборку бота.

/health
Краткий технический статус бота.

/storage_status
Реальный размер базы, файлов, экспортов и data volume.

/prompt_status
Проверить, какой work analysis prompt используется.

/reset_work_prompt
Отключить custom prompt и вернуться к example/fallback prompt.

Чтобы обновить prompt, отправьте .txt файл с подписью /set_work_prompt.

/vacuum_db
Сжать SQLite-базу после массовой очистки.

/status
Статус текущего чата.

/global_status
Общий статус по всем чатам.

Управление чатами:

/pending_chats
Чаты, ожидающие подтверждения.

/approved_chats
Подтверждённые чаты.

/approve_chat <chat_id>
Подтвердить чат вручную.

/reject_chat <chat_id>
Отклонить чат вручную.

/remove_chat <chat_id>
Удалить чат из списка известных. При следующем сообщении бот снова запросит подтверждение.

Очистка данных:

/cleanup_now
Принудительно запустить retention-очистку старше 14 дней.

/purge_chat <chat_id>
Полностью удалить архив конкретного чата и его файлы.

/purge_all_data CONFIRM
Полностью удалить все сохранённые сообщения, файлы и экспорты.
Список известных чатов сохраняется.

Старые экспортные команды:

/export_today
Markdown текущего чата за сегодня.

/export_week
Markdown текущего чата за последние 7 дней.

/export_zip_today
ZIP текущего чата за сегодня.

/export_zip_week
ZIP текущего чата за последние 7 дней.

/export_all_today
Markdown всех чатов за сегодня.

/export_all_week
Markdown всех чатов за последние 7 дней.

/export_zip_all_today
ZIP всех чатов за сегодня.

/export_zip_all_week
ZIP всех чатов за последние 7 дней.

Старые prompt/package команды:

/summary_prompt
Промпт для анализа недельного архива.

/weekly_package
ZIP за неделю + старый промпт.

Примечание:
Для рабочих чатов основной способ работы теперь — /report_chats в личке с ботом.
"""


def get_group_help_text() -> str:
    return """Бот работает в этом чате как тихий архиватор.

Основное управление лучше делать в личке с ботом командой:

/report_chats

Так рабочий чат не будет засоряться служебными командами и файлами.

Доступные команды в группе:

/status
Статус текущего чата.

/version
Версия бота.

/help
Краткая справка.

Рабочие пакеты, экспорты, очистку и диагностику нужно запускать в личке с ботом.
"""


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    chat = update.effective_chat
    reply_markup = None

    if chat and chat.type == "private":
        reply_markup = get_private_admin_inline_keyboard()
        help_text = get_private_help_text()
    else:
        help_text = get_group_help_text()

    await update.message.reply_text(
        help_text,
        reply_markup=reply_markup,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    reply_markup = None

    if chat and chat.type == "private":
        await update.message.reply_text(
            "Обновляю личный кабинет...",
            reply_markup=ReplyKeyboardRemove(),
        )

        if is_admin(update):
            reply_markup = get_private_admin_inline_keyboard()

    await update.message.reply_text(
        "Личный кабинет\n\n"
        "Бот работает. Я сохраняю сообщения и вложения из подтверждённых чатов для архива.\n\n"
        "Выбери действие кнопкой ниже или используй текстовые команды как раньше.",
        reply_markup=reply_markup,
    )




async def version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    await update.message.reply_text(
        "Bot version\n\n"
        f"Version: {BOT_VERSION}\n"
        f"Build: {BOT_BUILD_NAME}\n"
        f"Build date: {BOT_BUILD_DATE}\n\n"
        "Enabled features:\n"
        "- approved chats\n"
        "- private /report_chats with buttons\n"
        "- work packages for 7 days\n"
        "- work shift packages 09:00–now\n"
        "- attachments index\n"
        "- OCR-friendly image contact sheets\n"
        "- full archive ZIP\n"
        "- cleanup / purge commands\n"
        "- storage diagnostics\n"
        "- quiet group mode"
    )


async def health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    data = get_health_data()

    await update.message.reply_text(
        "Bot health\n\n"
        "Status: running\n"
        f"Retention: {RETENTION_DAYS} days\n"
        f"Max file size: {MAX_FILE_SIZE_MB} MB\n\n"
        "Storage:\n"
        f"Base dir: {data['base_dir']}\n"
        f"Data dir: {data['data_dir']}\n"
        f"Files dir exists: {data['files_dir_exists']}\n"
        f"Exports dir exists: {data['exports_dir_exists']}\n"
        f"Database exists: {data['db_exists']}\n\n"
        "Data:\n"
        f"Saved messages: {data['total_messages']}\n"
        f"Saved files: {data['saved_files']}\n"
        f"Skipped files: {data['skipped_files']}\n\n"
        "Chats:\n"
        f"Approved: {data['approved_chats']}\n"
        f"Pending: {data['pending_chats']}\n"
        f"Rejected: {data['rejected_chats']}"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    if not await ensure_chat_approved(update, context):
        return

    chat = update.effective_chat
    total_messages, week_messages, total_files = get_stats(chat.id)

    await update.message.reply_text(
        f"Статус текущего чата: работаю.\n\n"
        f"Чат: {chat.title or chat.full_name or chat.id}\n"
        f"Всего сохранено сообщений: {total_messages}\n"
        f"За последние 7 дней: {week_messages}\n"
        f"Сохранено файлов: {total_files}"
    )


async def global_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    total_messages, week_messages, total_files = get_stats(None)

    await update.message.reply_text(
        f"Глобальный статус: работаю.\n\n"
        f"Всего сохранено сообщений во всех чатах: {total_messages}\n"
        f"За последние 7 дней во всех чатах: {week_messages}\n"
        f"Сохранено файлов во всех чатах: {total_files}"
    )


async def export_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    if not await ensure_chat_approved(update, context):
        return

    chat = update.effective_chat
    export_path, count = export_last_days(
        7,
        chat_id=chat.id,
        chat_title_for_filename=chat.title or chat.full_name or str(chat.id),
    )

    if count == 0:
        await update.message.reply_text("За последние 7 дней в этом чате пока нет сохранённых сообщений.")
        return

    await send_document_safely(
        update,
        context,
        export_path,
        caption=f"Экспорт текущего чата за последние 7 дней. Сообщений: {count}",
    )


async def export_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    if not await ensure_chat_approved(update, context):
        return

    chat = update.effective_chat
    export_path, count = export_last_days(
        1,
        chat_id=chat.id,
        chat_title_for_filename=chat.title or chat.full_name or str(chat.id),
    )

    if count == 0:
        await update.message.reply_text("За сегодня в этом чате пока нет сохранённых сообщений.")
        return

    await send_document_safely(
        update,
        context,
        export_path,
        caption=f"Экспорт текущего чата за сегодня. Сообщений: {count}",
    )


async def export_all_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    export_path, count = export_last_days(7, chat_id=None)

    if count == 0:
        await update.message.reply_text("За последние 7 дней пока нет сохранённых сообщений.")
        return

    await send_document_safely(
        update,
        context,
        export_path,
        caption=f"Глобальный экспорт за последние 7 дней. Сообщений: {count}",
    )


async def export_all_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    export_path, count = export_last_days(1, chat_id=None)

    if count == 0:
        await update.message.reply_text("За сегодня пока нет сохранённых сообщений.")
        return

    await send_document_safely(
        update,
        context,
        export_path,
        caption=f"Глобальный экспорт за сегодня. Сообщений: {count}",
    )



async def export_zip_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    if not await ensure_chat_approved(update, context):
        return

    chat = update.effective_chat
    await update.message.reply_text("Готовлю ZIP-архив, это может занять немного времени...")

    zip_path, count, copied_files = create_zip_export(
        7,
        chat_id=chat.id,
        chat_title_for_filename=chat.title or chat.full_name or str(chat.id),
    )

    if count == 0:
        await update.message.reply_text("За последние 7 дней в этом чате пока нет сохранённых сообщений.")
        return

    await send_document_safely(
        update,
        context,
        zip_path,
        caption=(
            f"ZIP-архив текущего чата за последние 7 дней.\n"
            f"Сообщений: {count}\n"
            f"Файлов: {copied_files}"
        ),
    )


async def export_zip_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    if not await ensure_chat_approved(update, context):
        return

    chat = update.effective_chat
    await update.message.reply_text("Готовлю ZIP-архив, это может занять немного времени...")

    zip_path, count, copied_files = create_zip_export(
        1,
        chat_id=chat.id,
        chat_title_for_filename=chat.title or chat.full_name or str(chat.id),
    )

    if count == 0:
        await update.message.reply_text("За сегодня в этом чате пока нет сохранённых сообщений.")
        return

    await send_document_safely(
        update,
        context,
        zip_path,
        caption=(
            f"ZIP-архив текущего чата за сегодня.\n"
            f"Сообщений: {count}\n"
            f"Файлов: {copied_files}"
        ),
    )


async def export_zip_all_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    zip_path, count, copied_files = create_zip_export(7, chat_id=None)

    if count == 0:
        await update.message.reply_text("За последние 7 дней пока нет сохранённых сообщений.")
        return

    await send_document_safely(
        update,
        context,
        zip_path,
        caption=(
            f"Глобальный ZIP-архив за последние 7 дней.\n"
            f"Сообщений: {count}\n"
            f"Файлов: {copied_files}"
        ),
    )


async def export_zip_all_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    zip_path, count, copied_files = create_zip_export(1, chat_id=None)

    if count == 0:
        await update.message.reply_text("За сегодня пока нет сохранённых сообщений.")
        return

    await send_document_safely(
        update,
        context,
        zip_path,
        caption=(
            f"Глобальный ZIP-архив за сегодня.\n"
            f"Сообщений: {count}\n"
            f"Файлов: {copied_files}"
        ),
    )



async def chat_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not query:
        return

    await query.answer()

    user = query.from_user

    if not user or user.id not in ADMIN_USER_IDS:
        await query.edit_message_text("Эта кнопка доступна только администратору бота.")
        return

    data = query.data or ""

    if ":" not in data:
        await query.edit_message_text("Некорректная команда.")
        return

    action, chat_id_raw = data.split(":", 1)

    try:
        chat_id = int(chat_id_raw)
    except ValueError:
        await query.edit_message_text("Некорректный chat_id.")
        return

    if action == "approve_chat":
        ok = set_chat_status(chat_id, "approved", user.id)

        if not ok:
            await query.edit_message_text(
                "Чат не найден в списке pending. Возможно, он уже был обработан."
            )
            return

        await query.edit_message_text(
            f"✅ Чат {chat_id} подтверждён. Теперь бот будет сохранять сообщения и файлы из него."
        )

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Чат подтверждён администратором. Теперь бот будет сохранять сообщения и файлы для архива."
            )
        except Exception as e:
            print(f"Could not send approval message to chat {chat_id}: {e}")

    elif action == "reject_chat":
        ok = set_chat_status(chat_id, "rejected", None)

        if not ok:
            await query.edit_message_text(
                "Чат не найден. Возможно, он уже был обработан."
            )
            return

        await query.edit_message_text(
            f"❌ Чат {chat_id} отклонён. Бот не будет сохранять сообщения и файлы из него."
        )

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Чат не подтверждён администратором. Бот не будет сохранять сообщения и файлы из этого чата."
            )
        except Exception as e:
            print(f"Could not send rejection message to chat {chat_id}: {e}")

    else:
        await query.edit_message_text("Неизвестное действие.")


async def pending_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    rows = list_chats_by_status("pending")

    if not rows:
        await update.message.reply_text("Нет чатов, ожидающих подтверждения.")
        return

    lines = ["Чаты, ожидающие подтверждения:\n"]

    for chat_id, chat_title, chat_type, requested_at, last_seen_at in rows:
        lines.append(
            f"Название: {chat_title}\n"
            f"Chat ID: {chat_id}\n"
            f"Тип: {chat_type}\n"
            f"Подтвердить: /approve_chat {chat_id}\n"
            f"Отклонить: /reject_chat {chat_id}\n"
        )

    await update.message.reply_text("\n".join(lines))


async def approved_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    rows = list_chats_by_status("approved")

    if not rows:
        await update.message.reply_text("Нет подтверждённых чатов.")
        return

    lines = ["Подтверждённые чаты:\n"]

    for chat_id, chat_title, chat_type, requested_at, last_seen_at in rows:
        lines.append(
            f"Название: {chat_title}\n"
            f"Chat ID: {chat_id}\n"
            f"Тип: {chat_type}\n"
            f"Отключить: /remove_chat {chat_id}\n"
        )

    await update.message.reply_text("\n".join(lines))


async def approve_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    if not context.args:
        await update.message.reply_text("Укажи chat_id: /approve_chat -1001234567890")
        return

    try:
        chat_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("chat_id должен быть числом.")
        return

    ok = set_chat_status(chat_id, "approved", update.effective_user.id)

    if not ok:
        await update.message.reply_text(
            "Чат не найден в списке pending. "
            "Сначала бот должен увидеть этот чат."
        )
        return

    await update.message.reply_text(f"Чат {chat_id} подтверждён. Теперь бот будет сохранять сообщения из него.")

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Чат подтверждён администратором. Теперь бот будет сохранять сообщения и файлы для архива."
        )
    except Exception as e:
        print(f"Could not send approval message to chat {chat_id}: {e}")


async def reject_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    if not context.args:
        await update.message.reply_text("Укажи chat_id: /reject_chat -1001234567890")
        return

    try:
        chat_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("chat_id должен быть числом.")
        return

    ok = set_chat_status(chat_id, "rejected", None)

    if not ok:
        await update.message.reply_text(
            "Чат не найден. Сначала бот должен увидеть этот чат."
        )
        return

    await update.message.reply_text(f"Чат {chat_id} отклонён. Бот не будет сохранять сообщения из него.")

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Чат не подтверждён администратором. Бот не будет сохранять сообщения и файлы из этого чата."
        )
    except Exception as e:
        print(f"Could not send rejection message to chat {chat_id}: {e}")


async def remove_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    if not context.args:
        await update.message.reply_text("Укажи chat_id: /remove_chat -1001234567890")
        return

    try:
        chat_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("chat_id должен быть числом.")
        return

    ok, text = remove_chat_by_id(chat_id)

    if not ok:
        await update.message.reply_text(text)
        return

    await update.message.reply_text(text)


def remove_chat_by_id(chat_id: int):
    ok = delete_chat_record(chat_id)

    if not ok:
        return False, "Чат не найден."

    return (
        True,
        f"Чат {chat_id} удалён из списка. "
        "При следующем сообщении бот снова запросит подтверждение администратора.",
    )



async def friendly_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    if not await ensure_chat_approved(update, context):
        return

    chat = update.effective_chat
    chat_title = chat.title or chat.full_name or str(chat.id)

    await update.message.reply_text(
        get_friendly_summary_prompt(chat_title, 7)
    )


async def friendly_package(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    if not await ensure_chat_approved(update, context):
        return

    chat = update.effective_chat
    chat_title = chat.title or chat.full_name or str(chat.id)

    await update.message.reply_text("Готовлю ZIP-архив, это может занять немного времени...")

    zip_path, count, copied_files = create_zip_export(
        7,
        chat_id=chat.id,
        chat_title_for_filename=chat_title,
    )

    if count == 0:
        await update.message.reply_text("За последние 7 дней в этом чате пока нет сохранённых сообщений.")
        return

    await send_document_safely(
        update,
        context,
        zip_path,
        caption=(
            f"Дружеский пакет для анализа в Gemini.\n"
            f"Чат: {chat_title}\n"
            f"Сообщений: {count}\n"
            f"Файлов: {copied_files}\n\n"
            f"Следующим сообщением отправлю дружеский промпт для анализа."
        ),
    )

    await update.message.reply_text(
        get_friendly_summary_prompt(chat_title, 7)
    )





def get_approved_report_chats():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("PRAGMA table_info(chats)")
    columns = [row[1] for row in cur.fetchall()]

    if "chat_id" not in columns:
        conn.close()
        return []

    title_col = "chat_title" if "chat_title" in columns else ("title" if "title" in columns else None)
    type_col = "chat_type" if "chat_type" in columns else ("type" if "type" in columns else None)
    status_col = "status" if "status" in columns else None

    select_cols = ["chat_id"]
    select_cols.append(title_col if title_col else "chat_id")
    select_cols.append(type_col if type_col else "''")

    query = f"SELECT {', '.join(select_cols)} FROM chats"
    params = []

    if status_col:
        query += " WHERE status = ?"
        params.append("approved")

    query += " ORDER BY 2 ASC"

    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()

    result = []

    for chat_id, title, chat_type in rows:
        if str(chat_type).lower() == "private":
            continue

        result.append(
            {
                "chat_id": int(chat_id),
                "title": str(title or chat_id),
                "type": str(chat_type or ""),
            }
        )

    return result


def get_approved_chat_title(chat_id: int):
    chats = get_approved_report_chats()

    for chat in chats:
        if chat["chat_id"] == chat_id:
            return chat["title"]

    return None


async def send_work_package_for_source_chat(context: ContextTypes.DEFAULT_TYPE, target_chat_id: int, source_chat_id: int, source_chat_title: str, mode: str):
    if mode == "shift":
        since_dt, until_dt, period_label = get_current_work_shift_interval()

        await context.bot.send_message(
            chat_id=target_chat_id,
            text=f"Готовлю рабочий пакет за смену для чата: {source_chat_title}\nПериод: {period_label}",
        )

        prepare_transcripts_for_period(
            since_dt,
            until_dt,
            chat_id=source_chat_id,
        )

        export_path, message_count = export_interval(
            since_dt,
            until_dt,
            chat_id=source_chat_id,
            chat_title_for_filename=source_chat_title,
            period_label=period_label,
        )

        attachments_index_path, attachment_count = create_attachments_index(
            1,
            chat_id=source_chat_id,
            chat_title_for_filename=source_chat_title,
            since_dt=since_dt,
            until_dt=until_dt,
            period_label=period_label,
        )

        prompt_path = create_prompt_file(
            get_chatgpt_work_prompt(source_chat_title, 1, period_label),
            chat_title_for_filename=source_chat_title,
        )

        contact_sheet_paths = create_contact_sheets(
            1,
            chat_id=source_chat_id,
            chat_title_for_filename=source_chat_title,
            since_dt=since_dt,
            until_dt=until_dt,
            period_label=period_label,
        )

        zip_path, zip_message_count, copied_files = create_zip_export_interval(
            since_dt,
            until_dt,
            chat_id=source_chat_id,
            chat_title_for_filename=source_chat_title,
        )

        package_label = "за смену"

    else:
        await context.bot.send_message(
            chat_id=target_chat_id,
            text=f"Готовлю рабочий пакет за последние 7 дней для чата: {source_chat_title}",
        )

        until_dt = datetime.now(timezone.utc)
        since_dt = until_dt - timedelta(days=7)

        prepare_transcripts_for_period(
            since_dt,
            until_dt,
            chat_id=source_chat_id,
        )

        export_path, message_count = export_last_days(
            7,
            chat_id=source_chat_id,
            chat_title_for_filename=source_chat_title,
        )

        attachments_index_path, attachment_count = create_attachments_index(
            7,
            chat_id=source_chat_id,
            chat_title_for_filename=source_chat_title,
            period_label="last 7 days",
        )

        prompt_path = create_prompt_file(
            get_chatgpt_work_prompt(source_chat_title, 7, "последние 7 дней"),
            chat_title_for_filename=source_chat_title,
        )

        contact_sheet_paths = create_contact_sheets(
            7,
            chat_id=source_chat_id,
            chat_title_for_filename=source_chat_title,
            period_label="last 7 days",
        )

        zip_path, zip_message_count, copied_files = create_zip_export(
            7,
            chat_id=source_chat_id,
            chat_title_for_filename=source_chat_title,
        )

        package_label = "за последние 7 дней"

    await send_document_safely_to_chat(
        context,
        target_chat_id,
        export_path,
        caption=f"Chat export. Переписка чата «{source_chat_title}» {package_label}. Сообщений: {message_count}",
    )

    await send_document_safely_to_chat(
        context,
        target_chat_id,
        attachments_index_path,
        caption=f"Attachments index. Индекс вложений. Вложений/файловых сообщений: {attachment_count}",
    )

    await send_document_safely_to_chat(
        context,
        target_chat_id,
        prompt_path,
        caption="Analysis prompt. Промпт для анализа рабочего чата в ChatGPT.",
    )

    if contact_sheet_paths:
        for index, sheet_path in enumerate(contact_sheet_paths, start=1):
            await send_document_safely_to_chat(
                context,
                target_chat_id,
                sheet_path,
                caption=f"Contact sheet {index}/{len(contact_sheet_paths)}. Крупный обзор изображений и скриншотов.",
            )
    else:
        await context.bot.send_message(
            chat_id=target_chat_id,
            text="Изображений за период не найдено, contact sheet не создан.",
        )

    await send_document_safely_to_chat(
        context,
        target_chat_id,
        zip_path,
        caption=(
            f"Full archive. Полный ZIP-архив чата «{source_chat_title}» {package_label}.\n"
            f"Сообщений: {zip_message_count}\n"
            f"Файлов: {copied_files}\n\n"
            "Используй ZIP как источник вложений, если нужно открыть конкретные файлы из attachments_index.md."
        ),
    )

    await context.bot.send_message(
        chat_id=target_chat_id,
        text="Пакет готов. Для анализа загрузи в ChatGPT: chat_export.md, attachments_index.md, work_analysis_prompt.txt и contact sheets. ZIP используй как источник оригинальных вложений при необходимости.",
    )



async def storage_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await group_private_control_notice(update):
        return
    if not await admin_only(update):
        return

    data = get_storage_status_data()

    await update.message.reply_text(format_storage_status_text(data))


async def vacuum_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await group_private_control_notice(update):
        return
    if not await admin_only(update):
        return

    before_size, after_size = vacuum_database()
    saved = before_size - after_size

    await update.message.reply_text(
        "SQLite VACUUM выполнен.\n\n"
        f"Было: {human_file_size(before_size)}\n"
        f"Стало: {human_file_size(after_size)}\n"
        f"Освобождено: {human_file_size(saved)}"
    )



async def group_private_control_notice(update: Update) -> bool:
    """
    Returns True if command should stop because it was called in a group.
    Work package / admin workflow should be controlled from private chat.
    """
    if update.effective_chat and update.effective_chat.type != "private":
        await update.message.reply_text(
            "Чтобы не засорять рабочий чат, управление ботом вынесено в личку.\n\n"
            "Открой личный чат с ботом и используй /report_chats."
        )
        return True

    return False


async def send_or_edit_admin_text(message, text: str, reply_markup=None, edit: bool = False):
    if edit:
        await message.edit_text(text, reply_markup=reply_markup)
    else:
        await message.reply_text(text, reply_markup=reply_markup)


async def show_report_chats_selection(message, edit: bool = False):
    chats = get_approved_report_chats()

    if not chats:
        await send_or_edit_admin_text(
            message,
            "Нет подтверждённых групповых чатов для отчётов.",
            edit=edit,
        )
        return

    keyboard = []

    for chat in chats:
        title = chat["title"]
        chat_id = chat["chat_id"]

        keyboard.append([InlineKeyboardButton(f"📌 {title}", callback_data=f"noop:{chat_id}")])
        keyboard.append(
            [
                InlineKeyboardButton("Смена 09:00–сейчас", callback_data=f"workpkg:shift:{chat_id}"),
                InlineKeyboardButton("7 дней", callback_data=f"workpkg:week:{chat_id}"),
            ]
        )
        if OPENAI_REPORTS_ENABLED:
            keyboard.append(
                [
                    InlineKeyboardButton("📄 Краткий PDF — смена", callback_data=f"workpdf:shift:{chat_id}"),
                    InlineKeyboardButton("📄 Краткий PDF — 7 дней", callback_data=f"workpdf:week:{chat_id}"),
                ]
            )

    await send_or_edit_admin_text(
        message,
        "Выбери чат и тип рабочего пакета:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        edit=edit,
    )


async def show_pending_chats_selection(message, edit: bool = False):
    rows = list_chats_by_status("pending")

    if not rows:
        await send_or_edit_admin_text(
            message,
            "Нет чатов, ожидающих подтверждения.",
            reply_markup=get_private_admin_inline_keyboard(),
            edit=edit,
        )
        return

    keyboard = []

    for chat_id, chat_title, chat_type, requested_at, last_seen_at in rows:
        title = chat_title or str(chat_id)
        keyboard.append([InlineKeyboardButton(f"{title} ({chat_type})", callback_data=f"noop:{chat_id}")])
        keyboard.append(
            [
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"approve_chat:{chat_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_chat:{chat_id}"),
            ]
        )

    keyboard.append([InlineKeyboardButton("↩️ В кабинет", callback_data="admin:home")])

    await send_or_edit_admin_text(
        message,
        "Чаты, ожидающие подтверждения:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        edit=edit,
    )


async def show_approved_chats_selection(message, edit: bool = False):
    chats = get_approved_report_chats()

    if not chats:
        await send_or_edit_admin_text(
            message,
            "Нет подтверждённых групповых чатов для отчётов.",
            reply_markup=get_private_admin_inline_keyboard(),
            edit=edit,
        )
        return

    keyboard = []

    for chat in chats:
        title = chat["title"]
        chat_id = chat["chat_id"]

        keyboard.append([InlineKeyboardButton(f"📌 {title}", callback_data=f"noop:{chat_id}")])
        keyboard.append(
            [
                InlineKeyboardButton("📦 Пакет", callback_data=f"admin:package:{chat_id}"),
                InlineKeyboardButton("❌ Отключить", callback_data=f"admin:remove_prompt:{chat_id}"),
            ]
        )

    keyboard.append([InlineKeyboardButton("↩️ В кабинет", callback_data="admin:home")])

    await send_or_edit_admin_text(
        message,
        "Подтверждённые чаты:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        edit=edit,
    )


async def admin_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not query:
        return

    user = query.from_user

    if not user or user.id not in ADMIN_USER_IDS:
        await query.answer("Эта кнопка доступна только администратору бота.", show_alert=True)
        return

    if not query.message or query.message.chat.type != "private":
        await query.answer("Управление доступно в личном чате с ботом.", show_alert=True)
        return

    data = query.data or ""
    await query.answer()

    if data == "admin:home":
        await query.message.edit_text(
            "Личный кабинет\n\nВыбери действие:",
            reply_markup=get_private_admin_inline_keyboard(),
        )
        return

    if data == "admin:reports":
        await show_report_chats_selection(query.message, edit=True)
        return

    if data == "admin:pending":
        await show_pending_chats_selection(query.message, edit=True)
        return

    if data == "admin:approved":
        await show_approved_chats_selection(query.message, edit=True)
        return

    if data == "admin:storage":
        await query.message.edit_text(
            format_storage_status_text(get_storage_status_data()),
            reply_markup=get_private_admin_inline_keyboard(),
        )
        return

    if data == "admin:help":
        await query.message.edit_text(
            get_private_help_text(),
            reply_markup=get_private_admin_inline_keyboard(),
        )
        return

    if data.startswith("admin:package:"):
        try:
            chat_id = int(data.split(":", 2)[2])
        except ValueError:
            await query.message.reply_text("Некорректный chat_id.")
            return

        chat_title = get_approved_chat_title(chat_id)

        if not chat_title:
            await query.message.edit_text(
                "Этот чат не найден среди подтверждённых. Обнови список.",
                reply_markup=get_private_admin_inline_keyboard(),
            )
            return

        await query.message.edit_text(
            f"Выбери тип рабочего пакета для чата:\n{chat_title}",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Смена 09:00–сейчас", callback_data=f"workpkg:shift:{chat_id}"),
                    InlineKeyboardButton("7 дней", callback_data=f"workpkg:week:{chat_id}"),
                ],
                [InlineKeyboardButton("↩️ К списку чатов", callback_data="admin:approved")],
            ]),
        )
        return

    if data.startswith("admin:remove_prompt:"):
        try:
            chat_id = int(data.split(":", 2)[2])
        except ValueError:
            await query.message.reply_text("Некорректный chat_id.")
            return

        chat_title = get_approved_chat_title(chat_id) or str(chat_id)

        await query.message.edit_text(
            f"Отключить чат {chat_title}?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Да, отключить", callback_data=f"admin:remove_confirm:{chat_id}")],
                [InlineKeyboardButton("Отмена", callback_data="admin:approved")],
            ]),
        )
        return

    if data.startswith("admin:remove_confirm:"):
        try:
            chat_id = int(data.split(":", 2)[2])
        except ValueError:
            await query.message.reply_text("Некорректный chat_id.")
            return

        ok, text = remove_chat_by_id(chat_id)
        await query.message.edit_text(
            text,
            reply_markup=get_private_admin_inline_keyboard() if ok else None,
        )
        return

    await query.message.reply_text("Неизвестное действие.")


async def report_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "Управление рабочими пакетами лучше делать в личке с ботом, чтобы не шуметь в рабочем чате."
        )
        return

    await show_report_chats_selection(update.message)


async def work_package_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not query:
        return

    user = update.effective_user

    if not user or user.id not in ADMIN_USER_IDS:
        await query.answer("Эта кнопка доступна только администратору бота.", show_alert=True)
        return

    data = query.data or ""

    if data.startswith("noop:"):
        await query.answer()
        return

    if not data.startswith("workpkg:"):
        if not data.startswith("workpdf:"):
            return

    await query.answer()

    try:
        _, mode, raw_chat_id = data.split(":", 2)
        source_chat_id = int(raw_chat_id)
    except Exception:
        await query.message.reply_text("Не удалось разобрать команду кнопки.")
        return

    source_chat_title = get_approved_chat_title(source_chat_id)

    if not source_chat_title:
        await query.message.reply_text(
            "Этот чат не найден среди подтверждённых. Обнови список командой /report_chats."
        )
        return

    try:
        if data.startswith("workpdf:"):
            if query.message.chat.type != "private":
                await query.answer("AI PDF-отчёты доступны только в личном чате с ботом.", show_alert=True)
                return
            await send_ai_pdf_report_for_source_chat(
                context,
                query.message.chat_id,
                source_chat_id,
                source_chat_title,
                mode,
            )
        else:
            await send_work_package_for_source_chat(
                context,
                query.message.chat_id,
                source_chat_id,
                source_chat_title,
                mode,
            )
    except Exception as e:
        logging.exception("Could not create work package or AI PDF from inline button")
        await query.message.reply_text(f"Не удалось выполнить действие.\n\nОшибка: {e}")


async def cleanup_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await group_private_control_notice(update):
        return
    if not await admin_only(update):
        return

    deleted_messages, deleted_files = force_retention_cleanup()

    await update.message.reply_text(
        "Принудительная retention-очистка выполнена.\n\n"
        f"Удалено сообщений: {deleted_messages}\n"
        f"Удалено файлов: {deleted_files}\n"
        f"Retention: {RETENTION_DAYS} дней"
    )


async def purge_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await group_private_control_notice(update):
        return
    if not await admin_only(update):
        return

    if not context.args or not context.args[0].lstrip("-").isdigit():
        await update.message.reply_text(
            "Использование:\n/purge_chat <chat_id>\n\n"
            "Команда полностью удалит сообщения, файлы и запись чата из списка известных."
        )
        return

    target_chat_id = int(context.args[0])
    deleted_messages, deleted_files = purge_chat_archive_data(target_chat_id)

    await update.message.reply_text(
        "Данные чата удалены.\n\n"
        f"Chat ID: {target_chat_id}\n"
        f"Удалено сообщений: {deleted_messages}\n"
        f"Удалено файлов: {deleted_files}\n\n"
        "Если бот снова увидит этот чат, он заново запросит подтверждение."
    )


async def purge_all_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await group_private_control_notice(update):
        return
    if not await admin_only(update):
        return

    if not context.args or context.args[0] != "CONFIRM":
        await update.message.reply_text(
            "Опасная команда полной очистки архива.\n\n"
            "Использование:\n/purge_all_data CONFIRM\n\n"
            "Будут удалены все сохранённые сообщения, файлы и экспорты.\n"
            "Список approved/pending/rejected чатов сохранится."
        )
        return

    deleted_messages, deleted_files = purge_all_archive_data()

    await update.message.reply_text(
        "Полная очистка архива выполнена.\n\n"
        f"Удалено сообщений из базы: {deleted_messages}\n"
        f"Удалено физических файлов и экспортов: {deleted_files}\n"
        "Список известных чатов сохранён."
    )


async def work_package(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await group_private_control_notice(update):
        return
    if not await admin_only(update):
        return
    if not await ensure_chat_approved(update, context):
        return

    chat = update.effective_chat
    chat_title = chat.title or chat.full_name or str(chat.id)

    await update.message.reply_text(
        "Готовлю рабочий пакет для анализа в ChatGPT: переписка, индекс вложений и промпт..."
    )

    until_dt = datetime.now(timezone.utc)
    since_dt = until_dt - timedelta(days=7)

    prepare_transcripts_for_period(
        since_dt,
        until_dt,
        chat_id=chat.id,
    )

    export_path, message_count = export_last_days(
        7,
        chat_id=chat.id,
        chat_title_for_filename=chat_title,
    )

    attachments_index_path, attachment_count = create_attachments_index(
        7,
        chat_id=chat.id,
        chat_title_for_filename=chat_title,
        period_label="last 7 days",
    )

    prompt_path = create_prompt_file(
        get_chatgpt_work_prompt(chat_title, 7, "последние 7 дней"),
        chat_title_for_filename=chat_title,
    )

    contact_sheet_paths = create_contact_sheets(
        7,
        chat_id=chat.id,
        chat_title_for_filename=chat_title,
        period_label="last 7 days",
    )

    await send_document_safely(
        update,
        context,
        export_path,
        caption=f"Chat export. Переписка текущего чата за последние 7 дней. Сообщений: {message_count}",
    )

    await send_document_safely(
        update,
        context,
        attachments_index_path,
        caption=f"Attachments index. Индекс вложений за последние 7 дней. Вложений/файловых сообщений: {attachment_count}",
    )

    await send_document_safely(
        update,
        context,
        prompt_path,
        caption="Analysis prompt. Промпт для анализа рабочего чата в ChatGPT.",
    )

    if contact_sheet_paths:
        for index, sheet_path in enumerate(contact_sheet_paths, start=1):
            await send_document_safely(
                update,
                context,
                sheet_path,
                caption=f"Contact sheet {index}/{len(contact_sheet_paths)}. Крупный обзор изображений и скриншотов.",
            )
    else:
        await update.message.reply_text("Изображений за период не найдено, contact sheet не создан.")

    await update.message.reply_text(
        "Готовлю полный ZIP-архив с перепиской и вложениями. Это может занять немного времени..."
    )

    zip_path, zip_message_count, copied_files = create_zip_export(
        7,
        chat_id=chat.id,
        chat_title_for_filename=chat_title,
    )

    await send_document_safely(
        update,
        context,
        zip_path,
        caption=(
            f"Full archive. Полный ZIP-архив текущего чата за последние 7 дней.\n"
            f"Сообщений: {zip_message_count}\n"
            f"Файлов: {copied_files}\n\n"
            "Используй его как источник вложений, если нужно открыть конкретные файлы из attachments_index.md."
        ),
    )

    await update.message.reply_text(
        "Рабочий пакет готов. Для анализа загрузи в ChatGPT: chat_export.md, attachments_index.md, work_analysis_prompt.txt и contact sheets. ZIP используй как источник оригинальных вложений при необходимости."
    )



async def work_shift_package(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await group_private_control_notice(update):
        return
    if not await admin_only(update):
        return
    if not await ensure_chat_approved(update, context):
        return

    chat = update.effective_chat
    chat_title = chat.title or chat.full_name or str(chat.id)

    since_dt, until_dt, period_label = get_current_work_shift_interval()

    await update.message.reply_text(
        "Готовлю рабочий пакет за текущую смену для анализа в ChatGPT..."
    )

    prepare_transcripts_for_period(
        since_dt,
        until_dt,
        chat_id=chat.id,
    )

    export_path, message_count = export_interval(
        since_dt,
        until_dt,
        chat_id=chat.id,
        chat_title_for_filename=chat_title,
        period_label=period_label,
    )

    attachments_index_path, attachment_count = create_attachments_index(
        1,
        chat_id=chat.id,
        chat_title_for_filename=chat_title,
        since_dt=since_dt,
        until_dt=until_dt,
        period_label=period_label,
    )

    prompt_path = create_prompt_file(
        get_chatgpt_work_prompt(chat_title, 1, period_label),
        chat_title_for_filename=chat_title,
    )

    contact_sheet_paths = create_contact_sheets(
        1,
        chat_id=chat.id,
        chat_title_for_filename=chat_title,
        since_dt=since_dt,
        until_dt=until_dt,
        period_label=period_label,
    )

    await send_document_safely(
        update,
        context,
        export_path,
        caption=f"Chat export. Переписка текущего чата за смену. Сообщений: {message_count}",
    )

    await send_document_safely(
        update,
        context,
        attachments_index_path,
        caption=f"Attachments index. Индекс вложений за смену. Вложений/файловых сообщений: {attachment_count}",
    )

    await send_document_safely(
        update,
        context,
        prompt_path,
        caption="Analysis prompt. Промпт для анализа рабочей смены в ChatGPT.",
    )

    if contact_sheet_paths:
        for index, sheet_path in enumerate(contact_sheet_paths, start=1):
            await send_document_safely(
                update,
                context,
                sheet_path,
                caption=f"Contact sheet {index}/{len(contact_sheet_paths)}. Крупный обзор изображений и скриншотов за смену.",
            )
    else:
        await update.message.reply_text("Изображений за смену не найдено, contact sheet не создан.")

    await update.message.reply_text(
        "Готовлю полный ZIP-архив смены с перепиской и вложениями. Это может занять немного времени..."
    )

    try:
        zip_path, zip_message_count, copied_files = create_zip_export_interval(
            since_dt,
            until_dt,
            chat_id=chat.id,
            chat_title_for_filename=chat_title,
        )
    except Exception as e:
        logging.exception("Could not create work shift ZIP archive")
        await update.message.reply_text(
            "Не удалось создать ZIP-архив смены. Первые 3 файла уже готовы, но вложения в ZIP не собраны.\n\n"
            f"Ошибка: {e}"
        )
        return

    sent = await send_document_safely(
        update,
        context,
        zip_path,
        caption=(
            f"Full archive. Полный ZIP-архив текущего чата за смену.\n"
            f"Период: {period_label}\n"
            f"Сообщений: {zip_message_count}\n"
            f"Файлов: {copied_files}\n\n"
            "Используй его как источник вложений, если нужно открыть конкретные файлы из attachments_index.md."
        ),
    )

    if sent:
        await update.message.reply_text(
            "Рабочий пакет за смену готов. Для анализа загрузи в ChatGPT: chat_export.md, attachments_index.md, work_analysis_prompt.txt и contact sheets. ZIP используй как источник оригинальных вложений при необходимости."
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    if not message:
        return

    if await handle_set_work_prompt_upload(update, context):
        return

    if not await ensure_chat_approved(update, context):
        return

    try:
        file_info = await download_attachment(message, context)
        save_message(message, file_info)
        cleanup_old_data()

        user = message.from_user
        author = user.full_name if user else "unknown"
        text = message.text or message.caption or ""
        file_part = f" | file={file_info.get('file_path')}" if file_info.get("file_path") else ""

        print(
            f"Saved: chat={message.chat.id} msg={message.message_id} | "
            f"{author} | {detect_message_type(message)} | {text[:80]}{file_part}"
        )

    except Exception as e:
        print(f"Error while saving message {message.message_id}: {e}")


async def setup_bot_commands(app: Application):
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Личный кабинет"),
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )

    await app.bot.set_my_commands(
        [
            BotCommand("status", "Статус бота в этом чате"),
            BotCommand("help", "Краткая справка"),
        ],
        scope=BotCommandScopeAllGroupChats(),
    )


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в файле .env")

    init_db()
    cleanup_old_data()

    app = Application.builder().token(BOT_TOKEN).post_init(setup_bot_commands).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("version", version))
    app.add_handler(CommandHandler("health", health))
    app.add_handler(CommandHandler("storage_status", storage_status))
    app.add_handler(CommandHandler("prompt_status", prompt_status))
    app.add_handler(CommandHandler("reset_work_prompt", reset_work_prompt))
    app.add_handler(CommandHandler("vacuum_db", vacuum_db))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("global_status", global_status))
    app.add_handler(CommandHandler("export_week", export_week))
    app.add_handler(CommandHandler("export_today", export_today))
    app.add_handler(CommandHandler("export_all_week", export_all_week))
    app.add_handler(CommandHandler("export_all_today", export_all_today))
    app.add_handler(CommandHandler("export_zip_week", export_zip_week))
    app.add_handler(CommandHandler("export_zip_today", export_zip_today))
    app.add_handler(CommandHandler("export_zip_all_week", export_zip_all_week))
    app.add_handler(CommandHandler("export_zip_all_today", export_zip_all_today))
    app.add_handler(CommandHandler("pending_chats", pending_chats))
    app.add_handler(CommandHandler("approved_chats", approved_chats))
    app.add_handler(CommandHandler("approve_chat", approve_chat))
    app.add_handler(CommandHandler("reject_chat", reject_chat))
    app.add_handler(CommandHandler("remove_chat", remove_chat))
    app.add_handler(CommandHandler("summary_prompt", summary_prompt))
    app.add_handler(CommandHandler("weekly_package", weekly_package))
    app.add_handler(CommandHandler("work_package", work_package))
    app.add_handler(CommandHandler("work_shift_package", work_shift_package))
    app.add_handler(CommandHandler("report_chats", report_chats))
    app.add_handler(CommandHandler("cleanup_now", cleanup_now))
    app.add_handler(CommandHandler("purge_chat", purge_chat))
    app.add_handler(CommandHandler("purge_all_data", purge_all_data))
    app.add_handler(CommandHandler("friendly_prompt", friendly_prompt))
    app.add_handler(CommandHandler("friendly_package", friendly_package))
    app.add_handler(CallbackQueryHandler(admin_menu_callback, pattern=r"^admin:"))
    app.add_handler(CallbackQueryHandler(work_package_callback, pattern=r"^(workpkg|workpdf|noop):"))
    app.add_handler(CallbackQueryHandler(chat_approval_callback, pattern="^(approve_chat|reject_chat):"))

    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    print("Бот запущен. Нажми Ctrl+C для остановки.")
    print(f"Хранение данных: последние {RETENTION_DAYS} дней")
    print(f"Максимальный размер файла: {MAX_FILE_SIZE_MB} MB")
    print(f"Максимальный размер отправляемого файла: {MAX_TELEGRAM_SEND_SIZE_MB} MB")
    print(f"Telegram send timeout: {TELEGRAM_SEND_TIMEOUT_SECONDS} seconds")
    print(f"Contact sheets: {CONTACT_SHEET_IMAGES_PER_PAGE} images/page, width {CONTACT_SHEET_PAGE_WIDTH}px, quality {CONTACT_SHEET_JPEG_QUALITY}")
    print("Команды:")
    print("/help — справка и меню в личном чате")
    print("/version — версия и сборка бота")
    print("/health — технический статус бота")
    print("/storage_status — размер базы и файлов")
    print("/vacuum_db — сжать SQLite после очистки")
    print("/status — статус текущего чата")
    print("/global_status — статус всех чатов")
    print("/export_today — экспорт текущего чата за сегодня")
    print("/export_week — экспорт текущего чата за 7 дней")
    print("/export_all_today — экспорт всех чатов за сегодня")
    print("/export_all_week — экспорт всех чатов за 7 дней")
    print("/export_zip_today — ZIP текущего чата за сегодня")
    print("/export_zip_week — ZIP текущего чата за 7 дней")
    print("/export_zip_all_today — ZIP всех чатов за сегодня")
    print("/export_zip_all_week — ZIP всех чатов за 7 дней")
    print("/pending_chats — чаты на подтверждение")
    print("/approved_chats — подтверждённые чаты")
    print("/approve_chat <chat_id> — подтвердить чат")
    print("/reject_chat <chat_id> — отклонить чат")
    print("/remove_chat <chat_id> — отключить чат")
    print("/summary_prompt — промпт для Gemini")
    print("/weekly_package — legacy, используйте /report_chats")
    print("/work_package — рабочий пакет для ChatGPT")
    print("/work_shift_package — рабочий пакет за смену 09:00–сейчас")
    print("/report_chats — выбрать чат для пакета кнопками в личке")
    print("/cleanup_now — принудительная retention-очистка")
    print("/purge_chat <chat_id> — удалить архив конкретного чата")
    print("/purge_all_data CONFIRM — удалить весь архив")

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
