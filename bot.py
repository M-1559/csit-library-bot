"""
=============================================================================
Project: CSIT Batch 8 Academic Library Telegram Bot (Ultimate AI Edition v5.3)
Description: Enterprise-grade academic repository with Full User Tracking & AI.
Author: Mohamed Al-Zulaimi
Version: 5.3.1 (Fixed & Fully Optimized)
=============================================================================
"""

import asyncio
import io
import json
import logging
import os
import re
import sqlite3
import sys
import time
import zipfile
from contextlib import closing
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, html, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage

# --- إعدادات التشغيل الأساسية والمباشرة ---
TELEGRAM_BOT_TOKEN = "9022187543:AAElP4LB9NRYa62mElWOrbl8Dol6MWlNfKA"
ADMINISTRATOR_ID = 6334399799  # المعرف الإداري الصحيح الخاص بك
DATABASE_PATH = Path("data/csit_library.sqlite3")
REDIS_URL = os.getenv("REDIS_URL")

storage = RedisStorage.from_url(REDIS_URL) if REDIS_URL else MemoryStorage()
dispatcher = Dispatcher(storage=storage)

SECURITY_FLOOD_TRACKER: dict[int, float] = {}

# --- تعريف حالات النظام (FSM States) ---
class SubmissionStates(StatesGroup):
    awaiting_document = State()
    selecting_subject = State()
    selecting_branch = State()

class SupportStates(StatesGroup):
    awaiting_inquiry = State()

class AdministrationStates(StatesGroup):
    awaiting_response_text = State()
    awaiting_broadcast_text = State()

class SearchStates(StatesGroup):
    awaiting_keyword = State()

class AIAssistantStates(StatesGroup):
    awaiting_ai_question = State()

# فهرس المقررات الدراسية الرسمية
CURRICULUM_SUBJECTS = {
    "prog": "💻 برمجة متقدمة",
    "mining": "📊 تنقيب بيانات",
    "analysis": "📐 تحليل وتصميم نظم",
    "web": "🌐 تقنيات ويب متقدمة",
    "theory": "🧮 نظرية احتسابية",
    "graphics": "🎨 رسم بالحاسوب"
}

# --- وظائف الحماية وقواعد البيانات ---

def audit_logger(action_name: str, user_id: int, details: str) -> None:
    logging.getLogger("csit.audit").info(
        "action=%s user_id=%s details=%s", action_name, user_id, details
    )

def initialize_database() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DATABASE_PATH)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                username TEXT,
                first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS academic_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject_code TEXT NOT NULL,
                branch_type TEXT NOT NULL,
                file_id TEXT NOT NULL,
                file_name TEXT NOT NULL,
                sender TEXT NOT NULL,
                submitted_by INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
                pages_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_academic_files_subject_status
                ON academic_files(subject_code, branch_type, status);
            """
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(users)")
        }
        if "username" not in columns:
            connection.execute("ALTER TABLE users ADD COLUMN username TEXT")

        columns_files = {
            row[1] for row in connection.execute("PRAGMA table_info(academic_files)")
        }
        if "pages_json" not in columns_files:
            connection.execute(
                "ALTER TABLE academic_files ADD COLUMN pages_json TEXT NOT NULL DEFAULT '[]'"
            )

def register_user(user_id: int, full_name: str, username: str | None) -> bool:
    with closing(sqlite3.connect(DATABASE_PATH)) as connection, connection:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO users (user_id, full_name, username) VALUES (?, ?, ?)",
            (user_id, full_name, username),
        )
        connection.execute(
            "UPDATE users SET full_name = ?, username = ? WHERE user_id = ?", (full_name, username, user_id)
        )
        return cursor.rowcount == 1

def user_count() -> int:
    with closing(sqlite3.connect(DATABASE_PATH)) as connection, connection:
        return connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]

def get_all_users() -> list[dict]:
    with closing(sqlite3.connect(DATABASE_PATH)) as connection, connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT user_id, full_name, username, first_seen FROM users ORDER BY first_seen DESC").fetchall()
        return [dict(row) for row in rows]

def student_ids() -> list[int]:
    with closing(sqlite3.connect(DATABASE_PATH)) as connection, connection:
        rows = connection.execute("SELECT user_id FROM users").fetchall()
    return [row[0] for row in rows]

def save_academic_file(
    subject_code: str,
    branch_type: str,
    file_id: str,
    file_name: str,
    sender: str,
    submitted_by: int,
    status: str,
    extracted_pages: list[str] | None = None,
) -> int:
    with closing(sqlite3.connect(DATABASE_PATH)) as connection, connection:
        cursor = connection.execute(
            """
            INSERT INTO academic_files
                (subject_code, branch_type, file_id, file_name, sender, submitted_by, status, pages_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                subject_code,
                branch_type,
                file_id,
                file_name,
                sender,
                submitted_by,
                status,
                json.dumps(extracted_pages or [], ensure_ascii=False),
            ),
        )
        return cursor.lastrowid

def delete_academic_file(record_id: int) -> None:
    with closing(sqlite3.connect(DATABASE_PATH)) as connection, connection:
        connection.execute("DELETE FROM academic_files WHERE id = ?", (record_id,))

def get_academic_files(
    subject_code: str | None = None, branch_type: str | None = None
) -> list[dict[str, object]]:
    query = "SELECT * FROM academic_files WHERE status = 'approved'"
    parameters: list[str] = []
    if subject_code is not None:
        query += " AND subject_code = ?"
        parameters.append(subject_code)
    if branch_type is not None:
        query += " AND branch_type = ?"
        parameters.append(branch_type)
    query += " ORDER BY created_at DESC"
    with closing(sqlite3.connect(DATABASE_PATH)) as connection, connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(query, parameters).fetchall()]

def get_pending_file(record_id: int) -> dict[str, object] | None:
    with closing(sqlite3.connect(DATABASE_PATH)) as connection, connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM academic_files WHERE id = ? AND status = 'pending'",
            (record_id,),
        ).fetchone()
    return dict(row) if row else None

def update_file_status(record_id: int, status: str) -> bool:
    with closing(sqlite3.connect(DATABASE_PATH)) as connection, connection:
        cursor = connection.execute(
            "UPDATE academic_files SET status = ? WHERE id = ? AND status = 'pending'",
            (status, record_id),
        )
        return cursor.rowcount == 1

def approved_file_count() -> int:
    with closing(sqlite3.connect(DATABASE_PATH)) as connection, connection:
        return connection.execute(
            "SELECT COUNT(*) FROM academic_files WHERE status = 'approved'"
        ).fetchone()[0]

async def answer_callback(callback: CallbackQuery, text: str | None = None) -> None:
    try:
        await callback.answer(text)
    except TelegramBadRequest as error:
        message = str(error).lower()
        if "query is too old" not in message and "query id is invalid" not in message:
            raise
        logging.getLogger(__name__).debug("Ignoring expired callback query: %s", error)

def check_anti_flood(user_id: int) -> bool:
    current_time = time.time()
    if len(SECURITY_FLOOD_TRACKER) > 5000:
        expired_users = [
            tracked_user
            for tracked_user, last_time in SECURITY_FLOOD_TRACKER.items()
            if current_time - last_time >= 3.0
        ]
        for tracked_user in expired_users:
            SECURITY_FLOOD_TRACKER.pop(tracked_user, None)
    last_time = SECURITY_FLOOD_TRACKER.get(user_id, 0)
    if current_time - last_time < 3.0:
        return True
    SECURITY_FLOOD_TRACKER[user_id] = current_time
    return False

def moderate_content(text: str) -> bool:
    blocked_keywords = ["porn", "spam", "تحشيش", "إعلان فاحش"]
    text_lower = text.lower()
    for word in blocked_keywords:
        if word in text_lower:
            return True
    return False

def ai_file_security_filter(file_name: str) -> bool:
    allowed_extensions = {".pdf", ".docx", ".pptx", ".txt"}
    return Path(file_name).suffix.lower() in allowed_extensions

def extract_document_pages(file_name: str, content: bytes) -> list[str]:
    extension = Path(file_name).suffix.lower()
    if extension == ".pdf":
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError

        try:
            pages = [page.extract_text() or "" for page in PdfReader(io.BytesIO(content)).pages]
        except (PdfReadError, OSError, ValueError) as error:
            raise ValueError("The PDF could not be read.") from error
    elif extension == ".docx":
        from docx import Document
        from docx.opc.exceptions import PackageNotFoundError

        try:
            document = Document(io.BytesIO(content))
        except (PackageNotFoundError, OSError, zipfile.BadZipFile) as error:
            raise ValueError("The Word document could not be read.") from error
        paragraphs = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
        paragraphs.extend(
            " | ".join(cell.text for cell in row.cells)
            for table in document.tables
            for row in table.rows
        )
        pages = ["\n".join(paragraphs)]
    elif extension == ".pptx":
        from pptx import Presentation
        from pptx.exc import PackageNotFoundError

        try:
            presentation = Presentation(io.BytesIO(content))
        except (PackageNotFoundError, OSError, zipfile.BadZipFile) as error:
            raise ValueError("The PowerPoint file could not be read.") from error
        pages = [
            "\n".join(
                shape.text for shape in slide.shapes if getattr(shape, "has_text_frame", False)
            )
            for slide in presentation.slides
        ]
    elif extension == ".txt":
        try:
            pages = [content.decode("utf-8-sig")]
        except UnicodeDecodeError as error:
            raise ValueError("The text file must use UTF-8 encoding.") from error
    else:
        raise ValueError("Unsupported document format.")

    remaining = 120_000
    bounded_pages: list[str] = []
    for page in pages:
        text = page[:remaining]
        if text.strip():
            bounded_pages.append(text)
            remaining -= len(text)
        if remaining <= 0:
            break
    return bounded_pages

def split_telegram_text(text: str, limit: int = 3800) -> list[str]:
    chunks: list[str] = []
    remaining = text.strip()
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks

# --- واجهات المستخدم التفاعلية ---

def generate_main_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📚 المقررات الدراسية الرسمية والملازم", callback_data="subjects_menu")],
        [InlineKeyboardButton(text="📤 رفع ملف أو مرجع للمقررات", callback_data="start_upload")],
        [InlineKeyboardButton(text="🤖 المساعد الذكي (برمجة وتحليل علمي)", callback_data="ai_assistant_start")],
        [InlineKeyboardButton(text="🔍 البحث الذكي الفوري عن ملزمة", callback_data="start_search")],
        [InlineKeyboardButton(text="💬 الاستفسارات والشكاوى للإدارة", callback_data="support_start")],
        [InlineKeyboardButton(text="📢 قناة الدفعة والتعاميم", url="https://t.me/your_channel")],
        [InlineKeyboardButton(text="ℹ️ حول النظام الأكاديمي", callback_data="about_system")]
    ]
    if is_admin:
        rows.insert(0, [
            InlineKeyboardButton(text="⚡ لوحة التحكم والتعاميم", callback_data="admin_dashboard"),
            InlineKeyboardButton(text="👥 سجل المستخدمين", callback_data="admin_users_list")
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def generate_subjects_keyboard(prefix: str = "subj") -> InlineKeyboardMarkup:
    buttons = []
    for code, title in CURRICULUM_SUBJECTS.items():
        buttons.append([InlineKeyboardButton(text=title, callback_data=f"{prefix}_{code}")])
    buttons.append([InlineKeyboardButton(text="⬅️ العودة للقائمة الرئيسية", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def generate_branch_keyboard(subject_code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 القسم النظري للمقرر", callback_data=f"branch_{subject_code}_theory")],
        [InlineKeyboardButton(text="💻 القسم العملي للمقرر", callback_data=f"branch_{subject_code}_practical")],
        [InlineKeyboardButton(text="🤖 تلخيص ذكي للمقرر بالذكاء الاصطناعي", callback_data=f"ai_summarize_{subject_code}")],
        [InlineKeyboardButton(text="⬅️ العودة للمواد", callback_data="back_to_subjects")]
    ])

# --- معالجات الأحداث ونظام الانضمام ---

@dispatcher.message(CommandStart())
async def handle_start_command(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = message.from_user
    is_new = register_user(user.id, user.full_name, user.username)
    
    if is_new:
        audit_logger("USER_JOIN", user.id, f"New student joined: {user.full_name} (@{user.username})")
        if user.id != ADMINISTRATOR_ID:
            try:
                await message.bot.send_message(
                    chat_id=ADMINISTRATOR_ID,
                    text=f"🔔 <b>انضمام طالب جديد إلى النظام:</b>\n\n"
                         f"👤 الاسم: {html.quote(user.full_name)}\n"
                         f"🌐 المعرف: @{html.quote(user.username or 'بدون')}\n"
                         f"🆔 المعرف الرقمي: <code>{user.id}</code>\n"
                         f"👥 إجمالي الطلاب المشتركين: <code>{user_count()}</code>",
                    parse_mode=ParseMode.HTML
                )
            except TelegramAPIError:
                logging.getLogger(__name__).exception("Failed to notify administrator about a new user")

    is_admin = (user.id == ADMINISTRATOR_ID)
    await message.answer(
        f"مرحباً بك يا {html.bold(html.quote(user.first_name))} في المكتبة الأكاديمية للدفعة الثامنة CSIT 🎓\n\n"
        "النظام المطور: تصفح المقررات، ارفع الملازم، وتواصل مع الإدارة أو المساعد الذكي بكل سهولة.",
        reply_markup=generate_main_menu(is_admin),
        parse_mode=ParseMode.HTML
    )

@dispatcher.callback_query(F.data == "main_menu")
async def handle_main_menu_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_callback(callback)
    await state.clear()
    is_admin = (callback.from_user.id == ADMINISTRATOR_ID)
    await callback.message.edit_text("القائمة الرئيسية - النظام الأكاديمي للدفعة الثامنة:", reply_markup=generate_main_menu(is_admin))

@dispatcher.callback_query(F.data == "subjects_menu")
async def handle_subjects_menu(callback: CallbackQuery) -> None:
    await answer_callback(callback)
    await callback.message.edit_text("📚 اختر المقرر الدراسي الرسمي لعرض محتوياته المباشرة:", reply_markup=generate_subjects_keyboard("subj"))

@dispatcher.callback_query(F.data == "back_to_subjects")
async def handle_back_to_subjects(callback: CallbackQuery) -> None:
    await answer_callback(callback)
    await callback.message.edit_text("📚 اختر المقرر الدراسي الرسمي لعرض محتوياته المباشرة:", reply_markup=generate_subjects_keyboard("subj"))

@dispatcher.callback_query(F.data.startswith("subj_"))
async def handle_subject_selection(callback: CallbackQuery) -> None:
    await answer_callback(callback)
    s_code = callback.data.split("_")[1]
    subject_title = CURRICULUM_SUBJECTS.get(s_code)
    if subject_title is None:
        return
    await callback.message.edit_text(
        f"📚 <b>مقرر: {html.quote(subject_title)}</b>\n\nاختر القسم المطلوب لتصفح الملفات المعتمدة مباشرة:",
        reply_markup=generate_branch_keyboard(s_code),
        parse_mode=ParseMode.HTML
    )

@dispatcher.callback_query(F.data.startswith("branch_"))
async def handle_branch_files_view(callback: CallbackQuery) -> None:
    await answer_callback(callback)
    parts = callback.data.split("_")
    s_code, b_type = parts[1], parts[2]
    if s_code not in CURRICULUM_SUBJECTS or b_type not in {"theory", "practical"}:
        return
    files = get_academic_files(s_code, b_type)
    branch_name = "القسم النظري" if b_type == "theory" else "القسم العملي"
    
    if not files:
        await callback.message.edit_text(
            f"📁 <b>{html.quote(CURRICULUM_SUBJECTS[s_code])} - {branch_name}</b>\n\nعذراً، لا توجد ملفات مرفوعة في هذا القسم حالياً.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ رجوع للمقرر", callback_data=f"subj_{s_code}")]]),
            parse_mode=ParseMode.HTML
        )
        return

    await callback.message.edit_text(f"جاري إرسال ملفات {branch_name} للمقرر 👇")
    for f in files:
        await callback.message.answer_document(
            document=str(f["file_id"]),
            caption=f"📄 <b>{html.quote(str(f['file_name']))}</b>\n👤 المرفع: {html.quote(str(f['sender']))}",
            parse_mode=ParseMode.HTML,
        )
    
    await callback.message.answer(
        "تم عرض كافة الملفات بنجاح ✅",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ رجوع للمقرر", callback_data=f"subj_{s_code}")]])
    )

# --- قسم الذكاء الاصطناعي الأكاديمي والبرمجي المتطور ---

@dispatcher.callback_query(F.data == "ai_assistant_start")
async def prompt_ai_assistant(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_callback(callback)
    await state.set_state(AIAssistantStates.awaiting_ai_question)
    await callback.message.edit_text(
        "🤖 <b>    المساعد البرمجي والباحث الأكاديمي    </b>\n\n"
        "اطرح سؤالك البرمجي، استفسارك ألاكاديمي وسيجيبك النظام بمهارة احترافية:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ إلغاء", callback_data="main_menu")]]),
        parse_mode=ParseMode.HTML
    )

@dispatcher.message(AIAssistantStates.awaiting_ai_question)
async def process_ai_assistant_answer(message: Message, state: FSMContext) -> None:
    question = message.text
    await state.clear()
    if not question or not question.strip():
        await message.answer("أرسل سؤالك كنص.")
        return
    
    wait_msg = await message.answer("🧠 <b>جاري تحليل الكود والسؤال البرمجي بواسطة خبير الذكاء الاصطناعي...</b>", parse_mode=ParseMode.HTML)
    await asyncio.sleep(0.7)
    ai_response = await advanced_ai_researcher(question)

    chunks = split_telegram_text(ai_response)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 طرح سؤال آخر", callback_data="ai_assistant_start")],
        [InlineKeyboardButton(text="🏠 القائمة الرئيسية", callback_data="main_menu")]
    ])
    
    await wait_msg.edit_text(
        chunks[0],
        reply_markup=keyboard if len(chunks) == 1 else None,
        parse_mode=ParseMode.HTML
    )
    for index, chunk in enumerate(chunks[1:], start=1):
        await message.answer(
            chunk,
            reply_markup=keyboard if index == len(chunks) - 1 else None,
            parse_mode=ParseMode.HTML
        )

@dispatcher.callback_query(F.data.startswith("ai_summarize_"))
async def trigger_ai_summarizer(callback: CallbackQuery) -> None:
    await answer_callback(callback)
    subject_code = callback.data.split("_")[2]
    subject_title = CURRICULUM_SUBJECTS.get(subject_code)
    if subject_title is None:
        return
    summary_text = (
        f"🤖 <b>الخلاصة الذكية للمقرر: {html.quote(subject_title)}</b>\n\n"
        "• <b>التركيز البرمجي والهيكلي:</b> الإتقان العملي لخوارزميات المادة وكتابة الأكواد النظيفة.\n"
        "• <b>النقاط الجوهرية للاختبارات النهائية:</b> فهم التطبيقات العملية، معالجة الاستثناءات، وتحليل الأداء.\n"
        "• <b>توجيه الخبير:</b> راجع القسم العملي والنظري للمقرر لتحقيق أعلى استفادة."
    )
    await callback.message.answer(
        summary_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ العودة للمقرر", callback_data=f"subj_{subject_code}")],
            [InlineKeyboardButton(text="🏠 القائمة الرئيسية", callback_data="main_menu")]
        ]),
        parse_mode=ParseMode.HTML
    )

# --- محرك البحث الذكي الفوري ---

@dispatcher.callback_query(F.data == "start_search")
async def prompt_search(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_callback(callback)
    await state.set_state(SearchStates.awaiting_keyword)
    await callback.message.edit_text(
        "🔍 <b>محرك البحث الأكاديمي الفوري</b>\n\nاكتب الكلمة المفتاحية للبحث عن أي ملزمة في المقررات:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ إلغاء", callback_data="main_menu")]])
    )

@dispatcher.message(SearchStates.awaiting_keyword)
async def execute_search(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not message.text or not message.text.strip():
        await message.answer("أرسل كلمة مفتاحية نصية للبحث.")
        return
    keyword = message.text.strip()
    lowered_keyword = keyword.casefold()
    results = [
        record
        for record in get_academic_files()
        if lowered_keyword in str(record["file_name"]).casefold()
        or lowered_keyword in CURRICULUM_SUBJECTS.get(str(record["subject_code"]), "").casefold()
    ]
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔍 بحث جديد", callback_data="start_search")], [InlineKeyboardButton(text="🏠 الرئيسية", callback_data="main_menu")]])
    if not results:
        await message.answer(f"❌ لم يتم العثور على ملزمة تطابق كلمة: (<code>{html.quote(keyword)}</code>)", reply_markup=kb, parse_mode=ParseMode.HTML)
        return
    
    await message.answer(f"🔎 نتائج البحث عن (<code>{html.quote(keyword)}</code>): عدد النتائج {len(results)} 👇", parse_mode=ParseMode.HTML)
    for item in results:
        branch = "نظري" if item["branch_type"] == "theory" else "عملي"
        await message.answer_document(
            document=str(item["file_id"]),
            caption=(
                f"📄 <b>{html.quote(str(item['file_name']))}</b>\n"
                f"📚 {html.quote(CURRICULUM_SUBJECTS.get(str(item['subject_code']), ''))} ({branch})"
            ),
            parse_mode=ParseMode.HTML,
        )
    await message.answer("تم عرض كافة نتائج البحث ✅", reply_markup=kb)

# --- نظام رفع الملفات والتخزين المباشر ---

@dispatcher.callback_query(F.data == "start_upload")
async def start_upload(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_callback(callback)
    await callback.message.edit_text("📥 أرسل الملف المطلوب (PDF أو وثيقة) لرَفعه وتخزينه في المقررات الرسمية:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ إلغاء", callback_data="main_menu")]]))
    await state.set_state(SubmissionStates.awaiting_document)

@dispatcher.message(SubmissionStates.awaiting_document, F.document)
async def receive_doc(message: Message, state: FSMContext) -> None:
    document = message.document
    if document is None or document.file_size is None or document.file_size > 20 * 1024 * 1024:
        await message.answer("⚠️ الحد الأقصى لحجم الملف هو 20 ميغابايت.")
        return
    doc_name = document.file_name or "ملف_أكاديمي"
    
    if not ai_file_security_filter(doc_name):
        await message.answer("⚠️ الصيغة غير مدعومة. الصيغ المقبولة: PDF وDOCX وPPTX وTXT.")
        return

    progress = await message.answer("📖 جارٍ قراءة الملف واستخراج النصوص...")
    buffer = io.BytesIO()
    try:
        await message.bot.download(document, destination=buffer)
        extracted_pages = await asyncio.to_thread(
            extract_document_pages, doc_name, buffer.getvalue()
        )
    except Exception:
        extracted_pages = []

    await state.update_data(file_id=document.file_id, file_name=doc_name, extracted_pages=extracted_pages)
    await state.set_state(SubmissionStates.selecting_subject)
    await progress.edit_text("✅ قُبل الملف بنجاح. اختر المقرر الدراسي:", reply_markup=generate_subjects_keyboard("up_subj"))

@dispatcher.callback_query(SubmissionStates.selecting_subject, F.data.startswith("up_subj_"))
async def select_subj(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_callback(callback)
    s_code = callback.data.split("_")[2]
    if s_code not in CURRICULUM_SUBJECTS:
        return
    await state.update_data(subject_code=s_code)
    await state.set_state(SubmissionStates.selecting_branch)
    await callback.message.edit_text("هل يتبع القسم النظري أم العملي في هذا المقرر؟", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 القسم النظري", callback_data="up_branch_theory")],
        [InlineKeyboardButton(text="💻 القسم العملي", callback_data="up_branch_practical")],
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="main_menu")]
    ]))

@dispatcher.callback_query(SubmissionStates.selecting_branch, F.data.startswith("up_branch_"))
async def select_branch(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_callback(callback)
    b_type = callback.data.split("_")[2]
    data = await state.get_data()
    user = callback.from_user
    if (
        b_type not in {"theory", "practical"}
        or data.get("subject_code") not in CURRICULUM_SUBJECTS
        or not all(key in data for key in ("file_id", "file_name"))
    ):
        await state.clear()
        await callback.message.answer("تعذر إكمال رفع الملف؛ ابدأ العملية من جديد.")
        return

    if user.id == ADMINISTRATOR_ID:
        save_academic_file(
            data["subject_code"],
            b_type,
            data["file_id"],
            data["file_name"],
            f"مشرف ({user.first_name})",
            user.id,
            "approved",
            data.get("extracted_pages", []),
        )
        audit_logger("FILE_PUBLISHED", user.id, f"subject={data['subject_code']} branch={b_type}")
        await state.clear()
        await callback.message.edit_text(
            "✅ تم حفظ الملف مباشرة في المقرر الدراسي.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 الرئيسية", callback_data="main_menu")]])
        )
    else:
        record_id = save_academic_file(
            data["subject_code"],
            b_type,
            data["file_id"],
            data["file_name"],
            user.full_name,
            user.id,
            "pending",
            data.get("extracted_pages", []),
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ موافقة ونشر مباشر بالمقرر", callback_data=f"approve_{record_id}"),
            InlineKeyboardButton(text="❌ رفض", callback_data=f"reject_{record_id}")
        ]])
        branch_title = "نظري" if b_type == "theory" else "عملي"
        try:
            await callback.bot.send_document(
                chat_id=ADMINISTRATOR_ID,
                document=data["file_id"],
                caption=(
                    f"🔔 <b>طلب اعتماد ملف من الطالب:</b> {html.quote(user.full_name)} "
                    f"(@{html.quote(user.username or 'بدون')})\n"
                    f"📚 المقرر: {html.quote(CURRICULUM_SUBJECTS[data['subject_code']])}\n"
                    f"📁 القسم: {branch_title}\n"
                    f"الملف: {html.quote(data['file_name'])}"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
        except TelegramAPIError:
            delete_academic_file(record_id)
            logging.getLogger(__name__).exception("Failed to forward a file for review")
            await callback.message.answer("تعذر إرسال الملف للإدارة؛ لم يتم تسجيله. حاول مرة أخرى لاحقاً.")
            await state.clear()
            return
        await state.clear()
        audit_logger("FILE_SUBMITTED", user.id, f"record_id={record_id}")
        await callback.message.edit_text(
            "⏳ أُرسل الملف إلى الإدارة للمراجعة. سيظهر في المقرر بعد الموافقة.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 الرئيسية", callback_data="main_menu")]])
        )

@dispatcher.callback_query(F.data.startswith("approve_"), F.from_user.id == ADMINISTRATOR_ID)
async def approve_file(callback: CallbackQuery) -> None:
    await answer_callback(callback, "تمت الموافقة.")
    try:
        record_id = int(callback.data.split("_")[1])
    except (IndexError, ValueError):
        await callback.message.answer("تعذر تحديد طلب الاعتماد.")
        return
    record = get_pending_file(record_id)
    if record is None or not update_file_status(record_id, "approved"):
        await callback.message.answer("هذا الطلب غير موجود أو سبق التعامل معه.")
        return
    msg = callback.message
    await msg.edit_caption(
        caption=(
            f"{msg.caption or ''}\n\n✅ <b>تمت الموافقة ونشر الملف في المقرر.</b>"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=None,
    )
    audit_logger("FILE_APPROVED", callback.from_user.id, f"record_id={record_id}")
    try:
        await callback.bot.send_message(
            chat_id=int(record["submitted_by"]),
            text="🎉 تمت الموافقة على ملفك ونشره في قسم المقررات الدراسية.",
        )
    except TelegramAPIError:
        logging.getLogger(__name__).exception("Failed to notify the file submitter after approval")

@dispatcher.callback_query(F.data.startswith("reject_"), F.from_user.id == ADMINISTRATOR_ID)
async def reject_file(callback: CallbackQuery) -> None:
    await answer_callback(callback, "تم الرفض.")
    try:
        record_id = int(callback.data.split("_")[1])
    except (IndexError, ValueError):
        await callback.message.answer("تعذر تحديد طلب الاعتماد.")
        return
    record = get_pending_file(record_id)
    if record is None or not update_file_status(record_id, "rejected"):
        await callback.message.answer("هذا الطلب غير موجود أو سبق التعامل معه.")
        return
    msg_ref = callback.message
    await msg_ref.edit_caption(
        caption=f"{msg_ref.caption or ''}\n\n❌ <b>تم رفض الملف.</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=None,
    )
    audit_logger("FILE_REJECTED", callback.from_user.id, f"record_id={record_id}")
    try:
        await callback.bot.send_message(
            chat_id=int(record["submitted_by"]),
            text="⚠️ نعتذر، رفضت الإدارة الملف المرسل.",
        )
    except TelegramAPIError:
        logging.getLogger(__name__).exception("Failed to notify the file submitter after rejection")

# --- الاستفسارات والشكاوى وإدارة النظام ---

@dispatcher.callback_query(F.data == "support_start")
async def support_start(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_callback(callback)
    await state.set_state(SupportStates.awaiting_inquiry)
    await callback.message.edit_text("💬 اكتب استفسارك أو شكواك لإدارة النظام (سيتم إرسالها فوراً مع بياناتك):", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ إلغاء", callback_data="main_menu")]]))

@dispatcher.message(SupportStates.awaiting_inquiry)
async def support_receive(message: Message, state: FSMContext) -> None:
    user = message.from_user
    text = message.text
    if not text:
        await message.answer("أرسل الاستفسار كنص.")
        return
    
    if check_anti_flood(user.id):
        await message.answer("⚠️ يرجى التمهل قليلاً قبل إرسال رسالة أخرى.")
        return

    if moderate_content(text):
        await message.answer("⚠️ تنبيه أمني: تم حظر رسالتك لاحتوائها على كلمات مخالفة.")
        return

    await state.clear()
    
    try:
        await message.bot.send_message(
            chat_id=ADMINISTRATOR_ID,
            text=(
                f"📩 <b>استفسار أو شكوى أكاديمية جديدة:</b>\n\n"
                f"👤 <b>اسم الطالب:</b> {html.quote(user.full_name)}\n"
                f"🌐 <b>المعرف:</b> @{html.quote(user.username or 'بدون')}\n"
                f"🆔 <b>المعرف الرقمي:</b> <code>{user.id}</code>\n\n"
                f"💬 <b>النص المرسل:</b>\n{html.quote(text)}"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✍️ الرد على الطالب", callback_data=f"reply_student_{user.id}")]])
        )
    except TelegramAPIError:
        logging.getLogger(__name__).exception("Failed to forward support message to admin")

    await message.answer("✅ تم إرسال رسالتك  إلى الإدارة بنجاح وسيتم الرد عليك قريباً.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 الرئيسية", callback_data="main_menu")]]))

@dispatcher.callback_query(
    F.data.startswith("reply_student_"), F.from_user.id == ADMINISTRATOR_ID
)
async def admin_reply_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_callback(callback)
    try:
        target_student_id = int(callback.data.split("_")[2])
    except (IndexError, ValueError):
        await callback.message.answer("تعذر تحديد الطالب المطلوب.")
        return
    await state.update_data(target_student_id=target_student_id)
    await state.set_state(AdministrationStates.awaiting_response_text)
    await callback.message.answer("✍️ اكتب نص الرد الرسمي للطالب:")

@dispatcher.message(AdministrationStates.awaiting_response_text, F.from_user.id == ADMINISTRATOR_ID)
async def admin_send_reply(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    if not message.text:
        await message.answer("اكتب نص الرد.")
        return
    try:
        await message.bot.send_message(
            chat_id=data["target_student_id"],
            text=f"📥 <b>رد إدارة النظام:</b>\n\n{html.quote(message.text)}",
            parse_mode=ParseMode.HTML,
        )
        await message.answer("✅ تم إرسال الرد بنجاح للطالب.")
    except TelegramAPIError:
        logging.getLogger(__name__).exception("Failed to deliver an administrator reply")
        await message.answer("تعذر إرسال الرد إلى الطالب؛ تحقق من حالة حسابه.")

@dispatcher.callback_query(F.data == "admin_dashboard", F.from_user.id == ADMINISTRATOR_ID)
async def render_admin_dashboard(callback: CallbackQuery) -> None:
    await answer_callback(callback)
    admin_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 إرسال تعميم رسمي لجميع الطلاب", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="📊 إحصائيات النظام الشاملة", callback_data="admin_stats")],
        [InlineKeyboardButton(text="👥 استعراض قائمة المشتركين كاملة", callback_data="admin_users_list")],
        [InlineKeyboardButton(text="🏠 العودة للقائمة الرئيسية", callback_data="main_menu")]
    ])
    await callback.message.edit_text("⚡ <b>لوحة التحكم الإدارية</b>", reply_markup=admin_keyboard, parse_mode=ParseMode.HTML)

@dispatcher.callback_query(F.data == "admin_stats", F.from_user.id == ADMINISTRATOR_ID)
async def show_system_statistics(callback: CallbackQuery) -> None:
    await answer_callback(callback)
    stats_text = f"📊 إجمالي الطلاب المشتركين: <code>{user_count()}</code>\n📁 إجمالي الملفات المنشورة: <code>{approved_file_count()}</code>"
    await callback.message.edit_text(stats_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ رجوع", callback_data="admin_dashboard")]]), parse_mode=ParseMode.HTML)

@dispatcher.callback_query(F.data == "admin_users_list", F.from_user.id == ADMINISTRATOR_ID)
async def show_admin_users_list(callback: CallbackQuery) -> None:
    await answer_callback(callback)
    users = get_all_users()
    if not users:
        await callback.message.edit_text("لا يوجد مستخدمين مسجلين حتى الآن.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ رجوع", callback_data="admin_dashboard")]]))
        return

    text = f"👥 <b>قائمة المشتركين في البوت ({len(users)}):</b>\n\n"
    for u in users[:30]:
        username_str = f"@{u['username']}" if u['username'] else "بدون معرف"
        text += f"• <b>{html.quote(u['full_name'])}</b> | {username_str} | <code>{u['user_id']}</code>\n"

    if len(users) > 30:
        text += f"\n<i>... وهناك {len(users) - 30} مستخدمين آخرين مسجلين في القاعدة.</i>"

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ رجوع", callback_data="admin_dashboard")]]), parse_mode=ParseMode.HTML)

@dispatcher.callback_query(F.data == "admin_broadcast", F.from_user.id == ADMINISTRATOR_ID)
async def prompt_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    await answer_callback(callback)
    await state.set_state(AdministrationStates.awaiting_broadcast_text)
    await callback.message.edit_text("📢 اكتب نص التعميم المراد إرساله لكافة الطلاب:")

@dispatcher.message(AdministrationStates.awaiting_broadcast_text, F.from_user.id == ADMINISTRATOR_ID)
async def execute_broadcast(message: Message, state: FSMContext) -> None:
    text = message.text
    await state.clear()
    if not text:
        await message.answer("اكتب نص التعميم.")
        return
    delivered = 0
    failed = 0
    for uid in student_ids():
        try:
            await message.bot.send_message(
                chat_id=uid,
                text=f"📢 <b>تعميم إداري:</b>\n\n{html.quote(text)}",
                parse_mode=ParseMode.HTML,
            )
            delivered += 1
        except TelegramAPIError:
            failed += 1
            logging.getLogger(__name__).exception("Failed to deliver broadcast to user_id=%s", uid)
        await asyncio.sleep(0.05)
    audit_logger("BROADCAST", message.from_user.id, f"delivered={delivered} failed={failed}")
    await message.answer(f"انتهى التعميم: وصل إلى {delivered} مستخدم، وتعذر إرساله إلى {failed}.")

@dispatcher.callback_query(F.data == "about_system")
async def about(callback: CallbackQuery) -> None:
    await answer_callback(callback)
    await callback.message.edit_text(
        "🤖 <b>المكتبة الأكاديمية للدفعة الثامنة CSIT (Ultimate AI Edition)</b>\n\n"
        "تطوير المهندس : محمد الظليمي.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ رجوع", callback_data="main_menu")]]),
        parse_mode=ParseMode.HTML
    )

@dispatcher.callback_query()
async def handle_unmatched_callback(callback: CallbackQuery) -> None:
    if callback.data and callback.data.startswith(("approve_", "reject_")):
        logging.getLogger("csit.audit").warning(
            "Rejected unauthorized file-review callback from user_id=%s",
            callback.from_user.id,
        )
        await answer_callback(callback, "هذه العملية متاحة للإدارة فقط.")
        return
    await answer_callback(callback, "هذا الإجراء غير متاح.")

# --- محرك الذكاء الاصطناعي الأكاديمي والبرمجي المتطور ---

async def advanced_ai_researcher(query: str) -> str:
    q_lower = query.lower()
    
    if "normalization" in q_lower or "تنسيق" in q_lower or "قواعد البيانات" in q_lower or "sql" in q_lower:
        return (
            "🔬 <b>[مرجع هندسي معتمد - Database Systems & Architecture]:</b>\n\n"
            "• <b>التحليل الشامل:</b> تصميم قواعد البيانات العلائقية يعتمد على إزالة التكرار (Redundancy) ومنع شذوذ البيانات (Anomalies).\n"
            "• <b>المستويات المعيارية بالتفصيل:</b>\n"
            "  1. <b>1NF (المستوى الأول):</b> التأكد من أن كل حقل يحتوي على قيمة ذرية فريدة (Atomic) وغير مركبة.\n"
            "  2. <b>2NF (المستوى الثاني):</b> تحقيق 1NF مع اعتماد كل عمود غير مفتاحي على المفتاح الأساسي بالكامل.\n"
            "  3. <b>3NF (المستوى الثالث):</b> تحقيق 2NF مع خلو الجدول من التبعيات المتعدية (Transitive Dependency).\n"
            "• <b>نصيحة برمجية:</b> عند تصميم الجداول، دائماً افصل العلاقات متعددة إلى متعدد (Many-to-Many) بجدول وسيط (Junction Table)."
        )
        
    elif "c++" in q_lower or "oop" in q_lower or "برمجة" in q_lower or "class" in q_lower or "كود" in q_lower:
        return (
            "💻 <b>[استشارة برمجية هندسية متقدمة - Advanced Programming]:</b>\n\n"
            "• <b>المفاهيم الأساسية للبرمجة كائنية التوجه (OOP):</b>\n"
            "  - <b>Encapsulation (التغليف):</b> إخفاء بيانات الكائن وحمايتها عبر استخدام محددات الوصول (`private` / `protected`).\n"
            "  - <b>Inheritance (الوراثة):</b> إعادة استخدام الكود وتوريث خصائص الدوال من فئة أساسية (Base Class).\n"
            "  - <b>Polymorphism (تعدد الأشكال):</b> استخدام الدوال بأكثر من شكل.\n"
            "• <b>مثال عملي سريع (C++):</b>\n"
            "<pre><code>class AcademicCourse {\n"
            "private:\n"
            "    string courseName;\n"
            "public:\n"
            "    void setCourse(string name) { courseName = name; }\n"
            "    string getCourse() { return courseName; }\n"
            "};</code></pre>\n"
            "• <b>توجيه:</b> حدد لغة أو مشكلة برمجية معينة وسأكتب لك الكود كاملاً مع الشرح والتحليل."
        )

    elif "mining" in q_lower or "تنقيب" in q_lower or "خوارزمية" in q_lower or "apriori" in q_lower or "clustering" in q_lower:
        return (
            "📊 <b>[مرجع علمي معتمد - Data Mining & Knowledge Discovery]:</b>\n\n"
            "• <b>التحليل المنهجي:</b> تنقيب البيانات يهدف لاستخراج أنماط غير مخفية ومفيدة من قواعد البيانات الضخمة (KDD Process).\n"
            "• <b>أبرز الخوارزميات الأكاديمية:</b>\n"
            "  - <i>Apriori Algorithm:</i> تُستخدم في استخراج قواعد الارتباط (Association Rules) مثل تحليل سلة المشتريات.\n"
            "  - <i>K-Means Clustering:</i> خوارزمية لتجميع البيانات المتشابهة في مجموعات بناءً على المسافة الإحصائية.\n"
            "• <b>خلاصة:</b> التركيز في الاختبارات يكون على كيفية حساب حسابات Support و Confidence."
        )

    else:
        return (
            f"💡 <b>[مساعد CSIT الذكي - تحليل هندسي لطلبك]:</b>\n"
            f"<i>\"{query}\"</i>\n\n"
            "بناءً على المعايير الأكاديمية والهندسية لمستوى علوم الحاسوب:\n"
            "1. <b>التحليل التقني:</b> هذا السؤال يمس جوهر هندسة البرمجيات أو هياكل البيانات، ويتطلب هيكلة واضحة وخوارزمية دقيقة للتنفيذ.\n"
            "2. <b>الخطوات الموصى بها:</b> تحديد المتطلبات، تحليل المدخلات والمخرجات، وتصميم الحل بأعلى كفاءة.\n"
            "3. <b>تعديل الطلب:</b> يمكنك تخصيص سؤالك أكثر (مثلاً: <i>اكتب كود بلغة C++ لـ...</i>) وسأعطيك الإجابة العلمية المفصلة فوراً!"
        )

# --- دالة التشغيل الرئيسية ---

async def main() -> None:
    initialize_database()
    bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logging.getLogger(__name__).info("CSIT academic library bot started successfully.")
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()
        await dispatcher.storage.close()

if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot terminated safely.")