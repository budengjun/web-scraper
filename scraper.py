import asyncio
import logging
import random
from typing import List, Dict, Any, Optional
from playwright.async_api import async_playwright, Page, Response, Browser, BrowserContext
from playwright_stealth import Stealth
from models import Job
from datetime import datetime, timezone
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from urllib.parse import urljoin, quote_plus

logger = logging.getLogger(__name__)

# ── Browser session profiles ──
_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

# Common job-related keywords for generic scraping
JOB_KEYWORDS = [
    "engineer", "developer", "designer", "analyst", "scientist",
    "manager", "architect", "devops", "sre", "intern",
    "full-stack", "fullstack", "backend", "frontend", "machine learning",
    "data", "software", "product", "qa", "security",
]

# Common description container selectors (ordered by specificity)
DESCRIPTION_SELECTORS = [
    ".job-description",
    "#job-description",
    "[data-automation-id='jobPostingDescription']",
    ".posting-page .section-wrapper",
    ".description__text",               # LinkedIn
    ".show-more-less-html__markup",      # LinkedIn
    "#jobDescriptionText",              # Indeed
    ".content-intro",
    "#content",
    "article",
    "main",
]


class ScraperEngine:
    def __init__(self, headless: bool = True, timeout: int = 30000, proxy: str = None):
        self.headless = headless
        self.timeout = timeout
        self.proxy = proxy  # e.g. "http://user:pass@host:port"
        self.intercepted_data: List[Dict[str, Any]] = []

    # ──────────────────────────────────────────────
    # Browser interaction helpers for dynamic pages
    # ──────────────────────────────────────────────

    @staticmethod
    async def _random_delay(min_s: float = 1.0, max_s: float = 3.5):
        """Wait between page actions to keep requests rate-limited."""
        await asyncio.sleep(random.uniform(min_s, max_s))

    @staticmethod
    async def _scroll_results_page(page: Page, scroll_count: int = 3):
        """Scroll dynamic result pages to load additional listings."""
        for i in range(scroll_count):
            # Variable scroll distance
            distance = random.randint(400, 900)
            await page.evaluate(f"window.scrollBy(0, {distance})")
            await asyncio.sleep(random.uniform(0.8, 2.2))

    @staticmethod
    async def _random_mouse_move(page: Page):
        """Optional pointer interaction for pages that depend on user events."""
        try:
            x = random.randint(100, 800)
            y = random.randint(100, 500)
            await page.mouse.move(x, y)
            await asyncio.sleep(random.uniform(0.1, 0.4))
        except Exception:
            pass  # Mouse move is best-effort

    @staticmethod
    async def _is_unavailable_result_page(page: Page) -> bool:
        """Detect login walls, verification pages, or unavailable result pages."""
        try:
            url = page.url
            # LinkedIn login redirect
            if "linkedin.com/authwall" in url or "linkedin.com/checkpoint" in url:
                logger.warning("⚠️  LinkedIn login wall detected. Rotating to next query.")
                return True
            # Check for verification or unavailable-page text
            body_text = await page.inner_text("body")
            unavailable_signals = [
                "Sign in to view", "Join now to see",
                "Let's do a quick security check", "unusual activity",
                "verify you're a real person",
            ]
            if any(sig.lower() in body_text.lower() for sig in unavailable_signals):
                logger.warning("⚠️  Verification page detected. Skipping.")
                return True
        except Exception:
            pass
        return False

    # ──────────────────────────────────────────────
    # API Interception
    # ──────────────────────────────────────────────

    async def _handle_response(self, response: Response):
        """API Interception: capture JSON responses from career-related XHR/fetch calls."""
        url = response.url
        if (
            any(kw in url for kw in ("graphql", "api", "jobs", "postings", "career"))
            and response.request.resource_type in ["fetch", "xhr"]
        ):
            try:
                content_type = response.headers.get("content-type", "")
                if "application/json" in content_type:
                    data = await response.json()
                    self.intercepted_data.append({"url": url, "data": data})
                    logger.debug(f"Intercepted API response from {url}")
            except Exception:
                pass

    def _parse_intercepted_data(self, company_name: str, base_url: str) -> List[Job]:
        """
        Attempt to extract Job objects from intercepted API JSON payloads.
        Handles common structures: lists of objects with title/name + url/link fields,
        as well as nested structures used by Lever, Greenhouse, and Workday.
        """
        jobs: List[Job] = []
        now = datetime.now(timezone.utc)

        for entry in self.intercepted_data:
            data = entry["data"]
            records = []

            # Unwrap common nesting patterns
            if isinstance(data, list):
                records = data
            elif isinstance(data, dict):
                # Greenhouse: { "jobs": [...] }
                # Workday: { "jobPostings": [...] }
                # Lever: direct list or { "postings": [...] }
                for key in ("jobs", "postings", "jobPostings", "results", "data", "items"):
                    if key in data and isinstance(data[key], list):
                        records = data[key]
                        break

            for rec in records:
                if not isinstance(rec, dict):
                    continue
                # Extract title
                title = rec.get("title") or rec.get("text") or rec.get("name") or ""
                if not title:
                    continue

                # Extract link
                link = (
                    rec.get("hostedUrl")          # Lever
                    or rec.get("absolute_url")     # Greenhouse
                    or rec.get("applyUrl")
                    or rec.get("url")
                    or rec.get("externalPath")     # Workday
                    or ""
                )

                # Extract location
                location = ""
                loc_obj = rec.get("categories", {})
                if isinstance(loc_obj, dict):
                    location = loc_obj.get("location", "")
                if not location:
                    location = rec.get("location", {})
                    if isinstance(location, dict):
                        location = location.get("name", "")
                    elif not isinstance(location, str):
                        location = ""

                # Extract description (some APIs include it inline)
                description = rec.get("description") or rec.get("descriptionPlain") or rec.get("content") or ""

                # Extract posted date
                posted_date = None
                for date_key in ("createdAt", "updated_at", "postedDate", "published_at"):
                    raw = rec.get(date_key)
                    if raw:
                        try:
                            # Handle Unix ms timestamps
                            if isinstance(raw, (int, float)):
                                posted_date = datetime.fromtimestamp(raw / 1000, tz=timezone.utc)
                            else:
                                posted_date = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                        except Exception:
                            pass
                        break

                if title:
                    # Normalize link
                    full_link = link or ""
                    if full_link and not full_link.startswith("http"):
                        full_link = urljoin(base_url, full_link)

                    jobs.append(Job(
                        title=title.strip(),
                        company=company_name,
                        location=location.strip() if location else "See listing",
                        description=description[:8000] if description else "",
                        apply_link=full_link,
                        posted_date=posted_date or now,
                    ))

        return jobs

    # ──────────────────────────────────────────────
    # Public entry point — reuses one browser
    # ──────────────────────────────────────────────

    async def scrape_all(self, targets: List[Dict[str, Any]]) -> List[Job]:
        """Scrape all targets using a single shared browser instance."""
        all_jobs: List[Job] = []

        async with async_playwright() as p:
            launch_opts = {"headless": self.headless}
            if self.proxy:
                launch_opts["proxy"] = {"server": self.proxy}
                logger.info(f"Using proxy: {self.proxy[:30]}...")

            browser = await p.chromium.launch(**launch_opts)

            # Randomized browser fingerprint per session
            ua = random.choice(_USER_AGENTS)
            context = await browser.new_context(
                user_agent=ua,
                viewport={"width": random.choice([1366, 1440, 1536, 1920]), "height": random.choice([768, 900, 1024, 1080])},
                device_scale_factor=2,
                has_touch=False,
                is_mobile=False,
                locale="en-US",
                timezone_id="America/Vancouver",
            )
            logger.debug(f"Browser session UA: {ua[:60]}...")

            for target in targets:
                jobs = await self._scrape_target(context, target)
                logger.info(f"✅ Found {len(jobs)} jobs for {target['name']}")
                all_jobs.extend(jobs)

            await browser.close()

        return all_jobs

    # Kept for backward compat — opens its own browser
    async def scrape_target(self, target: Dict[str, Any]) -> List[Job]:
        return await self.scrape_all([target])

    # ──────────────────────────────────────────────
    # Internal target scraping
    # ──────────────────────────────────────────────

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=15),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    async def _scrape_target(self, context: BrowserContext, target: Dict[str, Any]) -> List[Job]:
        company_name = target["name"]
        url = target["url"]
        platform_type = target.get("type", "custom")
        selectors = target.get("selectors", {})
        self.intercepted_data = []

        logger.info(f"Starting scrape for {company_name} ({platform_type}) at {url}")

        page = await context.new_page()
        stealth = Stealth()
        await stealth.apply_stealth_async(page)
        page.on("response", self._handle_response)

        scraped_jobs: List[Job] = []

        try:
            # ── LinkedIn and Indeed use search-based scraping (skip API interception) ──
            if platform_type == "linkedin":
                scraped_jobs = await self._scrape_linkedin(page, company_name, target)
            elif platform_type == "indeed":
                scraped_jobs = await self._scrape_indeed(page, company_name, target)
            else:
                await page.goto(url, wait_until="networkidle", timeout=self.timeout)

                # ── Phase 1: Try intercepted API data first ──
                api_jobs = self._parse_intercepted_data(company_name, url)
                if api_jobs:
                    logger.info(
                        f"📡 Parsed {len(api_jobs)} jobs from intercepted API data for {company_name}"
                    )
                    scraped_jobs = api_jobs
                else:
                    # ── Phase 2: Fall back to DOM parsing ──
                    if platform_type == "workday":
                        scraped_jobs = await self._scrape_workday(page, company_name, selectors)
                    elif platform_type in ("lever", "greenhouse"):
                        scraped_jobs = await self._scrape_lever_greenhouse(page, company_name, selectors)
                    else:
                        scraped_jobs = await self._scrape_custom(page, company_name, selectors)

                # ── Phase 3: Fetch real descriptions for jobs that lack them ──
                for job in scraped_jobs:
                    if not job.description or job.description.startswith("Description") or job.description.startswith("Detail"):
                        if job.apply_link and job.apply_link.startswith("http"):
                            desc = await self._fetch_job_description(page, job.apply_link)
                            if desc:
                                job.description = desc

        except Exception as e:
            logger.error(f"Failed to scrape {company_name}: {e}")
        finally:
            await page.close()

        return scraped_jobs

    # ──────────────────────────────────────────────
    # Platform-specific scrapers
    # ──────────────────────────────────────────────

    async def _scrape_workday(self, page: Page, company_name: str, selectors: Dict = None) -> List[Job]:
        """Handle Workday: dynamic loading, Load More buttons, infinite scroll."""
        selectors = selectors or {}
        jobs: List[Job] = []

        try:
            # Handle pagination / infinite scroll
            await self._handle_pagination(page, selectors)

            job_list_sel = selectors.get("job_list", "li.css-1q2dra3")
            title_sel = selectors.get("title", "h3 a")
            location_sel = selectors.get("location", "dd.css-129m7dg")

            job_elements = await page.locator(job_list_sel).all()

            for el in job_elements:
                try:
                    title_el = el.locator(title_sel).first
                    title = await title_el.inner_text()
                    link = await title_el.get_attribute("href")
                    full_link = urljoin(page.url, link) if link else page.url

                    location = "Check Listing"
                    loc_el = el.locator(location_sel)
                    if await loc_el.count() > 0:
                        location = await loc_el.first.inner_text()

                    jobs.append(Job(
                        title=title.strip(),
                        company=company_name,
                        location=location.strip(),
                        description="",
                        apply_link=full_link or page.url,
                        posted_date=datetime.now(timezone.utc),
                    ))
                except Exception as e:
                    logger.warning(f"Error parsing a Workday job element: {e}")

        except Exception as e:
            logger.error(f"Workday scraping error: {e}")

        return jobs

    async def _scrape_lever_greenhouse(self, page: Page, company_name: str, selectors: Dict = None) -> List[Job]:
        """Handle Greenhouse/Lever: simpler DOM structure."""
        selectors = selectors or {}
        jobs: List[Job] = []

        try:
            posting_sel = selectors.get("job_list", ".posting, .level-0, .opening")
            title_sel = selectors.get("title", "h5, a")
            location_sel = selectors.get("location", ".sort-by-location, .location")

            posting_elements = await page.locator(posting_sel).all()
            for posting in posting_elements:
                try:
                    title_el = posting.locator(title_sel)
                    title = await title_el.first.inner_text()
                    link = await title_el.first.get_attribute("href")

                    loc_text = "Remote/Vancouver"
                    loc_el = posting.locator(location_sel)
                    if await loc_el.count() > 0:
                        loc_text = await loc_el.first.inner_text()

                    jobs.append(Job(
                        title=title.strip(),
                        company=company_name,
                        location=loc_text.strip(),
                        description="",
                        apply_link=urljoin(page.url, link) if link else page.url,
                        posted_date=datetime.now(timezone.utc),
                    ))
                except Exception as e:
                    logger.debug(f"Skipping Lever/Greenhouse element: {e}")

        except Exception as e:
            logger.error(f"Lever/Greenhouse scraping error: {e}")

        return jobs

    async def _scrape_custom(self, page: Page, company_name: str, selectors: Dict = None) -> List[Job]:
        """Generic fallback: find anchor tags whose text contains job-related keywords."""
        selectors = selectors or {}
        jobs: List[Job] = []

        logger.info(f"Running generic scraper for {company_name}")

        try:
            # Use custom selectors if provided
            if "job_list" in selectors and "title" in selectors:
                container_els = await page.locator(selectors["job_list"]).all()
                for el in container_els:
                    try:
                        title_el = el.locator(selectors["title"])
                        title = await title_el.first.inner_text()
                        link = await title_el.first.get_attribute("href")

                        location = ""
                        if "location" in selectors:
                            loc_el = el.locator(selectors["location"])
                            if await loc_el.count() > 0:
                                location = await loc_el.first.inner_text()

                        if link and not link.startswith("http"):
                            base = page.url.rstrip("/")
                            link = f"{base}/{link.lstrip('/')}"

                        jobs.append(Job(
                            title=title.strip(),
                            company=company_name,
                            location=location.strip() or "See listing",
                            description="",
                            apply_link=link or page.url,
                            posted_date=datetime.now(timezone.utc),
                        ))
                    except Exception as e:
                        logger.debug(f"Skipping custom element: {e}")
            else:
                # Keyword-based link extraction as last resort
                links = await page.locator("a").all()
                for link_el in links:
                    try:
                        text = (await link_el.inner_text()).strip()
                        href = await link_el.get_attribute("href")
                        if not text or not href:
                            continue
                        text_lower = text.lower()
                        if any(kw in text_lower for kw in JOB_KEYWORDS):
                            full_url = href
                            if not href.startswith("http"):
                                base = page.url.rstrip("/")
                                full_url = f"{base}/{href.lstrip('/')}"

                            jobs.append(Job(
                                title=text,
                                company=company_name,
                                location="See listing",
                                description="",
                                apply_link=full_url,
                                posted_date=datetime.now(timezone.utc),
                            ))
                    except Exception:
                        continue

        except Exception as e:
            logger.error(f"Custom scraping error for {company_name}: {e}")

        return jobs

    # ──────────────────────────────────────────────
    # LinkedIn & Indeed scrapers
    # ──────────────────────────────────────────────

    async def _scrape_linkedin(self, page: Page, company_name: str, target: Dict) -> List[Job]:
        """
        Scrape LinkedIn's public job search (no login required).
        Iterates through search_queries to find intern/co-op roles.
        """
        search_queries = target.get("search_queries", ["Software Engineer Intern"])
        location = target.get("location", "Vancouver, BC, Canada")
        jobs: List[Job] = []
        seen_links: set = set()

        for query in search_queries:
            encoded_query = query.replace(" ", "%20")
            encoded_location = location.replace(" ", "%20").replace(",", "%2C")
            search_url = (
                f"https://www.linkedin.com/jobs/search/"
                f"?keywords={encoded_query}"
                f"&location={encoded_location}"
                f"&f_TPR=r604800"   # Last 7 days (24h too narrow for Vancouver)
                f"&sortBy=DD"      # Sort by date
            )

            logger.info(f"LinkedIn search: '{query}' in {location}")

            try:
                await page.goto(search_url, wait_until="domcontentloaded", timeout=self.timeout)
                await self._random_delay(2.5, 4.5)  # Human-like wait after page load

                # Check for anti-bot blocks
                if await self._is_unavailable_result_page(page):
                    continue

                # Interact with dynamic result pages before reading cards
                await self._random_mouse_move(page)
                await self._scroll_results_page(page, scroll_count=random.randint(2, 4))

                # Try multiple selector patterns (LinkedIn changes these frequently)
                card_selectors = [
                    ".base-card",
                    ".job-search-card",
                    ".jobs-search__results-list li",
                    "[data-entity-urn]",
                ]

                cards = []
                for sel in card_selectors:
                    cards = await page.locator(sel).all()
                    if cards:
                        break

                for card in cards:
                    try:
                        # Title
                        title_el = card.locator("h3, .base-search-card__title, .job-search-card__title").first
                        title = (await title_el.inner_text()).strip()

                        # Link
                        link_el = card.locator("a.base-card__full-link, a[data-tracking-control-name]").first
                        link = await link_el.get_attribute("href") if await link_el.count() > 0 else None
                        if not link:
                            link_el = card.locator("a").first
                            link = await link_el.get_attribute("href") if await link_el.count() > 0 else None

                        if not link or link in seen_links:
                            continue
                        seen_links.add(link)

                        # Company
                        company = company_name
                        company_el = card.locator("h4, .base-search-card__subtitle, .job-search-card__company-name")
                        if await company_el.count() > 0:
                            company = (await company_el.first.inner_text()).strip() or company_name

                        # Location
                        loc = location
                        loc_el = card.locator(".job-search-card__location, .base-search-card__metadata span")
                        if await loc_el.count() > 0:
                            loc = (await loc_el.first.inner_text()).strip() or location

                        full_link = urljoin(page.url, link) if link else page.url
                        jobs.append(Job(
                            title=title,
                            company=company,
                            location=loc,
                            description=f"[LinkedIn] Search: {query}",
                            apply_link=full_link.split("?")[0],  # Clean tracking params
                            posted_date=datetime.now(timezone.utc),
                        ))
                    except Exception as e:
                        logger.debug(f"Skipping LinkedIn card: {e}")
                        continue

                logger.info(f"  → Found {len(cards)} cards for query '{query}'")

            except Exception as e:
                logger.warning(f"LinkedIn search failed for '{query}': {e}")

            # Rate-limit requests between searches
            await self._random_delay(3.0, 7.0)

        logger.info(f"LinkedIn total: {len(jobs)} unique jobs across {len(search_queries)} queries")
        return jobs

    async def _scrape_indeed(self, page: Page, company_name: str, target: Dict) -> List[Job]:
        """
        Scrape Indeed Canada's job search.
        Iterates through search_queries to find intern/co-op roles.
        """
        search_queries = target.get("search_queries", ["Software Intern"])
        location = target.get("location", "Vancouver, BC")
        jobs: List[Job] = []
        seen_links: set = set()

        for query in search_queries:
            encoded_query = query.replace(" ", "+")
            encoded_location = location.replace(" ", "+").replace(",", "%2C")
            search_url = (
                f"https://ca.indeed.com/jobs"
                f"?q={encoded_query}"
                f"&l={encoded_location}"
                f"&fromage=7"      # Last 7 days (24h too narrow for Vancouver)
                f"&sort=date"      # Sort by date
            )

            logger.info(f"Indeed search: '{query}' in {location}")

            try:
                await page.goto(search_url, wait_until="domcontentloaded", timeout=self.timeout)
                await page.wait_for_timeout(3000)

                # Indeed card selectors (they change frequently)
                card_selectors = [
                    ".job_seen_beacon",
                    ".resultContent",
                    ".jobsearch-ResultsList > li",
                    "div.cardOutline",
                    "td.resultContent",
                ]

                cards = []
                for sel in card_selectors:
                    cards = await page.locator(sel).all()
                    if cards:
                        break

                for card in cards:
                    try:
                        # Title
                        title_el = card.locator("h2.jobTitle a, h2 a, .jobTitle a, a[data-jk]").first
                        title = (await title_el.inner_text()).strip()

                        # Link
                        link = await title_el.get_attribute("href")
                        link = urljoin(page.url, link) if link else None

                        if not link or link in seen_links:
                            continue
                        seen_links.add(link)

                        # Company
                        company = company_name
                        company_el = card.locator("span[data-testid='company-name'], .companyName, .company")
                        if await company_el.count() > 0:
                            company = (await company_el.first.inner_text()).strip() or company_name

                        # Location
                        loc = location
                        loc_el = card.locator("div[data-testid='text-location'], .companyLocation, .location")
                        if await loc_el.count() > 0:
                            loc = (await loc_el.first.inner_text()).strip() or location

                        # Snippet (Indeed shows a short summary)
                        snippet = ""
                        snippet_el = card.locator(".job-snippet, .underShelfFooter, li")
                        if await snippet_el.count() > 0:
                            snippet = (await snippet_el.first.inner_text()).strip()

                        jobs.append(Job(
                            title=title,
                            company=company,
                            location=loc,
                            description=f"[Indeed] {snippet}" if snippet else f"[Indeed] Search: {query}",
                            apply_link=link.split("&")[0] if "&" in link else link,  # Clean
                            posted_date=datetime.now(timezone.utc),
                        ))
                    except Exception as e:
                        logger.debug(f"Skipping Indeed card: {e}")
                        continue

                logger.info(f"  → Found {len(cards)} cards for query '{query}'")

            except Exception as e:
                logger.warning(f"Indeed search failed for '{query}': {e}")

            # Rate-limit requests between searches
            await page.wait_for_timeout(5000)

        logger.info(f"Indeed total: {len(jobs)} unique jobs across {len(search_queries)} queries")
        return jobs

    # ──────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────

    async def _handle_pagination(self, page: Page, selectors: Dict = None, max_pages: int = 5):
        """
        Attempt to load more content via:
        1. Clicking a 'Load More' / 'Next' button
        2. Scrolling to trigger infinite scroll
        """
        selectors = selectors or {}
        load_more_sel = selectors.get(
            "load_more",
            'button[data-automation-id="loadMoreJobs"], '
            'button:has-text("Load More"), '
            'a:has-text("Next"), '
            'button:has-text("Show More")'
        )

        for _ in range(max_pages):
            try:
                btn = page.locator(load_more_sel).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    await page.wait_for_timeout(2000)
                else:
                    # Try infinite scroll fallback
                    prev_height = await page.evaluate("document.body.scrollHeight")
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(2000)
                    new_height = await page.evaluate("document.body.scrollHeight")
                    if new_height == prev_height:
                        break
            except Exception:
                break

    async def _fetch_job_description(self, page: Page, url: str) -> str:
        """Navigate to a job detail page and extract the description text."""
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout)
            await page.wait_for_timeout(1500)

            for selector in DESCRIPTION_SELECTORS:
                try:
                    el = page.locator(selector).first
                    if await el.is_visible(timeout=1500):
                        text = await el.inner_text()
                        if text and len(text.strip()) > 50:
                            return text.strip()[:8000]
                except Exception:
                    continue

            # Last resort: grab all text from <body>
            body_text = await page.locator("body").inner_text()
            if body_text and len(body_text.strip()) > 100:
                return body_text.strip()[:8000]

        except Exception as e:
            logger.warning(f"Could not fetch description from {url}: {e}")

        return ""
