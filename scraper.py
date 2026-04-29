"""
Job Scraper for Christian Richey — v5
Scrapes: JournalismJobs, MediaBistro, Poynter, IRE, SPJ,
         Adzuna API, The Muse API, Indeed RSS, GovernmentJobs, USAJobs API
New in v5:
  - Added GovernmentJobs.com (state/county/city roles in MD and NY)
  - Full job description fetching for richer Claude scoring (#2)
  - Fuzzy deduplication — catches same job posted on multiple boards (#5)
  - Deadline detection — Claude extracts closing dates, flags urgent ones (#6)
  - Reply-to-mark-applied tracking — mailto links in every email listing (#1 Option A)
"""

import requests
from bs4 import BeautifulSoup
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import time
import json
import os
import xml.etree.ElementTree as ET
import difflib   # for fuzzy deduplication
import re        # for deadline detection

# ─────────────────────────────────────────────
# ✏️  YOUR SETTINGS — fill these in before running
# ─────────────────────────────────────────────

EMAIL_SENDER      = os.environ.get("EMAIL_SENDER",      "thatoneguycpr@gmail.com")
EMAIL_PASSWORD    = os.environ.get("EMAIL_PASSWORD",    "hblb hujk obhg bhzm")
EMAIL_RECIPIENT   = os.environ.get("EMAIL_RECIPIENT",   "thatoneguycpr@gmail.com")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "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")
USAJOBS_API_KEY   = os.environ.get("USAJOBS_API_KEY",   "0Y3KpPqF9246teFFcBk4QXmCkBEaqEyJGNVVut11TRs=")
ADZUNA_APP_ID     = os.environ.get("ADZUNA_APP_ID",     "4803a542")    # free at developer.adzuna.com
ADZUNA_APP_KEY    = os.environ.get("ADZUNA_APP_KEY",    "c7f5e97a2cb7ec100337f0abdf26ca4e")   # free at developer.adzuna.com

KEYWORDS = [
    # Core journalism/editorial
    "reporter", "editor", "writer", "journalist", "copy editor", "editorial",
    "correspondent", "producer", "anchor", "broadcast",
    # Communications & PR
    "communications", "public affairs", "public relations", "PR ", "media relations",
    "communications coordinator", "communications associate", "communications specialist",
    # Content & digital
    "content", "content writer", "content creator", "content strategist",
    "copywriter", "digital media", "social media", "newsletter",
    "digital content", "multimedia",
    # Adjacent roles that fit your background
    "research", "fact-check", "analyst", "policy", "outreach",
    "marketing communications", "brand", "storytell"
]

LOCATIONS = [
    # New York
    "new york", "nyc", "brooklyn", "manhattan", "queens", "bronx",
    "new york city", "ny,", ", ny", "(ny)", "10001", "11201",
    # Maryland
    "maryland", "annapolis", "baltimore", "bethesda", "silver spring",
    "rockville", "college park", "greenbelt", "bowie", "md,", ", md", "(md)",
    # DC area (close to Maryland)
    "washington, dc", "washington dc", "dc,", ", dc",
    # Remote — always include
    "remote", "work from home", "hybrid"
]

# Minimum Claude relevance score (1–10) to include in the email
MIN_SCORE = 5

# ─────────────────────────────────────────────
# SEEN JOBS — Deduplication across days
# ─────────────────────────────────────────────
# Jobs you've already been emailed are saved in seen_jobs.json.
# Each run loads this file, skips already-seen jobs, then saves the new ones.

SEEN_JOBS_FILE = "seen_jobs.json"

def load_seen_jobs():
    if os.path.exists(SEEN_JOBS_FILE):
        with open(SEEN_JOBS_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_seen_jobs(seen_links):
    with open(SEEN_JOBS_FILE, "w") as f:
        json.dump(list(seen_links), f, indent=2)

def filter_new_jobs(jobs, seen_links):
    new_jobs = [j for j in jobs if j["link"] not in seen_links]
    skipped  = len(jobs) - len(new_jobs)
    if skipped:
        print(f"  ↩️  Skipped {skipped} jobs already seen in previous digests")
    return new_jobs


def fuzzy_deduplicate(jobs, threshold=0.85):
    """
    Remove duplicate jobs that appear on multiple boards.
    Two jobs are considered duplicates if their titles are 85%+ similar
    AND they share the same employer (or one has no employer listed).
    Much smarter than URL-only dedup — catches the same role posted
    on Indeed, Adzuna, and JournalismJobs simultaneously.
    """
    deduped = []
    for job in jobs:
        title_a = job["title"].lower().strip()
        emp_a   = job.get("employer", "").lower().strip()
        is_dupe = False
        for existing in deduped:
            title_b = existing["title"].lower().strip()
            emp_b   = existing.get("employer", "").lower().strip()
            title_similarity = difflib.SequenceMatcher(None, title_a, title_b).ratio()
            # Employers match if they're similar OR one is unknown
            employer_match = (
                not emp_a or not emp_b or
                difflib.SequenceMatcher(None, emp_a, emp_b).ratio() > 0.8
            )
            if title_similarity >= threshold and employer_match:
                is_dupe = True
                break
        if not is_dupe:
            deduped.append(job)
    removed = len(jobs) - len(deduped)
    if removed:
        print(f"  🔁 Fuzzy dedup removed {removed} cross-board duplicates")
    return deduped


# ─────────────────────────────────────────────
# DESCRIPTION FETCHER
# ─────────────────────────────────────────────

def fetch_description(url, max_chars=1500):
    """
    Fetch the job posting page and extract plain text from the description.
    Capped at 1500 chars to keep Claude token usage reasonable.
    Returns empty string on any failure — scoring still works without it.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "html.parser")
        # Remove nav, header, footer noise
        for tag in soup(["nav", "header", "footer", "script", "style"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        # Collapse whitespace
        text = re.sub(r"\s+", " ", text)
        return text[:max_chars]
    except Exception:
        return ""


def fetch_descriptions_for_jobs(jobs):
    """
    Fetch descriptions for all jobs, with a small delay between requests.
    Skips jobs from sources that don't have direct listing pages.
    """
    print(f"  📄 Fetching job descriptions ({len(jobs)} jobs)...")
    no_fetch_sources = {"Adzuna"}  # redirect URLs, not useful to scrape
    for i, job in enumerate(jobs):
        if job.get("source") in no_fetch_sources:
            job["description"] = ""
            continue
        job["description"] = fetch_description(job["link"])
        time.sleep(0.3)
    fetched = sum(1 for j in jobs if j.get("description"))
    print(f"  📄 Got descriptions for {fetched}/{len(jobs)} jobs")
    return jobs


# ─────────────────────────────────────────────
# CLAUDE RELEVANCE SCORING
# ─────────────────────────────────────────────

RESUME_SUMMARY = """
Christian Richey — Communications & Journalism professional
- Internships at Bloomberg, Wall Street Journal, Sports Illustrated
- Currently works at Handshake (AI-related, remote)
- Background in reporting, writing, editorial, digital media
- Looking for: entry-level or associate-level roles
- Target locations: New York City area, Maryland (Baltimore/Annapolis/DC metro)
- Open to: reporter, editor, content writer, communications coordinator,
  PR associate, digital media, social media, copywriter, public affairs
"""

def score_jobs_with_claude(jobs):
    """
    Score jobs using Claude. Now includes:
    - Full job description text for richer, more accurate scoring (#2)
    - Deadline extraction — Claude pulls closing dates if mentioned (#6)
    - Retry logic with detailed error messages
    """
    if not jobs:
        return jobs

    if ANTHROPIC_API_KEY == "your-claude-api-key-here":
        print("  ⚠️  Claude scoring skipped — add your ANTHROPIC_API_KEY to enable it")
        for job in jobs:
            job["score"]    = 5
            job["reason"]   = "Scoring unavailable — add Claude API key"
            job["deadline"] = ""
        return jobs

    job_list_text = "\n".join([
        f"{i+1}. Title: {j['title']} | Employer: {j.get('employer','?')} | "
        f"Location: {j.get('location','?')}\n"
        f"   Description snippet: {j.get('description','(none available)') or '(none available)'}"
        for i, j in enumerate(jobs)
    ])

    prompt = f"""You are evaluating job listings for a specific candidate. Score each job's fit on a scale of 1-10, and extract any application deadline if mentioned.

CANDIDATE PROFILE:
{RESUME_SUMMARY}

JOBS TO EVALUATE:
{job_list_text}

Return ONLY a JSON array (no markdown, no explanation outside JSON) with one object per job, in the same order:
[
  {{
    "index": 1,
    "score": 8,
    "reason": "Strong editorial role in NYC matching journalism background",
    "deadline": "May 15, 2026"
  }},
  {{
    "index": 2,
    "score": 4,
    "reason": "Too senior, requires 5+ years experience",
    "deadline": ""
  }}
]

Rules:
- Score 9-10 only for entry-level communications/journalism roles in NYC or Maryland that closely match the candidate's background
- Score 1-3 for roles that are too senior, wrong field, or wrong location
- For deadline: extract the application closing/deadline date if mentioned in the description. Leave as "" if not mentioned.
- Keep each reason under 12 words
- Use the description snippet to inform scoring — a title alone can be misleading"""

    MAX_RETRIES = 3
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"  🤖 Claude scoring attempt {attempt}/{MAX_RETRIES}...")
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":         ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json={
                    "model":      "claude-sonnet-4-5",
                    "max_tokens": 4096,
                    "messages":   [{"role": "user", "content": prompt}]
                },
                timeout=60
            )

            if resp.status_code != 200:
                print(f"  ⚠️  Claude API returned HTTP {resp.status_code}: {resp.text[:200]}")
                if resp.status_code in (401, 403):
                    print("      → Your API key may be wrong or inactive. Check console.anthropic.com")
                    break
                time.sleep(3)
                continue

            result_text = resp.json()["content"][0]["text"].strip()
            if result_text.startswith("```"):
                result_text = result_text.split("```")[1]
                if result_text.startswith("json"):
                    result_text = result_text[4:]

            scores = json.loads(result_text)
            for item in scores:
                idx = item["index"] - 1
                if 0 <= idx < len(jobs):
                    jobs[idx]["score"]    = item.get("score", 5)
                    jobs[idx]["reason"]   = item.get("reason", "")
                    jobs[idx]["deadline"] = item.get("deadline", "")

            print(f"  ✅ Claude scored {len(jobs)} jobs successfully")
            return jobs

        except requests.exceptions.ConnectionError as e:
            print(f"  ⚠️  Connection error on attempt {attempt}: {e}")
            print("      → Usually a firewall blocking api.anthropic.com")
            print("      → Try disabling Windows Defender / antivirus temporarily")
            if attempt < MAX_RETRIES:
                print(f"      Retrying in 5 seconds...")
                time.sleep(5)

        except requests.exceptions.Timeout:
            print(f"  ⚠️  Timeout on attempt {attempt}")
            if attempt < MAX_RETRIES:
                time.sleep(5)

        except Exception as e:
            print(f"  ⚠️  Unexpected scoring error on attempt {attempt}: {type(e).__name__}: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(3)

    print("  ℹ️  Scoring failed — jobs will appear with default score of 5")
    for job in jobs:
        job.setdefault("score",    5)
        job.setdefault("reason",   "Scoring unavailable — see terminal for details")
        job.setdefault("deadline", "")
    return jobs


# ─────────────────────────────────────────────
# SCRAPER FUNCTIONS
# ─────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

def is_relevant(title, location=""):
    """Check if a job title and location match our filters."""
    title_lower    = title.lower()
    location_lower = location.lower()
    keyword_match  = any(kw in title_lower for kw in KEYWORDS)
    location_match = any(loc in location_lower for loc in LOCATIONS) or location == ""
    # Exclude clearly senior roles — but keep coordinator, associate, specialist
    senior_words = ["senior", "director", "vp ", "vice president", "chief", " manager", "head of", "managing editor"]
    is_senior      = any(word in title_lower for word in senior_words)
    return keyword_match and location_match and not is_senior


def scrape_journalismjobs():
    jobs = []
    url  = "https://www.journalismjobs.com/journalism-jobs"
    try:
        resp     = requests.get(url, headers=HEADERS, timeout=15)
        soup     = BeautifulSoup(resp.text, "html.parser")
        listings = soup.find_all("div", class_="job-info")
        for listing in listings:
            title_tag    = listing.find("h2") or listing.find("h3")
            employer_tag = listing.find("span", class_="employer") or listing.find("div", class_="employer")
            location_tag = listing.find("span", class_="location") or listing.find("div", class_="location")
            link_tag     = listing.find("a", href=True)
            title    = title_tag.get_text(strip=True)    if title_tag    else "Unknown Title"
            employer = employer_tag.get_text(strip=True) if employer_tag else ""
            location = location_tag.get_text(strip=True) if location_tag else ""
            link     = link_tag["href"]                  if link_tag     else url
            if not link.startswith("http"):
                link = "https://www.journalismjobs.com" + link
            if is_relevant(title, location):
                jobs.append({"title": title, "employer": employer, "location": location, "link": link, "source": "JournalismJobs"})
        print(f"  JournalismJobs: {len(jobs)} relevant jobs")
    except Exception as e:
        print(f"  JournalismJobs error: {e}")
    return jobs


def scrape_mediabistro():
    jobs = []
    url  = "https://www.mediabistro.com/jobs/search/?q=&location=&level=entry"
    try:
        resp     = requests.get(url, headers=HEADERS, timeout=15)
        soup     = BeautifulSoup(resp.text, "html.parser")
        listings = soup.find_all("article") or soup.find_all("li", class_=lambda c: c and "job" in c.lower())
        for listing in listings:
            title_tag    = listing.find("h2") or listing.find("h3") or listing.find("a")
            location_tag = listing.find(class_=lambda c: c and "location" in c.lower() if c else False)
            employer_tag = listing.find(class_=lambda c: c and ("company" in c.lower() or "employer" in c.lower()) if c else False)
            link_tag     = listing.find("a", href=True)
            title    = title_tag.get_text(strip=True)    if title_tag    else "Unknown Title"
            location = location_tag.get_text(strip=True) if location_tag else ""
            employer = employer_tag.get_text(strip=True) if employer_tag else ""
            link     = link_tag["href"]                  if link_tag     else url
            if not link.startswith("http"):
                link = "https://www.mediabistro.com" + link
            if is_relevant(title, location):
                jobs.append({"title": title, "employer": employer, "location": location, "link": link, "source": "MediaBistro"})
        print(f"  MediaBistro: {len(jobs)} relevant jobs")
    except Exception as e:
        print(f"  MediaBistro error: {e}")
    return jobs


def scrape_poynter():
    """
    Poynter moved their job board from jobs.poynter.org (now dead)
    to poynter.org/media-jobs/ — updated April 2026.
    The page embeds jobs via mediajobboard.com widgets, so we scrape
    the raw text lines that appear in the page's job listing section.
    """
    jobs = []
    url  = "https://www.poynter.org/media-jobs/"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")

        # The page renders job titles and locations as plain text spans/divs
        # Strategy: find all anchor tags pointing to job listings
        all_links = soup.find_all("a", href=True)
        job_links = [
            a for a in all_links
            if "mediajobboard.com" in a.get("href", "")
            or "/jobs/" in a.get("href", "")
            or "job" in a.get("href", "").lower()
        ]

        # Also try grabbing structured text — Poynter lists jobs as:
        # "Job Title - City, ST (zip) Employer"
        # They appear inside a specific content div
        content_div = soup.find("div", class_=lambda c: c and "entry-content" in c if c else False)
        if not content_div:
            content_div = soup.find("main") or soup.find("article") or soup

        raw_lines = content_div.get_text(separator="\n").splitlines()

        # Each job line looks like: "Reporter - New York, NY (10001)The Times"
        import re
        job_pattern = re.compile(r"^(.+?)\s*[-–]\s*(.+?,\s*\w{2})", re.IGNORECASE)

        for line in raw_lines:
            line = line.strip()
            if not line or len(line) < 10:
                continue
            m = job_pattern.match(line)
            if m:
                title    = m.group(1).strip()
                location = m.group(2).strip()
                # Try to find a matching link for this title
                link = url
                for a in job_links:
                    if title.lower()[:15] in a.get_text(strip=True).lower():
                        link = a["href"]
                        if not link.startswith("http"):
                            link = "https://www.poynter.org" + link
                        break
                if is_relevant(title, location):
                    jobs.append({
                        "title":    title,
                        "employer": "",
                        "location": location,
                        "link":     link,
                        "source":   "Poynter"
                    })

        # Fallback: if pattern found nothing, try any anchor with job-like text
        if not jobs:
            for a in job_links:
                title = a.get_text(strip=True)
                link  = a["href"]
                if not link.startswith("http"):
                    link = "https://www.poynter.org" + link
                if title and is_relevant(title, ""):
                    jobs.append({
                        "title":    title,
                        "employer": "",
                        "location": "",
                        "link":     link,
                        "source":   "Poynter"
                    })

        print(f"  Poynter: {len(jobs)} relevant jobs")
    except Exception as e:
        print(f"  Poynter error: {e}")
    return jobs


def scrape_ire():
    """
    Investigative Reporters and Editors job board — ire.org/jobs
    Great for reporting, research, and data journalism roles.
    Very scraper-friendly, plain HTML.
    """
    jobs = []
    url  = "https://www.ire.org/jobs/"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")

        # IRE lists jobs in table rows or div blocks
        listings = soup.select("table tr") or soup.find_all("div", class_=lambda c: c and "job" in c.lower() if c else False)
        if not listings:
            listings = soup.find_all("li")

        for listing in listings:
            cells    = listing.find_all("td")
            title_tag    = cells[0] if cells else listing.find("a") or listing.find("h3") or listing.find("h2")
            location_tag = cells[2] if len(cells) > 2 else listing.find(class_=lambda c: "location" in c.lower() if c else False)
            link_tag     = listing.find("a", href=True)

            title    = title_tag.get_text(strip=True) if title_tag    else ""
            location = location_tag.get_text(strip=True) if location_tag else ""
            link     = link_tag["href"]               if link_tag     else url

            if not link.startswith("http"):
                link = "https://www.ire.org" + link

            if title and is_relevant(title, location):
                jobs.append({
                    "title":    title,
                    "employer": "",
                    "location": location,
                    "link":     link,
                    "source":   "IRE"
                })

        print(f"  IRE: {len(jobs)} relevant jobs")
    except Exception as e:
        print(f"  IRE error: {e}")
    return jobs


def scrape_spj():
    """
    Society of Professional Journalists job board — jobs.spj.org
    Lists journalism, PR, communications roles across the US.
    """
    jobs = []
    url  = "https://jobs.spj.org/jobs/"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")

        listings = soup.find_all("article") or soup.find_all("li", class_=lambda c: c and "job" in c.lower() if c else False)
        if not listings:
            listings = soup.select(".job, .job-listing, .jb-job-list-row")

        for listing in listings:
            title_tag    = listing.find("h2") or listing.find("h3") or listing.find("a")
            location_tag = listing.find(class_=lambda c: "location" in c.lower() if c else False)
            employer_tag = listing.find(class_=lambda c: ("company" in c.lower() or "employer" in c.lower()) if c else False)
            link_tag     = listing.find("a", href=True)

            title    = title_tag.get_text(strip=True)    if title_tag    else ""
            location = location_tag.get_text(strip=True) if location_tag else ""
            employer = employer_tag.get_text(strip=True) if employer_tag else ""
            link     = link_tag["href"]                  if link_tag     else url

            if not link.startswith("http"):
                link = "https://jobs.spj.org" + link

            if title and is_relevant(title, location):
                jobs.append({
                    "title":    title,
                    "employer": employer,
                    "location": location,
                    "link":     link,
                    "source":   "SPJ"
                })

        print(f"  SPJ: {len(jobs)} relevant jobs")
    except Exception as e:
        print(f"  SPJ error: {e}")
    return jobs


def scrape_adzuna():
    """
    Adzuna API — aggregates listings from 100s of sources including
    LinkedIn, Indeed, Glassdoor, and company career pages.
    Free API key from developer.adzuna.com (instant signup).
    Searches NY and MD separately across several keyword queries.
    """
    jobs = []

    if ADZUNA_APP_ID == "your-adzuna-app-id":
        print("  Adzuna: skipped — add your ADZUNA_APP_ID and ADZUNA_APP_KEY")
        return jobs

    keyword_queries = [
        "communications", "journalist", "writer editor",
        "public affairs", "content creator", "media relations"
    ]
    # Adzuna uses country + region codes. US regions by state name.
    locations = [
        ("new york", "NY"),
        ("maryland",  "MD"),
    ]

    for keyword in keyword_queries:
        for loc_name, loc_code in locations:
            try:
                resp = requests.get(
                    f"https://api.adzuna.com/v1/api/jobs/us/search/1",
                    params={
                        "app_id":           ADZUNA_APP_ID,
                        "app_key":          ADZUNA_APP_KEY,
                        "results_per_page": 20,
                        "what":             keyword,
                        "where":            loc_name,
                        "sort_by":          "date",
                        "max_days_old":     3,          # only last 3 days
                    },
                    timeout=15
                )
                if resp.status_code != 200:
                    print(f"  Adzuna: HTTP {resp.status_code} — check your API keys")
                    return jobs

                for item in resp.json().get("results", []):
                    title    = item.get("title", "")
                    employer = item.get("company", {}).get("display_name", "")
                    location = item.get("location", {}).get("display_name", "")
                    link     = item.get("redirect_url", "")

                    if is_relevant(title, location):
                        jobs.append({
                            "title":    title,
                            "employer": employer,
                            "location": location,
                            "link":     link,
                            "source":   "Adzuna"
                        })

                time.sleep(0.5)

            except Exception as e:
                print(f"  Adzuna error ({keyword}/{loc_name}): {e}")

    # Deduplicate within Adzuna results
    seen, deduped = set(), []
    for job in jobs:
        if job["link"] not in seen:
            seen.add(job["link"])
            deduped.append(job)

    print(f"  Adzuna: {len(deduped)} relevant jobs")
    return deduped


def scrape_the_muse():
    """
    The Muse API — free, no API key required, just call it.
    Skews toward media, communications, and creative companies.
    Great source for NYC-area roles especially.
    """
    jobs = []
    base = "https://www.themuse.com/api/public/jobs"

    keyword_queries = ["communications", "writer", "editor", "journalist", "media", "public relations"]

    for keyword in keyword_queries:
        try:
            resp = requests.get(
                base,
                params={
                    "descending": "true",
                    "page":       1,
                    "category":   "Media, PR & Communications",  # exact Muse category
                },
                headers=HEADERS,
                timeout=15
            )
            if resp.status_code != 200:
                print(f"  The Muse: HTTP {resp.status_code}")
                return jobs

            for item in resp.json().get("results", []):
                title    = item.get("name", "")
                employer = item.get("company", {}).get("name", "")
                locations_list = item.get("locations", [])
                location = ", ".join(l.get("name", "") for l in locations_list)
                link     = item.get("refs", {}).get("landing_page", "")

                if is_relevant(title, location):
                    jobs.append({
                        "title":    title,
                        "employer": employer,
                        "location": location,
                        "link":     link,
                        "source":   "The Muse"
                    })

            time.sleep(0.5)

        except Exception as e:
            print(f"  The Muse error ({keyword}): {e}")
        break  # one call covers the category — no need to loop keywords

    # Also try a general search page for NY/MD
    try:
        for location_filter in ["New York", "Maryland"]:
            resp = requests.get(
                base,
                params={
                    "descending": "true",
                    "page":       1,
                    "location":   location_filter,
                },
                headers=HEADERS,
                timeout=15
            )
            if resp.status_code == 200:
                for item in resp.json().get("results", []):
                    title    = item.get("name", "")
                    employer = item.get("company", {}).get("name", "")
                    locations_list = item.get("locations", [])
                    location = ", ".join(l.get("name", "") for l in locations_list)
                    link     = item.get("refs", {}).get("landing_page", "")
                    if is_relevant(title, location):
                        jobs.append({
                            "title":    title,
                            "employer": employer,
                            "location": location,
                            "link":     link,
                            "source":   "The Muse"
                        })
            time.sleep(0.5)
    except Exception as e:
        print(f"  The Muse location search error: {e}")

    # Deduplicate
    seen, deduped = set(), []
    for job in jobs:
        if job["link"] not in seen:
            seen.add(job["link"])
            deduped.append(job)

    print(f"  The Muse: {len(deduped)} relevant jobs")
    return deduped


def scrape_indeed_rss():
    """
    Indeed RSS feeds — stable, free, no API key, no scraping tricks needed.
    Indeed exposes search results as RSS XML for any query+location combo.
    We run several targeted searches and parse the feed.
    """
    jobs = []

    searches = [
        # (query,               location)
        ("communications",      "New York, NY"),
        ("journalist writer",   "New York, NY"),
        ("content editor",      "New York, NY"),
        ("public affairs",      "New York, NY"),
        ("social media",        "New York, NY"),
        ("communications",      "Maryland"),
        ("public affairs",      "Maryland"),
        ("writer editor",       "Maryland"),
        ("communications",      "Washington, DC"),
        ("journalist",          "remote"),
    ]

    for query, location in searches:
        try:
            rss_url = (
                f"https://www.indeed.com/rss"
                f"?q={requests.utils.quote(query)}"
                f"&l={requests.utils.quote(location)}"
                f"&sort=date"
                f"&fromage=3"   # posted in last 3 days
            )

            resp = requests.get(rss_url, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                continue

            root = ET.fromstring(resp.content)
            channel = root.find("channel")
            if channel is None:
                continue

            for item in channel.findall("item"):
                title_el    = item.find("title")
                link_el     = item.find("link")
                # Indeed puts "Company - Location" in the <author> or description
                desc_el     = item.find("description")

                title = title_el.text.strip() if title_el is not None else ""
                link  = link_el.text.strip()  if link_el  is not None else ""

                # Parse employer and location out of description HTML
                employer, loc = "", location
                if desc_el is not None and desc_el.text:
                    desc_soup = BeautifulSoup(desc_el.text, "html.parser")
                    desc_text = desc_soup.get_text(" ", strip=True)
                    # Indeed description often starts with "Company: X  Location: Y"
                    if "Company:" in desc_text:
                        employer = desc_text.split("Company:")[-1].split("Location:")[0].strip()
                    if "Location:" in desc_text:
                        loc = desc_text.split("Location:")[-1].split("Salary:")[0].strip()

                if title and is_relevant(title, loc):
                    jobs.append({
                        "title":    title,
                        "employer": employer,
                        "location": loc,
                        "link":     link,
                        "source":   "Indeed"
                    })

            time.sleep(0.5)

        except ET.ParseError:
            # Indeed sometimes returns HTML instead of XML when blocking — skip quietly
            pass
        except Exception as e:
            print(f"  Indeed RSS error ({query}/{location}): {e}")

    # Deduplicate by link
    seen, deduped = set(), []
    for job in jobs:
        if job["link"] not in seen:
            seen.add(job["link"])
            deduped.append(job)

    print(f"  Indeed RSS: {len(deduped)} relevant jobs")
    return deduped


def scrape_governmentjobs():
    """
    GovernmentJobs.com (NEOGOV) — the platform used by most US state,
    county, and city governments. Hits their internal JSON API directly.
    Great for Maryland state/county roles and NYC city government roles.
    Freshness filter loosened to 7 days since govt postings stay up longer.
    """
    jobs = []
    base = "https://www.governmentjobs.com/api/listing/"

    searches = [
        ("communications",  "Maryland"),
        ("public affairs",  "Maryland"),
        ("writer",          "Maryland"),
        ("communications",  "New York"),
        ("public affairs",  "New York City"),
        ("media",           "Maryland"),
    ]

    for keyword, location in searches:
        try:
            resp = requests.get(
                base,
                params={
                    "keyword":        keyword,
                    "location":       location,
                    "sort":           "UpdatedDate",
                    "sortDescending": "true",
                },
                headers=HEADERS,
                timeout=15
            )
            if resp.status_code != 200:
                # API may have changed — fall back to scraping the search page
                page_resp = requests.get(
                    "https://www.governmentjobs.com/careers/search",
                    params={"keyword": keyword, "location": location},
                    headers=HEADERS,
                    timeout=15
                )
                if page_resp.status_code != 200:
                    continue
                soup     = BeautifulSoup(page_resp.text, "html.parser")
                listings = soup.find_all("li", class_=lambda c: c and "job" in c.lower() if c else False)
                for listing in listings:
                    title_tag    = listing.find("h2") or listing.find("h3") or listing.find("a")
                    location_tag = listing.find(class_=lambda c: "location" in c.lower() if c else False)
                    employer_tag = listing.find(class_=lambda c: "department" in c.lower() if c else False)
                    link_tag     = listing.find("a", href=True)
                    title    = title_tag.get_text(strip=True)    if title_tag    else ""
                    loc      = location_tag.get_text(strip=True) if location_tag else location
                    employer = employer_tag.get_text(strip=True) if employer_tag else ""
                    link     = link_tag["href"]                  if link_tag     else ""
                    if not link.startswith("http") and link:
                        link = "https://www.governmentjobs.com" + link
                    if title and is_relevant(title, loc):
                        jobs.append({"title": title, "employer": employer, "location": loc, "link": link, "source": "GovernmentJobs"})
                continue

            data = resp.json()
            items = data if isinstance(data, list) else data.get("data", data.get("results", []))

            for item in items:
                title    = item.get("PositionTitle") or item.get("title", "")
                employer = item.get("DepartmentName") or item.get("department", "")
                loc      = item.get("Location") or item.get("location", location)
                job_id   = item.get("JobID") or item.get("id", "")
                link     = f"https://www.governmentjobs.com/careers/detail/{job_id}" if job_id else ""

                if title and is_relevant(title, loc):
                    jobs.append({
                        "title":    title,
                        "employer": employer,
                        "location": loc,
                        "link":     link,
                        "source":   "GovernmentJobs"
                    })

            time.sleep(0.5)

        except Exception as e:
            print(f"  GovernmentJobs error ({keyword}/{location}): {e}")

    seen, deduped = set(), []
    for job in jobs:
        key = job["title"].lower() + job.get("employer", "").lower()
        if key not in seen:
            seen.add(key)
            deduped.append(job)

    print(f"  GovernmentJobs: {len(deduped)} relevant jobs")
    return deduped


def scrape_usajobs():
    jobs = []
    keyword_queries = ["communications", "public affairs", "writer editor", "journalist"]
    location_codes  = ["MD", "NY"]
    for keyword in keyword_queries:
        for location in location_codes:
            try:
                resp = requests.get(
                    "https://data.usajobs.gov/api/search",
                    headers={
                        "Host":              "data.usajobs.gov",
                        "User-Agent":        EMAIL_SENDER,
                        "Authorization-Key": USAJOBS_API_KEY,
                    },
                    params={"Keyword": keyword, "LocationName": location, "ResultsPerPage": 25, "GradeBasePay": "GS-05;GS-07;GS-09"},
                    timeout=15
                )
                if resp.status_code != 200:
                    print(f"  USAJobs: skipped ({resp.status_code} — check your API key)")
                    return jobs
                for item in resp.json().get("SearchResult", {}).get("SearchResultItems", []):
                    pos = item.get("MatchedObjectDescriptor", {})
                    title, employer, location_str, link = (
                        pos.get("PositionTitle", ""),
                        pos.get("OrganizationName", ""),
                        pos.get("PositionLocationDisplay", ""),
                        pos.get("PositionURI", "")
                    )
                    if is_relevant(title, location_str):
                        jobs.append({"title": title, "employer": employer, "location": location_str, "link": link, "source": "USAJobs"})
                time.sleep(1)
            except Exception as e:
                print(f"  USAJobs error ({keyword}/{location}): {e}")
    seen, deduped = set(), []
    for job in jobs:
        if job["link"] not in seen:
            seen.add(job["link"])
            deduped.append(job)
    print(f"  USAJobs: {len(deduped)} relevant jobs")
    return deduped


# ─────────────────────────────────────────────
# EMAIL DIGEST
# ─────────────────────────────────────────────

SCORE_COLORS = {
    (9, 10): ("#1a7a1a", "🔥 Excellent match"),
    (7,  8): ("#2a6099", "⭐ Strong match"),
    (5,  6): ("#7a5a00", "👍 Worth considering"),
    (1,  4): ("#888888", "➡️  Weak match"),
}

def score_label(score):
    for (lo, hi), (color, label) in SCORE_COLORS.items():
        if lo <= score <= hi:
            return color, label
    return "#888", "➡️  Weak match"


def build_email_html(all_jobs):
    today = datetime.now().strftime("%A, %B %d, %Y")
    total = len(all_jobs)
    sorted_jobs = sorted(all_jobs, key=lambda j: j.get("score", 5), reverse=True)

    job_rows = ""
    for job in sorted_jobs:
        score        = job.get("score", 5)
        reason       = job.get("reason", "")
        deadline     = job.get("deadline", "")
        color, label = score_label(score)
        employer_str = f" &mdash; {job['employer']}" if job.get("employer") else ""
        location_str = f"<span style='color:#888;'>📍 {job['location']}</span><br>" if job.get("location") else ""

        # ⏰ Deadline badge — shown in red if deadline is present
        deadline_html = ""
        if deadline:
            deadline_html = (
                f"<span style='font-size:11px; background:#ffeaea; color:#c0392b; "
                f"padding:2px 8px; border-radius:10px; display:inline-block; margin:4px 4px 4px 0;'>"
                f"⏰ Deadline: {deadline}</span>"
            )

        # mailto link — clicking opens a pre-written email saying "applied"
        # Subject line encodes the job title so the scraper can parse it later
        mailto_subject = requests.utils.quote(f"APPLIED: {job['title']} | {job.get('employer','')}")
        mailto_link = f"mailto:{EMAIL_SENDER}?subject={mailto_subject}&body=Marking%20this%20as%20applied."

        job_rows += f"""
        <tr>
          <td style="padding:14px 0; border-bottom:1px solid #f0f0f0; vertical-align:top;">

            <div style="float:right; text-align:center; min-width:52px; margin-left:16px;">
              <div style="font-size:22px; font-weight:bold; color:{color}; line-height:1;">{score}</div>
              <div style="font-size:10px; color:{color}; text-transform:uppercase; letter-spacing:0.5px;">/ 10</div>
            </div>

            <div>
              <a href="{job['link']}" style="font-weight:bold; color:#1a1a2e; font-size:15px; text-decoration:none;">
                {job['title']}
              </a>{employer_str}<br>
              {location_str}
              <span style="font-size:11px; background:{color}18; color:{color}; padding:2px 8px; border-radius:10px; display:inline-block; margin:4px 4px 4px 0;">
                {label}
              </span>
              {deadline_html}
              {"<br><span style='font-size:12px; color:#555; font-style:italic;'>" + reason + "</span>" if reason else ""}
              <br>
              <span style="font-size:11px; color:#aaa;">via {job['source']}</span>
              &nbsp;·&nbsp;
              <a href="{job['link']}" style="color:#4a90d9; font-size:12px;">View Job →</a>
              &nbsp;·&nbsp;
              <a href="{mailto_link}" style="color:#27ae60; font-size:12px;">✅ Mark Applied</a>
            </div>

          </td>
        </tr>"""

    html = f"""
    <!DOCTYPE html>
    <html>
    <body style="font-family: Georgia, serif; max-width:680px; margin:0 auto; padding:24px; color:#333;">

      <div style="background:#1a1a2e; color:white; padding:24px; border-radius:8px; margin-bottom:16px;">
        <h1 style="margin:0; font-size:22px;">📰 Your Daily Job Digest</h1>
        <p style="margin:6px 0 0; opacity:0.8;">{today} &middot; {total} new listings &middot; sorted by relevance</p>
      </div>

      <p style="font-size:12px; color:#888; margin:0 0 24px; padding:10px 14px; background:#f9f9f9; border-radius:6px; border-left:3px solid #27ae60;">
        <strong>Tip:</strong> Click <strong>✅ Mark Applied</strong> on any job — it opens a pre-written email, just hit Send.
        The scraper will track it automatically in your next run.
      </p>

      {"<table width='100%' cellpadding='0' cellspacing='0'>" + job_rows + "</table>"
        if total > 0 else
        "<p style='color:#888; text-align:center; padding:40px;'>No new matching jobs found today. Check back tomorrow!</p>"
      }

      <hr style="margin:40px 0; border:none; border-top:1px solid #eee;">
      <p style="font-size:12px; color:#aaa; text-align:center;">
        Scraped from JournalismJobs &middot; MediaBistro &middot; Poynter &middot; IRE &middot; SPJ
        &middot; Adzuna &middot; The Muse &middot; Indeed &middot; GovernmentJobs &middot; USAJobs<br>
        Filtered for NYC &middot; Maryland &middot; entry/associate level &middot; scored by Claude AI
      </p>

    </body>
    </html>
    """
    return html


def send_email(all_jobs):
    today   = datetime.now().strftime("%b %d, %Y")
    subject = f"📰 Job Digest — {len(all_jobs)} new listings — {today}"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECIPIENT
    msg.attach(MIMEText(build_email_html(all_jobs), "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_RECIPIENT, msg.as_string())
        print(f"\n✅ Email sent to {EMAIL_RECIPIENT} with {len(all_jobs)} jobs!")
    except Exception as e:
        print(f"\n❌ Email failed: {e}")
        print("   Check your EMAIL_SENDER and EMAIL_PASSWORD settings.")


# ─────────────────────────────────────────────
# APPLIED JOBS TRACKER
# ─────────────────────────────────────────────

APPLIED_JOBS_FILE = "applied_jobs.json"

def load_applied_jobs():
    if os.path.exists(APPLIED_JOBS_FILE):
        with open(APPLIED_JOBS_FILE, "r") as f:
            return json.load(f)
    return []

def save_applied_jobs(applied):
    with open(APPLIED_JOBS_FILE, "w") as f:
        json.dump(applied, f, indent=2)

def check_inbox_for_applied():
    """
    Check Gmail inbox for 'Mark Applied' reply emails sent from the digest.
    Each reply has a subject like: APPLIED: Job Title | Employer
    Parses the title out and saves it to applied_jobs.json.
    Returns count of newly recorded applications.
    """
    import imaplib
    import email as email_lib
    from email.header import decode_header

    applied = load_applied_jobs()
    applied_titles = {a["title"].lower() for a in applied}
    new_count = 0

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(EMAIL_SENDER, EMAIL_PASSWORD)
        mail.select("inbox")

        # Search for emails with "APPLIED:" in subject sent TO ourselves
        _, data = mail.search(None, '(SUBJECT "APPLIED:")')
        ids = data[0].split()

        for msg_id in ids:
            _, msg_data = mail.fetch(msg_id, "(RFC822)")
            msg = email_lib.message_from_bytes(msg_data[0][1])

            raw_subject = msg.get("Subject", "")
            # Decode encoded subjects
            decoded_parts = decode_header(raw_subject)
            subject = ""
            for part, enc in decoded_parts:
                if isinstance(part, bytes):
                    subject += part.decode(enc or "utf-8", errors="ignore")
                else:
                    subject += part

            if "APPLIED:" not in subject:
                continue

            # Parse: "APPLIED: Job Title | Employer"
            content = subject.replace("APPLIED:", "").strip()
            parts   = content.split("|")
            title   = parts[0].strip()
            employer = parts[1].strip() if len(parts) > 1 else ""

            if title.lower() not in applied_titles:
                applied.append({
                    "title":    title,
                    "employer": employer,
                    "date":     datetime.now().strftime("%Y-%m-%d"),
                })
                applied_titles.add(title.lower())
                new_count += 1

        mail.logout()

    except Exception as e:
        print(f"  ⚠️  Could not check inbox for applied jobs: {e}")
        print("      (This is non-critical — scraper will continue normally)")

    if new_count:
        save_applied_jobs(applied)
        print(f"  ✅ Recorded {new_count} new job application(s) from your inbox")
        for a in applied[-new_count:]:
            print(f"     → {a['title']} @ {a['employer']} ({a['date']})")
    else:
        print(f"  📋 Applied jobs on record: {len(applied)} total (no new replies found)")

    return new_count


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    print(f"\n🔍 Job scrape starting — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

    # Step 1: Check inbox for any "Mark Applied" replies from previous digests
    print("📬 Checking inbox for applied job replies...")
    check_inbox_for_applied()
    print()

    # Step 2: Load seen jobs
    seen_links = load_seen_jobs()
    print(f"  📁 {len(seen_links)} jobs already seen from previous digests\n")

    # Step 3: Scrape all sources
    all_jobs = []
    all_jobs += scrape_journalismjobs()
    all_jobs += scrape_mediabistro()
    all_jobs += scrape_poynter()
    all_jobs += scrape_ire()
    all_jobs += scrape_spj()
    all_jobs += scrape_adzuna()
    all_jobs += scrape_the_muse()
    all_jobs += scrape_indeed_rss()
    all_jobs += scrape_governmentjobs()
    all_jobs += scrape_usajobs()

    # Step 4: Fuzzy deduplicate within today's results (catches cross-board dupes)
    print()
    deduped = fuzzy_deduplicate(all_jobs)

    # Step 5: Remove jobs already emailed on previous days
    new_jobs = filter_new_jobs(deduped, seen_links)

    print(f"\n🆕 New jobs not yet sent: {len(new_jobs)}")

    if not new_jobs:
        print("   Nothing new today — no email sent.")
        return

    # Step 6: Fetch full descriptions for richer scoring
    print()
    new_jobs = fetch_descriptions_for_jobs(new_jobs)

    # Step 7: Score with Claude (now using descriptions + deadline detection)
    print("\n🤖 Scoring jobs with Claude...\n")
    scored_jobs = score_jobs_with_claude(new_jobs)

    # Step 8: Filter by minimum score
    filtered = [j for j in scored_jobs if j.get("score", 5) >= MIN_SCORE]
    print(f"\n📋 Jobs at or above score {MIN_SCORE}: {len(filtered)} of {len(scored_jobs)}")

    # Step 9: Send email and save seen jobs
    if filtered:
        send_email(filtered)
        seen_links.update(j["link"] for j in new_jobs)
        save_seen_jobs(seen_links)
    else:
        print("   All jobs scored below minimum — no email sent today.")
        seen_links.update(j["link"] for j in new_jobs)
        save_seen_jobs(seen_links)


if __name__ == "__main__":
    main()
