import re
from io import BytesIO
from pathlib import Path
from typing import Iterable, List

from career_models import KeywordEntry, ResumeProfile

MAX_RESUME_BYTES = 5 * 1024 * 1024

SKILL_CATALOG = [
    "Python", "Java", "JavaScript", "TypeScript", "React", "Vue", "Node.js",
    "FastAPI", "Flask", "Django", "SQL", "PostgreSQL", "SQLite", "MongoDB",
    "Machine Learning", "AI", "PyTorch", "TensorFlow", "Scikit-learn",
    "Pandas", "NumPy", "Docker", "AWS", "Git", "Linux", "REST API",
    "Data Analysis", "Data Science", "LLM", "LangChain",
]

COURSE_RE = re.compile(r"\b[A-Z]{2,5}\s*\d{3}\b")


def extract_resume_text(file_bytes: bytes, filename: str) -> str:
    if len(file_bytes) > MAX_RESUME_BYTES:
        raise ValueError("Resume file is too large. Maximum size is 5 MB.")

    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf_text(file_bytes)
    if suffix == ".docx":
        return _extract_docx_text(file_bytes)
    if suffix == ".txt":
        return file_bytes.decode("utf-8", errors="ignore")
    raise ValueError("Unsupported resume format. Upload a PDF, DOCX, or TXT file.")


def parse_resume_profile(raw_text: str) -> ResumeProfile:
    text = _clean_text(raw_text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    skills = _extract_skills(text)
    courses = sorted({match.group(0).replace(" ", " ") for match in COURSE_RE.finditer(text)})
    education = _extract_lines(lines, ["university", "college", "bachelor", "master", "computer science"])
    projects = _extract_lines(lines, ["project", "built", "developed", "implemented"])
    experience = _extract_lines(lines, ["intern", "assistant", "developer", "engineer", "analyst"])
    target_roles = _infer_target_roles(skills, text)
    search_keywords = _generate_search_keywords(skills, target_roles)

    return ResumeProfile(
        name=_guess_name(lines),
        education=education[:5],
        skills=skills,
        projects=projects[:8],
        experience=experience[:8],
        courses=courses[:12],
        target_roles=target_roles,
        search_keywords=search_keywords,
    )


def build_keyword_entries(profile: ResumeProfile) -> List[KeywordEntry]:
    entries = []
    for keyword in profile.search_keywords:
        entries.append(
            KeywordEntry(
                keyword=keyword,
                category=_keyword_category(keyword),
                priority=5 if any(term in keyword for term in ("intern", "co-op", "machine learning")) else 4,
                enabled=True,
                source="ai_generated",
            )
        )
    return entries


def _extract_pdf_text(file_bytes: bytes) -> str:
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PDF parsing requires PyMuPDF. Install requirements.txt first.") from exc

    with fitz.open(stream=file_bytes, filetype="pdf") as doc:
        return "\n".join(page.get_text("text") for page in doc)


def _extract_docx_text(file_bytes: bytes) -> str:
    try:
        from docx import Document
    except ImportError as exc:
        raise RuntimeError("DOCX parsing requires python-docx. Install requirements.txt first.") from exc

    doc = Document(BytesIO(file_bytes))
    return "\n".join(paragraph.text for paragraph in doc.paragraphs)


def _clean_text(raw_text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", raw_text.replace("\r\n", "\n")).strip()


def _guess_name(lines: List[str]) -> str | None:
    for line in lines[:5]:
        if 2 <= len(line.split()) <= 4 and not any(char.isdigit() for char in line):
            if not any(token in line.lower() for token in ("resume", "email", "@", "github", "linkedin")):
                return line
    return None


def _extract_skills(text: str) -> List[str]:
    text_lower = text.lower()
    found = []
    for skill in SKILL_CATALOG:
        pattern = re.escape(skill.lower()).replace(r"\ ", r"[\s\-]+")
        if re.search(rf"\b{pattern}\b", text_lower):
            found.append(skill)
    return found


def _extract_lines(lines: Iterable[str], signals: List[str]) -> List[str]:
    found = []
    for line in lines:
        lower = line.lower()
        if any(signal in lower for signal in signals) and len(line) <= 180:
            found.append(line)
    return list(dict.fromkeys(found))


def _infer_target_roles(skills: List[str], text: str) -> List[str]:
    skill_set = {skill.lower() for skill in skills}
    roles = ["Software Developer Intern"]
    if {"machine learning", "ai", "pytorch", "tensorflow", "scikit-learn"} & skill_set:
        roles.extend(["Machine Learning Intern", "AI Research Assistant"])
    if {"sql", "pandas", "numpy", "data analysis", "data science"} & skill_set:
        roles.append("Data Analyst Intern")
    if {"react", "node.js", "typescript", "javascript"} & skill_set:
        roles.append("Full Stack Developer Intern")
    if "backend" in text.lower() or {"fastapi", "flask", "django"} & skill_set:
        roles.append("Backend Developer Intern")
    return list(dict.fromkeys(roles))


def _generate_search_keywords(skills: List[str], target_roles: List[str]) -> List[str]:
    keywords = [role.lower() for role in target_roles]
    skill_set = {skill.lower() for skill in skills}
    if "python" in skill_set:
        keywords.append("python developer intern")
    if "sql" in skill_set:
        keywords.append("sql data intern")
    if "react" in skill_set:
        keywords.append("react developer intern")
    return list(dict.fromkeys(keywords))[:12]


def _keyword_category(keyword: str) -> str:
    if any(term in keyword for term in ("machine learning", "ai", "research")):
        return "ml_research"
    if any(term in keyword for term in ("data", "sql", "analyst")):
        return "data"
    if any(term in keyword for term in ("backend", "full stack", "developer", "software", "react")):
        return "software"
    return "general"
