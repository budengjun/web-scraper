from typing import Iterable

from career_models import JobMatch, ResumeProfile
from models import Job

SENIORITY_NEGATIVE = ["senior", "staff", "principal", "director", "manager", "lead"]
ENTRY_SIGNALS = ["intern", "co-op", "coop", "internship", "new grad", "junior", "entry"]


def score_job_match(
    profile: ResumeProfile,
    job: Job,
    keyword: str = "",
    preferred_locations: Iterable[str] = (),
) -> JobMatch:
    haystack = " ".join([job.title or "", job.company or "", job.location or "", job.description or ""]).lower()
    keyword_lower = keyword.lower()

    matched_skills = [skill for skill in profile.skills if skill.lower() in haystack]
    missing_skills = [skill for skill in profile.skills[:8] if skill not in matched_skills]

    score = 35
    score += min(30, len(matched_skills) * 6)
    if keyword_lower and keyword_lower in haystack:
        score += 20
    elif keyword_lower and any(part in haystack for part in keyword_lower.split() if len(part) > 3):
        score += 10
    if any(signal in haystack for signal in ENTRY_SIGNALS):
        score += 15
    if any(signal in haystack for signal in SENIORITY_NEGATIVE):
        score -= 35
    if _location_matches(job.location, preferred_locations):
        score += 10

    score = max(0, min(100, score))
    reason = _build_reason(job, matched_skills, missing_skills, keyword, score)

    return JobMatch(
        job_apply_link=job.apply_link,
        match_score=score,
        matched_skills=matched_skills,
        missing_skills=missing_skills[:5],
        ai_reason=reason,
        status="new",
    )


def _location_matches(job_location: str, preferred_locations: Iterable[str]) -> bool:
    job_lower = (job_location or "").lower()
    for location in preferred_locations:
        loc_lower = location.lower()
        if "remote" in loc_lower and "remote" in job_lower:
            return True
        if loc_lower and (loc_lower in job_lower or job_lower in loc_lower):
            return True
    return False


def _build_reason(job: Job, matched_skills: list[str], missing_skills: list[str], keyword: str, score: int) -> str:
    parts = []
    if keyword:
        parts.append(f"Matched search intent: {keyword}.")
    if matched_skills:
        parts.append(f"Resume skills found in job: {', '.join(matched_skills[:5])}.")
    if missing_skills:
        parts.append(f"Potential gaps: {', '.join(missing_skills[:3])}.")
    if any(signal in (job.title or "").lower() for signal in ENTRY_SIGNALS):
        parts.append("Seniority appears aligned with internship or entry-level search.")
    if score >= 80:
        parts.append("Strong resume-job fit.")
    elif score >= 60:
        parts.append("Moderate fit worth reviewing.")
    else:
        parts.append("Lower fit based on current resume signals.")
    return " ".join(parts)
