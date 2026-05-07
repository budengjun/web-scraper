import json
import logging
from typing import Optional

from career_models import ResumeProfile
from resume_parser import parse_resume_profile

logger = logging.getLogger(__name__)

RESUME_PROFILE_SCHEMA = {
    "name": "string or null",
    "education": ["string"],
    "skills": ["string"],
    "projects": ["string"],
    "experience": ["string"],
    "courses": ["string"],
    "target_roles": ["string"],
    "search_keywords": ["string"],
}


def parse_resume_profile_with_optional_llm(
    raw_text: str,
    api_key: Optional[str] = None,
    model_name: str = "gemini-2.0-flash-lite",
) -> ResumeProfile:
    """Use an LLM JSON extractor when configured, otherwise fall back to deterministic parsing."""
    fallback_profile = parse_resume_profile(raw_text)
    if not api_key or api_key == "YOUR_GEMINI_API_KEY":
        return fallback_profile

    try:
        llm_profile = _parse_with_gemini(raw_text, api_key, model_name)
        return _merge_with_fallback(llm_profile, fallback_profile)
    except Exception as exc:
        logger.warning("LLM resume parsing failed; using rule-based fallback: %s", exc)
        return fallback_profile


def _parse_with_gemini(raw_text: str, api_key: str, model_name: str) -> ResumeProfile:
    try:
        from google import genai
    except ImportError as exc:
        raise RuntimeError("google-genai is required for LLM resume parsing.") from exc

    prompt = f"""
Extract a structured resume profile from this resume text.
Return only valid JSON with this schema:
{json.dumps(RESUME_PROFILE_SCHEMA, indent=2)}

Guidelines:
- Keep arrays concise and deduplicated.
- Generate practical internship job-search keywords.
- Do not invent skills, projects, or experience not supported by the resume text.

Resume text:
{raw_text[:12000]}
"""

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model_name,
        contents=prompt,
        config={"response_mime_type": "application/json"},
    )
    payload = _load_json_response(response.text)
    return ResumeProfile(**payload)


def _load_json_response(text: str) -> dict:
    cleaned = (text or "").strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return json.loads(cleaned.strip())


def _merge_with_fallback(llm_profile: ResumeProfile, fallback_profile: ResumeProfile) -> ResumeProfile:
    return ResumeProfile(
        name=llm_profile.name or fallback_profile.name,
        education=llm_profile.education or fallback_profile.education,
        skills=_dedupe(llm_profile.skills or fallback_profile.skills),
        projects=llm_profile.projects or fallback_profile.projects,
        experience=llm_profile.experience or fallback_profile.experience,
        courses=_dedupe(llm_profile.courses or fallback_profile.courses),
        target_roles=_dedupe(llm_profile.target_roles or fallback_profile.target_roles),
        search_keywords=_dedupe(llm_profile.search_keywords or fallback_profile.search_keywords),
    )


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item.strip() for item in items if item and item.strip()))
