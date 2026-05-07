"""Tests for ScraperEngine's _parse_intercepted_data method."""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from scraper import ScraperEngine


class _FakeLocator:
    def __init__(self, text="", href=None, children=None):
        self.text = text
        self.href = href
        self.children = children or {}

    @property
    def first(self):
        return self

    def locator(self, selector):
        return self.children.get(selector, _FakeLocator())

    async def all(self):
        return self.children.get("__all__", [])

    async def inner_text(self):
        return self.text

    async def get_attribute(self, name):
        return self.href if name == "href" else None

    async def count(self):
        return 1 if self.text or self.href or self.children else 0


class _FakePage:
    def __init__(self, url, locators):
        self.url = url
        self.locators = locators

    def locator(self, selector):
        return self.locators.get(selector, _FakeLocator())

    async def goto(self, *args, **kwargs):
        return None

    async def wait_for_timeout(self, *args, **kwargs):
        return None


class TestParseInterceptedData:
    """Test API interception parsing with various JSON structures."""

    def _engine(self):
        return ScraperEngine(headless=True, timeout=5000)

    def test_lever_style_list(self):
        """Lever returns a flat list of posting objects."""
        engine = self._engine()
        engine.intercepted_data = [
            {
                "url": "https://api.lever.co/v0/postings/acme",
                "data": [
                    {
                        "text": "Senior ML Engineer",
                        "hostedUrl": "https://jobs.lever.co/acme/abc123",
                        "categories": {"location": "Vancouver, BC"},
                        "descriptionPlain": "Build ML systems at scale.",
                        "createdAt": 1700000000000,
                    },
                    {
                        "text": "Frontend Developer",
                        "hostedUrl": "https://jobs.lever.co/acme/def456",
                        "categories": {"location": "Remote"},
                    },
                ],
            }
        ]

        jobs = engine._parse_intercepted_data("Acme Corp", "https://api.lever.co")
        assert len(jobs) == 2
        assert jobs[0].title == "Senior ML Engineer"
        assert jobs[0].location == "Vancouver, BC"
        assert jobs[0].description == "Build ML systems at scale."
        assert jobs[0].company == "Acme Corp"
        assert "lever.co" in jobs[0].apply_link
        assert jobs[1].title == "Frontend Developer"

    def test_greenhouse_style_nested(self):
        """Greenhouse wraps jobs in a { "jobs": [...] } object."""
        engine = self._engine()
        engine.intercepted_data = [
            {
                "url": "https://boards-api.greenhouse.io/v1/boards/acme/jobs",
                "data": {
                    "jobs": [
                        {
                            "title": "Data Scientist",
                            "absolute_url": "https://boards.greenhouse.io/acme/jobs/111",
                            "location": {"name": "Toronto, ON"},
                            "updated_at": "2025-01-15T10:00:00Z",
                        }
                    ]
                },
            }
        ]

        jobs = engine._parse_intercepted_data("Acme", "https://boards-api.greenhouse.io")
        assert len(jobs) == 1
        assert jobs[0].title == "Data Scientist"
        assert jobs[0].location == "Toronto, ON"
        assert "greenhouse.io" in jobs[0].apply_link

    def test_empty_intercepted_data(self):
        """No intercepted data returns empty list."""
        engine = self._engine()
        engine.intercepted_data = []
        assert engine._parse_intercepted_data("X", "https://example.com") == []

    def test_non_job_api_responses_ignored(self):
        """Intercepted data without recognizable job fields is skipped."""
        engine = self._engine()
        engine.intercepted_data = [
            {
                "url": "https://api.example.com/analytics",
                "data": {"page_views": 1234, "sessions": 56},
            }
        ]
        jobs = engine._parse_intercepted_data("Example", "https://api.example.com")
        assert len(jobs) == 0

    def test_mixed_valid_and_invalid_records(self):
        """Parser extracts valid records and skips invalid ones."""
        engine = self._engine()
        engine.intercepted_data = [
            {
                "url": "https://api.example.com/jobs",
                "data": {
                    "results": [
                        {"title": "Valid Job", "url": "https://example.com/j/1"},
                        {"not_a_title": "Missing title field"},
                        "just a string, not a dict",
                    ]
                },
            }
        ]

        jobs = engine._parse_intercepted_data("Example", "https://api.example.com")
        assert len(jobs) == 1
        assert jobs[0].title == "Valid Job"

    def test_unix_timestamp_parsed(self):
        """Unix millisecond timestamps are parsed to datetime."""
        engine = self._engine()
        engine.intercepted_data = [
            {
                "url": "https://api.lever.co/v0/postings/co",
                "data": [
                    {
                        "text": "Engineer",
                        "hostedUrl": "https://example.com",
                        "createdAt": 1700000000000,
                    }
                ],
            }
        ]

        jobs = engine._parse_intercepted_data("Co", "https://api.lever.co")
        assert len(jobs) == 1
        assert jobs[0].posted_date is not None
        assert isinstance(jobs[0].posted_date, datetime)


class TestDomScrapers:
    """Test DOM scraper link extraction without launching a browser."""

    @pytest.mark.asyncio
    async def test_workday_scraper_extracts_title_href(self):
        engine = ScraperEngine(headless=True, timeout=5000)
        engine._handle_pagination = AsyncMock()

        job_el = _FakeLocator(children={
            "h3 a": _FakeLocator(text="Software Intern", href="/job/software-intern"),
            "dd.css-129m7dg": _FakeLocator(text="Vancouver, BC"),
        })
        page = _FakePage(
            "https://company.wd1.myworkdayjobs.com/Careers",
            {"li.css-1q2dra3": _FakeLocator(children={"__all__": [job_el]})},
        )

        jobs = await engine._scrape_workday(page, "Company")

        assert len(jobs) == 1
        assert jobs[0].title == "Software Intern"
        assert jobs[0].apply_link == "https://company.wd1.myworkdayjobs.com/job/software-intern"

    @pytest.mark.asyncio
    async def test_indeed_scraper_extracts_title_href(self):
        engine = ScraperEngine(headless=True, timeout=5000)

        card = _FakeLocator(children={
            "h2.jobTitle a, h2 a, .jobTitle a, a[data-jk]": _FakeLocator(
                text="Data Science Intern",
                href="/viewjob?jk=abc123&from=serp",
            ),
            "span[data-testid='company-name'], .companyName, .company": _FakeLocator(text="DataCo"),
            "div[data-testid='text-location'], .companyLocation, .location": _FakeLocator(text="Vancouver, BC"),
            ".job-snippet, .underShelfFooter, li": _FakeLocator(text="Python and ML internship."),
        })
        page = _FakePage(
            "https://ca.indeed.com/jobs?q=Data+Science+Intern",
            {".job_seen_beacon": _FakeLocator(children={"__all__": [card]})},
        )

        jobs = await engine._scrape_indeed(
            page,
            "Indeed",
            {"search_queries": ["Data Science Intern"], "location": "Vancouver, BC"},
        )

        assert len(jobs) == 1
        assert jobs[0].title == "Data Science Intern"
        assert jobs[0].company == "DataCo"
        assert jobs[0].apply_link == "https://ca.indeed.com/viewjob?jk=abc123"
