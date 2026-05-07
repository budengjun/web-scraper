from career_models import JobMatch, KeywordEntry, LocationPreference, ResumeProfile
from profile_store import ProfileStore


def test_profile_store_saves_resume_keywords_locations_and_matches(tmp_path):
    store = ProfileStore(str(tmp_path / "profiles.db"))
    profile = ResumeProfile(
        name="Test User",
        skills=["Python", "SQL"],
        search_keywords=["software developer intern"],
    )

    resume_id = store.save_resume("resume.txt", "Python SQL", profile)
    latest = store.get_latest_resume()

    assert latest["id"] == resume_id
    assert latest["parsed_json"]["skills"] == ["Python", "SQL"]

    store.upsert_keywords([
        KeywordEntry(keyword="software developer intern", category="software", priority=5)
    ])
    keywords = store.list_keywords(enabled_only=True)
    assert len(keywords) == 1
    assert keywords[0].keyword == "software developer intern"

    store.add_location(LocationPreference(city="Vancouver", province_state="BC", country="Canada"))
    locations = store.list_locations()
    assert locations[0].city == "Vancouver"

    store.upsert_job_match(
        JobMatch(
            job_apply_link="https://example.com/job",
            keyword_id=keywords[0].id,
            match_score=88,
            matched_skills=["Python"],
            missing_skills=["SQL"],
            ai_reason="Strong fit.",
        )
    )
    matches = store.list_job_matches()
    assert matches[0].match_score == 88
    assert matches[0].matched_skills == ["Python"]
