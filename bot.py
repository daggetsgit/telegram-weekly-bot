import os
import re
import sqlite3
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.error import TelegramError, TimedOut


load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

ADMIN_USER_IDS_RAW = os.getenv("ADMIN_USER_IDS", "")
ADMIN_USER_IDS = {
    int(x.strip())
    for x in ADMIN_USER_IDS_RAW.split(",")
    if x.strip().isdigit()
}

MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
MAX_TELEGRAM_SEND_SIZE_MB = int(os.getenv("MAX_TELEGRAM_SEND_SIZE_MB", "49"))
MAX_TELEGRAM_SEND_SIZE_BYTES = MAX_TELEGRAM_SEND_SIZE_MB * 1024 * 1024
TELEGRAM_SEND_TIMEOUT_SECONDS = int(os.getenv("TELEGRAM_SEND_TIMEOUT_SECONDS", "180"))

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
EXPORT_DIR = DATA_DIR / "exports"
FILES_DIR = DATA_DIR / "files"
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


def is_admin(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False

    if not ADMIN_USER_IDS:
        return True

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
        candidate = DATA_DIR / file_path_value
        if candidate.exists() and candidate.is_file():
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
                message_type, text, caption, file_id, file_name, file_mime_type,
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
                message_type, text, caption, file_id, file_name, file_mime_type,
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
                message_type, text_value, caption, file_id, file_name, file_mime_type,
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
    zip_path = EXPORT_DIR / f"telegram_export_{scope}_last_{days}_days.zip"

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


def export_last_days(days: int = 7, chat_id=None, chat_title_for_filename=None):
    since = datetime.now(timezone.utc) - timedelta(days=days)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    if chat_id is None:
        cur.execute(
            """
            SELECT
                date_utc,
                chat_title,
                message_id,
                full_name,
                username,
                message_type,
                text,
                caption,
                reply_to_message_id,
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
                date_utc,
                chat_title,
                message_id,
                full_name,
                username,
                message_type,
                text,
                caption,
                reply_to_message_id,
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
                date_utc,
                chat_title,
                message_id,
                full_name,
                username,
                message_type,
                text,
                caption,
                reply_to_message_id,
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

            content = text or caption or ""

            if content:
                f.write("\n")
                f.write(content.strip())
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



def get_chatgpt_work_prompt(chat_title: str, days: int = 7, period_label: str = None) -> str:
    period_text = period_label or f"последние {days} дней"

    return f"""Ты — аналитик рабочей коммуникации и помощник по управлению задачами.

Я загрузил рабочий пакет с экспортом Telegram-чата "{chat_title}" за период: {period_text}.

В пакете могут быть:
1. chat_export.md — полная переписка за период.
2. attachments_index.md — индекс вложений: файлы, фото, документы, аудио, подписи и контекст вокруг них.
3. Отдельные вложения или полный ZIP-архив, если они были загружены дополнительно.

Контекст компании:
Best Service Assistance — компания медицинского ассистанса. В рабочих чатах могут встречаться чувствительные медицинские, юридические и страховые темы: смерть пациента, тело/труп, травмы, ДТП, госпитализация, операции, медицинская эвакуация, репатриация, полиция, насилие, страховые споры, финансовые претензии, счета клиник и другие сложные случаи.

Такие упоминания являются частью профессионального контекста medical assistance. Не исключай их автоматически и не прекращай анализ. Обрабатывай такие темы нейтрально, деловым языком, без сенсационности, без лишних графичных деталей и без домыслов. Фиксируй только то, что нужно для понимания задач, решений, рисков, сроков, ответственных и дальнейших действий.

Порядок анализа:
- Сначала прочитай chat_export.md.
- Затем изучи attachments_index.md.
- Если загружены отдельные документы, изображения, скриншоты или файлы — изучи их содержимое.
- Если есть изображения или скриншоты, анализируй видимый текст, интерфейсы, таблицы, документы, ошибки, суммы, даты, имена, статусы и другие важные детали.
- Если есть PDF, Word, Excel, презентации или текстовые документы — изучи их содержимое, если оно доступно.
- Если есть аудио или голосовые без расшифровки, не игнорируй их и не выдумывай содержание.

Работа с голосовыми:
Если в attachments_index.md есть voice/audio без transcript, сначала попроси пользователя прислать расшифровки вручную. Сформируй короткий список:
1. дата/время — автор — имя файла — краткий контекст
2. ...

Пока расшифровки не предоставлены:
- не выдумывай содержание голосовых;
- используй только факт наличия голосового и контекст сообщений до/после;
- пометь такие голосовые как “требует ручной расшифровки”.

Если пользователь прислал расшифровки, сопоставь их с voice/audio по порядку, времени, автору или имени файла и учитывай в общем анализе.

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
- срок, если он был указан или явно подразумевается;
- источник/контекст из переписки;
- связанные файлы;
- статус: новая / в работе / ожидает ответа / выполнена / риск просрочки.

Если ответственный или срок не указаны явно, не выдумывай. Напиши «не указан» или «требуется подтверждение».

4. Открытые вопросы
Выдели вопросы, которые обсуждались, но не были закрыты. Для каждого укажи:
- вопрос;
- кто должен ответить, если понятно;
- что нужно сделать дальше;
- почему это важно.

5. Решения и договорённости
Отдельно перечисли все решения, договорённости и подтверждения, которые были явно зафиксированы в переписке.

6. Риски и проблемные места
Отметь возможные риски:
- задержки;
- отсутствие ответственного;
- отсутствие срока;
- финансовые вопросы;
- конфликтные или спорные моменты;
- срочные задачи;
- темы, которые могут быть забыты;
- документы или файлы, которые требуют проверки.

7. Файлы и вложения
Составь список важных вложений. Для каждого укажи:
- название файла;
- тип файла;
- к какой теме относится;
- что в нём содержится, если файл доступен для чтения;
- какой контекст вокруг файла был в переписке;
- какие действия с ним нужны, если это понятно.

Если файл нельзя прочитать, открыть, прослушать или понять — явно укажи это.

8. Следующие шаги
Сформируй короткий список приоритетных действий на ближайшие дни.

9. Executive summary
В конце дай краткую версию на 5–7 пунктов, которую можно быстро отправить руководителю или команде.

Правила:
- Не выдумывай факты, имена, сроки, решения или содержание файлов.
- Если информация неочевидна, помечай её как предположение.
- Не пересказывай всю переписку подряд, а группируй информацию по смыслу.
- Пиши деловым, нейтральным стилем.
- Если есть персональные или чувствительные данные, используй их только тогда, когда это необходимо для понимания задач и контекста.
"""


async def summary_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    if not await ensure_chat_approved(update, context):
        return

    chat = update.effective_chat
    chat_title = chat.title or chat.full_name or str(chat.id)

    await update.message.reply_text(
        get_gemini_summary_prompt(chat_title, 7)
    )


async def weekly_package(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            f"Недельный пакет для анализа в Gemini.\n"
            f"Чат: {chat_title}\n"
            f"Сообщений: {count}\n"
            f"Файлов: {copied_files}\n\n"
            f"Следующим сообщением отправлю промпт для анализа."
        ),
    )

    await update.message.reply_text(
        get_gemini_summary_prompt(chat_title, 7)
    )



def get_private_admin_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["/help", "/health"],
            ["/global_status"],
            ["/pending_chats", "/approved_chats"],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        selective=True,
    )


def get_private_help_text() -> str:
    return """Weekly Work Summary Bot

Что делает бот:
- сохраняет сообщения и вложения из подтверждённых рабочих Telegram-чатов;
- формирует Markdown и ZIP-архивы;
- помогает подготовить архив для анализа в Gemini;
- хранит данные только за последние 14 дней;
- не сохраняет файлы больше 50 MB;
- новые чаты начинает сохранять только после подтверждения администратором.

Основной рабочий сценарий в группе:

/work_package
Рабочий пакет для ChatGPT: переписка, индекс вложений и промпт.

/weekly_package
Отправляет ZIP-архив за последние 7 дней и готовый промпт для Gemini.

/export_zip_week
Отправляет ZIP-архив текущего чата за последние 7 дней.

/export_zip_today
Отправляет ZIP-архив текущего чата за сегодня.

/summary_prompt
Отправляет только промпт для Gemini без архива.

/status
Показывает статус текущего чата: сколько сообщений и файлов сохранено.

Управление чатами:

/pending_chats
Показывает чаты, ожидающие подтверждения.

/approved_chats
Показывает подтверждённые чаты.

/approve_chat <chat_id>
Подтвердить чат вручную.

/reject_chat <chat_id>
Отклонить чат вручную.

/remove_chat <chat_id>
Удалить чат из списка известных. При следующем сообщении бот снова запросит подтверждение.

/cleanup_now
Принудительно запустить retention-очистку.

/purge_chat <chat_id>
Полностью удалить архив конкретного чата.

/purge_all_data CONFIRM
Полностью удалить все сохранённые сообщения, файлы и экспорты.

Глобальные команды:

/health
Технический статус бота: хранение, база, файлы, чаты.

/global_status
Общий статус по всем чатам.

/export_all_week
Markdown-экспорт всех чатов за 7 дней.

/export_zip_all_week
ZIP-экспорт всех чатов за 7 дней.

Доступ:
Команды экспорта и управления доступны только администратору бота.
Обычные участники не могут выгружать архивы.
"""


def get_group_help_text() -> str:
    return """Weekly Work Summary Bot

Этот бот сохраняет сообщения и вложения из подтверждённого рабочего чата и помогает подготовить недельный архив для анализа в Gemini.

Основные команды для текущего чата:

/work_package
Рабочий пакет для ChatGPT: переписка, индекс вложений и промпт.

/weekly_package
ZIP-архив за последние 7 дней + готовый промпт для Gemini.

/export_zip_week
ZIP-архив текущего чата за последние 7 дней.

/export_zip_today
ZIP-архив текущего чата за сегодня.

/summary_prompt
Только промпт для Gemini без архива.

/status
Статус текущего чата: сколько сообщений и файлов сохранено.

Важно:
- команды доступны только администратору бота;
- обычные участники не могут выгружать архивы;
- данные хранятся только за последние 14 дней;
- файлы больше 50 MB не скачиваются;
- новые чаты сохраняются только после подтверждения администратором.
"""


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    chat = update.effective_chat
    reply_markup = None

    if chat and chat.type == "private":
        reply_markup = get_private_admin_keyboard()
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

    if chat and chat.type == "private" and is_admin(update):
        reply_markup = get_private_admin_keyboard()

    await update.message.reply_text(
        "Бот работает. Я сохраняю сообщения и вложения из подтверждённых чатов для недельного архива.\n\n"
        "Напиши /help, чтобы посмотреть доступные команды.",
        reply_markup=reply_markup,
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

    ok = delete_chat_record(chat_id)

    if not ok:
        await update.message.reply_text("Чат не найден.")
        return

    await update.message.reply_text(
        f"Чат {chat_id} удалён из списка. "
        "При следующем сообщении бот снова запросит подтверждение администратора."
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



async def cleanup_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    if not await admin_only(update):
        return
    if not await ensure_chat_approved(update, context):
        return

    chat = update.effective_chat
    chat_title = chat.title or chat.full_name or str(chat.id)

    await update.message.reply_text(
        "Готовлю рабочий пакет для анализа в ChatGPT: переписка, индекс вложений и промпт..."
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

    await send_document_safely(
        update,
        context,
        export_path,
        caption=f"1/4. Переписка текущего чата за последние 7 дней. Сообщений: {message_count}",
    )

    await send_document_safely(
        update,
        context,
        attachments_index_path,
        caption=f"2/4. Индекс вложений за последние 7 дней. Вложений/файловых сообщений: {attachment_count}",
    )

    await send_document_safely(
        update,
        context,
        prompt_path,
        caption="3/4. Промпт для анализа рабочего чата в ChatGPT.",
    )

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
            f"4/4. Полный ZIP-архив текущего чата за последние 7 дней.\n"
            f"Сообщений: {zip_message_count}\n"
            f"Файлов: {copied_files}\n\n"
            "Используй его как источник вложений, если нужно открыть конкретные файлы из attachments_index.md."
        ),
    )

    await update.message.reply_text(
        "Рабочий пакет готов. Загрузи в ChatGPT файлы 1–3 для структуры анализа, а ZIP используй как источник вложений при необходимости."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    if not message:
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


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в файле .env")

    init_db()
    cleanup_old_data()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("health", health))
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
    app.add_handler(CommandHandler("cleanup_now", cleanup_now))
    app.add_handler(CommandHandler("purge_chat", purge_chat))
    app.add_handler(CommandHandler("purge_all_data", purge_all_data))
    app.add_handler(CommandHandler("friendly_prompt", friendly_prompt))
    app.add_handler(CommandHandler("friendly_package", friendly_package))
    app.add_handler(CallbackQueryHandler(chat_approval_callback, pattern="^(approve_chat|reject_chat):"))

    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    print("Бот запущен. Нажми Ctrl+C для остановки.")
    print(f"Хранение данных: последние {RETENTION_DAYS} дней")
    print(f"Максимальный размер файла: {MAX_FILE_SIZE_MB} MB")
    print(f"Максимальный размер отправляемого файла: {MAX_TELEGRAM_SEND_SIZE_MB} MB")
    print(f"Telegram send timeout: {TELEGRAM_SEND_TIMEOUT_SECONDS} seconds")
    print("Команды:")
    print("/help — справка и меню в личном чате")
    print("/health — технический статус бота")
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
    print("/weekly_package — ZIP за неделю + промпт для Gemini")
    print("/work_package — рабочий пакет для ChatGPT")
    print("/cleanup_now — принудительная retention-очистка")
    print("/purge_chat <chat_id> — удалить архив конкретного чата")
    print("/purge_all_data CONFIRM — удалить весь архив")

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
