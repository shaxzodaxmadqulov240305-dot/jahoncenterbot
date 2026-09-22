"""
main.py
=======
OMR (optik test tekshiruvchi) mantiqida ishlaydigan Telegram bot.

O'QUVCHI UCHUN IKKI USUL:
  1) Ikki bosqichli: avval test kodini yuboradi (mas. 105),
     bot test ma'lumotlarini ko'rsatadi, keyin javoblarni yuboradi (abcdabcd).
  2) Bir bosqichli (tezkor): 105*abcdabcd

ADMIN UCHUN:
  /addtest  — test qo'shish
  /post     — kanalga tashlash uchun tayyor e'lon matni
  /results  — reyting jadvali
  /finish   — testni yakunlash va umumiy hisobot
  /reopen   — yakunlangan testni qayta ochish
  /tests    — barcha testlar ro'yxati
  /reset    — o'quvchiga qayta topshirishga ruxsat
  /deltest  — testni o'chirish

Ishga tushirish:  py main.py
"""

import asyncio
import html
import logging
import os
import re
import sys
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)

import database as db

# ---------------------------------------------------------------------------
# SOZLAMALAR
# ---------------------------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "8677879125:AAGCE_kWEHuZ9y3_BtWhzvcIvH67hNupFgY")

# Admin(lar)ning Telegram ID raqamlari. Vergul bilan bir nechta yozish mumkin.
ADMINS: set[int] = {7253695670}

# Javoblarda ruxsat etilgan harflar
ALLOWED_LETTERS = "abcde"

# Tugmali javob berishda bitta sahifada nechta savol ko'rsatiladi.
# Telegram'da bitta xabarda tugmalar soni cheklangan (juda ko'p bo'lsa
# xabar buzilib qoladi/sekinlashadi), shu sabab sahifalab chiqaramiz.
PAGE_SIZE = 10

# Bir bosqichli format: 105*abcdabcd
ONESHOT_PATTERN = re.compile(r"^\s*(\d+)\s*\*\s*(\S+)\s*$")

# Faqat raqamdan iborat xabar (test kodi)
CODE_PATTERN = re.compile(r"^\s*(\d{1,9})\s*$")

# Ism-familiyada ruxsat etilgan belgilar: harflar, apostrof, chiziqcha, bo'sh joy.
# Lotin va kirill alifbolari, o'zbekcha ʻ ' ' belgilari qo'llab-quvvatlanadi.
NAME_PATTERN = re.compile(r"^[A-Za-zА-Яа-яЁёЎўҚқҒғҲҳ'ʻ`’\-\s]+$")

router = Router()


class Registration(StatesGroup):
    """O'quvchidan ism-familiya so'ralayotgan holat."""
    waiting_for_name = State()


class AnswerState(StatesGroup):
    """O'quvchi test kodini yuborgach, javoblarini kutish holati."""
    waiting_for_answers = State()


# ---------------------------------------------------------------------------
# FILTRLAR
# ---------------------------------------------------------------------------

class IsAdmin(BaseFilter):
    """Xabar admin tomonidan yuborilganini tekshiruvchi filtr."""

    async def __call__(self, message: Message) -> bool:
        return message.from_user is not None and message.from_user.id in ADMINS


# ---------------------------------------------------------------------------
# YORDAMCHI FUNKSIYALAR
# ---------------------------------------------------------------------------

def safe(text: str) -> str:
    """Matndagi < > & belgilarini HTML uchun xavfsizlaydi."""
    return html.escape(str(text) if text else "Noma'lum")


def letters() -> str:
    """Ruxsat etilgan harflarni chiroyli ko'rinishda: a , b , c , d , e"""
    return " , ".join(ALLOWED_LETTERS)


def validate_full_name(raw_name: str) -> tuple[bool, str]:
    """
    Kiritilgan ism-familiyani tekshiradi va tozalaydi.

    Qaytaradi: (to'g'rimi, natija)
      - to'g'ri bo'lsa: (True, tozalangan ism)
      - xato bo'lsa:   (False, xato sababi)

    Talablar: kamida ikkita so'z, faqat harflar, 5-60 belgi.
    """
    # Ortiqcha bo'sh joylarni olib tashlaymiz
    name = " ".join(raw_name.split())

    if len(name) < 5:
        return False, "Ism-familiya juda qisqa (kamida 5 ta harf)."

    if len(name) > 60:
        return False, "Ism-familiya juda uzun (ko'pi bilan 60 ta belgi)."

    if not NAME_PATTERN.match(name):
        return False, (
            "Ism-familiyada raqam yoki maxsus belgi bo'lmasligi kerak.\n"
            "Faqat harflardan foydalaning."
        )

    words = name.split()
    if len(words) < 2:
        return False, "Ism va familiyani <b>ikkalasini</b> ham yozing."

    if any(len(word) < 2 for word in words):
        return False, "Har bir so'z kamida 2 ta harfdan iborat bo'lsin."

    # Har bir so'zning bosh harfini katta qilamiz: "ali valiyev" -> "Ali Valiyev"
    return True, " ".join(word.capitalize() for word in words)


async def require_registration(message: Message, state: FSMContext) -> Optional[str]:
    """
    O'quvchi ro'yxatdan o'tganini tekshiradi.

    O'tgan bo'lsa — ism-familiyasini qaytaradi.
    O'tmagan bo'lsa — ism so'rab, None qaytaradi (amal to'xtatiladi).
    """
    full_name = await db.get_user_name(message.from_user.id)
    if full_name:
        return full_name

    await state.set_state(Registration.waiting_for_name)
    await message.answer(
        "👋 <b>Avval ro'yxatdan o'ting.</b>\n\n"
        "Testda qatnashish uchun to'liq <b>ism va familiyangizni</b> yozing.\n\n"
        "Namuna: <code>Ahmadqulov Shahzod</code>\n\n"
        "❗️ Reyting jadvalida aynan shu ism ko'rinadi, "
        "shuning uchun to'g'ri yozing."
    )
    return None


def medal(position: int) -> str:
    """Birinchi uchtaga medal, qolganlariga tartib raqami."""
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(position, f"{position}.")


def calculate_score(user_answers: str, correct_answers: str) -> tuple[int, list[int]]:
    """
    Javoblarni harfma-harf solishtiradi.
    Qaytaradi: (to'g'ri javoblar soni, xato belgilangan savol raqamlari)
    """
    wrong_numbers = [
        number
        for number, (user_char, correct_char) in enumerate(
            zip(user_answers, correct_answers), start=1
        )
        if user_char != correct_char
    ]
    return len(correct_answers) - len(wrong_numbers), wrong_numbers


def format_wrong_list(wrong_numbers: list[int], per_line: int = 10) -> str:
    """
    Xato savol raqamlarini vergul bilan, qatorlarga bo'lib chiqaradi.
    Masalan:  2, 3, 4, 5, 7, 8, 9, 10, 11, 13,
              14, 15, 16, 18, 19, 20
    """
    if not wrong_numbers:
        return "Yo'q — barcha javoblar to'g'ri! 🎉"

    lines = []
    for start in range(0, len(wrong_numbers), per_line):
        chunk = wrong_numbers[start:start + per_line]
        lines.append(", ".join(str(number) for number in chunk))
    return ",\n".join(lines)


def build_full_keyboard(total: int, answers: dict[int, str], page: int) -> InlineKeyboardMarkup:
    """
    Joriy sahifadagi savollarni (PAGE_SIZE tadan) qator-qator ko'rsatadi:
    1-qator: savol raqami (bosilmaydi) + A B C D E tugmalari.
    Belgilangan javob ✅ bilan ajratib ko'rsatiladi.
    Sahifalar ko'p bo'lsa — navigatsiya qatori, oxirida esa
    Yuborish va Bekor qilish tugmalari qo'shiladi.
    """
    max_page = (total - 1) // PAGE_SIZE
    page = max(0, min(page, max_page))

    start = page * PAGE_SIZE
    end = min(total, start + PAGE_SIZE)

    rows: list[list[InlineKeyboardButton]] = []

    for question_number in range(start + 1, end + 1):
        row = [
            InlineKeyboardButton(
                text=f"{question_number}.",
                callback_data="ans:noop",
            )
        ]
        chosen = answers.get(question_number)
        for letter in ALLOWED_LETTERS:
            label = f"✅{letter.upper()}" if chosen == letter else letter.upper()
            row.append(
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"ans:{question_number}:{letter}",
                )
            )
        rows.append(row)

    if max_page > 0:
        nav_row = []
        if page > 0:
            nav_row.append(
                InlineKeyboardButton(text="⬅️ Oldingi", callback_data="ans:nav:prev")
            )
        nav_row.append(
            InlineKeyboardButton(
                text=f"📄 {page + 1}/{max_page + 1}", callback_data="ans:noop"
            )
        )
        if page < max_page:
            nav_row.append(
                InlineKeyboardButton(text="Keyingi ➡️", callback_data="ans:nav:next")
            )
        rows.append(nav_row)

    answered = len(answers)
    submit_label = (
        f"✅ Yuborish ({answered}/{total})"
        if answered == total
        else f"📨 Yuborish ({answered}/{total})"
    )
    rows.append(
        [
            InlineKeyboardButton(text=submit_label, callback_data="ans:submit"),
            InlineKeyboardButton(text="❌ Bekor qilish", callback_data="ans:cancel"),
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_all_questions(
    message: Message, test: dict, answers: dict[int, str], page: int, edit: bool
) -> None:
    """
    Test topilgach yuboriladigan/yangilanadigan yagona xabar:
    joriy sahifadagi savollar uchun tugmalar.
    """
    total = len(test["correct_answers"])
    text = (
        "✅ <b>Test topildi!</b>\n\n"
        + test_info_block(test)
        + "\n➖➖➖➖➖➖➖➖➖➖➖\n"
        "👇 Har bir savol qatorida to'g'ri javobingizni bosing.\n"
        "Xato bossangiz, boshqa harfni bossangiz bo'ldi — o'zgaradi."
    )
    if total > PAGE_SIZE:
        text += (
            "\n\n📄 Savollar bir necha sahifada — "
            "pastdagi <b>Keyingi/Oldingi</b> tugmalari orqali o'ting."
        )
    text += (
        "\n\nHammasini belgilab bo'lgach, pastdagi "
        "<b>Yuborish</b> tugmasini bosing."
    )
    keyboard = build_full_keyboard(total, answers, page)

    if edit:
        try:
            await message.edit_text(text, reply_markup=keyboard)
            return
        except Exception:
            pass

    await message.answer(text, reply_markup=keyboard)


def test_info_block(test: dict) -> str:
    """Test haqidagi ma'lumot bloki — bir necha joyda ishlatiladi."""
    return (
        f"📋 Test nomi: <b>{safe(test['title'])}</b>\n"
        f"🔢 Savollar soni: <b>{len(test['correct_answers'])}</b> ta\n"
        f"‼️ Test kodi: <b>{test['test_id']}</b>\n"
        f"👤 Test yaratuvchisi: <i>{safe(test['author'])}</i>"
    )


# ---------------------------------------------------------------------------
# UMUMIY BUYRUQLAR
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    """/start — xush kelibsiz xabari va ro'yxatdan o'tish."""
    await state.clear()

    full_name = await db.get_user_name(message.from_user.id)

    # Hali ro'yxatdan o'tmagan bo'lsa — avval ism so'raymiz
    if not full_name:
        await state.set_state(Registration.waiting_for_name)
        await message.answer(
            "Assalomu alaykum! 👋\n\n"
            "Men test javoblaringizni avtomatik tekshiruvchi botman.\n\n"
            "📝 <b>Boshlash uchun to'liq ism va familiyangizni yozing.</b>\n\n"
            "Namuna: <code>Ahmadqulov Shahzod</code>\n\n"
            "❗️ Reyting jadvalida aynan shu ism ko'rinadi, "
            "shuning uchun to'g'ri yozing."
        )
        return

    text = (
        f"Assalomu alaykum, <b>{safe(full_name)}</b>! 👋\n\n"
        "📌 <b>Testda qatnashish uchun:</b>\n"
        "Test kodini (masalan <code>105</code>) menga yuboring.\n"
        "Men test ma'lumotlarini ko'rsataman, siz esa javoblaringizni "
        "🔘 tugmalar orqali belgilaysiz.\n\n"
        "❗️ Har bir testni faqat <b>bir marta</b> topshira olasiz.\n\n"
        "Ismni o'zgartirish: /ism"
    )

    if message.from_user.id in ADMINS:
        text += (
            "\n\n➖➖➖➖➖➖➖➖➖\n"
            "🔐 <b>Admin buyruqlari:</b>\n"
            "<code>/addtest</code> — test qo'shish\n"
            "<code>/post [kod]</code> — kanal uchun e'lon matni\n"
            "<code>/results [kod]</code> — reyting jadvali\n"
            "<code>/finish [kod]</code> — testni yakunlash\n"
            "<code>/reopen [kod]</code> — qayta ochish\n"
            "<code>/tests</code> — barcha testlar\n"
            "<code>/reset [kod] [user_id]</code> — qayta topshirishga ruxsat\n"
            "<code>/deltest [kod]</code> — testni o'chirish"
        )

    await message.answer(text)


@router.message(Command("ism"))
async def cmd_change_name(message: Message, state: FSMContext) -> None:
    """/ism — ism-familiyani o'zgartirish."""
    current = await db.get_user_name(message.from_user.id)

    await state.set_state(Registration.waiting_for_name)
    await message.answer(
        (f"Hozirgi ismingiz: <b>{safe(current)}</b>\n\n" if current else "")
        + "📝 Yangi ism va familiyangizni yozing.\n\n"
        "Namuna: <code>Ahmadqulov Shahzod</code>\n\n"
        "ℹ️ Avval topshirgan testlaringizdagi ism o'zgarmaydi."
    )


@router.message(Registration.waiting_for_name, F.text)
async def handle_name_input(message: Message, state: FSMContext) -> None:
    """O'quvchi kiritgan ism-familiyani tekshiradi va saqlaydi."""
    # Buyruq yuborilsa, ro'yxatdan o'tishni bekor qilmaymiz
    if message.text.startswith("/"):
        await message.answer(
            "❗️ Avval ism va familiyangizni yozing.\n\n"
            "Namuna: <code>Ahmadqulov Shahzod</code>"
        )
        return

    is_valid, result = validate_full_name(message.text)

    if not is_valid:
        await message.answer(
            f"❌ {result}\n\n"
            "Qaytadan urinib ko'ring.\n"
            "Namuna: <code>Ahmadqulov Shahzod</code>"
        )
        return

    await db.save_user(
        user_id=message.from_user.id,
        full_name=result,
        username=message.from_user.username or "",
    )
    await state.clear()

    await message.answer(
        f"✅ <b>Ro'yxatdan o'tdingiz!</b>\n\n"
        f"👤 Ism-familiya: <b>{safe(result)}</b>\n\n"
        "📌 Endi testda qatnashishingiz mumkin.\n"
        "Test kodini yuboring (masalan: <code>105</code>).\n\n"
        "📖 To'liq ko'rsatma: /qollanma"
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """/help — qisqacha yordam."""
    await message.answer(
        "ℹ️ <b>Yordam</b>\n\n"
        "<b>1️⃣ Ro'yxatdan o'tish</b>\n"
        "Birinchi marta kirganda ism-familiyangizni yozasiz. "
        "O'zgartirish uchun: /ism\n\n"
        "<b>2️⃣ Testda qatnashish</b>\n"
        "Test kodini yuboring → <code>105</code>\n"
        "Keyin javoblarni yuboring → <code>abcdabcdabcd</code>\n\n"
        "<b>Tezkor usul:</b> hammasini bitta xabarda → "
        "<code>105*abcdabcdabcd</code>\n\n"
        "<b>Qoidalar:</b>\n"
        f"• Faqat <b>{letters()}</b> harflaridan foydalaning.\n"
        "• Barcha savollarga javob belgilang.\n"
        "• Har bir testni faqat <b>bir marta</b> topshira olasiz."
    )


@router.message(Command("qollanma"))
async def cmd_guide(message: Message) -> None:
    """/qollanma — o'quvchilar uchun batafsil ko'rsatma."""
    bot_user = await message.bot.get_me()

    await message.answer(
        "📖 <b>TESTDA QATNASHISH QO'LLANMASI</b>\n"
        "➖➖➖➖➖➖➖➖➖➖➖\n\n"
        "<b>1️⃣ Ro'yxatdan o'ting</b>\n"
        "Birinchi marta kirganingizda ism-familiyangizni yozing.\n"
        "Namuna: <code>Ahmadqulov Shahzod</code>\n\n"
        "<b>2️⃣ Test kodini yuboring</b>\n"
        "E'londa berilgan raqamni yozing. Masalan:\n"
        "<code>1</code>\n\n"
        "Men test nomi va savollar sonini ko'rsataman.\n\n"
        "<b>3️⃣ Javoblarni tugmalar orqali belgilang</b>\n"
        f"Har bir savol qatorida <b>A B C D E</b> tugmalari bo'ladi "
        f"(bir sahifada {PAGE_SIZE} tadan savol, ko'p bo'lsa "
        "<b>Keyingi/Oldingi</b> tugmalari orqali o'tasiz).\n\n"
        "Xohlagan tartibda bosaverasiz — 5-savolni oldin, "
        "1-savolni keyin belgilasangiz ham bo'ladi.\n"
        "Xato bossangiz, o'sha savol qatorida boshqa harfni bossangiz — "
        "javob almashadi.\n\n"
        "🔘 <b>Yuborish</b> — barcha savollarga javob belgilagach bosiladi\n"
        "🔘 <b>❌ Bekor qilish</b> — testdan chiqish uchun\n\n"
        "Yuborish tugmasi bosilgach, natija avtomatik chiqadi.\n\n"
        "➖➖➖➖➖➖➖➖➖➖➖\n"
        "⚡️ <b>TEZKOR USUL (ixtiyoriy)</b>\n\n"
        "Xohlasangiz, javoblarni matn qilib ham yuborishingiz mumkin.\n"
        "Hammasini qo'shib yozing: <code>abc...</code>\n"
        "Yoki bitta xabarda: test kodi, <b>*</b> belgisi, javoblar:\n\n"
        "<code>1*aaaaa</code>\n"
        "<code>105*abcdabcdabcd</code>\n\n"
        "➖➖➖➖➖➖➖➖➖➖➖\n"
        "❗️ <b>MUHIM QOIDALAR</b>\n\n"
        "• Tugma orqali belgilaganda xato bo'lmaydi — "
        "faqat mavjud variantlardan birini tanlaysiz.\n"
        "• Matn bilan yuborsangiz, faqat "
        f"<b>{letters()}</b> harflari qabul qilinadi.\n"
        "• Har bir testni <b>faqat bir marta</b> topshirasiz. "
        "Yuborishdan oldin tekshiring!\n\n"
        "➖➖➖➖➖➖➖➖➖➖➖\n"
        "✅ <b>XATO QILSANGIZ QO'RQMANG</b>\n\n"
        "Agar noto'g'ri harf yozsangiz yoki javoblar soni kam bo'lsa, "
        "men xatoni aytaman va <b>urinishingiz hisoblanmaydi</b>. "
        "Tuzatib qayta yuboraverasiz.\n\n"
        "Urinish faqat javoblar to'g'ri formatda va to'liq "
        "bo'lganda hisoblanadi.\n\n"
        "➖➖➖➖➖➖➖➖➖➖➖\n"
        f"🤖 Bot: @{bot_user.username}\n"
        "Ismni o'zgartirish: /ism"
    )


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    """/id — foydalanuvchiga o'z Telegram ID sini ko'rsatadi."""
    await message.answer(
        f"🆔 Sizning ID raqamingiz: <code>{message.from_user.id}</code>"
    )


@router.message(Command("bekor", "cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """/bekor — boshlangan testdan chiqish."""
    if await state.get_state() is None:
        await message.answer("Hozir bekor qiladigan amal yo'q.")
        return

    await state.clear()
    await message.answer(
        "🚫 Bekor qilindi. Testda qatnashish uchun kodni qaytadan yuboring."
    )


# ---------------------------------------------------------------------------
# ADMIN: TEST QO'SHISH
# ---------------------------------------------------------------------------

ADDTEST_HELP = (
    "📝 <b>Test qo'shish</b>\n\n"
    "Format (bo'laklar <code>|</code> belgisi bilan ajratiladi):\n"
    "<code>/addtest kod | test nomi | muallif | javoblar</code>\n\n"
    "<b>Namuna:</b>\n"
    "<code>/addtest 105 | MATEMATIKA | Ism Familiya | abcdabcdabcd</code>\n\n"
    "Muallifni yozmasangiz, o'z ismingiz qo'yiladi:\n"
    "<code>/addtest 105 | MATEMATIKA | abcdabcdabcd</code>"
)


@router.message(Command("addtest"), IsAdmin())
async def cmd_add_test(message: Message, command: CommandObject) -> None:
    """
    /addtest kod | nomi | muallif | javoblar
    Muallif ixtiyoriy — yozilmasa, adminning ismi olinadi.
    """
    if not command.args:
        await message.answer(ADDTEST_HELP)
        return

    parts = [part.strip() for part in command.args.split("|")]

    # 3 yoki 4 ta bo'lak bo'lishi mumkin
    if len(parts) == 4:
        test_id_raw, title, author, answers_raw = parts
    elif len(parts) == 3:
        test_id_raw, title, answers_raw = parts
        author = message.from_user.first_name or "Admin"
    else:
        await message.answer(
            "❌ Bo'laklar soni noto'g'ri.\n\n" + ADDTEST_HELP
        )
        return

    # Test kodi tekshiruvi
    if not test_id_raw.isdigit():
        await message.answer("❌ Test kodi faqat raqamlardan iborat bo'lishi kerak.")
        return

    test_id = int(test_id_raw)

    # Test nomi bo'sh bo'lmasin
    if not title:
        await message.answer("❌ Test nomi bo'sh bo'lmasligi kerak.")
        return

    answers = answers_raw.lower().replace(" ", "")

    if not answers:
        await message.answer("❌ Javoblar kiritilmadi.")
        return

    # Kalitda faqat ruxsat etilgan harflar bo'lsin
    invalid = sorted({char for char in answers if char not in ALLOWED_LETTERS})
    if invalid:
        await message.answer(
            "❌ Javoblar kalitida notanish belgi bor: "
            f"<code>{safe(' '.join(invalid))}</code>\n\n"
            f"Faqat <b>{letters()}</b> harflaridan foydalaning."
        )
        return

    already_exists = await db.get_test(test_id) is not None
    await db.add_test(test_id, title[:64], author[:64], answers)

    status = "♻️ <b>Test yangilandi!</b>" if already_exists else "✅ <b>Test qo'shildi!</b>"
    await message.answer(
        f"{status}\n\n"
        f"📋 Test nomi: <b>{safe(title)}</b>\n"
        f"‼️ Test kodi: <b>{test_id}</b>\n"
        f"🔢 Savollar soni: <b>{len(answers)}</b> ta\n"
        f"👤 Muallif: <i>{safe(author)}</i>\n"
        f"🔑 Kalit: <code>{answers}</code>\n\n"
        f"Kanal uchun e'lon matnini olish: <code>/post {test_id}</code>"
    )


# ---------------------------------------------------------------------------
# ADMIN: KANAL UCHUN E'LON MATNI
# ---------------------------------------------------------------------------

@router.message(Command("post"), IsAdmin())
async def cmd_post(message: Message, command: CommandObject) -> None:
    """
    /post [kod] — kanalga nusxalab tashlash uchun tayyor e'lon matni.
    """
    if not command.args or not command.args.strip().isdigit():
        await message.answer("❌ Format: <code>/post 105</code>")
        return

    test_id = int(command.args.strip())
    test = await db.get_test(test_id)

    if test is None:
        await message.answer(f"❌ <code>{test_id}</code> kodli test topilmadi.")
        return

    # Bot username'ini o'zidan so'raymiz — qo'lda yozish shart emas
    bot_user = await message.bot.get_me()
    bot_link = f"@{bot_user.username}"

    # Test uzunligiga mos namuna javob tayyorlaymiz (mas. 5 savol -> abcde)
    question_count = len(test["correct_answers"])
    example = (ALLOWED_LETTERS * (question_count // len(ALLOWED_LETTERS) + 1))[
        :question_count
    ]

    announcement = (
        "✅ <b>Test ishlanishga tayyor</b>\n\n"
        f"📋 Test nomi: <b>{safe(test['title'])}</b>\n"
        f"🔢 Testlar soni: <b>{len(test['correct_answers'])}</b> ta\n"
        f"‼️ Test kodi: <b>{test_id}</b>\n"
        f"👤 Test yaratuvchisi: <i>{safe(test['author'])}</i>\n\n"
        "Test javoblaringizni quyidagi botga jo'nating:\n\n"
        f"👉 {bot_link}\n"
        f"👉 {bot_link}\n"
        f"👉 {bot_link}\n\n"
        f"📌 Testda qatnashish uchun {bot_link} ga kirib "
        f"<b>{test_id}</b> sonini botga yuboring.\n\n"
        "➖➖➖➖➖➖➖➖➖➖➖\n"
        "✍️ <b>Javoblarni qanday yuborish kerak?</b>\n\n"
        "Javoblarni bo'sh joysiz, ketma-ket yozing:\n"
        f"<code>{example}</code>\n\n"
        "Yoki bitta xabarda:\n"
        f"<code>{test_id}*{example}</code>\n\n"
        f"❗️ Faqat <b>{letters()}</b> harflari (inglizcha klaviatura).\n"
        f"❗️ Aynan <b>{len(test['correct_answers'])}</b> ta javob.\n"
        "❗️ Har bir test <b>bir marta</b> topshiriladi.\n"
        "➖➖➖➖➖➖➖➖➖➖➖\n\n"
        "♻️ <b>Test ishlanishga tayyor!!!</b>"
    )

    await message.answer(
        "📤 <b>Kanal uchun e'lon tayyor.</b>\n"
        "Quyidagi xabarni nusxalab kanalingizga joylang 👇"
    )
    await message.answer(announcement)


# ---------------------------------------------------------------------------
# ADMIN: REYTING VA HISOBOTLAR
# ---------------------------------------------------------------------------

async def build_rating(test: dict, limit: int | None = None) -> str:
    """
    Reyting jadvalini matn ko'rinishida tayyorlaydi.
    `limit` berilsa — faqat shuncha kishi ko'rsatiladi (mas. top 10).
    """
    test_id = test["test_id"]
    total = len(test["correct_answers"])
    results = await db.get_results(test_id)

    lines = [
        f"🏆 <b>{safe(test['title'])} — reyting</b>",
        f"‼️ Test kodi: <b>{test_id}</b> | "
        f"👥 Qatnashchilar: <b>{len(results)}</b> ta",
        "➖➖➖➖➖➖➖➖➖➖➖",
    ]

    shown = results[:limit] if limit else results
    for position, (first_name, score, _user_id) in enumerate(shown, start=1):
        percent = score / total * 100
        name = first_name if len(first_name) <= 28 else first_name[:27] + "…"
        lines.append(
            f"{medal(position)} <b>{safe(name)}</b> — {score}/{total} ({percent:.0f}%)"
        )

    if limit and len(results) > limit:
        lines.append(f"... va yana {len(results) - limit} ta o'quvchi")

    stats = await db.get_stats(test_id)
    lines.append("➖➖➖➖➖➖➖➖➖➖➖")
    lines.append(
        f"📊 O'rtacha: <b>{stats['average']:.1f}</b> / {total}  |  "
        f"⬆️ Eng yuqori: <b>{stats['best']}</b>"
    )

    return "\n".join(lines)


async def send_long(message: Message, text: str) -> None:
    """Uzun matnni Telegram chegarasiga (4096) moslab bo'lib yuboradi."""
    for start in range(0, len(text), 3800):
        await message.answer(text[start:start + 3800])


@router.message(Command("results"), IsAdmin())
async def cmd_results(message: Message, command: CommandObject) -> None:
    """/results [kod] — to'liq reyting jadvali."""
    if not command.args or not command.args.strip().isdigit():
        await message.answer("❌ Format: <code>/results 105</code>")
        return

    test_id = int(command.args.strip())
    test = await db.get_test(test_id)

    if test is None:
        await message.answer(f"❌ <code>{test_id}</code> kodli test topilmadi.")
        return

    if not await db.get_results(test_id):
        await message.answer(
            f"📭 <b>{safe(test['title'])}</b> testini hozircha hech kim topshirmagan."
        )
        return

    await send_long(message, await build_rating(test))


@router.message(Command("finish"), IsAdmin())
async def cmd_finish(message: Message, command: CommandObject) -> None:
    """
    /finish [kod] — testni yakunlaydi.
    Shundan keyin o'quvchilar javob yubora olmaydi.
    Buyruq umumiy hisobot va top-10 reytingni chiqaradi.
    """
    if not command.args or not command.args.strip().isdigit():
        await message.answer("❌ Format: <code>/finish 105</code>")
        return

    test_id = int(command.args.strip())
    test = await db.get_test(test_id)

    if test is None:
        await message.answer(f"❌ <code>{test_id}</code> kodli test topilmadi.")
        return

    await db.set_test_active(test_id, False)

    total = len(test["correct_answers"])
    stats = await db.get_stats(test_id)

    report = (
        "📌 <b>Test yakunlandi</b>\n\n"
        f"<b>{test_id}-test haqida ma'lumotlar</b>\n"
        "➖➖➖➖➖➖➖➖➖➖➖\n"
        f"📋 Test nomi: <b>{safe(test['title'])}</b>\n"
        f"📝 Test kodi: <b>{test_id}</b>\n"
        f"🔢 Savollar soni: <b>{total}</b> ta\n"
        f"👤 Test yaratuvchisi: <i>{safe(test['author'])}</i>\n"
        f"🔑 To'g'ri javoblar: <code>{test['correct_answers']}</code>\n"
        "➖➖➖➖➖➖➖➖➖➖➖\n"
        f"👥 Testda qatnashganlar soni: <b>{stats['participants']}</b> ta\n"
    )

    if stats["participants"]:
        report += (
            f"📊 O'rtacha ball: <b>{stats['average']:.1f}</b> / {total}\n"
            f"⬆️ Eng yuqori natija: <b>{stats['best']}</b> / {total}\n"
            f"⬇️ Eng past natija: <b>{stats['worst']}</b> / {total}\n"
        )

    report += "\n🔒 Endi bu testga javob yuborib bo'lmaydi."
    await message.answer(report)

    # Top-10 reytingni alohida xabar qilib yuboramiz
    if stats["participants"]:
        await send_long(message, await build_rating(test, limit=10))
        await message.answer(
            f"To'liq ro'yxat: <code>/results {test_id}</code>"
        )


@router.message(Command("reopen"), IsAdmin())
async def cmd_reopen(message: Message, command: CommandObject) -> None:
    """/reopen [kod] — yakunlangan testni qayta ochadi."""
    if not command.args or not command.args.strip().isdigit():
        await message.answer("❌ Format: <code>/reopen 105</code>")
        return

    test_id = int(command.args.strip())

    if await db.get_test(test_id) is None:
        await message.answer(f"❌ <code>{test_id}</code> kodli test topilmadi.")
        return

    await db.set_test_active(test_id, True)
    await message.answer(
        f"🔓 <code>{test_id}</code> kodli test qayta ochildi. "
        "O'quvchilar javob yubora oladi."
    )


@router.message(Command("tests"), IsAdmin())
async def cmd_tests(message: Message) -> None:
    """/tests — barcha testlar ro'yxati."""
    tests = await db.get_all_tests()

    if not tests:
        await message.answer("📭 Bazada hali birorta ham test yo'q.")
        return

    lines = ["📚 <b>Testlar ro'yxati</b>", "➖➖➖➖➖➖➖➖➖➖➖"]
    for test in tests:
        status = "🟢 Faol" if test["is_active"] else "🔴 Yakunlangan"
        lines.append(
            f"‼️ <b>{test['test_id']}</b> — {safe(test['title'])}\n"
            f"    {status} | {test['questions']} ta savol | "
            f"👥 {test['participants']} ta"
        )

    await send_long(message, "\n".join(lines))


@router.message(Command("reset"), IsAdmin())
async def cmd_reset_attempt(message: Message, command: CommandObject) -> None:
    """/reset [kod] [user_id] — o'quvchiga qayta topshirishga ruxsat beradi."""
    parts = command.args.split() if command.args else []

    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        await message.answer(
            "❌ Format: <code>/reset [kod] [user_id]</code>\n"
            "Namuna: <code>/reset 105 7253695670</code>\n\n"
            "O'quvchi o'z ID sini <code>/id</code> buyrug'i orqali biladi."
        )
        return

    test_id, user_id = int(parts[0]), int(parts[1])

    if await db.reset_attempt(user_id, test_id):
        await message.answer(
            f"♻️ <code>{user_id}</code> ID li o'quvchining "
            f"<code>{test_id}</code>-testdagi natijasi o'chirildi.\n"
            "Endi u testni qaytadan topshira oladi."
        )
    else:
        await message.answer(
            "❌ Bunday natija topilmadi. Kod va o'quvchi ID sini tekshiring."
        )


@router.message(Command("deltest"), IsAdmin())
async def cmd_delete_test(message: Message, command: CommandObject) -> None:
    """/deltest [kod] — testni va uning natijalarini o'chiradi."""
    if not command.args or not command.args.strip().isdigit():
        await message.answer("❌ Format: <code>/deltest 105</code>")
        return

    test_id = int(command.args.strip())

    if await db.delete_test(test_id):
        await message.answer(
            f"🗑 <code>{test_id}</code> kodli test va uning barcha "
            "natijalari o'chirildi."
        )
    else:
        await message.answer(f"❌ <code>{test_id}</code> kodli test topilmadi.")


# ---------------------------------------------------------------------------
# O'QUVCHI: 1-BOSQICH — TEST KODINI YUBORISH
# ---------------------------------------------------------------------------

@router.message(F.text.regexp(CODE_PATTERN))
async def handle_test_code(message: Message, state: FSMContext) -> None:
    """
    O'quvchi faqat raqam yuborganda ishlaydi (mas. 105).
    Testni ochadi va javoblarni kutish holatiga o'tadi.
    """
    # Ro'yxatdan o'tmagan bo'lsa — avval ism so'raladi
    if await require_registration(message, state) is None:
        return

    test_id = int(message.text.strip())
    test = await db.get_test(test_id)

    # Test bazada bormi?
    if test is None:
        await state.clear()
        await message.answer(
            f"❌ <code>{test_id}</code> kodli test topilmadi.\n"
            "Kodni tekshirib, qaytadan yuboring."
        )
        return

    # Test yakunlanganmi?
    if not test["is_active"]:
        await state.clear()
        await message.answer(
            f"🔒 <b>{safe(test['title'])}</b> testi yakunlangan.\n"
            "Endi bu testga javob qabul qilinmaydi."
        )
        return

    # O'quvchi avval topshirganmi?
    previous_score = await db.get_user_score(message.from_user.id, test_id)
    if previous_score is not None:
        await state.clear()
        total = len(test["correct_answers"])
        await message.answer(
            "🔒 <b>Siz bu testni allaqachon topshirgansiz.</b>\n\n"
            f"📋 {safe(test['title'])}\n"
            f"🎯 Natijangiz: <b>{previous_score}</b> / {total}\n\n"
            "Qayta topshirish uchun o'qituvchiga murojaat qiling."
        )
        return

    # Javoblarni kutish holatiga o'tamiz
    await state.set_state(AnswerState.waiting_for_answers)
    await state.update_data(test_id=test_id, answers={}, page=0)

    # Birinchi sahifadagi savollar tugmalari bitta xabarda yuboriladi
    await show_all_questions(message, test, {}, page=0, edit=False)


# ---------------------------------------------------------------------------
# JAVOBLARNI TEKSHIRISH VA BAHOLASH (umumiy mantiq)
# ---------------------------------------------------------------------------

async def grade_answers(
    message: Message,
    test_id: int,
    raw_answers: str,
    state: FSMContext,
    *,
    user: Optional[User] = None,
) -> None:
    """
    Javoblarni tekshiradi va baholaydi.

    Bazaga yozish (save_result) FAQAT barcha tekshiruvlardan o'tgandan
    keyin, funksiyaning eng oxirida bajariladi. Ya'ni xato harf yoki
    to'liqsiz javob yuborilsa, o'quvchining urinishi SARFLANMAYDI.
    """
    user = user or message.from_user
    test = await db.get_test(test_id)

    # 0) Ro'yxatdan o'tganmi?
    full_name = await db.get_user_name(user.id)
    if full_name is None:
        await state.set_state(Registration.waiting_for_name)
        await message.answer(
            "👋 <b>Avval ro'yxatdan o'ting.</b>\n\n"
            "To'liq ism va familiyangizni yozing.\n"
            "Namuna: <code>Ahmadqulov Shahzod</code>\n\n"
            "✅ Bu urinish hisobga olinmadi — ismni yozgach, "
            "javoblarni qaytadan yuborasiz."
        )
        return

    # 1) Test bormi?
    if test is None:
        await state.clear()
        await message.answer(
            f"❌ <code>{test_id}</code> kodli test topilmadi.\n\n"
            "✅ Bu urinish hisobga olinmadi."
        )
        return

    # 2) Test faolmi?
    if not test["is_active"]:
        await state.clear()
        await message.answer(
            f"🔒 <b>{safe(test['title'])}</b> testi yakunlangan.\n"
            "Javob qabul qilinmaydi."
        )
        return

    correct_answers = test["correct_answers"]
    total = len(correct_answers)

    # 3) Bir test — bir marta
    previous_score = await db.get_user_score(user.id, test_id)
    if previous_score is not None:
        await state.clear()
        await message.answer(
            "🔒 <b>Siz bu testni allaqachon topshirgansiz.</b>\n\n"
            f"🎯 Natijangiz: <b>{previous_score}</b> / {total}\n\n"
            "Qayta topshirish uchun o'qituvchiga murojaat qiling."
        )
        return

    user_answers = raw_answers.lower().replace(" ", "")

    # 4) Faqat ruxsat etilgan harflar
    invalid_chars = []
    for char in user_answers:
        if char not in ALLOWED_LETTERS and char not in invalid_chars:
            invalid_chars.append(char)

    if invalid_chars:
        positions = [
            str(index)
            for index, char in enumerate(user_answers, start=1)
            if char not in ALLOWED_LETTERS
        ]
        shown = ", ".join(positions[:10]) + (" ..." if len(positions) > 10 else "")

        await message.answer(
            "⚠️ <b>Javoblarda notanish belgi bor!</b>\n\n"
            f"Faqat <b>{letters()}</b> harflaridan foydalaning.\n\n"
            f"❌ Noto'g'ri belgi(lar): <code>{safe(' '.join(invalid_chars))}</code>\n"
            f"📍 Qaysi savolda: {shown}\n\n"
            "Klaviaturangiz tilini tekshiring — kirill yoki boshqa tildagi "
            "harflar qabul qilinmaydi.\n\n"
            "✅ <b>Xavotir olmang:</b> bu urinish hisobga olinmadi. "
            "Tuzatib, qaytadan yuboravering."
        )
        return

    # 5) Barcha savollarga javob berilganmi?
    if len(user_answers) != total:
        missing = total - len(user_answers)
        if missing > 0:
            hint = (
                f"⚠️ Yana <b>{missing}</b> ta savolga javob belgilamadingiz.\n"
                "Barchasini to'ldirib yuboring."
            )
        else:
            hint = (
                f"⚠️ <b>{abs(missing)}</b> ta ortiqcha javob yubordingiz.\n"
                "Ortiqchasini o'chiring."
            )

        await message.answer(
            "❌ <b>Javoblar soni mos kelmadi!</b>\n\n"
            f"🔢 Testda savollar: <b>{total}</b> ta\n"
            f"✍️ Siz yubordingiz: <b>{len(user_answers)}</b> ta\n\n"
            f"{hint}\n\n"
            "✅ <b>Xavotir olmang:</b> bu urinish hisobga olinmadi."
        )
        return

    # 6) Harfma-harf solishtirish
    score, wrong_numbers = calculate_score(user_answers, correct_answers)
    percent = score / total * 100

    # 7) Bazaga saqlash — endi urinish sarflanadi
    await db.save_result(
        user_id=user.id,
        first_name=full_name,
        test_id=test_id,
        score=score,
    )
    await state.clear()

    if percent >= 90:
        verdict = "🏆 A'lo natija!"
    elif percent >= 70:
        verdict = "👍 Yaxshi natija!"
    elif percent >= 50:
        verdict = "🙂 Qoniqarli, yana harakat qiling."
    else:
        verdict = "📚 Mavzuni takrorlash kerak."

    await message.answer(
        "✅ <b>Javoblaringiz qabul qilindi!</b>\n\n"
        f"👤 O'quvchi: <b>{safe(full_name)}</b>\n"
        f"📋 Test: <b>{safe(test['title'])}</b> (kod: {test_id})\n"
        "➖➖➖➖➖➖➖➖➖➖➖\n"
        f"🎯 To'g'ri javoblar: <b>{score}</b> / {total}\n"
        f"❌ Xato javoblar: <b>{total - score}</b> ta\n"
        f"📊 Foiz: <b>{percent:.1f}%</b>\n\n"
        f"{verdict}\n\n"
        "❌ <b>Xato belgilangan savollar:</b>\n"
        f"{format_wrong_list(wrong_numbers)}\n\n"
        "ℹ️ Natijangiz saqlandi. Bu testni qayta topshirib bo'lmaydi."
    )


# ---------------------------------------------------------------------------
# O'QUVCHI: 2-BOSQICH — JAVOBLARNI YUBORISH
# ---------------------------------------------------------------------------

@router.message(AnswerState.waiting_for_answers, F.text)
async def handle_answers_in_state(message: Message, state: FSMContext) -> None:
    """Test kodi yuborilgandan keyingi javoblar xabari."""
    data = await state.get_data()
    test_id = data.get("test_id")

    if test_id is None:
        await state.clear()
        await message.answer("Test kodini qaytadan yuboring.")
        return

    await grade_answers(message, test_id, message.text.strip(), state)


# ---------------------------------------------------------------------------
# O'QUVCHI: TUGMA BOSISH ORQALI JAVOB BERISH (asosiy usul)
# ---------------------------------------------------------------------------

@router.callback_query(AnswerState.waiting_for_answers, F.data.startswith("ans:"))
async def handle_answer_button(callback: CallbackQuery, state: FSMContext) -> None:
    """
    Grid'dagi istalgan tugma bosilganda ishlaydi:
    - savol qatoridagi harf bosilsa — o'sha savolning javobi yoziladi/o'zgaradi
    - "Yuborish" bosilsa — hammasi belgilangan bo'lsa, baholashga yuboriladi
    - "Bekor qilish" bosilsa — test bekor qilinadi
    """
    data = await state.get_data()
    test_id = data.get("test_id")
    page = int(data.get("page", 0))
    # Kalitlar JSON orqali string bo'lib qolishi mumkin, shu sabab int'ga o'tkazamiz
    answers: dict[int, str] = {
        int(question): letter for question, letter in dict(data.get("answers", {})).items()
    }

    if test_id is None or callback.message is None:
        await callback.answer("Xatolik yuz berdi. Test kodini qaytadan yuboring.", show_alert=True)
        await state.clear()
        return

    test = await db.get_test(test_id)

    if test is None:
        await callback.answer()
        await callback.message.edit_text(f"❌ <code>{test_id}</code> kodli test topilmadi.")
        await state.clear()
        return

    if not test["is_active"]:
        await callback.answer()
        await callback.message.edit_text(
            f"🔒 <b>{safe(test['title'])}</b> testi yakunlangan.\nJavob qabul qilinmaydi."
        )
        await state.clear()
        return

    total = len(test["correct_answers"])
    parts = callback.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "noop":
        await callback.answer()
        return

    if action == "nav":
        direction = parts[2] if len(parts) > 2 else ""
        max_page = (total - 1) // PAGE_SIZE
        if direction == "next":
            page = min(page + 1, max_page)
        elif direction == "prev":
            page = max(page - 1, 0)
        await state.update_data(page=page)
        await callback.answer()
        keyboard = build_full_keyboard(total, answers, page)
        try:
            await callback.message.edit_reply_markup(reply_markup=keyboard)
        except Exception:
            pass
        return

    if action == "cancel":
        await state.clear()
        await callback.answer()
        await callback.message.edit_text(
            "🚫 Bekor qilindi. Testda qatnashish uchun kodni qaytadan yuboring."
        )
        return

    if action == "submit":
        if len(answers) < total:
            missing = [str(q) for q in range(1, total + 1) if q not in answers]
            shown = ", ".join(missing[:15]) + (" ..." if len(missing) > 15 else "")
            await callback.answer(
                f"Yana {len(missing)} ta savolga javob belgilanmagan: {shown}",
                show_alert=True,
            )
            return

        raw_answers = "".join(answers[q] for q in range(1, total + 1))
        await callback.answer("Yuborilmoqda...")
        await callback.message.edit_text("⏳ Javoblaringiz tekshirilmoqda...")
        await grade_answers(
            callback.message, test_id, raw_answers, state, user=callback.from_user
        )
        return

    # Qolgan variant: savol raqami + harf, masalan "ans:7:c"
    if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in ALLOWED_LETTERS:
        await callback.answer()
        return

    question_number = int(parts[1])
    letter = parts[2]

    if not (1 <= question_number <= total):
        await callback.answer()
        return

    answers[question_number] = letter
    await state.update_data(answers=answers)
    await callback.answer(f"{question_number}-savol: {letter.upper()}")

    keyboard = build_full_keyboard(total, answers, page)
    try:
        await callback.message.edit_reply_markup(reply_markup=keyboard)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# O'QUVCHI: TEZKOR USUL — 105*abcdabcd
# ---------------------------------------------------------------------------

@router.message(F.text.contains("*"))
async def handle_oneshot(message: Message, state: FSMContext) -> None:
    """Bitta xabarda kod va javoblar: 105*abcdabcd"""
    match = ONESHOT_PATTERN.match(message.text)

    if not match:
        await message.answer(
            "❌ <b>Format noto'g'ri.</b>\n\n"
            "Tezkor usul: <code>105*abcdabcdabcd</code>\n"
            "Yoki avval test kodini yuboring: <code>105</code>\n\n"
            "✅ Bu urinish hisobga olinmadi."
        )
        return

    await grade_answers(message, int(match.group(1)), match.group(2), state)


# ---------------------------------------------------------------------------
# BOSHQA XABARLAR
# ---------------------------------------------------------------------------

@router.message(F.text)
async def handle_unknown(message: Message) -> None:
    """Hech qaysi qoidaga tushmagan matnli xabar."""
    await message.answer(
        "🤔 Tushunmadim.\n\n"
        "📌 Testda qatnashish uchun <b>test kodini</b> yuboring.\n"
        "Namuna: <code>105</code>\n\n"
        "📖 To'liq ko'rsatma: /qollanma"
    )


# ---------------------------------------------------------------------------
# BOTNI ISHGA TUSHIRISH
# ---------------------------------------------------------------------------

async def main() -> None:
    """Bazani tayyorlaydi va botni polling rejimida ishga tushiradi."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        stream=sys.stdout,
    )

    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        logging.error("BOT_TOKEN noto'g'ri yoki o'rnatilmagan!")
        return

    await db.init_db()
    logging.info("Ma'lumotlar bazasi tayyor.")

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    # MemoryStorage — ikki bosqichli suhbat holatini saqlash uchun.
    # Bot qayta ishga tushganda holatlar tozalanadi (bu normal holat).
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # Render.com bepul Web Service uchun port tinglash (health-check)
    render_port = os.getenv("PORT")
    if render_port:
        from aiohttp import web

        async def health_check(_request: web.Request) -> web.Response:
            return web.Response(text="Bot is running!")

        app = web.Application()
        app.router.add_get("/", health_check)
        app.router.add_get("/health", health_check)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", int(render_port))
        await site.start()
        logging.info(f"Render health-check serveri {render_port}-portda ishga tushdi.")

    logging.info("Bot ishga tushdi. To'xtatish uchun: Ctrl+C")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\nBot to'xtatildi.")
