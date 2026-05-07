import json
import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Optional

from career_models import JobMatch, KeywordEntry, LocationPreference, ResumeProfile

DEFAULT_USER_ID = "local"

CREATE_RESUMES_SQL = """
CREATE TABLE IF NOT EXISTS resumes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    file_name TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    parsed_json TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

CREATE_KEYWORDS_SQL = """
CREATE TABLE IF NOT EXISTS keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    keyword TEXT NOT NULL,
    category TEXT DEFAULT 'general',
    priority INTEGER DEFAULT 3,
    enabled INTEGER DEFAULT 1,
    source TEXT DEFAULT 'ai_generated',
    created_at TEXT NOT NULL,
    UNIQUE(user_id, keyword)
)
"""

CREATE_LOCATIONS_SQL = """
CREATE TABLE IF NOT EXISTS locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    city TEXT NOT NULL,
    province_state TEXT DEFAULT '',
    country TEXT DEFAULT '',
    latitude REAL,
    longitude REAL,
    radius_km INTEGER DEFAULT 25,
    work_mode TEXT DEFAULT 'hybrid',
    created_at TEXT NOT NULL,
    UNIQUE(user_id, city, province_state, country, work_mode)
)
"""

CREATE_JOB_MATCHES_SQL = """
CREATE TABLE IF NOT EXISTS job_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    job_apply_link TEXT NOT NULL,
    keyword_id INTEGER,
    match_score INTEGER NOT NULL,
    matched_skills TEXT DEFAULT '[]',
    missing_skills TEXT DEFAULT '[]',
    ai_reason TEXT DEFAULT '',
    status TEXT DEFAULT 'new',
    created_at TEXT NOT NULL,
    UNIQUE(user_id, job_apply_link)
)
"""


class ProfileStore:
    """SQLite store for the personalized career profile MVP."""

    def __init__(self, db_path: str = "jobs.db"):
        self.db_path = db_path

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(CREATE_RESUMES_SQL)
            conn.execute(CREATE_KEYWORDS_SQL)
            conn.execute(CREATE_LOCATIONS_SQL)
            conn.execute(CREATE_JOB_MATCHES_SQL)
            conn.commit()

    def save_resume(
        self,
        file_name: str,
        raw_text: str,
        profile: ResumeProfile,
        user_id: str = DEFAULT_USER_ID,
    ) -> int:
        self.ensure_schema()
        now = _now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO resumes (user_id, file_name, raw_text, parsed_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, file_name, raw_text, profile.model_dump_json(), now),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def get_resume(self, resume_id: int, user_id: str = DEFAULT_USER_ID) -> Optional[dict]:
        self.ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM resumes WHERE id = ? AND user_id = ?",
                (resume_id, user_id),
            ).fetchone()
        return _resume_row(row) if row else None

    def get_latest_resume(self, user_id: str = DEFAULT_USER_ID) -> Optional[dict]:
        self.ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM resumes WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        return _resume_row(row) if row else None

    def upsert_keywords(self, keywords: Iterable[KeywordEntry], user_id: str = DEFAULT_USER_ID) -> list[KeywordEntry]:
        self.ensure_schema()
        now = _now()
        saved = []
        with self._connect() as conn:
            for item in keywords:
                conn.execute(
                    """
                    INSERT INTO keywords (user_id, keyword, category, priority, enabled, source, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, keyword) DO UPDATE SET
                        category = excluded.category,
                        priority = excluded.priority,
                        enabled = excluded.enabled,
                        source = excluded.source
                    """,
                    (
                        user_id,
                        item.keyword.strip().lower(),
                        item.category,
                        item.priority,
                        int(item.enabled),
                        item.source,
                        now,
                    ),
                )
            conn.commit()
        saved.extend(self.list_keywords(user_id=user_id))
        return saved

    def add_keyword(self, keyword: KeywordEntry, user_id: str = DEFAULT_USER_ID) -> KeywordEntry:
        return self.upsert_keywords([keyword], user_id=user_id)[0]

    def list_keywords(self, enabled_only: bool = False, user_id: str = DEFAULT_USER_ID) -> list[KeywordEntry]:
        self.ensure_schema()
        sql = "SELECT * FROM keywords WHERE user_id = ?"
        params: list[object] = [user_id]
        if enabled_only:
            sql += " AND enabled = 1"
        sql += " ORDER BY priority DESC, keyword ASC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_keyword_row(row) for row in rows]

    def update_keyword(self, keyword_id: int, changes: dict, user_id: str = DEFAULT_USER_ID) -> Optional[KeywordEntry]:
        self.ensure_schema()
        allowed = {"keyword", "category", "priority", "enabled", "source"}
        fields = [key for key in changes if key in allowed]
        if not fields:
            return self.get_keyword(keyword_id, user_id=user_id)
        assignments = ", ".join(f"{field} = ?" for field in fields)
        values = [int(changes[field]) if field == "enabled" else changes[field] for field in fields]
        values.extend([keyword_id, user_id])
        with self._connect() as conn:
            conn.execute(
                f"UPDATE keywords SET {assignments} WHERE id = ? AND user_id = ?",
                values,
            )
            conn.commit()
        return self.get_keyword(keyword_id, user_id=user_id)

    def delete_keyword(self, keyword_id: int, user_id: str = DEFAULT_USER_ID) -> None:
        self.ensure_schema()
        with self._connect() as conn:
            conn.execute("DELETE FROM keywords WHERE id = ? AND user_id = ?", (keyword_id, user_id))
            conn.commit()

    def get_keyword(self, keyword_id: int, user_id: str = DEFAULT_USER_ID) -> Optional[KeywordEntry]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM keywords WHERE id = ? AND user_id = ?",
                (keyword_id, user_id),
            ).fetchone()
        return _keyword_row(row) if row else None

    def add_location(self, location: LocationPreference, user_id: str = DEFAULT_USER_ID) -> LocationPreference:
        self.ensure_schema()
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO locations (
                    user_id, city, province_state, country, latitude, longitude,
                    radius_km, work_mode, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, city, province_state, country, work_mode) DO UPDATE SET
                    latitude = excluded.latitude,
                    longitude = excluded.longitude,
                    radius_km = excluded.radius_km
                """,
                (
                    user_id,
                    location.city.strip(),
                    location.province_state.strip(),
                    location.country.strip(),
                    location.latitude,
                    location.longitude,
                    location.radius_km,
                    location.work_mode,
                    now,
                ),
            )
            conn.commit()
        return self.list_locations(user_id=user_id)[0]

    def list_locations(self, user_id: str = DEFAULT_USER_ID) -> list[LocationPreference]:
        self.ensure_schema()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM locations WHERE user_id = ? ORDER BY created_at DESC, id DESC",
                (user_id,),
            ).fetchall()
        return [_location_row(row) for row in rows]

    def delete_location(self, location_id: int, user_id: str = DEFAULT_USER_ID) -> None:
        self.ensure_schema()
        with self._connect() as conn:
            conn.execute("DELETE FROM locations WHERE id = ? AND user_id = ?", (location_id, user_id))
            conn.commit()

    def upsert_job_match(self, match: JobMatch, user_id: str = DEFAULT_USER_ID) -> JobMatch:
        self.ensure_schema()
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO job_matches (
                    user_id, job_apply_link, keyword_id, match_score, matched_skills,
                    missing_skills, ai_reason, status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, job_apply_link) DO UPDATE SET
                    keyword_id = excluded.keyword_id,
                    match_score = excluded.match_score,
                    matched_skills = excluded.matched_skills,
                    missing_skills = excluded.missing_skills,
                    ai_reason = excluded.ai_reason
                """,
                (
                    user_id,
                    match.job_apply_link,
                    match.keyword_id,
                    match.match_score,
                    json.dumps(match.matched_skills),
                    json.dumps(match.missing_skills),
                    match.ai_reason,
                    match.status,
                    now,
                ),
            )
            conn.commit()
        return match

    def list_job_matches(self, user_id: str = DEFAULT_USER_ID) -> list[JobMatch]:
        self.ensure_schema()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM job_matches WHERE user_id = ? ORDER BY match_score DESC",
                (user_id,),
            ).fetchall()
        return [_match_row(row) for row in rows]

    def update_match_status(self, match_id: int, status: str, user_id: str = DEFAULT_USER_ID) -> Optional[JobMatch]:
        self.ensure_schema()
        with self._connect() as conn:
            conn.execute(
                "UPDATE job_matches SET status = ? WHERE id = ? AND user_id = ?",
                (status, match_id, user_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM job_matches WHERE id = ? AND user_id = ?",
                (match_id, user_id),
            ).fetchone()
        return _match_row(row) if row else None

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resume_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "file_name": row["file_name"],
        "raw_text": row["raw_text"],
        "parsed_json": json.loads(row["parsed_json"]),
        "created_at": row["created_at"],
    }


def _keyword_row(row: sqlite3.Row) -> KeywordEntry:
    return KeywordEntry(
        id=row["id"],
        keyword=row["keyword"],
        category=row["category"],
        priority=row["priority"],
        enabled=bool(row["enabled"]),
        source=row["source"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _location_row(row: sqlite3.Row) -> LocationPreference:
    return LocationPreference(
        id=row["id"],
        city=row["city"],
        province_state=row["province_state"],
        country=row["country"],
        latitude=row["latitude"],
        longitude=row["longitude"],
        radius_km=row["radius_km"],
        work_mode=row["work_mode"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _match_row(row: sqlite3.Row) -> JobMatch:
    return JobMatch(
        id=row["id"],
        job_apply_link=row["job_apply_link"],
        keyword_id=row["keyword_id"],
        match_score=row["match_score"],
        matched_skills=json.loads(row["matched_skills"] or "[]"),
        missing_skills=json.loads(row["missing_skills"] or "[]"),
        ai_reason=row["ai_reason"],
        status=row["status"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )
