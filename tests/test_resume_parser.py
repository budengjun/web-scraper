from resume_parser import build_keyword_entries, extract_resume_text, parse_resume_profile
from resume_llm_parser import parse_resume_profile_with_optional_llm


def test_extract_resume_text_supports_txt_for_local_testing():
    text = extract_resume_text(b"Yaolong Hu\nPython React Machine Learning\n", "resume.txt")
    assert "Python" in text


def test_parse_resume_profile_generates_structured_keywords():
    raw = """
    Yaolong Hu
    UBC Computer Science
    Skills: Python, FastAPI, React, SQL, Machine Learning, PyTorch
    Projects: Built a Job Scraper and Photo Denoising Web App
    Experience: Software Developer Intern
    Courses: CPSC 304, CPSC 322, CPSC 330
    """

    profile = parse_resume_profile(raw)

    assert profile.name == "Yaolong Hu"
    assert "Python" in profile.skills
    assert "Machine Learning" in profile.skills
    assert "CPSC 304" in profile.courses
    assert "machine learning intern" in profile.search_keywords


def test_build_keyword_entries_adds_categories_and_priorities():
    profile = parse_resume_profile("Python React SQL Machine Learning")
    entries = build_keyword_entries(profile)

    assert entries
    assert all(entry.enabled for entry in entries)
    assert {entry.category for entry in entries} & {"software", "data", "ml_research"}


def test_optional_llm_parser_falls_back_without_api_key():
    profile = parse_resume_profile_with_optional_llm("Yaolong Hu\nPython SQL React", api_key="")

    assert profile.name == "Yaolong Hu"
    assert "Python" in profile.skills
