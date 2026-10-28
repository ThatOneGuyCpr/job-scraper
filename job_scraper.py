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
import json, os, re, time, difflib, warnings
from bs4 import XMLParsedAsHTMLWarning
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
from datetime import datetime, timedelta

# ═══════════════════════════════════════════════════════════
#  SETTINGS — the only part you need to edit
# ═══════════════════════════════════════════════════════════

ADZUNA_APP_ID    = os.environ.get("ADZUNA_APP_ID",    "your-adzuna-app-id")
ADZUNA_APP_KEY   = os.environ.get("ADZUNA_APP_KEY",   "your-adzuna-app-key")
USAJOBS_API_KEY  = os.environ.get("USAJOBS_API_KEY",  "your-usajobs-api-key")
USAJOBS_EMAIL    = os.environ.get("USAJOBS_EMAIL",    "your.email@gmail.com")

# Gmail account the job newsletters land in. Use a DEDICATED address that these
# newsletters forward to, not your primary mailbox — an app password grants full
# read access to whatever account it belongs to.
GMAIL_ADDRESS  = os.environ.get("GMAIL_ADDRESS",  "your.address@gmail.com")
GMAIL_APP_PASS = os.environ.get("GMAIL_APP_PASS", "your-16-char-app-password")

# Sender address or domain -> source label.
#
# This is how boards that have no public listings page get in: you subscribe to
# their alert email, and the scraper reads it. LinkedIn matters most here, since
# its listings cannot be scraped at all.
#
# Use the most specific sender you can. "linkedin.com" would match every
# LinkedIn email you get, including network updates, so match the alert
# address instead. Comment out anything you have not signed up for.
NEWSLETTER_SENDERS = {
    "studyhall.xyz":                  "Study Hall",
    "jobalerts-noreply@linkedin.com": "LinkedIn",
    "jobs-listings@linkedin.com":     "LinkedIn",
    # "alert@indeed.com":             "Indeed",
    # "noreply@journalismjobs.com":   "JournalismJobs",
    # "jobs@mediabistro.com":         "MediaBistro",
    # "noreply@idealist.org":         "Idealist",
    # "jobs@mediajobboard.com":       "Media Job Board",
    #
    # These four block the scraper outright, so email is the only way in:
    # "noreply@inn.org":              "INN",
    # "careers@journalists.org":      "ONA",
    # "jobs@prnewsonline.com":        "PR News",
    # "alerts@workinsports.com":      "Work In Sports",
}
NEWSLETTER_LOOKBACK_DAYS = 8      # Study Hall's opportunities email is weekly
NEWSLETTER_CHAR_CAP      = 22000  # per email. Study Hall's roundup runs long and
                                  # the old 7000 cap was cutting off most of the
                                  # listings before Claude ever saw them.
NEWSLETTER_TOTAL_CAP     = 50000  # across all emails in one run, so a busy week
                                  # cannot blow up usage

# Which email sources get their full text handed to Claude. Only prose
# newsletters belong here, where listings can't be pulled out as links.
# LinkedIn alerts are structured job cards that the scraper reads directly.
# Sending their text as well made Claude pull the same jobs out a second time,
# which is why LinkedIn flooded the digest.
DIGEST_SOURCES = {"Study Hall"}

MAX_FINALISTS = 25          # how many jobs get handed to Claude
DESC_CHARS    = 900         # characters of job description kept per listing

# Per-source caps. Stops one high-volume API from crowding out the smaller
# boards. Adzuna alone can return hundreds; without this it fills the list.
PER_SOURCE_CAP = 3
SOURCE_CAPS = {
    "Adzuna": 4,
    "Study Hall": 5,      # curated, high signal, worth more slots
    "LinkedIn": 3,        # cannot be reached any other way, but it was
                          # crowding out every other source at 5
    "Indeed": 3,
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
    "president", "founder", "partner",
    # "intern" is checked as a whole word in is_relevant. As a plain substring
    # it also matched "Internal Communications" and threw those roles out.
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

# A bare User-Agent is the most common reason a board returns 403. Real browsers
# send a dozen headers; sending only one is a strong bot signal. This set mimics
# Chrome closely enough to get past most basic filtering.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "DNT": "1",
}

# One session for the whole run. Cookies persist, which matters for sites that
# set a token on first contact and reject requests that arrive without it.
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def fetch(url, timeout=20, retry_403=True):
    """
    GET a page through the shared session. On a 403 or 429, wait and retry once
    with a Referer set, which is enough for some filters. Returns the response
    either way so the caller can record the status.
    """
    r = SESSION.get(url, timeout=timeout)
    if r.status_code in (403, 429) and retry_403:
        time.sleep(3)
        host = "/".join(url.split("/")[:3])
        r = SESSION.get(url, timeout=timeout,
                        headers={"Referer": host + "/", "Sec-Fetch-Site": "same-origin"})
    return r


# Query parameters that only track the click. They change on every email and
# every API call, so a link that carries them never matches the copy already in
# seen_jobs.json, and the same job comes back day after day.
TRACKING_PARAMS = ("utm_", "trackingid", "refid", "trk", "midtoken", "midsig",
                   "otptoken", "lipi")

def normalize_link(url):
    """One canonical form per posting, so dedup can compare links reliably."""
    if not url:
        return url
    m = re.search(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)", url)
    if m:
        return f"https://www.linkedin.com/jobs/view/{m.group(1)}/"
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    try:
        s = urlsplit(url)
        q = [(k, v) for k, v in parse_qsl(s.query, keep_blank_values=True)
             if not k.lower().startswith(TRACKING_PARAMS)]
        return urlunsplit((s.scheme, s.netloc, s.path, urlencode(q), ""))
    except ValueError:
        return url


SEEN_FILE   = "seen_jobs.json"

def fingerprint(job):
    """
    A source-independent identity for a job, so the same posting is recognised
    whether it arrives from LinkedIn, Adzuna, or a board scrape. Links differ
    between sources, which is why link-only deduplication let the same role
    reappear on consecutive days.

    Returns None when there is no employer to anchor on. Title alone is unsafe:
    fingerprinting on "Associate Editor" would suppress every future Associate
    Editor posting anywhere.
    """
    emp = re.sub(r"[^a-z0-9]", "", (job.get("employer") or "").lower())
    # Drop suffixes that vary between sources for the same company
    for tail in ("digital", "media", "inc", "llc", "group", "company", "corp",
                 "news", "magazine", "publishing"):
        if emp.endswith(tail) and len(emp) > len(tail) + 3:
            emp = emp[:-len(tail)]
    if not emp or len(emp) < 3:
        return None

    title = re.sub(r"[^a-z0-9 ]", " ", (job.get("title") or "").lower())
    title = " ".join(title.split())
    if not title:
        return None
    return f"{title}|{emp}"


def seen_before(job, seen_fp):
    """
    True if this job matches something already sent. Exact match first, then a
    tolerant comparison, because sources write employer names differently:
    "Newsweek" against "Newsweek Digital", "Vox" against "Vox Media".
    """
    fp = fingerprint(job)
    if not fp:
        return False
    if fp in seen_fp:
        return True

    title, emp = fp.split("|", 1)
    for old in seen_fp:
        o_title, o_emp = old.split("|", 1)
        if o_title != title:
            continue
        # Same title. Treat the employers as the same if one contains the
        # other, or they are close enough to be a naming variant.
        if emp.startswith(o_emp) or o_emp.startswith(emp):
            return True
        if difflib.SequenceMatcher(None, emp, o_emp).ratio() > 0.85:
            return True
    return False


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

def clean_title(raw):
    """
    Anchor-scan grabs whatever text sits inside a link, which on card-based
    boards is the entire listing: employer, posting age, title, location, and
    the first line of the description all run together. Salvage the title or
    return empty to drop it.
    """
    t = re.sub(r"\s+", " ", raw).strip()

    # Strip a leading "N days ago" / "NEW" / "Featured" badge anywhere early
    t = re.sub(r"\b\d+\s*(?:days?|hours?|weeks?|months?)\s*ago\b", " ", t, flags=re.I)
    t = re.sub(r"^\s*(?:NEW|Featured|Urgent|Hot)\b[\s:-]*", "", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip(" -|,")

    # A real job title is short. Anything long is a mashed-up card.
    if len(t) > 90 or len(t) < 4:
        return ""
    return t


# URLs that are category or search pages rather than a single posting
CATEGORY_URL_HINTS = (
    "/jobs/copywriter", "/jobs/editor", "/jobs/writer", "/jobs/reporter",
    "/search", "/category", "/browse", "/all-jobs", "?page=", "/tag/",
)

def is_job_url(url):
    """
    Reject links that point at a list of jobs instead of one job. A real posting
    URL almost always carries an ID: digits, or a long hyphenated slug.
    """
    u = url.lower().rstrip("/")
    if any(h in u for h in CATEGORY_URL_HINTS):
        return False
    tail = u.split("/")[-1].split("?")[0]
    query = u.split("?", 1)[1] if "?" in u else ""
    if not tail:
        return False
    has_digits = any(c.isdigit() for c in tail) or bool(re.search(r"=\d{3,}", query))
    long_slug  = tail.count("-") >= 2     # "reporter-oklahoma-voice"
    return has_digits or long_slug


def is_relevant(title, location=""):
    """
    A title must contain a TOPIC word. Level words like "associate" and
    "coordinator" are never sufficient on their own — that bug is what let
    "Associate Attorney" and "Service Loyalty Coordinator" through.
    """
    t, l = title.lower(), location.lower()
    if any(bad in t for bad in EXCLUDE_TITLE):
        return False
    if re.search(r"\bintern(s|ship|ships)?\b", t):
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
    "page not found", "error 404", "404 error", "404 not found",
    "position filled",
    # A bare "404" used to be on this list. It matched federal pay ranges
    # like "$84,404" and threw out live USAJobs postings.
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
        r = fetch(url, timeout=12, retry_403=False)
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

# Links that point at a single posting, used when a board has no recognisable
# listing markup. "/staff-openings/" is States Newsroom; "bulpreview" is
# Maryland's state jobs system.
JOB_LINK_HINTS = ("/job", "/jobs/", "-job/", "/career", "/position", "/opening",
                  "/staff-openings/", "bulpreview", "/vacanc", "/posting")

# Anchor text that says nothing about the job. When a board links each listing
# with "Learn more", the title has to come from the heading above the link.
GENERIC_ANCHORS = {"learn more", "read more", "apply", "apply now", "view job",
                   "details", "more", "view", "see details", "view details",
                   "more info", "view posting", "continue reading"}

JOB_URL_IN_SITEMAP = re.compile(r"/jobs?/[a-z0-9][a-z0-9-]{6,}/?(\d+)?/?$", re.I)


def _abs(link, base):
    if not link:
        return ""
    if link.startswith("//"):
        return "https:" + link
    if not link.startswith("http"):
        return base.rstrip("/") + "/" + link.lstrip("/")
    return link


def _slug_title(url):
    """Best-effort title from a URL slug: /job/associate-editor/83086473/."""
    parts = [p for p in url.split("?")[0].rstrip("/").split("/") if p]
    for p in reversed(parts):
        p = re.sub(r"^\d+-", "", p)           # MediaBistro: 3543053054-junior-writer
        if re.search(r"[a-z]", p, re.I) and "-" in p and not p.endswith((".html", ".asp")):
            return p.replace("-", " ").strip().title()
    return ""


def _from_jsonld(soup, base):
    """
    schema.org JobPosting data. Job boards embed it so Google for Jobs can index
    their listings, and it is in the page even when the visible list is drawn
    by JavaScript.
    """
    out = []
    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            kind = node.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if "JobPosting" in kinds and node.get("title"):
                org = node.get("hiringOrganization") or {}
                loc = node.get("jobLocation") or {}
                if isinstance(loc, list):
                    loc = loc[0] if loc else {}
                addr = (loc.get("address") or {}) if isinstance(loc, dict) else {}
                place = ", ".join(x for x in (addr.get("addressLocality"),
                                              addr.get("addressRegion")) if isinstance(x, str) and x)
                if node.get("jobLocationType") == "TELECOMMUTE":
                    place = f"{place} (Remote)" if place else "Remote"
                out.append({"title": node["title"],
                            "employer": org.get("name", "") if isinstance(org, dict) else "",
                            "location": place,
                            "link": _abs(node.get("url") or node.get("@id") or "", base)})
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v)
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(tag.string or tag.get_text() or "null"))
        except (ValueError, TypeError):
            continue
    return [j for j in out if j["link"]]


def _from_embedded_json(soup, base):
    """
    Data that JavaScript-built sites ship inside the page (Next.js puts it in
    __NEXT_DATA__). Looks for objects with a job title and a link to a posting.
    """
    out, seen = [], set()
    def walk(node, depth=0):
        if depth > 14:
            return
        if isinstance(node, list):
            for n in node:
                walk(n, depth + 1)
        elif isinstance(node, dict):
            title = next((node[k] for k in ("jobTitle", "title", "name")
                          if isinstance(node.get(k), str)), "")
            link = next((node[k] for k in ("url", "absolute_url", "applyUrl", "link", "href")
                         if isinstance(node.get(k), str)), "")
            if title and link and any(h in link.lower() for h in JOB_LINK_HINTS):
                link = _abs(link, base)
                if link not in seen:
                    seen.add(link)
                    emp = node.get("orgName") or node.get("companyName") or node.get("company") or ""
                    out.append({"title": title,
                                "employer": emp if isinstance(emp, str) else "",
                                "location": "", "link": link})
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v, depth + 1)
    for tag in soup.find_all("script"):
        raw = (tag.string or "").strip()
        if tag.get("id") == "__NEXT_DATA__" or tag.get("type") == "application/json" \
                or raw.startswith(("{", "[")):
            try:
                walk(json.loads(raw))
            except (ValueError, TypeError):
                continue
    return out


def _parse_feed(xml):
    out = []
    soup = BeautifulSoup(xml, "html.parser")
    for item in soup.find_all(["item", "entry"]):
        t = item.find("title")
        l = item.find("link")
        link = ""
        if l is not None:
            link = l.get("href") or l.get_text(strip=True) or (l.next_sibling or "").strip()
        if t and link:
            out.append({"title": t.get_text(strip=True), "employer": "",
                        "location": "", "link": link.strip()})
    return out


def _from_feed(soup, url, base):
    """An RSS or Atom feed: the one the page advertises, else a few common paths."""
    tried = []
    for tag in soup.find_all("link", href=True):
        if "rss" in (tag.get("type") or "") or "atom" in (tag.get("type") or ""):
            tried.append(_abs(tag["href"], base))
    root = url.split("?")[0].rstrip("/")
    tried += [root + "/feed/", base.rstrip("/") + "/jobs/feed/", base.rstrip("/") + "/rss"]
    for feed in dict.fromkeys(tried):
        try:
            r = fetch(feed, timeout=12, retry_403=False)
        except Exception:
            continue
        if r.status_code == 200 and ("<rss" in r.text[:500] or "<feed" in r.text[:500]):
            jobs = _parse_feed(r.text)
            if jobs:
                return jobs
    return []


def _from_sitemap(base):
    """
    The site's sitemap, which lists every page including each posting, and is
    plain XML that needs no JavaScript. Posting URLs usually carry the title as
    a slug, which is enough to filter on; the link check fills in the rest.
    """
    try:
        r = fetch(base.rstrip("/") + "/sitemap.xml", timeout=15, retry_403=False)
    except Exception:
        return []
    if r.status_code != 200:
        return []
    locs = re.findall(r"<loc>\s*([^<]+?)\s*</loc>", r.text)
    # A sitemap index points at further sitemaps; follow the job-related ones
    if locs and all(l.endswith(".xml") for l in locs[:5]):
        sub = [l for l in locs if "job" in l.lower()][:3] or locs[:2]
        locs = []
        for sm in sub:
            try:
                rr = fetch(sm, timeout=15, retry_403=False)
                if rr.status_code == 200:
                    locs += re.findall(r"<loc>\s*([^<]+?)\s*</loc>", rr.text)
            except Exception:
                continue
    out = []
    for loc in locs:
        if JOB_URL_IN_SITEMAP.search(loc.split("?")[0]):
            title = _slug_title(loc)
            if title:
                out.append({"title": title, "employer": "", "location": "", "link": loc})
    return out[-400:]     # newest postings are usually listed last


def _from_html(soup, url, base):
    """Listing markup first, then any link that looks like a single posting."""
    rows = (soup.select("article, .job, .job-listing, .job-result, .job-card, "
                        ".jb-job-list-row, li.job, tr.data-row, .views-row")
            or soup.find_all("div", class_=lambda c: c and "job" in c.lower()))
    out = []
    if len(rows) >= 2:
        for row in rows:
            t_tag = row.find(["h2", "h3", "h4"]) or row.find("a")
            if not t_tag:
                continue
            l_tag = row.find(class_=lambda c: c and "location" in c.lower())
            e_tag = row.find(class_=lambda c: c and any(
                k in c.lower() for k in ("company", "employer", "organization", "department")))
            a_tag = row.find("a", href=True)
            out.append({"title": t_tag.get_text(strip=True),
                        "employer": e_tag.get_text(strip=True) if e_tag else "",
                        "location": l_tag.get_text(strip=True) if l_tag else "",
                        "link": _abs(a_tag["href"] if a_tag else url, base)})
        # One stray <article> wrapping the whole page is not a listing. Only
        # trust the markup when it yields several distinct postings.
        good = [j for j in out if j["link"] != url and is_job_url(j["link"])]
        if len({j["link"] for j in good}) >= 2:
            return good, "selectors"
        out = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not any(k in href.lower() for k in JOB_LINK_HINTS):
            continue
        link = _abs(href, base)
        if not is_job_url(link):
            continue
        # When the link wraps a whole card, the heading inside it is the
        # title; otherwise the first line is, before employer and location.
        inner = a.find(["h1", "h2", "h3", "h4", "h5", "strong"])
        lines = [l for l in a.get_text("\n", strip=True).split("\n") if l.strip()]
        text = inner.get_text(" ", strip=True) if inner else (lines[0] if lines else "")
        if text.lower().strip(" »›>→") in GENERIC_ANCHORS or len(text) < 4:
            head = a.find_previous(["h1", "h2", "h3", "h4"])
            text = head.get_text(" ", strip=True) if head else _slug_title(link)
        out.append({"title": text, "employer": "", "location": "", "link": link})
    return out, "anchor-scan"


def scrape_board(name, urls):
    """
    Collect listings from a job board, trying several ways in until one works.

    Many boards send a near-empty page and draw the listings with JavaScript,
    which this script can't run. The listings still exist somewhere readable:
    structured data embedded for Google, data bundled into the page, an RSS
    feed, or the sitemap. The first method that turns anything up wins, and the
    diagnostics record which one, so a board going quiet shows why.
    """
    if isinstance(urls, str):
        urls = [urls]
    jobs, methods, statuses, raw_total = [], [], [], 0
    seen_links = set()

    for url in urls:
        base = "/".join(url.split("/")[:3])
        try:
            r = fetch(url)
        except Exception as e:
            statuses.append(f"error {type(e).__name__}")
            continue
        statuses.append(str(r.status_code))
        if r.status_code != 200:
            continue

        soup = BeautifulSoup(r.text, "html.parser")
        # Structured data and the visible links are read together, since a
        # page can carry structured data for only a featured job or two.
        ld = _from_jsonld(soup, base)
        html_jobs, method = _from_html(soup, url, base)
        found = ld + [j for j in html_jobs if j["link"] not in {x["link"] for x in ld}]
        if ld:
            method = "structured data" + ("+" + method if html_jobs else "")
        if not found:
            found, method = _from_embedded_json(soup, base), "page data"
        if not found:
            found, method = _from_feed(soup, url, base), "rss feed"
        if not found:
            found, method = _from_sitemap(base), "sitemap"
        if not found:
            continue

        methods.append(method)
        raw_total += len(found)
        for j in found:
            title = clean_title(j["title"])
            link = j["link"]
            if not title or not link or link in seen_links:
                continue
            if method == "anchor-scan" and not is_job_url(link):
                continue
            if is_relevant(title, j.get("location", "")):
                seen_links.add(link)
                jobs.append({"title": title, "employer": j.get("employer", ""),
                             "location": j.get("location", ""), "link": link,
                             "source": name})
        time.sleep(0.3)

    method = "/".join(dict.fromkeys(methods)) or "none"
    http = ",".join(dict.fromkeys(statuses))
    if not methods:
        if statuses and all(s in ("403", "429") for s in statuses):
            note = "blocked or rate limited"
        elif statuses and all(s == "404" for s in statuses):
            note = "page not found (404): the board moved its listings"
        elif statuses and not any(s == "200" for s in statuses):
            note = f"http {http}"
        else:
            note = "page loaded but no listings found by any method"
        diag(name, http=http, raw=0, kept=0, method="none", note=note)
        print(f"  {name}: 0  ({note})")
    else:
        diag(name, http=http, raw=raw_total, method=method, kept=len(jobs))
        print(f"  {name}: {len(jobs)}  (via {method}, {raw_total} listings read)")
    return jobs


# Job boards. Each entry is a name and one or more listing pages. Several of
# these draw their listings with JavaScript; scrape_board works around that
# (see its docstring). Addresses were updated in October 2026: MediaBistro,
# Media Job Board, SPJ, PRSA and Maryland had all moved their listings.
HTML_BOARDS = [
    ("JournalismJobs",   "https://www.journalismjobs.com/"),
    ("IRE",              "https://www.ire.org/jobs/"),
    ("MediaBistro",      ["https://www.mediabistro.com/jobs?category=Writing+%26+Editing",
                          "https://www.mediabistro.com/jobs?category=Marketing+%26+Communications"]),
    ("Media Job Board",  ["https://jobs.mediajobboard.com/search?keyword=journalism&location=Remote",
                          "https://jobs.mediajobboard.com/search?keyword=communications"]),
    ("SPJ",              ["https://jobs.spj.org/q-reporter-jobs.html",
                          "https://jobs.spj.org/q-editor-jobs.html"]),
    ("PRSA",             ["https://jobs.prsa.org/q-communications-jobs.html",
                          "https://jobs.prsa.org/l-remote-jobs.html"]),
    ("States Newsroom",  "https://statesnewsroom.com/staff-openings/"),
    ("Ragan TalentHub",  "https://www.ragan.com/talenthub/"),
    ("Idealist",         "https://www.idealist.org/en/jobs?q=communications"),
    ("MD State Jobs",    "https://www.jobapscloud.com/MD/"),
    # ONA blocks GitHub's servers outright (403). It stays in case that
    # changes, but its job alert email is the reliable route.
    ("ONA Career Center","https://journalists.careerwebsite.com/jobs/"),
]


# ═══════════════════════════════════════════════════════════
#  API SOURCES
# ═══════════════════════════════════════════════════════════

BOILERPLATE_MARKERS = (
    "unsubscribe", "manage your preferences", "update your preferences",
    "view this email in your browser", "you are receiving this",
    "you're receiving this", "copyright ©", "all rights reserved",
    "sent to you by", "privacy policy", "terms of service",
    "was this forwarded to you", "add us to your address book",
    "no longer wish to receive",
)

def strip_newsletter_boilerplate(text):
    """
    Newsletters carry a lot of text that is not a job: header navigation at the
    top, and an unsubscribe and legal block at the bottom. Cut both so the
    character budget goes to listings.
    """
    lines = [l.rstrip() for l in text.splitlines()]

    # Drop everything from the first footer marker onward
    for i, l in enumerate(lines):
        low = l.lower()
        if any(m in low for m in BOILERPLATE_MARKERS):
            lines = lines[:i]
            break

    out, blanks = [], 0
    for l in lines:
        if not l.strip():
            blanks += 1
            if blanks > 1:
                continue
        else:
            blanks = 0
        # Drop bare social and nav links
        if l.strip().lower() in ("twitter", "facebook", "instagram", "linkedin",
                                 "share", "forward", "web version", "view online"):
            continue
        out.append(l)

    return "\n".join(out).strip()


# Lines inside a LinkedIn job card that are badges, not the title
LINKEDIN_BADGE = re.compile(
    r"^(actively recruiting|easy apply|promoted|new|be an early applicant|"
    r"view job|apply|save|see all jobs|\d+\s+(connections?|school alum(ni|nus)?|"
    r"company alum(ni|nus)?|applicants?)|.*\$.*|.*/\s*(hour|hr|year|yr))$", re.I)


def parse_linkedin_alert(soup, label):
    """
    Read job cards out of a LinkedIn alert email.

    Each card links to the same posting more than once (the title, the logo,
    the whole card), and every link carries a tracking code that changes from
    email to email. Grouping by LinkedIn's job ID collapses those into one job.
    Reading the card's lines separately gives a clean title plus the
    "Employer · Location" line, which the old anchor-text approach mashed
    together into one long title with no employer.
    """
    cards = {}
    for a in soup.find_all("a", href=True):
        m = re.search(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)", a["href"])
        if not m:
            continue
        card = cards.setdefault(m.group(1), {"title": "", "employer": "", "location": ""})
        lines = [l.strip() for l in a.get_text("\n", strip=True).split("\n") if l.strip()]
        for line in lines:
            if re.search(r"\s·\s", line):
                if not card["employer"]:
                    emp, loc = re.split(r"\s+·\s+", line, maxsplit=1)
                    card["employer"], card["location"] = emp.strip(), loc.strip()
            elif not card["title"] and len(line) > 3 and not LINKEDIN_BADGE.match(line):
                card["title"] = line

    jobs = []
    for job_id, card in cards.items():
        title = clean_title(card["title"])
        if not title or not is_relevant(title, card["location"]):
            continue
        jobs.append({"title": title, "employer": card["employer"],
                     "location": card["location"],
                     "link": f"https://www.linkedin.com/jobs/view/{job_id}/",
                     "from_email": True, "source": label})
    return jobs, len(cards)


def generic_email_jobs(soup, label):
    """Anchors that read like job titles, for newsletters without a known format."""
    jobs = []
    for a in soup.find_all("a", href=True):
        anchor = a.get_text(strip=True)
        href   = a["href"]
        if (len(anchor) > 8 and href.startswith("http")
                and "unsubscribe" not in href.lower()
                and is_relevant(anchor)):
            # Newsletter anchors usually read "Title at Employer" or "Title |
            # Employer". Splitting them gives each job an employer, which is
            # what makes cross-source dedup possible.
            raw = clean_title(anchor) or anchor
            emp = ""
            mm = re.match(r"^(.*?)\s+(?:at|@|\||–|,)\s+(.+)$", raw)
            if mm and len(mm.group(1)) > 3:
                raw, emp = mm.group(1).strip(), mm.group(2).strip()
            jobs.append({"title": raw, "employer": emp, "location": "",
                         "from_email": True, "link": href, "source": label})
    return jobs


# Invisible characters that marketing emails pad their preview text with.
# Study Hall's opens with hundreds of them, which ate into the character cap.
INVISIBLE = re.compile(r"[͏​-‏ ­﻿]")


def scrape_newsletters(seen_emails=frozenset()):
    """
    Read job newsletters out of Gmail over IMAP.

    Study Hall's opportunities email is a paid weekly roundup of freelance calls,
    staff openings, fellowships, and grants. There is no public page to scrape and
    no API, so email is the only route in.

    Rather than guess at the newsletter's markup, this pulls two things:
      1. Any hyperlink whose anchor text reads like a job title, which goes
         straight into the normal pipeline.
      2. The email's plain text, which is handed to Claude to read properly.

    Newsletter prose is too irregular for a regex and Claude is good at exactly
    this, so the split follows the usual rule: Python does the mechanical part,
    Claude does the judgment.
    """
    import imaplib, email as email_lib
    from email.header import decode_header

    jobs, digests, processed = [], [], set()

    if GMAIL_APP_PASS.startswith("your-"):
        diag("Newsletters", kept=0, note="SKIPPED — GMAIL_APP_PASS not set")
        print("  Newsletters: skipped (no Gmail app password)")
        return jobs, digests, set()

    since = (datetime.now() - timedelta(days=NEWSLETTER_LOOKBACK_DAYS)).strftime("%d-%b-%Y")

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_ADDRESS, GMAIL_APP_PASS)
        mail.select("inbox")

        # Counts are kept per source, not per sender address. LinkedIn sends
        # alerts from two addresses; reporting each address separately let the
        # empty one overwrite the one that had mail, so the diagnostics said no
        # LinkedIn email arrived on days when three had.
        stats = {}
        for domain, label in NEWSLETTER_SENDERS.items():
            st = stats.setdefault(label, {"new": 0, "seen": 0, "links": 0,
                                          "cards": 0, "error": ""})
            try:
                _, data = mail.search(None, f'(FROM "{domain}" SINCE {since})')
                ids = data[0].split()

                for num in ids[-4:]:              # newest four at most
                    _, raw = mail.fetch(num, "(RFC822)")
                    msg = email_lib.message_from_bytes(raw[0][1])

                    msg_id = (msg.get("Message-ID") or "").strip()
                    if msg_id and msg_id in seen_emails:
                        st["seen"] += 1
                        continue
                    st["new"] += 1
                    if msg_id:
                        processed.add(msg_id)

                    subject = ""
                    for part, enc in decode_header(msg.get("Subject", "")):
                        subject += part.decode(enc or "utf-8", "ignore") if isinstance(part, bytes) else part

                    html = text = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            ctype = part.get_content_type()
                            if ctype == "text/html" and not html:
                                html = part.get_payload(decode=True).decode("utf-8", "ignore")
                            elif ctype == "text/plain" and not text:
                                text = part.get_payload(decode=True).decode("utf-8", "ignore")
                    else:
                        payload = msg.get_payload(decode=True)
                        if payload:
                            body = payload.decode("utf-8", "ignore")
                            if msg.get_content_type() == "text/html":
                                html = body
                            else:
                                text = body

                    if html:
                        soup = BeautifulSoup(html, "html.parser")
                        for tag in soup(["script", "style"]):
                            tag.decompose()
                        if label == "LinkedIn":
                            found, cards = parse_linkedin_alert(soup, label)
                            st["cards"] += cards
                        else:
                            found = generic_email_jobs(soup, label)
                        jobs += found
                        st["links"] += len(found)
                        text = soup.get_text("\n", strip=True)

                    # The readable text goes to Claude only for prose
                    # newsletters. Boilerplate and invisible padding are
                    # stripped first so the character budget goes to listings.
                    if label in DIGEST_SOURCES and text:
                        text = INVISIBLE.sub("", text).replace("\xa0", " ")
                        text = re.sub(r"[ \t]{2,}", " ", text)
                        clean = strip_newsletter_boilerplate(text)
                        clean = re.sub(r"\n{3,}", "\n\n", clean)[:NEWSLETTER_CHAR_CAP]
                        used = sum(len(d.get("body", "")) for d in digests)
                        if used + len(clean) > NEWSLETTER_TOTAL_CAP:
                            clean = clean[:max(0, NEWSLETTER_TOTAL_CAP - used)]
                        digests.append({"source": label, "subject": subject,
                                        "id": msg_id,
                                        "date": msg.get("Date", ""), "body": clean})

            except Exception as e:
                st["error"] = type(e).__name__

        for label, st in stats.items():
            if st["error"]:
                note = f"error: {st['error']}"
            elif st["new"] == 0 and st["seen"] == 0:
                note = f"no email in the last {NEWSLETTER_LOOKBACK_DAYS} days"
            elif st["new"] == 0:
                note = (f"no new email ({st['seen']} already read in an "
                        f"earlier run)")
            else:
                note = f"{st['new']} new email(s), {st['links']} relevant job(s)"
                if label == "LinkedIn":
                    note += f" out of {st['cards']} in the alerts"
                if label in DIGEST_SOURCES:
                    note += ", full text sent to Claude"
                if label == "LinkedIn" and st["cards"] == 0:
                    note += ". No job cards found: LinkedIn may have changed its email layout"
            diag(label, kept=st["links"], note=note)
            print(f"  {label}: {note}")

        mail.logout()

    except Exception as e:
        diag("Newsletters", kept=0, note=f"IMAP error: {type(e).__name__}")
        print(f"  Newsletters: IMAP error ({type(e).__name__})")
        print("  → check GMAIL_ADDRESS, GMAIL_APP_PASS, and that IMAP is on in Gmail settings")

    return jobs, digests, processed


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
    """
    The Muse public API. Free, no key.
    Category names must match theirs exactly, so try a few spellings, then fall
    back to plain location queries if none of them return anything.
    """
    jobs, raw = [], 0
    categories = [
        "Media, PR, and Communications",
        "Media, PR & Communications",
        "Editorial and Writing",
        "Creative and Design",
    ]
    locations = ["Miami, FL", "New York City, NY", "Washington, DC", "Flexible / Remote"]

    def harvest(params):
        nonlocal raw
        try:
            r = SESSION.get("https://www.themuse.com/api/public/jobs",
                            params=params, timeout=15)
            diag("The Muse", http=r.status_code)
            if r.status_code != 200:
                return 0
            batch = r.json().get("results", [])
            raw += len(batch)
            for it in batch:
                title = it.get("name", "")
                loc = ", ".join(l.get("name", "") for l in it.get("locations", []))
                if is_relevant(title, loc):
                    jobs.append({"title": title,
                                 "employer": it.get("company", {}).get("name", ""),
                                 "location": loc,
                                 "link": it.get("refs", {}).get("landing_page", ""),
                                 "source": "The Muse"})
            return len(batch)
        except Exception as e:
            diag("The Muse", note=f"error: {type(e).__name__}")
            return 0

    for cat in categories:
        got = harvest({"category": cat, "page": 1, "descending": "true"})
        time.sleep(0.4)
        if got:
            break                      # found the right spelling

    if raw == 0:                       # no category worked, go by location
        for loc in locations:
            harvest({"location": loc, "page": 1, "descending": "true"})
            time.sleep(0.4)

    seen, out = set(), []
    for j in jobs:
        if j["link"] and j["link"] not in seen:
            seen.add(j["link"])
            out.append(j)

    diag("The Muse", raw=raw, kept=len(out))
    note = f"  ({raw} returned by API, none matched filters)" if raw and not out else ""
    print(f"  The Muse: {len(out)}{note}")
    return out


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
            r = fetch(url, timeout=10, retry_403=False)
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


def tally(jobs):
    out = {}
    for j in jobs:
        out[j["source"]] = out.get(j["source"], 0) + 1
    return out


def record_funnel(collected, new, checked, final):
    """
    Where each source's jobs went, stage by stage. Before this, the diagnostics
    showed only the first count, so a board that found eight jobs and had all
    eight filtered out as already sent looked broken when it was working.

      found    matched the filters this run
      new      not sent on an earlier day
      checked  made the per-source cap and had its link tested
      final    in today's file
    """
    for src in set(collected) | set(final):
        diag(src, found=collected.get(src, 0), new=new.get(src, 0),
             checked=checked.get(src, 0), final=final.get(src, 0))
    print("\n  Source funnel (found > new > checked > final):")
    for src in sorted(collected, key=lambda s: -collected[s]):
        print(f"    {src:18} {collected.get(src,0):>3} > {new.get(src,0):>3} > "
              f"{checked.get(src,0):>3} > {final.get(src,0):>3}")


def main():
    print(f"\nJob collection starting — {datetime.now():%Y-%m-%d %H:%M}\n")
    preflight()

    seen, seen_fp, seen_emails = set(), set(), set()
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            raw = json.load(f)
        if isinstance(raw, list):          # the original format: a list of links
            seen = set(raw)
        else:
            seen        = set(raw.get("links", []))
            seen_fp     = set(raw.get("fingerprints", []))
            seen_emails = set(raw.get("emails", []))
    print(f"{len(seen)} jobs and {len(seen_fp)} titles already sent in previous runs\n")

    print("Collecting:")
    jobs, news_digests = [], []
    for name, url in HTML_BOARDS:
        jobs += scrape_board(name, url)
        time.sleep(0.3)
    jobs += scrape_adzuna()
    jobs += scrape_muse()
    jobs += scrape_usajobs()

    news_jobs, news_digests, read_emails = scrape_newsletters(seen_emails)
    jobs += news_jobs

    print(f"\nRaw matches: {len(jobs)}")

    for j in jobs:
        j["link"] = normalize_link(j["link"])
    collected = tally(jobs)

    # Same posting twice in one run (one LinkedIn job sits in several alert
    # emails, and boards repeat featured listings)
    unique, links_this_run = [], set()
    for j in jobs:
        if j["link"] in links_this_run:
            continue
        links_this_run.add(j["link"])
        unique.append(j)
    jobs = fuzzy_dedupe(unique)

    before = len(jobs)
    jobs = [j for j in jobs if j["link"] not in seen]
    by_link = before - len(jobs)

    kept = [j for j in jobs if not seen_before(j, seen_fp)]
    by_title = len(jobs) - len(kept)
    jobs = kept

    print(f"New since last run: {len(jobs)}  "
          f"({by_link} seen by link, {by_title} seen by title and employer)")
    new = tally(jobs)

    if not jobs:
        record_funnel(collected, new, {}, {})
        with open(OUTPUT_FILE, "w") as f:
            json.dump({"generated": datetime.now().isoformat(),
                       "count": 0,
                       "dead_links_dropped": 0,
                       "off_target_dropped": 0,
                       "source_counts": {},
                       "diagnostics": DIAG,
                       "newsletters": news_digests,
                       "jobs": []}, f, indent=2)
        seen_emails.update(read_emails)
        with open(SEEN_FILE, "w") as f:
            json.dump({"links": sorted(seen), "fingerprints": sorted(seen_fp),
                       "emails": sorted(seen_emails)}, f, indent=2)
        print("\nNothing new today. Wrote empty job_results.json")
        return

    jobs = balance_sources(jobs)
    checked = tally(jobs)

    print(f"\nChecking links (this is the slow part)...")
    finalists, dead, off_target = [], 0, 0
    for job in jobs:
        if len(finalists) >= MAX_FINALISTS:
            break
        # Links out of an alert email came straight from the source that
        # posted them, so they are fresh by definition. They also point at
        # pages that reject datacenter traffic: LinkedIn serves a login wall
        # to anything without a browser session, which the checker would read
        # as a dead link and drop. Trust these and move on.
        if job.get("from_email"):
            job["description"] = ""
            job.setdefault("location", "")
            finalists.append(job)
            continue

        ok, result = link_is_good(job["link"])
        if not ok:
            dead += 1
            time.sleep(0.3)
            continue

        job["description"] = result

        # Boards often leave location out of the listing card, and a blank
        # location passes the filter by default. That is how jobs in Fort Myers
        # and Albuquerque reached the digest. Now that the posting page has been
        # fetched, check it properly.
        if not job.get("location"):
            head = result[:900].lower()
            hit = next((loc for loc in LOCATIONS if loc in head), None)
            if hit:
                job["location"] = hit.strip(" ,()").title()
            else:
                off_target += 1
                time.sleep(0.3)
                continue

        finalists.append(job)
        time.sleep(0.3)

    counts = tally(finalists)
    record_funnel(collected, new, checked, counts)

    print(f"  {len(finalists)} live postings, {dead} dead links dropped, "
          f"{off_target} dropped for being outside target areas")
    print(f"  Final spread: {counts}")

    with open(OUTPUT_FILE, "w") as f:
        json.dump({"generated": datetime.now().isoformat(),
                   "count": len(finalists),
                   "dead_links_dropped": dead,
                   "off_target_dropped": off_target,
                   "source_counts": counts,
                   "diagnostics": DIAG,
                   "newsletters": news_digests,
                   "jobs": finalists}, f, indent=2)

    # Mark everything checked as seen, including the dead ones
    seen.update(j["link"] for j in jobs)
    seen_fp.update(fp for fp in (fingerprint(j) for j in jobs) if fp)
    seen_emails.update(read_emails)
    with open(SEEN_FILE, "w") as f:
        json.dump({"links": sorted(seen),
                   "fingerprints": sorted(seen_fp),
                   "emails": sorted(seen_emails)}, f, indent=2)

    print(f"\nDone. Wrote {len(finalists)} jobs to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
