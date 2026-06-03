import os
import re
import sqlite3
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters


load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

ADMIN_USER_IDS_RAW = os.getenv("ADMIN_USER_IDS", "")
ADMIN_USER_IDS = {
    int(x.strip())
    for x in ADMIN_USER_IDS_RAW.split(",")
    if x.strip().isdigit()
}

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
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                file_path
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
                file_path
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Бот работает. Я сохраняю сообщения и вложения для недельного архива."
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

    await update.message.reply_document(
        document=export_path.open("rb"),
        filename=export_path.name,
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

    await update.message.reply_document(
        document=export_path.open("rb"),
        filename=export_path.name,
        caption=f"Экспорт текущего чата за сегодня. Сообщений: {count}",
    )


async def export_all_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    export_path, count = export_last_days(7, chat_id=None)

    if count == 0:
        await update.message.reply_text("За последние 7 дней пока нет сохранённых сообщений.")
        return

    await update.message.reply_document(
        document=export_path.open("rb"),
        filename=export_path.name,
        caption=f"Глобальный экспорт за последние 7 дней. Сообщений: {count}",
    )


async def export_all_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    export_path, count = export_last_days(1, chat_id=None)

    if count == 0:
        await update.message.reply_text("За сегодня пока нет сохранённых сообщений.")
        return

    await update.message.reply_document(
        document=export_path.open("rb"),
        filename=export_path.name,
        caption=f"Глобальный экспорт за сегодня. Сообщений: {count}",
    )



async def export_zip_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    if not await ensure_chat_approved(update, context):
        return

    chat = update.effective_chat
    zip_path, count, copied_files = create_zip_export(
        7,
        chat_id=chat.id,
        chat_title_for_filename=chat.title or chat.full_name or str(chat.id),
    )

    if count == 0:
        await update.message.reply_text("За последние 7 дней в этом чате пока нет сохранённых сообщений.")
        return

    await update.message.reply_document(
        document=zip_path.open("rb"),
        filename=zip_path.name,
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
    zip_path, count, copied_files = create_zip_export(
        1,
        chat_id=chat.id,
        chat_title_for_filename=chat.title or chat.full_name or str(chat.id),
    )

    if count == 0:
        await update.message.reply_text("За сегодня в этом чате пока нет сохранённых сообщений.")
        return

    await update.message.reply_document(
        document=zip_path.open("rb"),
        filename=zip_path.name,
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

    await update.message.reply_document(
        document=zip_path.open("rb"),
        filename=zip_path.name,
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

    await update.message.reply_document(
        document=zip_path.open("rb"),
        filename=zip_path.name,
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

    ok = set_chat_status(chat_id, "rejected", None)

    if not ok:
        await update.message.reply_text("Чат не найден.")
        return

    await update.message.reply_text(f"Чат {chat_id} удалён из разрешённых. Новые сообщения сохраняться не будут.")


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
    app.add_handler(CallbackQueryHandler(chat_approval_callback, pattern="^(approve_chat|reject_chat):"))

    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    print("Бот запущен. Нажми Ctrl+C для остановки.")
    print(f"Хранение данных: последние {RETENTION_DAYS} дней")
    print("Команды:")
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

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
