"""
database.py
===========
SQLite ma'lumotlar bazasi bilan ishlovchi barcha funksiyalar.

`aiosqlite` — standart `sqlite3` ustiga qurilgan kutubxona. Baza oddiy
SQLite fayli (.db) bo'lib qolaveradi, faqat so'rovlar async/await orqali,
botning asosiy oqimini bloklamagan holda bajariladi.

Jadvallar:
  tests   — test_id, title, author, correct_answers, is_active, created_at
  results — id, user_id, first_name, test_id, score, created_at
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite

DB_PATH = "bot_database.db"


# ---------------------------------------------------------------------------
# 1. Jadvallarni yaratish va yangilash (migratsiya)
# ---------------------------------------------------------------------------

CREATE_TESTS_TABLE = """
CREATE TABLE IF NOT EXISTS tests (
    test_id         INTEGER PRIMARY KEY,
    title           TEXT    NOT NULL DEFAULT 'Nomsiz test',
    author          TEXT    NOT NULL DEFAULT '',
    correct_answers TEXT    NOT NULL,
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL DEFAULT ''
);
"""

CREATE_RESULTS_TABLE = """
CREATE TABLE IF NOT EXISTS results (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    first_name TEXT    NOT NULL,
    test_id    INTEGER NOT NULL,
    score      INTEGER NOT NULL,
    created_at TEXT    NOT NULL DEFAULT ''
);
"""

CREATE_USERS_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY,
    full_name     TEXT    NOT NULL,
    username      TEXT    NOT NULL DEFAULT '',
    registered_at TEXT    NOT NULL DEFAULT ''
);
"""

CREATE_RESULTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_results_test ON results (test_id, score DESC);
"""

# Eski versiyadan qolgan bazaga qo'shilishi kerak bo'lgan ustunlar.
# Format: (jadval nomi, ustun nomi, ustun ta'rifi)
MIGRATIONS = [
    ("tests", "title", "TEXT NOT NULL DEFAULT 'Nomsiz test'"),
    ("tests", "author", "TEXT NOT NULL DEFAULT ''"),
    ("tests", "is_active", "INTEGER NOT NULL DEFAULT 1"),
    ("tests", "created_at", "TEXT NOT NULL DEFAULT ''"),
    ("results", "created_at", "TEXT NOT NULL DEFAULT ''"),
]


async def _column_exists(db: aiosqlite.Connection, table: str, column: str) -> bool:
    """Jadvalda shunday ustun bormi — shuni tekshiradi."""
    async with db.execute(f"PRAGMA table_info({table})") as cursor:
        columns = [row[1] for row in await cursor.fetchall()]
    return column in columns


async def init_db() -> None:
    """
    Jadvallarni yaratadi va eski bazani yangi tuzilishga moslaydi.

    Migratsiya tufayli avval yaratilgan `bot_database.db` faylidagi
    testlar va natijalar o'chib ketmaydi — ularga shunchaki yangi
    ustunlar qo'shiladi.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_TESTS_TABLE)
        await db.execute(CREATE_RESULTS_TABLE)
        await db.execute(CREATE_USERS_TABLE)
        await db.execute(CREATE_RESULTS_INDEX)

        # Eski bazada yetishmayotgan ustunlarni qo'shamiz
        for table, column, definition in MIGRATIONS:
            if not await _column_exists(db, table, column):
                await db.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                )

        await db.commit()


def _now() -> str:
    """Joriy sana-vaqtni matn ko'rinishida qaytaradi."""
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# 2. Testlar bilan ishlash
# ---------------------------------------------------------------------------

async def add_test(
    test_id: int, title: str, author: str, correct_answers: str
) -> None:
    """
    Yangi test qo'shadi. Agar shu `test_id` mavjud bo'lsa — yangilanadi.
    Yangi qo'shilgan test darhol faol (is_active = 1) holatda bo'ladi.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO tests
                (test_id, title, author, correct_answers, is_active, created_at)
            VALUES (?, ?, ?, ?, 1, ?)
            """,
            (test_id, title, author, correct_answers, _now()),
        )
        await db.commit()


async def get_test(test_id: int) -> Optional[Dict[str, Any]]:
    """
    Test haqidagi to'liq ma'lumotni lug'at ko'rinishida qaytaradi:
    {test_id, title, author, correct_answers, is_active, created_at}

    Test topilmasa — None.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT test_id, title, author, correct_answers, is_active, created_at
            FROM tests WHERE test_id = ?
            """,
            (test_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def set_test_active(test_id: int, active: bool) -> bool:
    """
    Testni yakunlaydi (active=False) yoki qayta ochadi (active=True).
    Test topilmasa False qaytaradi.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE tests SET is_active = ? WHERE test_id = ?",
            (1 if active else 0, test_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def delete_test(test_id: int) -> bool:
    """Testni va unga tegishli barcha natijalarni o'chiradi."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM tests WHERE test_id = ?", (test_id,))
        await db.execute("DELETE FROM results WHERE test_id = ?", (test_id,))
        await db.commit()
        return cursor.rowcount > 0


async def get_all_tests() -> List[Dict[str, Any]]:
    """
    Barcha testlar ro'yxatini qaytaradi. Har bir element:
    {test_id, title, questions, is_active, participants}
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT
                t.test_id,
                t.title,
                LENGTH(t.correct_answers) AS questions,
                t.is_active,
                (SELECT COUNT(*) FROM results r WHERE r.test_id = t.test_id)
                    AS participants
            FROM tests t
            ORDER BY t.is_active DESC, t.test_id DESC
            """
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


# ---------------------------------------------------------------------------
# 3. Foydalanuvchilar (ro'yxatdan o'tish)
# ---------------------------------------------------------------------------

async def save_user(user_id: int, full_name: str, username: str = "") -> None:
    """
    O'quvchini ro'yxatga oladi yoki ismini yangilaydi.
    Ro'yxatdan o'tish sanasi faqat birinchi marta yoziladi.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT registered_at FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()

        registered_at = row[0] if row else _now()

        await db.execute(
            """
            INSERT OR REPLACE INTO users (user_id, full_name, username, registered_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, full_name, username, registered_at),
        )
        await db.commit()


async def get_user_name(user_id: int) -> Optional[str]:
    """
    Ro'yxatdan o'tgan o'quvchining ism-familiyasi.
    O'tmagan bo'lsa — None (bu holda bot ro'yxatdan o'tishni so'raydi).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT full_name FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def get_users_count() -> int:
    """Ro'yxatdan o'tgan o'quvchilar soni."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            return (await cursor.fetchone())[0]


# ---------------------------------------------------------------------------
# 4. Natijalar bilan ishlash
# ---------------------------------------------------------------------------

async def save_result(
    user_id: int, first_name: str, test_id: int, score: int
) -> None:
    """O'quvchining natijasini saqlaydi."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO results (user_id, first_name, test_id, score, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, first_name, test_id, score, _now()),
        )
        await db.commit()


async def get_user_score(user_id: int, test_id: int) -> Optional[int]:
    """
    O'quvchining shu testdagi natijasi. Topshirmagan bo'lsa — None.
    "Bir test — bir marta" qoidasi shu funksiya orqali tekshiriladi.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT score FROM results WHERE user_id = ? AND test_id = ? LIMIT 1",
            (user_id, test_id),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def reset_attempt(user_id: int, test_id: int) -> bool:
    """O'quvchining urinishini o'chirib, qayta topshirishga ruxsat beradi."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM results WHERE user_id = ? AND test_id = ?",
            (user_id, test_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_results(test_id: int) -> List[Tuple[str, int, int, str]]:
    """
    Reyting jadvali: [(ism, ball, user_id, topshirilgan_vaqti), ...] —
    balldan pastga qarab, ball teng bo'lsa avvalroq topshirgan yuqorida.
    Har kim bir marta topshirgani uchun takrorlarni filtrlash shart emas.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT first_name, score, user_id, created_at
            FROM results
            WHERE test_id = ?
            ORDER BY score DESC, created_at ASC
            """,
            (test_id,),
        ) as cursor:
            return await cursor.fetchall()


async def get_stats(test_id: int) -> Dict[str, Any]:
    """
    Test bo'yicha umumiy statistika:
    {participants, average, best, worst}
    Hech kim topshirmagan bo'lsa, barcha qiymatlar 0 bo'ladi.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT COUNT(*), AVG(score), MAX(score), MIN(score)
            FROM results WHERE test_id = ?
            """,
            (test_id,),
        ) as cursor:
            count, average, best, worst = await cursor.fetchone()

    if not count:
        return {"participants": 0, "average": 0, "best": 0, "worst": 0}

    return {
        "participants": count,
        "average": average or 0,
        "best": best or 0,
        "worst": worst or 0,
    }
