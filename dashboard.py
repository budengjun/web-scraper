"""
Dashboard — A lightweight Flask web app to visualize the jobs database.
Run:  python dashboard.py
Open:  http://localhost:5050
"""

import asyncio
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from flask import Flask, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

import yaml
import os

from career_models import KeywordEntry, LocationPreference, ResumeProfile
from matching_engine import score_job_match
from profile_store import ProfileStore
from resume_llm_parser import parse_resume_profile_with_optional_llm
from resume_parser import build_keyword_entries, extract_resume_text
from scraper import ScraperEngine
from storage import CREATE_TABLE_SQL, JobStore

app = Flask(__name__, static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
SEARCH_TASKS = {}
SEARCH_TASK_LOCK = threading.Lock()

# Load DB path from config if available
CONFIG_PATH = "config.yaml"
DB_PATH = "jobs.db"
CONFIG = {}
if os.path.exists(CONFIG_PATH):
    try:
        with open(CONFIG_PATH, "r") as f:
            CONFIG = yaml.safe_load(f) or {}
            DB_PATH = CONFIG.get("settings", {}).get("database_path", "jobs.db")
    except Exception:
        pass


def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Ensure table exists so we don't crash on first load
    conn.execute(CREATE_TABLE_SQL)
    conn.commit()
    return conn


# ── API endpoints ────────────────────────────────────

@app.route("/api/jobs")
def api_jobs():
    conn = _get_db()
    rows = conn.execute(
        "SELECT apply_link, title, company, location, description, "
        "posted_date, match_score, match_reasoning, first_seen, last_seen, notified "
        "FROM jobs ORDER BY first_seen DESC"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/stats")
def api_stats():
    conn = _get_db()
    c = conn.cursor()

    total = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    scored = c.execute("SELECT COUNT(*) FROM jobs WHERE match_score IS NOT NULL").fetchone()[0]
    avg_score = c.execute("SELECT AVG(match_score) FROM jobs WHERE match_score IS NOT NULL").fetchone()[0]
    high_match = c.execute("SELECT COUNT(*) FROM jobs WHERE match_score > 80").fetchone()[0]
    companies = c.execute("SELECT COUNT(DISTINCT company) FROM jobs").fetchone()[0]
    notified = c.execute("SELECT COUNT(*) FROM jobs WHERE notified = 1").fetchone()[0]

    # Company breakdown
    company_rows = c.execute(
        "SELECT company, COUNT(*) as cnt FROM jobs GROUP BY company ORDER BY cnt DESC LIMIT 10"
    ).fetchall()

    conn.close()
    return jsonify({
        "total": total,
        "scored": scored,
        "avg_score": round(avg_score, 1) if avg_score else 0,
        "high_match": high_match,
        "companies": companies,
        "notified": notified,
        "company_breakdown": [{"name": r[0], "count": r[1]} for r in company_rows],
    })


@app.route("/api/resumes/upload", methods=["POST"])
def api_resume_upload():
    uploaded = request.files.get("resume")
    if not uploaded or not uploaded.filename:
        return jsonify({"error": "Upload a resume file."}), 400

    file_name = secure_filename(uploaded.filename)
    try:
        raw_text = extract_resume_text(uploaded.read(), file_name)
        profile = parse_resume_profile_with_optional_llm(raw_text, api_key=_resume_llm_api_key())
        store = ProfileStore(DB_PATH)
        resume_id = store.save_resume(file_name, raw_text, profile)
        keywords = store.upsert_keywords(build_keyword_entries(profile))
    except (RuntimeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify({
        "resume_id": resume_id,
        "profile": profile.model_dump(mode="json"),
        "keywords": [item.model_dump(mode="json") for item in keywords],
    })


@app.route("/api/resumes/latest")
def api_resume_latest():
    resume = ProfileStore(DB_PATH).get_latest_resume()
    return jsonify(resume or {})


@app.route("/api/keywords", methods=["GET", "POST"])
def api_keywords():
    store = ProfileStore(DB_PATH)
    if request.method == "POST":
        payload = request.get_json(force=True) or {}
        keyword = KeywordEntry(
            keyword=payload.get("keyword", ""),
            category=payload.get("category", "general"),
            priority=int(payload.get("priority", 3)),
            enabled=bool(payload.get("enabled", True)),
            source=payload.get("source", "user_added"),
        )
        if not keyword.keyword.strip():
            return jsonify({"error": "Keyword is required."}), 400
        store.upsert_keywords([keyword])
    return jsonify([item.model_dump(mode="json") for item in store.list_keywords()])


@app.route("/api/keywords/<int:keyword_id>", methods=["PATCH", "DELETE"])
def api_keyword_detail(keyword_id):
    store = ProfileStore(DB_PATH)
    if request.method == "DELETE":
        store.delete_keyword(keyword_id)
        return jsonify({"ok": True})
    updated = store.update_keyword(keyword_id, request.get_json(force=True) or {})
    if not updated:
        return jsonify({"error": "Keyword not found."}), 404
    return jsonify(updated.model_dump(mode="json"))


@app.route("/api/locations", methods=["GET", "POST"])
def api_locations():
    store = ProfileStore(DB_PATH)
    if request.method == "POST":
        payload = request.get_json(force=True) or {}
        city = payload.get("city", "").strip()
        if not city:
            return jsonify({"error": "City or location label is required."}), 400
        store.add_location(
            LocationPreference(
                city=city,
                province_state=payload.get("province_state", ""),
                country=payload.get("country", ""),
                latitude=payload.get("latitude"),
                longitude=payload.get("longitude"),
                radius_km=int(payload.get("radius_km", 25)),
                work_mode=payload.get("work_mode", "hybrid"),
            )
        )
    return jsonify([item.model_dump(mode="json") for item in store.list_locations()])


@app.route("/api/locations/<int:location_id>", methods=["DELETE"])
def api_location_detail(location_id):
    ProfileStore(DB_PATH).delete_location(location_id)
    return jsonify({"ok": True})


@app.route("/api/search/preview", methods=["POST"])
def api_search_preview():
    store = ProfileStore(DB_PATH)
    targets = _build_search_targets(
        store.list_keywords(enabled_only=True),
        store.list_locations(),
        (request.get_json(silent=True) or {}).get("platforms", ["Indeed", "LinkedIn"]),
    )
    return jsonify({"targets": targets, "task_count": len(targets)})


@app.route("/api/search/run", methods=["POST"])
def api_search_run():
    payload = request.get_json(silent=True) or {}
    platforms = payload.get("platforms", ["Indeed", "LinkedIn"])
    task_id = _create_search_task(platforms)
    worker = threading.Thread(target=_run_search_task, args=(task_id, platforms), daemon=True)
    worker.start()
    return jsonify(_get_search_task(task_id)), 202


@app.route("/api/search/status/<task_id>")
def api_search_status(task_id):
    task = _get_search_task(task_id)
    if not task:
        return jsonify({"error": "Search task not found."}), 404
    return jsonify(task)


@app.route("/api/job-matches")
def api_job_matches():
    matches = ProfileStore(DB_PATH).list_job_matches()
    return jsonify([item.model_dump(mode="json") for item in matches])


@app.route("/api/job-matches/<int:match_id>/status", methods=["PATCH"])
def api_job_match_status(match_id):
    payload = request.get_json(force=True) or {}
    status = payload.get("status", "new")
    if status not in {"new", "saved", "hidden", "applied", "notified"}:
        return jsonify({"error": "Unsupported status."}), 400
    updated = ProfileStore(DB_PATH).update_match_status(match_id, status)
    if not updated:
        return jsonify({"error": "Match not found."}), 404
    return jsonify(updated.model_dump(mode="json"))


# ── Serve frontend ───────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


async def _run_personalized_search(platforms, task_id=None):
    _update_search_task(task_id, status="running", message="Validating resume, keywords, and locations.")
    profile_store = ProfileStore(DB_PATH)
    latest_resume = profile_store.get_latest_resume()
    if not latest_resume:
        return {"error": "Upload and parse a resume before running personalized search."}

    profile = ResumeProfile(**latest_resume["parsed_json"])
    keywords = profile_store.list_keywords(enabled_only=True)
    if not keywords:
        return {"error": "Add or enable at least one keyword before searching."}

    locations = profile_store.list_locations()
    targets = _build_search_targets(keywords, locations, platforms)
    if not targets:
        return {"error": "No search targets were created."}

    _update_search_task(
        task_id,
        status="running",
        message=f"Created {len(targets)} search target(s). Scraping selected job sources.",
        target_count=len(targets),
    )
    settings = CONFIG.get("settings", {})
    scraper = ScraperEngine(
        headless=settings.get("headless", True),
        timeout=settings.get("timeout_ms", 30000),
        proxy=settings.get("proxy"),
    )
    jobs = await scraper.scrape_all(targets)
    _update_search_task(
        task_id,
        status="running",
        message=f"Scraping complete. Scoring {len(jobs)} job(s).",
        jobs_found=len(jobs),
    )
    preferred_locations = [_location_label(location) for location in locations] or ["Vancouver, BC"]

    async with JobStore(db_path=DB_PATH) as job_store:
        for index, job in enumerate(jobs, start=1):
            keyword = _infer_keyword_for_job(job, keywords)
            match = score_job_match(profile, job, keyword.keyword if keyword else "", preferred_locations)
            if keyword:
                match.keyword_id = keyword.id
            job.match_score = match.match_score
            job.match_reasoning = match.ai_reason
            await job_store.upsert_job(job)
            profile_store.upsert_job_match(match)
            if index % 10 == 0 or index == len(jobs):
                _update_search_task(task_id, matches_scored=index)

    return {
        "targets": len(targets),
        "jobs_found": len(jobs),
        "message": "Search complete.",
    }


def _build_search_targets(keywords, locations, platforms):
    queries = [item.keyword for item in keywords if item.enabled]
    location_labels = [_location_label(location) for location in locations] or ["Vancouver, BC"]
    targets = []
    for platform in platforms:
        platform_lower = platform.lower()
        for location in location_labels:
            if platform_lower == "indeed":
                targets.append({
                    "name": f"Indeed - {location}",
                    "url": "https://ca.indeed.com/jobs",
                    "type": "indeed",
                    "location": location,
                    "search_queries": queries,
                })
            elif platform_lower == "linkedin":
                targets.append({
                    "name": f"LinkedIn - {location}",
                    "url": "https://www.linkedin.com/jobs/search/",
                    "type": "linkedin",
                    "location": location,
                    "search_queries": queries,
                })
    return targets


def _location_label(location):
    parts = [location.city, location.province_state, location.country]
    return ", ".join(part for part in parts if part)


def _infer_keyword_for_job(job, keywords):
    text = f"{job.title} {job.description}".lower()
    for keyword in keywords:
        if keyword.keyword.lower() in text:
            return keyword
    for keyword in keywords:
        terms = [term for term in keyword.keyword.lower().split() if len(term) > 3]
        if any(term in text for term in terms):
            return keyword
    return keywords[0] if keywords else None


def _run_search_task(task_id, platforms):
    try:
        result = asyncio.run(_run_personalized_search(platforms=platforms, task_id=task_id))
        if result.get("error"):
            _update_search_task(task_id, status="failed", message=result["error"], result=result)
        else:
            _update_search_task(task_id, status="completed", message=result["message"], result=result)
    except Exception as exc:
        _update_search_task(task_id, status="failed", message=str(exc))


def _create_search_task(platforms):
    task_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    task = {
        "task_id": task_id,
        "status": "queued",
        "message": "Search queued.",
        "platforms": platforms,
        "target_count": 0,
        "jobs_found": 0,
        "matches_scored": 0,
        "created_at": now,
        "updated_at": now,
        "result": None,
    }
    with SEARCH_TASK_LOCK:
        SEARCH_TASKS[task_id] = task
    return task_id


def _update_search_task(task_id, **changes):
    if not task_id:
        return
    with SEARCH_TASK_LOCK:
        task = SEARCH_TASKS.get(task_id)
        if not task:
            return
        task.update(changes)
        task["updated_at"] = datetime.now(timezone.utc).isoformat()


def _get_search_task(task_id):
    with SEARCH_TASK_LOCK:
        task = SEARCH_TASKS.get(task_id)
        return dict(task) if task else None


def _resume_llm_api_key():
    settings = CONFIG.get("settings", {})
    configured = settings.get("gemini_api_key", "")
    if configured and configured != "YOUR_GEMINI_API_KEY":
        return configured
    return os.environ.get("GEMINI_API_KEY", "")


if __name__ == "__main__":
    print("\n  🚀  Job Scraper Dashboard running at  http://localhost:5050\n")
    app.run(host="0.0.0.0", port=5050, debug=True)
