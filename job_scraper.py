"""
JOB SCRAPER — collection only.

This script does NOT score jobs and does NOT send email. It gathers listings,
filters them, removes duplicates, checks that every link actually works, and
writes the survivors to a JSON file. Claude reads that JSON and does the rest.

Why it's split that way: everything this script does is free. Everything Claude
does costs usage. So we do as much as possible here and hand Claude a short list.

Run it with:  python job_scraper.py
Output file:  job_results.json
Memory file:  seen_jobs.json  (do not delete unless you want repeats)
"""

import requests
from bs4 import BeautifulSoup
import json, os, re, time, difflib
from datetime import datetime

# ═══════════════════════════════════════════════════════════
#  SETTINGS — the only part you need to edit
# ═══════════════════════════════════════════════════════════

ADZUNA_APP_ID    = os.environ.get("ADZUNA_APP_ID",    "your-adzuna-app-id")
ADZUNA_APP_KEY   = os.environ.get("ADZUNA_APP_KEY",   "your-adzuna-app-key")
USAJOBS_API_KEY  = os.environ.get("USAJOBS_API_KEY",  "your-usajobs-api-key")
USAJOBS_EMAIL    = os.environ.get("USAJOBS_EMAIL",    "your.email@gmail.com")

MAX_FINALISTS = 25          # how many jobs get handed to Claude
DESC_CHARS    = 900         # characters of job description kept per listing

# Per-source caps. Stops one high-volume API from crowding out the smaller
# boards. Adzuna alone can return hundreds; without this it fills the list.
PER_SOURCE_CAP = 3
SOURCE_CAPS = {
    "Adzuna": 4,            # firehose — best few only
    "The Muse": 3,
}

# TOPIC keywords. A title must match at least one of these to survive.
# These are all media, communications, and editorial terms. Nothing generic.
TOPIC_KEYWORDS = [
    # editorial
    "reporter", "editor", "writer", "journalist", "copy editor", "copy desk",
    "editorial", "correspondent", "producer", "newsroom",
    # communications and PR
    "communications", "public affairs", "public relations", "media relations",
    "press secretary", "press officer", "press assistant", "spokesperson",
    "marketing communications",
    # content and digital
    "content", "copywriter", "digital media", "social media", "multimedia",
    "audience", "newsletter", "digital content", "publications",
    # sports
    "sports information", "sports communications", "sports media",
    "sports writer", "sports editor", "athletic communications",
    # policy, his actual beat
    "policy", "fact-check",
]

# LEVEL keywords. These are NOT sufficient on their own — "Associate Attorney"
# and "Service Loyalty Coordinator" both passed the old flat list because of
# these words. They are used only for prescoring, never for relevance.
LEVEL_KEYWORDS = [
    "associate", "coordinator", "assistant editor", "specialist", "junior",
]

# Titles to throw out regardless of anything else
EXCLUDE_TITLE = [
    # seniority
    "senior", "sr.", "director", "vp ", "vice president", "chief", "head of",
    "managing editor", "executive editor", "principal", "lead ", "manager",
    "president", "founder", "partner", "intern",
    # wrong field entirely — these slipped through the old filter
    "attorney", "paralegal", "veterinar", "nurse", "physician", "dentist",
    "customer service", "loyalty", "sales associate", "retail", "cashier",
    "technician", "engineer", "accountant", "bookkeep", "teacher", "tutor",
    "driver", "warehouse", "insurance", "mortgage", "real estate", "claims",
    "therapist", "counselor", "caregiver", "security guard", "janitor",
]

# Locations we care about. Order does not matter here — Claude ranks later.
LOCATIONS = [
    # Miami / South Florida (preferred)
    "miami", "coral gables", "fort lauderdale", "west palm", "boca raton",
    "south florida", "doral", "hialeah", "fl,", ", fl", "(fl)", "florida",
    # New York
    "new york", "nyc", "brooklyn", "manhattan", "queens", "ny,", ", ny", "(ny)",
    # DMV
    "washington, dc", "washington dc", "dc,", ", dc", "arlington", "alexandria",
    "maryland", "baltimore", "annapolis", "bethesda", "silver spring",
    "rockville", "college park", "md,", ", md", "(md)", "virginia", "va,", ", va",
    # remote
    "remote", "work from home", "hybrid", "anywhere",
]

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/122.0.0.0 Safari/537.36"}

SEEN_FILE   = "seen_jobs.json"
OUTPUT_FILE = "job_results.json"

# Per-source diagnostics. Written into job_results.json so failures are
# visible without digging through Actions logs. Every source records why it
# returned what it returned.
DIAG = {}

def diag(source, **kw):
    DIAG.setdefault(source, {}).update(kw)


# ═══════════════════════════════════════════════════════════
#  FILTERING
# ═══════════════════════════════════════════════════════════

def is_relevant(title, location=""):
    """
    A title must contain a TOPIC word. Level words like "associate" and
    "coordinator" are never sufficient on their own — that bug is what let
    "Associate Attorney" and "Service Loyalty Coordinator" through.
    """
    t, l = title.lower(), location.lower()
    if any(bad in t for bad in EXCLUDE_TITLE):
        return False
    if not any(kw in t for kw in TOPIC_KEYWORDS):
        return False
    if location and not any(loc in l for loc in LOCATIONS):
        return False
    return True


def fuzzy_dedupe(jobs, threshold=0.85):
    """Collapse the same job posted on several boards."""
    out = []
    for job in jobs:
        ta, ea = job["title"].lower().strip(), job.get("employer", "").lower().strip()
        dupe = False
        for kept in out:
            tb, eb = kept["title"].lower().strip(), kept.get("employer", "").lower().strip()
            same_title = difflib.SequenceMatcher(None, ta, tb).ratio() >= threshold
            same_emp = (not ea or not eb or
                        difflib.SequenceMatcher(None, ea, eb).ratio() > 0.8)
            if same_title and same_emp:
                dupe = True
                break
        if not dupe:
            out.append(job)
    removed = len(jobs) - len(out)
    if removed:
        print(f"  Removed {removed} cross-board duplicates")
    return out


def prescore(job):
    """
    Cheap quality heuristic used only to decide which jobs from a single
    source are worth spending a link check on. This is NOT the real score —
    Claude does that later against the full description. This just picks the
    best few from a source that returned fifty.
    """
    t = job["title"].lower()
    l = job.get("location", "").lower()
    s = 0

    # Title signals we specifically want
    for good in ("associate", "coordinator", "assistant editor", "editor",
                 "communications", "writer", "reporter", "content"):
        if good in t:
            s += 2
            break
    if any(lv in t for lv in LEVEL_KEYWORDS):
        s += 2

    # Location priority
    if any(p in l for p in ("miami", "coral gables", "fort lauderdale",
                            "south florida", "boca raton")):
        s += 4
    elif any(p in l for p in ("new york", "nyc", "brooklyn", "manhattan")):
        s += 3
    elif any(p in l for p in ("washington", "maryland", "baltimore",
                              "annapolis", "arlington", "bethesda")):
        s += 3
    elif "remote" in l:
        s += 3

    # Knowing the employer is a sign of a real, complete listing
    if job.get("employer"):
        s += 1

    return s


def balance_sources(jobs):
    """
    Reorder so the finalist list is varied by construction.

    Groups by source, sorts each group by prescore, then takes one from each
    source in rotation. A board that returned two jobs still gets both of them
    looked at before Adzuna's fifth-best.
    """
    buckets = {}
    for job in jobs:
        buckets.setdefault(job["source"], []).append(job)

    for src in buckets:
        cap = SOURCE_CAPS.get(src, PER_SOURCE_CAP)
        buckets[src].sort(key=prescore, reverse=True)
        buckets[src] = buckets[src][:cap]

    # Round-robin: one per source per pass, smallest sources first so they
    # are never the ones squeezed out by the finalist limit.
    order = sorted(buckets, key=lambda s: len(buckets[s]))
    out, depth = [], 0
    while any(len(buckets[s]) > depth for s in order):
        for src in order:
            if len(buckets[src]) > depth:
                out.append(buckets[src][depth])
        depth += 1

    print(f"  Balanced across {len(buckets)} sources "
          f"({', '.join(f'{s}:{len(b)}' for s, b in sorted(buckets.items()))})")
    return out


# ═══════════════════════════════════════════════════════════
#  LINK CHECKING — fixes the dead-link problem in code
# ═══════════════════════════════════════════════════════════

DEAD_PAGE_SIGNS = [
    "no longer accepting", "position has been filled", "job has expired",
    "posting is closed", "this job is no longer", "no longer available",
    "page not found", "404", "position filled",
]

# If the link lands on one of these, it's a careers homepage, not a posting
GENERIC_ENDINGS = ("/jobs", "/jobs/", "/careers", "/careers/", "/search", "/search/")


def link_is_good(url):
    """
    Returns (True, description_text) if the link loads a real, live posting.
    Returns (False, reason) otherwise.
    """
    if not url or not url.startswith("http"):
        return False, "no url"
    try:
        r = requests.get(url, headers=HEADERS, timeout=12, allow_redirects=True)
    except Exception as e:
        return False, f"unreachable ({type(e).__name__})"

    if r.status_code >= 400:
        return False, f"http {r.status_code}"

    final = r.url.lower().rstrip("/")
    if final.endswith(GENERIC_ENDINGS):
        return False, "redirected to careers index"

    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["nav", "header", "footer", "script", "style", "aside"]):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    low = text[:3000].lower()
    for sign in DEAD_PAGE_SIGNS:
        if sign in low:
            return False, "posting closed"

    if len(text) < 300:
        return False, "page too thin to be a posting"

    return True, text[:DESC_CHARS]


# ═══════════════════════════════════════════════════════════
#  GENERIC HTML BOARD SCRAPER
# ═══════════════════════════════════════════════════════════

def scrape_board(name, url, base=None):
    """
    Handles any plain-HTML job board. Tries structured selectors first, then
    falls back to scanning anchors. Records diagnostics either way so a board
    that returns nothing tells you WHY instead of failing silently.
    """
    jobs = []
    base = base or "/".join(url.split("/")[:3])
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        diag(name, http=r.status_code)

        if r.status_code != 200:
            reason = ("blocked or rate limited" if r.status_code in (403, 429)
                      else f"http {r.status_code}")
            diag(name, raw=0, kept=0, note=reason)
            print(f"  {name}: 0  ({reason})")
            return jobs

        soup = BeautifulSoup(r.text, "html.parser")

        rows = (soup.select("article, .job, .job-listing, .job-result, .job-card, "
                            ".jb-job-list-row, li.job, tr.data-row, .views-row")
                or soup.find_all("div", class_=lambda c: c and "job" in c.lower()))

        method = "selectors"
        if not rows:
            # Fallback: any anchor whose href looks like a job detail page.
            method = "anchor-scan"
            rows = [a for a in soup.find_all("a", href=True)
                    if any(k in a["href"].lower()
                           for k in ("/job", "/jobs/", "/career", "/position", "/opening"))]

        diag(name, raw=len(rows), method=method)

        for row in rows:
            if row.name == "a":
                title, a_tag = row.get_text(strip=True), row
                location = employer = ""
            else:
                t_tag = row.find(["h2", "h3", "h4"]) or row.find("a")
                if not t_tag:
                    continue
                title = t_tag.get_text(strip=True)
                l_tag = row.find(class_=lambda c: c and "location" in c.lower())
                e_tag = row.find(class_=lambda c: c and any(
                    k in c.lower() for k in ("company", "employer", "organization", "department")))
                a_tag = row.find("a", href=True)
                location = l_tag.get_text(strip=True) if l_tag else ""
                employer = e_tag.get_text(strip=True) if e_tag else ""

            if not title or len(title) < 4:
                continue

            link = a_tag["href"] if a_tag and a_tag.has_attr("href") else url
            if not link.startswith("http"):
                link = base.rstrip("/") + "/" + link.lstrip("/")

            if is_relevant(title, location):
                jobs.append({"title": title, "employer": employer,
                             "location": location, "link": link, "source": name})

        diag(name, kept=len(jobs))
        note = ""
        if len(rows) == 0:
            note = "  (no listing elements found — layout likely changed or JS-rendered)"
        elif len(jobs) == 0:
            note = f"  ({len(rows)} candidates found, none matched filters)"
        print(f"  {name}: {len(jobs)}{note}")

    except Exception as e:
        diag(name, raw=0, kept=0, note=f"error: {type(e).__name__}")
        print(f"  {name}: 0  (error: {type(e).__name__})")
    return jobs


HTML_BOARDS = [
    ("JournalismJobs",   "https://www.journalismjobs.com/journalism-jobs"),
    ("MediaBistro",      "https://www.mediabistro.com/jobs/search/"),
    ("Poynter",          "https://www.poynter.org/media-jobs/"),
    ("IRE",              "https://www.ire.org/jobs/"),
    ("SPJ",              "https://jobs.spj.org/jobs/"),
    ("States Newsroom",  "https://statesnewsroom.com/jobs/"),
    ("INN",              "https://inn.org/jobs/"),
    ("PRSA",             "https://jobs.prsa.org/jobs/"),
    ("Ragan TalentHub",  "https://www.ragan.com/talenthub/"),
    ("PR News",          "https://jobs.prnewsonline.com/jobs/"),
    ("PRWeek",           "https://www.prweek.com/us/jobs"),
    ("Ed2010",           "https://www.ed2010.com/jobs"),
    ("Idealist",         "https://www.idealist.org/en/jobs?q=communications"),
    ("Work In Sports",   "https://www.workinsports.com/search-jobs?keywords=communications"),
    ("MD State Jobs",    "https://www.jobapscloud.com/MD/sup/bulklist.aspx"),
]


# ═══════════════════════════════════════════════════════════
#  API SOURCES
# ═══════════════════════════════════════════════════════════

def scrape_adzuna():
    """Aggregates hundreds of sources including LinkedIn and Glassdoor."""
    jobs = []
    if ADZUNA_APP_ID.startswith("your-"):
        diag("Adzuna", kept=0, note="SKIPPED — no API key set")
        print("  Adzuna: skipped (no key)")
        return jobs
    queries = ["communications", "editor", "content writer", "public affairs",
               "journalist", "social media"]
    places = ["miami", "new york", "washington dc", "maryland"]
    for q in queries:
        for where in places:
            try:
                r = requests.get(
                    "https://api.adzuna.com/v1/api/jobs/us/search/1",
                    params={"app_id": ADZUNA_APP_ID, "app_key": ADZUNA_APP_KEY,
                            "results_per_page": 20, "what": q, "where": where,
                            "sort_by": "date", "max_days_old": 5},
                    timeout=15)
                if r.status_code != 200:
                    diag("Adzuna", http=r.status_code, kept=0,
                         note="check ADZUNA_APP_ID / ADZUNA_APP_KEY secrets")
                    print(f"  Adzuna: 0  (http {r.status_code})")
                    return jobs
                for it in r.json().get("results", []):
                    title = it.get("title", "")
                    loc = it.get("location", {}).get("display_name", "")
                    if is_relevant(title, loc):
                        jobs.append({
                            "title": title,
                            "employer": it.get("company", {}).get("display_name", ""),
                            "location": loc,
                            "link": it.get("redirect_url", ""),
                            "source": "Adzuna"})
                time.sleep(0.4)
            except Exception:
                pass
    diag("Adzuna", kept=len(jobs))
    print(f"  Adzuna: {len(jobs)}")
    return jobs


def scrape_muse():
    """The Muse — free, no key needed."""
    jobs, raw = [], 0
    try:
        for page in (1, 2):
            r = requests.get("https://www.themuse.com/api/public/jobs",
                             params={"category": "Media, PR & Communications",
                                     "page": page, "descending": "true"},
                             headers=HEADERS, timeout=15)
            diag("The Muse", http=r.status_code)
            if r.status_code != 200:
                diag("The Muse", note=f"http {r.status_code}")
                break
            batch = r.json().get("results", [])
            raw += len(batch)
            for it in batch:
                title = it.get("name", "")
                loc = ", ".join(l.get("name", "") for l in it.get("locations", []))
                if is_relevant(title, loc):
                    jobs.append({
                        "title": title,
                        "employer": it.get("company", {}).get("name", ""),
                        "location": loc,
                        "link": it.get("refs", {}).get("landing_page", ""),
                        "source": "The Muse"})
            time.sleep(0.4)
    except Exception as e:
        diag("The Muse", note=f"error: {type(e).__name__}")
    diag("The Muse", raw=raw, kept=len(jobs))
    note = f"  ({raw} returned by API, none matched filters)" if raw and not jobs else ""
    print(f"  The Muse: {len(jobs)}{note}")
    return jobs


def scrape_usajobs():
    """Federal roles — free official API."""
    jobs = []
    if USAJOBS_API_KEY.startswith("your-"):
        diag("USAJobs", kept=0,
             note="SKIPPED — USAJOBS_API_KEY secret not set in the repo")
        print("  USAJobs: skipped (no key)")
        return jobs
    for kw in ["communications", "public affairs", "writer editor"]:
        for state in ["FL", "NY", "MD", "DC"]:
            try:
                r = requests.get("https://data.usajobs.gov/api/search",
                                 headers={"Host": "data.usajobs.gov",
                                          "User-Agent": USAJOBS_EMAIL,
                                          "Authorization-Key": USAJOBS_API_KEY},
                                 params={"Keyword": kw, "LocationName": state,
                                         "ResultsPerPage": 20},
                                 timeout=15)
                if r.status_code != 200:
                    diag("USAJobs", http=r.status_code, kept=0,
                         note="check USAJOBS_API_KEY and USAJOBS_EMAIL secrets")
                    print(f"  USAJobs: 0  (http {r.status_code})")
                    return jobs
                for it in r.json().get("SearchResult", {}).get("SearchResultItems", []):
                    p = it.get("MatchedObjectDescriptor", {})
                    title = p.get("PositionTitle", "")
                    loc = p.get("PositionLocationDisplay", "")
                    if is_relevant(title, loc):
                        jobs.append({"title": title,
                                     "employer": p.get("OrganizationName", ""),
                                     "location": loc,
                                     "link": p.get("PositionURI", ""),
                                     "source": "USAJobs"})
                time.sleep(0.6)
            except Exception:
                pass
    diag("USAJobs", kept=len(jobs))
    print(f"  USAJobs: {len(jobs)}")
    return jobs


# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════

def preflight():
    """
    Confirm we can actually reach the open internet before doing anything.
    Without this the script quietly writes an empty result file when it is
    running somewhere with a restricted network, and the digest looks fine
    while being silently empty every day.
    """
    probes = ["https://www.themuse.com/api/public/jobs?page=1",
              "https://www.journalismjobs.com/"]
    for url in probes:
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.status_code < 500:
                return
        except Exception:
            continue
    raise SystemExit(
        "\nFATAL: no network access to job boards from this environment.\n"
        "Every source would return nothing and the digest would be silently\n"
        "empty. Refusing to write a misleading result file.\n"
        "This script must run somewhere with open outbound network access,\n"
        "such as GitHub Actions. It cannot run inside the Cowork sandbox.\n")


def main():
    print(f"\nJob collection starting — {datetime.now():%Y-%m-%d %H:%M}\n")
    preflight()

    seen = set()
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            seen = set(json.load(f))
    print(f"{len(seen)} jobs already sent in previous runs\n")

    print("Collecting:")
    jobs = []
    for name, url in HTML_BOARDS:
        jobs += scrape_board(name, url)
        time.sleep(0.3)
    jobs += scrape_adzuna()
    jobs += scrape_muse()
    jobs += scrape_usajobs()

    print(f"\nRaw matches: {len(jobs)}")

    jobs = fuzzy_dedupe(jobs)
    jobs = [j for j in jobs if j["link"] not in seen]
    print(f"New since last run: {len(jobs)}")

    if not jobs:
        with open(OUTPUT_FILE, "w") as f:
            json.dump({"generated": datetime.now().isoformat(),
                       "count": 0, "source_counts": {}, "jobs": []}, f, indent=2)
        print("\nNothing new today. Wrote empty job_results.json")
        return

    jobs = balance_sources(jobs)

    print(f"\nChecking links (this is the slow part)...")
    finalists, dead = [], 0
    for job in jobs:
        if len(finalists) >= MAX_FINALISTS:
            break
        ok, result = link_is_good(job["link"])
        if ok:
            job["description"] = result
            finalists.append(job)
        else:
            dead += 1
        time.sleep(0.3)

    counts = {}
    for j in finalists:
        counts[j["source"]] = counts.get(j["source"], 0) + 1

    print(f"  {len(finalists)} live postings, {dead} dead links dropped")
    print(f"  Final spread: {counts}")

    with open(OUTPUT_FILE, "w") as f:
        json.dump({"generated": datetime.now().isoformat(),
                   "count": len(finalists),
                   "dead_links_dropped": dead,
                   "source_counts": counts,
                   "diagnostics": DIAG,
                   "jobs": finalists}, f, indent=2)

    # Mark everything checked as seen, including the dead ones
    seen.update(j["link"] for j in jobs)
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=2)

    print(f"\nDone. Wrote {len(finalists)} jobs to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
