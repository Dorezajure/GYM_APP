"""
Дневник тренировок — бэкенд (FastAPI + SQLite).

Структура проекта:
    main.py          ← этот файл: только API и раздача index.html
    index.html       ← фронтенд (вся вёрстка, стили, JS)
    requirements.txt
    fitness.db       ← создаётся автоматически

Запуск:
    uvicorn main:app --reload
"""
import os
import re
import sqlite3
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

# ---------------------------------------------------------------- Пути и константы
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "fitness.db"))
INDEX_HTML_PATH = os.path.join(BASE_DIR, "index.html")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^\d{2}:\d{2}$")

VALID_WORKOUT_TYPES = {"back_triceps", "chest_shoulders_biceps", "legs_cardio"}
VALID_DIFFICULTIES = {"easy", "medium", "hard"}

app = FastAPI(
    title="Fitness Diary API",
    description="Backend для дневника тренировок. Фронтенд — статический index.html.",
)


# ---------------------------------------------------------------- БД
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db() -> None:
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS exercises (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT    NOT NULL,
            name        TEXT    NOT NULL,
            name_key    TEXT    NOT NULL,
            sets        INTEGER,
            reps        INTEGER,
            reps_detail TEXT,
            weight      REAL,
            created_at  TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_name_date ON exercises(name_key, date, id);
        CREATE INDEX IF NOT EXISTS idx_date      ON exercises(date);

        CREATE TABLE IF NOT EXISTS workout_days (
            date          TEXT PRIMARY KEY,
            comment       TEXT,
            started_at    TEXT,
            finished_at   TEXT,
            body_weight   REAL,
            workout_type  TEXT,
            difficulty    TEXT
        );
        """
    )
    ensure_column(conn, "exercises",    "reps_detail",  "TEXT")
    ensure_column(conn, "workout_days", "body_weight",  "REAL")
    ensure_column(conn, "workout_days", "workout_type", "TEXT")
    ensure_column(conn, "workout_days", "difficulty",   "TEXT")
    conn.commit()
    conn.close()


init_db()


# ---------------------------------------------------------------- Хелперы
def norm(name: str) -> str:
    return " ".join(name.strip().lower().split())


def check_date(value: str) -> str:
    if not DATE_RE.match(value or ""):
        raise HTTPException(400, "Дата должна быть в формате YYYY-MM-DD")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "Некорректная дата")
    return value


def check_time(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    if not TIME_RE.match(value):
        raise HTTPException(400, "Время должно быть в формате HH:MM")
    h, m = int(value[:2]), int(value[3:])
    if not (0 <= h < 24 and 0 <= m < 60):
        raise HTTPException(400, "Некорректное время")
    return value


def parse_reps_input(reps_detail: Optional[str], fallback_reps: Optional[int]):
    if not reps_detail or not str(reps_detail).strip():
        if fallback_reps is None:
            return None, None, None
        return int(fallback_reps), None, 1

    parts = [p for p in re.split(r"[,;\s]+", str(reps_detail).strip()) if p]
    if not parts:
        if fallback_reps is None:
            return None, None, None
        return int(fallback_reps), None, 1

    nums = []
    for p in parts:
        try:
            v = int(p)
        except (ValueError, TypeError):
            raise HTTPException(400, f"Некорректное число повторений: «{p}»")
        if v < 0 or v > 999:
            raise HTTPException(400, f"Повторения должны быть 0–999: «{p}»")
        nums.append(v)

    if len(nums) == 1:
        return nums[0], None, 1

    avg = int(sum(nums) / len(nums) + 0.5)
    return avg, ",".join(str(n) for n in nums), len(nums)


def prev_for(conn: sqlite3.Connection, row: sqlite3.Row):
    return conn.execute(
        """
        SELECT * FROM exercises
        WHERE name_key = ?
          AND (date < ? OR (date = ? AND id < ?))
        ORDER BY date DESC, id DESC
        LIMIT 1
        """,
        (row["name_key"], row["date"], row["date"], row["id"]),
    ).fetchone()


def make_item(row: sqlite3.Row, prev: Optional[sqlite3.Row]) -> dict:
    reps_delta: Optional[int] = None
    weight_delta: Optional[float] = None

    if prev is not None:
        if prev["reps"] is not None and row["reps"] is not None:
            reps_delta = row["reps"] - prev["reps"]
        if prev["weight"] is not None and row["weight"] is not None:
            weight_delta = round(row["weight"] - prev["weight"], 2)

    return {
        "id": row["id"],
        "name": row["name"],
        "sets": row["sets"],
        "reps": row["reps"],
        "reps_detail": row["reps_detail"] if "reps_detail" in row.keys() else None,
        "weight": row["weight"],
        "improved": {
            "reps": reps_delta is not None and reps_delta > 0,
            "weight": weight_delta is not None and weight_delta > 0,
        },
        "reps_delta": reps_delta,
        "weight_delta": weight_delta,
        "prev": None if prev is None else {
            "date": prev["date"], "sets": prev["sets"],
            "reps": prev["reps"], "weight": prev["weight"],
        },
    }


# ---------------------------------------------------------------- Модели
class ExerciseIn(BaseModel):
    date: str
    name: str
    sets: Optional[int] = None
    reps: Optional[int] = None
    reps_detail: Optional[str] = None
    weight: Optional[float] = None


class DayNotesIn(BaseModel):
    date: str
    comment: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    body_weight: Optional[float] = None
    workout_type: Optional[str] = None
    difficulty: Optional[str] = None


# ---------------------------------------------------------------- API: дни
@app.get("/api/day")
def get_day(date: str):
    check_date(date)
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM exercises WHERE date = ? ORDER BY id", (date,)
        ).fetchall()
        items = [make_item(r, prev_for(conn, r)) for r in rows]
        nd = conn.execute(
            "SELECT * FROM workout_days WHERE date = ?", (date,)
        ).fetchone()
        notes = {
            "comment": nd["comment"] if nd else None,
            "started_at": nd["started_at"] if nd else None,
            "finished_at": nd["finished_at"] if nd else None,
            "body_weight": nd["body_weight"] if nd else None,
            "workout_type": nd["workout_type"] if nd else None,
            "difficulty": nd["difficulty"] if nd else None,
        }
    finally:
        conn.close()
    return {"date": date, "items": items, "notes": notes}


@app.get("/api/month")
def get_month(year: int, month: int):
    if not 1 <= month <= 12:
        raise HTTPException(400, "Месяц должен быть от 1 до 12")
    prefix = f"{year:04d}-{month:02d}"
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT date FROM exercises    WHERE substr(date, 1, 7) = ?
            UNION
            SELECT date FROM workout_days WHERE substr(date, 1, 7) = ?
            ORDER BY date
            """,
            (prefix, prefix),
        ).fetchall()
    finally:
        conn.close()
    return {"dates": [r["date"] for r in rows]}


@app.get("/api/weights")
def get_weights(year: int, month: int):
    if not 1 <= month <= 12:
        raise HTTPException(400, "Месяц должен быть от 1 до 12")
    prefix = f"{year:04d}-{month:02d}"
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT date, body_weight FROM workout_days
            WHERE substr(date, 1, 7) = ? AND body_weight IS NOT NULL
            ORDER BY date
            """,
            (prefix,),
        ).fetchall()
    finally:
        conn.close()
    return {"items": [{"date": r["date"], "weight": r["body_weight"]} for r in rows]}


@app.put("/api/day/notes")
def save_day_notes(payload: DayNotesIn):
    check_date(payload.date)
    started_at = check_time(payload.started_at)
    finished_at = check_time(payload.finished_at)
    comment = (payload.comment or "").strip() or None

    workout_type = (payload.workout_type or "").strip() or None
    difficulty   = (payload.difficulty or "").strip() or None

    if workout_type is not None and workout_type not in VALID_WORKOUT_TYPES:
        raise HTTPException(400, "Некорректный тип тренировки")
    if difficulty is not None and difficulty not in VALID_DIFFICULTIES:
        raise HTTPException(400, "Некорректная сложность")

    body_weight = payload.body_weight
    if body_weight is not None and not (0 < body_weight < 500):
        raise HTTPException(400, "Вес тела должен быть больше 0 и меньше 500 кг")

    conn = get_db()
    try:
        if (not comment and not started_at and not finished_at
                and body_weight is None and workout_type is None and difficulty is None):
            conn.execute("DELETE FROM workout_days WHERE date = ?", (payload.date,))
        else:
            conn.execute(
                """
                INSERT INTO workout_days
                    (date, comment, started_at, finished_at, body_weight, workout_type, difficulty)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    comment      = excluded.comment,
                    started_at   = excluded.started_at,
                    finished_at  = excluded.finished_at,
                    body_weight  = excluded.body_weight,
                    workout_type = excluded.workout_type,
                    difficulty   = excluded.difficulty
                """,
                (payload.date, comment, started_at, finished_at, body_weight,
                 workout_type, difficulty),
            )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


# ---------------------------------------------------------------- API: упражнения
@app.post("/api/exercises")
def add_exercise(payload: ExerciseIn):
    check_date(payload.date)
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Укажите название упражнения")

    avg_reps, detail, count = parse_reps_input(payload.reps_detail, payload.reps)

    sets = payload.sets
    if sets is None and count is not None and count > 1:
        sets = count

    conn = get_db()
    try:
        cur = conn.execute(
            """INSERT INTO exercises
               (date, name, name_key, sets, reps, reps_detail, weight, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (payload.date, name, norm(name), sets, avg_reps, detail, payload.weight,
             datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM exercises WHERE id = ?",
                           (cur.lastrowid,)).fetchone()
        return make_item(row, prev_for(conn, row))
    finally:
        conn.close()


@app.delete("/api/exercises/{item_id}")
def delete_exercise(item_id: int):
    conn = get_db()
    try:
        cur = conn.execute("DELETE FROM exercises WHERE id = ?", (item_id,))
        conn.commit()
    finally:
        conn.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "Запись не найдена")
    return {"ok": True}


@app.get("/api/summary")
def get_summary():
    conn = get_db()
    try:
        keys = conn.execute("SELECT name_key FROM exercises GROUP BY name_key").fetchall()
        items = []
        for k in keys:
            last = conn.execute(
                "SELECT * FROM exercises WHERE name_key = ? ORDER BY date DESC, id DESC LIMIT 1",
                (k["name_key"],),
            ).fetchone()
            items.append({
                "name_key": k["name_key"],
                "name": last["name"],
                "date": last["date"],
                "sets": last["sets"],
                "reps": last["reps"],
                "reps_detail": last["reps_detail"] if "reps_detail" in last.keys() else None,
                "weight": last["weight"],
            })
    finally:
        conn.close()
    items.sort(key=lambda x: x["date"], reverse=True)
    return {"items": items}


# ---------------------------------------------------------------- Раздача фронтенда
@app.get("/", include_in_schema=False)
def serve_index():
    if not os.path.exists(INDEX_HTML_PATH):
        raise HTTPException(
            500,
            f"Файл index.html не найден рядом с main.py (ожидался: {INDEX_HTML_PATH})"
        )
    return FileResponse(INDEX_HTML_PATH, media_type="text/html")