"""
Job Scraper for Christian Richey — v4
Scrapes: JournalismJobs, MediaBistro, Poynter, IRE, SPJ,
         Adzuna API, The Muse API, Indeed RSS, USAJobs API
New in v4:
  - Added Adzuna API (aggregates 100s of sources including LinkedIn/Indeed)
  - Added The Muse API (no key needed, great for media/creative companies)
  - Added Indeed RSS feeds (stable, no scraping, broad coverage)
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
import xml.etree.ElementTree as ET  # for Indeed RSS parsing

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
    """Load the set of job links we've already emailed."""
    if os.path.exists(SEEN_JOBS_FILE):
        with open(SEEN_JOBS_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_seen_jobs(seen_links):
    """Save the updated set of seen job links."""
    with open(SEEN_JOBS_FILE, "w") as f:
        json.dump(list(seen_links), f, indent=2)

def filter_new_jobs(jobs, seen_links):
    """Remove any jobs we've already sent in a previous digest."""
    new_jobs = [j for j in jobs if j["link"] not in seen_links]
    skipped  = len(jobs) - len(new_jobs)
    if skipped:
        print(f"  ↩️  Skipped {skipped} jobs already seen in previous digests")
    return new_jobs


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
    Send jobs to Claude API in one batch call.
    Includes retry logic (3 attempts) and detailed error output to help diagnose
    connection issues on Windows, which sometimes block outbound API calls.

    Score meaning:
      9-10 = Excellent match — apply immediately
      7-8  = Strong match
      5-6  = Decent fit, worth considering
      3-4  = Weak match
      1-2  = Poor fit
    """
    if not jobs:
        return jobs

    if ANTHROPIC_API_KEY == "your-claude-api-key-here":
        print("  ⚠️  Claude scoring skipped — add your ANTHROPIC_API_KEY to enable it")
        for job in jobs:
            job["score"]  = 5
            job["reason"] = "Scoring unavailable — add Claude API key"
        return jobs

    job_list_text = "\n".join([
        f"{i+1}. Title: {j['title']} | Employer: {j.get('employer','?')} | Location: {j.get('location','?')}"
        for i, j in enumerate(jobs)
    ])

    prompt = f"""You are evaluating job listings for a specific candidate. Score each job's fit on a scale of 1-10.

CANDIDATE PROFILE:
{RESUME_SUMMARY}

JOBS TO EVALUATE:
{job_list_text}

Return ONLY a JSON array (no markdown, no explanation outside JSON) with one object per job, in the same order, like:
[
  {{"index": 1, "score": 8, "reason": "Strong editorial role in NYC matching journalism background"}},
  {{"index": 2, "score": 4, "reason": "Too senior, requires 5+ years experience"}},
  ...
]

Score 9-10 only for roles that closely match entry-level communications/journalism in NYC or Maryland.
Score 1-3 for roles that are too senior, wrong field, or wrong location.
Keep each reason under 12 words."""

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
                    "model": "claude-sonnet-4-5",
                    "max_tokens": 4096,
                    "messages":   [{"role": "user", "content": prompt}]
                },
                timeout=45
            )

            # Show the HTTP status so you can see exactly what went wrong
            if resp.status_code != 200:
                print(f"  ⚠️  Claude API returned HTTP {resp.status_code}: {resp.text[:200]}")
                if resp.status_code in (401, 403):
                    print("      → Your API key may be wrong or inactive. Check console.anthropic.com")
                    break  # Don't retry auth errors
                time.sleep(3)
                continue

            result_text = resp.json()["content"][0]["text"].strip()

            # Strip markdown code fences if present
            if result_text.startswith("```"):
                result_text = result_text.split("```")[1]
                if result_text.startswith("json"):
                    result_text = result_text[4:]

            scores = json.loads(result_text)

            for item in scores:
                idx = item["index"] - 1
                if 0 <= idx < len(jobs):
                    jobs[idx]["score"]  = item.get("score", 5)
                    jobs[idx]["reason"] = item.get("reason", "")

            print(f"  ✅ Claude scored {len(jobs)} jobs successfully")
            return jobs  # Success — exit retry loop

        except requests.exceptions.ConnectionError as e:
            print(f"  ⚠️  Connection error on attempt {attempt}: {e}")
            print("      → This is usually a firewall or network issue blocking api.anthropic.com")
            print("      → Try: temporarily disable Windows Defender / antivirus and run again")
            print("      → Or: check if a VPN or proxy is interfering")
            if attempt < MAX_RETRIES:
                print(f"      Retrying in 5 seconds...")
                time.sleep(5)

        except requests.exceptions.Timeout:
            print(f"  ⚠️  Timeout on attempt {attempt} — Claude API took too long to respond")
            if attempt < MAX_RETRIES:
                time.sleep(5)

        except Exception as e:
            print(f"  ⚠️  Unexpected scoring error on attempt {attempt}: {type(e).__name__}: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(3)

    # All retries failed — default scores so the email still sends
    print("  ℹ️  Scoring failed after all retries — jobs will appear with default score of 5")
    for job in jobs:
        job.setdefault("score",  5)
        job.setdefault("reason", "Scoring unavailable — see terminal for details")

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

    # Sort by score descending
    sorted_jobs = sorted(all_jobs, key=lambda j: j.get("score", 5), reverse=True)

    job_rows = ""
    for job in sorted_jobs:
        score        = job.get("score", 5)
        reason       = job.get("reason", "")
        color, label = score_label(score)
        employer_str = f" &mdash; {job['employer']}" if job.get("employer") else ""
        location_str = f"<span style='color:#888;'>📍 {job['location']}</span><br>" if job.get("location") else ""

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
              <span style="font-size:11px; background:{color}18; color:{color}; padding:2px 8px; border-radius:10px; display:inline-block; margin:4px 0;">
                {label}
              </span>
              {"<br><span style='font-size:12px; color:#555; font-style:italic;'>" + reason + "</span>" if reason else ""}
              <br>
              <span style="font-size:11px; color:#aaa;">via {job['source']}</span>
              &nbsp;·&nbsp;
              <a href="{job['link']}" style="color:#4a90d9; font-size:12px;">View Job →</a>
            </div>

          </td>
        </tr>"""

    html = f"""
    <!DOCTYPE html>
    <html>
    <body style="font-family: Georgia, serif; max-width:680px; margin:0 auto; padding:24px; color:#333;">

      <div style="background:#1a1a2e; color:white; padding:24px; border-radius:8px; margin-bottom:28px;">
        <h1 style="margin:0; font-size:22px;">📰 Your Daily Job Digest</h1>
        <p style="margin:6px 0 0; opacity:0.8;">{today} &middot; {total} new listings &middot; sorted by relevance</p>
      </div>

      {"<table width='100%' cellpadding='0' cellspacing='0'>" + job_rows + "</table>"
        if total > 0 else
        "<p style='color:#888; text-align:center; padding:40px;'>No new matching jobs found today. Check back tomorrow!</p>"
      }

      <hr style="margin:40px 0; border:none; border-top:1px solid #eee;">
      <p style="font-size:12px; color:#aaa; text-align:center;">
        Scraped from JournalismJobs &middot; MediaBistro &middot; Poynter &middot; IRE &middot; SPJ &middot; Adzuna &middot; The Muse &middot; Indeed &middot; USAJobs<br>
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
# MAIN
# ─────────────────────────────────────────────

def main():
    print(f"\n🔍 Job scrape starting — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

    # Load jobs we've already emailed in previous runs
    seen_links = load_seen_jobs()
    print(f"  📁 {len(seen_links)} jobs already seen from previous digests\n")

    # Scrape all sources
    all_jobs = []
    all_jobs += scrape_journalismjobs()
    all_jobs += scrape_mediabistro()
    all_jobs += scrape_poynter()
    all_jobs += scrape_ire()
    all_jobs += scrape_spj()
    all_jobs += scrape_adzuna()
    all_jobs += scrape_the_muse()
    all_jobs += scrape_indeed_rss()
    all_jobs += scrape_usajobs()

    # Deduplicate within today's scrape (same job on multiple boards)
    seen_today, deduped = set(), []
    for job in all_jobs:
        key = (job["title"].lower(), job.get("employer", "").lower())
        if key not in seen_today:
            seen_today.add(key)
            deduped.append(job)

    # Remove jobs already emailed on previous days
    new_jobs = filter_new_jobs(deduped, seen_links)

    print(f"\n🆕 New jobs not yet sent: {len(new_jobs)}")

    if not new_jobs:
        print("   Nothing new today — no email sent.")
        return

    # Score with Claude
    print("\n🤖 Scoring jobs with Claude...\n")
    scored_jobs = score_jobs_with_claude(new_jobs)

    # Only include jobs above the minimum score
    filtered = [j for j in scored_jobs if j.get("score", 5) >= MIN_SCORE]
    print(f"\n📋 Jobs at or above score {MIN_SCORE}: {len(filtered)} of {len(scored_jobs)}")

    if filtered:
        send_email(filtered)
        # Save ALL new jobs as seen (including low-scorers — don't resurface them)
        seen_links.update(j["link"] for j in new_jobs)
        save_seen_jobs(seen_links)
    else:
        print("   All jobs scored below minimum — no email sent today.")
        # Still mark them seen so they don't reappear
        seen_links.update(j["link"] for j in new_jobs)
        save_seen_jobs(seen_links)


if __name__ == "__main__":
    main()
