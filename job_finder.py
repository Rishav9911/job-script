#!/usr/bin/env python3
"""
Job finder (fast version): reads companies from config.yaml, pulls open jobs from
each company's public job-board feed IN PARALLEL, filters by title / experience /
location, and saves NEW matches to a single .docx. It never applies to anything.

Run:  python job_finder.py                (uses config.yaml next to this file)
      python job_finder.py -c other.yaml
      python job_finder.py --workers 32   (more parallelism)
"""
import argparse
import difflib
import html
import json
import random
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from threading import Lock, Semaphore
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
import yaml

HEADERS = {
    "User-Agent": "Mozilla/5.0 (personal job search script)",
    "Accept-Encoding": "gzip, deflate",
}
BASE = Path(__file__).parent
CHECK_MODE = False       # set by --check: fetch only enough to prove a company works
TIMEOUT = (10, 20)       # (connect, read) seconds
PER_HOST = 6             # max simultaneous requests to the same host
QUERY_WORKERS = 4        # parallel title-queries inside one company
DETAIL_WORKERS = 8       # parallel description fetches inside one company

# ----------------------------------------------------------------- http
SESSION = requests.Session()
_adapter = HTTPAdapter(pool_connections=64, pool_maxsize=64)
SESSION.mount("https://", _adapter)
SESSION.mount("http://", _adapter)
SESSION.headers.update(HEADERS)

_HOST_SEM, _HOST_LOCK = {}, Lock()


def _sem(url):
    host = urlparse(url).netloc
    with _HOST_LOCK:
        return _HOST_SEM.setdefault(host, Semaphore(3 if "myworkday" in host else PER_HOST))


def _request(method, url, **kw):
    err = None
    attempts = 1 if CHECK_MODE else 3
    body = kw.get("json") or {}
    tag = f" [q={body.get('searchText')!r} offset={body.get('offset')}]" if body else ""
    for attempt in range(attempts):
        try:
            with _sem(url):
                r = SESSION.request(method, url, timeout=TIMEOUT, **kw)
            r.raise_for_status()
            return r
        except requests.HTTPError as e:
            err = e
            code = e.response.status_code if e.response is not None else 0
            if 400 <= code < 500 and code != 429:
                break                      # wrong board/host: retrying won't help
        except requests.RequestException as e:
            err = e
        if attempt < attempts - 1:
            time.sleep(1 + 2 * attempt)
    print(f"   [fetch failed] {url.split('?')[0]}{tag} -> {err}")
    return None


def get(url, **kw):
    return _request("GET", url, **kw)


def post(url, payload):
    return _request("POST", url, json=payload)


def clean(text):
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def job(company, title, url, location="", posted="", description=""):
    return {
        "company": company,
        "title": clean(title),
        "url": url,
        "location": clean(location),
        "posted": str(posted or ""),
        "description": clean(description),
    }


def run_queries(cfg, one_query):
    """Run one_query(query) for every title in parallel; merge and de-dupe by URL."""
    queries = list(dict.fromkeys(cfg["titles"]))
    if CHECK_MODE:
        queries = queries[:1]
    out, seen = [], set()
    def safe(q):
        try:
            return one_query(q)
        except Exception as e:
            print(f"   [query error] {q!r}: {type(e).__name__}: {e}")
            return []

    with ThreadPoolExecutor(max_workers=QUERY_WORKERS) as ex:
        for jobs in ex.map(safe, queries):
            for j in jobs:
                if j["url"] not in seen:
                    seen.add(j["url"])
                    out.append(j)
    return out


# ----------------------------------------------------------------- sources
# Each fetcher returns a list of job dicts. `c` is the company's config block.

def fetch_greenhouse(c, cfg):
    # List WITHOUT content (much smaller); description fetched later only for title matches.
    r = get(f"https://boards-api.greenhouse.io/v1/boards/{c['board']}/jobs")
    if not r:
        return []
    out = []
    for j in r.json().get("jobs", []):
        x = job(c["name"], j["title"], j["absolute_url"],
                (j.get("location") or {}).get("name", ""), j.get("updated_at", ""), "")
        x["_greenhouse"] = f"https://boards-api.greenhouse.io/v1/boards/{c['board']}/jobs/{j['id']}"
        out.append(x)
    return out


def greenhouse_description(j):
    r = get(j["_greenhouse"])
    return clean(r.json().get("content", "")) if r else ""


def fetch_lever(c, cfg):
    r = get(f"https://api.lever.co/v0/postings/{c['board']}?mode=json")
    if not r:
        return []
    return [
        job(c["name"], j["text"], j["hostedUrl"],
            (j.get("categories") or {}).get("location", ""),
            "", j.get("descriptionPlain", ""))
        for j in r.json()
    ]


def fetch_ashby(c, cfg):
    r = get(f"https://api.ashbyhq.com/posting-api/job-board/{c['board']}")
    if not r:
        return []
    return [
        job(c["name"], j["title"], j.get("jobUrl", ""), j.get("location", ""),
            j.get("publishedAt", ""), j.get("descriptionPlain", ""))
        for j in r.json().get("jobs", [])
    ]


def fetch_amazon(c, cfg):
    def one(query):
        out, offset = [], 0
        while offset < 300:
            params = {
                "base_query": query,
                "loc_query": c.get("location", ""),
                "country": c.get("country", ""),
                "result_limit": 1 if CHECK_MODE else 100,
                "offset": offset,
                "sort": "recent",
            }
            r = get("https://www.amazon.jobs/en/search.json", params=params)
            if not r:
                break
            jobs = r.json().get("jobs", [])
            if not jobs:
                break
            for j in jobs:
                desc = " ".join([
                    j.get("description_short", ""),
                    j.get("basic_qualifications", ""),
                    j.get("preferred_qualifications", ""),
                ])
                out.append(job(c["name"], j["title"],
                               "https://www.amazon.jobs" + j["job_path"],
                               j.get("normalized_location", j.get("location", "")),
                               j.get("posted_date", ""), desc))
            if CHECK_MODE or len(jobs) < 100:
                break
            offset += 100
        return out
    return run_queries(cfg, one)


def fetch_atlassian(c, cfg):
    r = get("https://www.atlassian.com/endpoint/careers/listings")
    if not r:
        return []
    out = []
    for j in r.json():
        locs = j.get("locations") or []
        if isinstance(locs, list):
            locs = "; ".join(str(x) for x in locs)
        desc = " ".join(str(j.get(k) or "") for k in ("overview", "responsibilities", "qualifications"))
        url = j.get("applyUrl") or f"https://www.atlassian.com/company/careers/details/{j.get('id')}"
        out.append(job(c["name"], j.get("title", ""), url, locs, "", desc))
    return out


def fetch_workday(c, cfg):
    """c needs: host (e.g. nvidia.wd5.myworkdayjobs.com), tenant, site."""
    host, tenant, site = c["host"], c["tenant"], c["site"]
    api = f"https://{host}/wday/cxs/{tenant}/{site}"
    page = 1 if CHECK_MODE else 20

    def one(query):
        out, offset = [], 0
        while offset < 200:
            r = post(f"{api}/jobs", {"appliedFacets": {}, "limit": page,
                                     "offset": offset, "searchText": query})
            if not r:
                break
            postings = r.json().get("jobPostings", [])
            if not postings:
                break
            for p in postings:
                path = p.get("externalPath", "")
                if not p.get("title") or not path:
                    continue
                x = job(c["name"], p["title"], f"https://{host}/en-US/{site}{path}",
                        p.get("locationsText", ""), p.get("postedOn", ""), "")
                x["_workday_detail"] = f"{api}{path}"
                out.append(x)
            if CHECK_MODE or len(postings) < page:
                break
            offset += page
        return out
    return run_queries(cfg, one)


def workday_description(j):
    r = get(j["_workday_detail"])
    if not r:
        return ""
    return clean(r.json().get("jobPostingInfo", {}).get("jobDescription", ""))


def fetch_oracle(c, cfg):
    """Oracle Cloud HCM 'Candidate Experience' sites. c needs: host and site."""
    host, site = c["host"], c["site"]
    api = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    page = 1 if CHECK_MODE else 25

    def one(query):
        out, offset = [], 0
        while offset < 300:
            finder = (f'findReqs;siteNumber={site},limit={page},offset={offset},'
                      f'keyword="{query}",sortBy=POSTING_DATES_DESC')
            r = get(api, params={"onlyData": "true", "finder": finder,
                                 "expand": "requisitionList.secondaryLocations"})
            if not r:
                break
            items = r.json().get("items") or [{}]
            reqs = items[0].get("requisitionList") or []
            if not reqs:
                break
            for q in reqs:
                rid = str(q.get("Id", ""))
                if not rid:
                    continue
                locs = [q.get("PrimaryLocation", "")] + [
                    s_.get("Name", "") for s_ in (q.get("secondaryLocations") or [])]
                j = job(c["name"], q.get("Title", ""),
                        f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{rid}",
                        "; ".join(x for x in locs if x), q.get("PostedDate", ""), "")
                j["_oracle"] = (host, site, rid)
                out.append(j)
            if CHECK_MODE or len(reqs) < page:
                break
            offset += page
        return out
    return run_queries(cfg, one)


def oracle_description(j):
    host, site, rid = j["_oracle"]
    r = get(f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails",
            params={"expand": "all", "onlyData": "true",
                    "finder": f'ById;Id="{rid}",siteNumber={site}'})
    if not r:
        return ""
    d = (r.json().get("items") or [{}])[0]
    return clean(" ".join(str(d.get(k) or "") for k in (
        "ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")))


def fetch_smartrecruiters(c, cfg):
    """SmartRecruiters public postings API. c needs: board. Optional: country."""
    board = c["board"]
    api = f"https://api.smartrecruiters.com/v1/companies/{board}/postings"
    page = 1 if CHECK_MODE else 100

    def one(query):
        out, offset = [], 0
        while offset < 300:
            params = {"q": query, "limit": page, "offset": offset}
            if c.get("country"):
                params["country"] = c["country"]
            r = get(api, params=params)
            if not r:
                break
            items = r.json().get("content") or []
            if not items:
                break
            for p in items:
                pid = str(p.get("id", ""))
                if not pid:
                    continue
                loc = p.get("location") or {}
                country = str(loc.get("country") or "")
                parts = [loc.get("city", ""), loc.get("region", ""),
                         "India" if country.lower() == "in" else country.upper()]
                j = job(c["name"], p.get("name", ""),
                        f"https://jobs.smartrecruiters.com/{board}/{pid}",
                        ", ".join(x for x in parts if x), p.get("releasedDate", ""), "")
                j["_smartrecruiters"] = f"{api}/{pid}"
                out.append(j)
            if CHECK_MODE or len(items) < page:
                break
            offset += page
        return out
    return run_queries(cfg, one)


def smartrecruiters_description(j):
    r = get(j["_smartrecruiters"])
    if not r:
        return ""
    sections = (r.json().get("jobAd") or {}).get("sections") or {}
    # skip companyDescription: its "N years of experience" boasts would confuse the experience check
    return clean(" ".join(str((sections.get(k) or {}).get("text") or "")
                          for k in ("jobDescription", "qualifications", "additionalInformation")))


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "amazon": fetch_amazon,
    "workday": fetch_workday,
    "atlassian": fetch_atlassian,
    "oracle": fetch_oracle,
    "smartrecruiters": fetch_smartrecruiters,
}
# marker key on a job -> function that fetches its description lazily
DETAIL_FETCHERS = {
    "_greenhouse": greenhouse_description,
    "_workday_detail": workday_description,
    "_oracle": oracle_description,
    "_smartrecruiters": smartrecruiters_description,
}


# ----------------------------------------------------------------- filtering
def norm(s):
    """Lower-case (casefold), strip accents, collapse whitespace."""
    s = unicodedata.normalize("NFKD", str(s if s is not None else ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("\xa0", " ").casefold()
    return re.sub(r"\s+", " ", s).strip()


def as_list(v):
    """Config value -> clean list of normalised, non-empty strings."""
    if v is None:
        return []
    if isinstance(v, (str, int, float)):
        v = [v]
    return [t for t in (norm(x) for x in v) if t]


def prefix_re(term):
    return re.compile(r"(?<![a-z0-9])" + r"\s+".join(re.escape(p) for p in term.split()))


def word_re(term):
    return re.compile(r"(?<![a-z0-9])" + r"\s+".join(re.escape(p) for p in term.split()) + r"(?![a-z0-9])")


BUILTIN_EXCLUDES = ["senior", "sr", "sr.", "staff", "principal", "lead", "manager", "director",
                    "head of", "architect", "vp", "vice president", "distinguished", "fellow"]
LEVEL_RE = re.compile(r"(?<![a-z0-9])(?:engineer|developer|sde|swe|sdet|programmer|scientist)"
                      r"\s*[-,:]?\s*(?:ii|iii|iv|v|2|3|4|5|6)(?![a-z0-9])")
LOCATION_ALIASES = [("bengaluru", "bangalore", "bengalooru"), ("gurugram", "gurgaon")]
TITLE_ALIASES = [("software engineer", "software development engineer", "software developer"),
                 ("ai engineer", "artificial intelligence engineer"),
                 ("ml engineer", "machine learning engineer")]

TITLE_HINTS = re.compile(r"(?<![a-z0-9])(freshers?|new grads?|graduates?|entry[- ]level|early career|"
                         r"campus|interns?|internships?|sde[- ]?(?:1|i)|engineer[- ]?(?:1|i)|associates?|"
                         r"trainees?)(?![a-z0-9])")
DESC_HINTS = re.compile(r"(?<![a-z0-9])(freshers?|new grad(?:uate)?s?|new college grad(?:uate)?s?|"
                        r"recent graduates?|entry[- ]level|early career|campus hire)(?![a-z0-9])")

_NUMW = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
         "eight": 8, "nine": 9, "ten": 10}
_NUM = r"(\d{1,2}(?:\.\d)?|one|two|three|four|five|six|seven|eight|nine|ten)"
EXP_RE = re.compile(r"(?<![a-z0-9.])" + _NUM + r"\s*(?:\+|plus)?\s*(?:(?:-|–|—|to)\s*" + _NUM
                    + r"\s*)?\+?\s*(?:years?|yrs?)(?![a-z])")


def min_years_required(text):
    text = norm(text)
    found = []
    for m in EXP_RE.finditer(text):
        window = text[max(0, m.start() - 80): m.end() + 80]
        if re.search(r"experience|\bexp\b", window):
            v = m.group(1)
            found.append(float(_NUMW.get(v, v)))
    return min(found) if found else None


def is_vague_location(loc):
    return (not loc
            or loc in {"india", "pan india", "anywhere in india", "multiple locations",
                       "various locations", "multiple", "various"}
            or re.fullmatch(r"\d+\s+locations?", loc) is not None
            or re.fullmatch(r"(remote\W+)?india(\W+remote)?", loc) is not None)


def get_rules(cfg):
    if "_rules" in cfg:
        return cfg["_rules"]
    include = as_list(cfg.get("titles"))
    for group in TITLE_ALIASES:
        if any(t in include for t in group):
            include += [t for t in group if t not in include]
    exclude = as_list(cfg.get("exclude_title_words"))
    if cfg.get("use_builtin_exclusions", True):
        exclude += [t for t in BUILTIN_EXCLUDES if t not in exclude]
    locs = as_list(cfg.get("locations"))
    for group in LOCATION_ALIASES:
        if any(t in locs for t in group):
            locs += [t for t in group if t not in locs]
    cfg["_rules"] = {
        "include": [(t, prefix_re(t)) for t in include],
        "exclude": [(t, word_re(t) if len(t) <= 3 and t[-1].isalnum() else prefix_re(t)) for t in exclude],
        "locs": [(t, prefix_re(t)) for t in locs],
        "desc_excl": [(t, re.compile(re.escape(t))) for t in as_list(cfg.get("exclude_description_words"))],
        "max_years": float(cfg.get("max_years_experience", 1)),
        "levels": bool(cfg.get("exclude_senior_levels", True)),
    }
    return cfg["_rules"]


def evaluate(j, cfg):
    """Return (verdict, note). verdict is 'match', 'maybe' or None (note then says why)."""
    R = get_rules(cfg)
    title = norm(j.get("title"))
    if not title:
        return None, "title: empty"
    if not any(rx.search(title) for _, rx in R["include"]):
        return None, "title: no keyword match"
    for t, rx in R["exclude"]:
        if rx.search(title):
            return None, f"title: excluded word '{t}'"
    if R["levels"] and LEVEL_RE.search(title):
        return None, "title: level II or above"

    loc = norm(j.get("location"))
    if R["locs"] and not is_vague_location(loc) and not any(rx.search(loc) for _, rx in R["locs"]):
        return None, f"location: '{j.get('location')}' not in your list"

    desc = norm(j.get("description"))
    for t, rx in R["desc_excl"]:
        if rx.search(desc):
            return None, f"description: excluded word '{t}'"

    yrs = min_years_required(desc)
    if yrs is not None:
        if yrs <= R["max_years"]:
            return "match", f"asks {yrs:g}+ yrs"
        return None, f"experience: needs {yrs:g}+ yrs (your limit is {R['max_years']:g})"
    if TITLE_HINTS.search(title) or DESC_HINTS.search(desc):
        return "match", "entry-level wording"
    return "maybe", "experience not stated - check manually"


# ----------------------------------------------------------------- config checks
KNOWN_KEYS = {"titles", "exclude_title_words", "max_years_experience", "locations",
              "exclude_description_words", "max_runtime_minutes", "output_dir", "companies",
              "exclude_senior_levels", "use_builtin_exclusions"}
REQUIRED_FIELDS = {"greenhouse": ["board"], "lever": ["board"], "ashby": ["board"],
                   "workday": ["host", "tenant", "site"], "oracle": ["host", "site"], "smartrecruiters": ["board"],
                   "amazon": [], "atlassian": [], "manual": []}


def load_config(path):
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict):
        sys.exit(f"[config error] {path} is empty or not valid YAML")
    cfg = dict(raw)
    for k in cfg:
        if k not in KNOWN_KEYS:
            hint = difflib.get_close_matches(k, KNOWN_KEYS, n=1)
            print(f"[config warning] unknown setting '{k}'" + (f" - did you mean '{hint[0]}'?" if hint else ""))
    if not as_list(cfg.get("titles")):
        sys.exit("[config error] 'titles' is empty - add at least one job-title keyword")
    try:
        float(cfg.get("max_years_experience", 1))
    except (TypeError, ValueError):
        sys.exit("[config error] max_years_experience must be a number, e.g. 1")
    if not as_list(cfg.get("locations")):
        print("[config warning] 'locations' is empty - jobs from every location will be kept")

    good, names = [], set()
    for c in cfg.get("companies") or []:
        if not isinstance(c, dict) or not c.get("name") or not c.get("source"):
            print(f"[config warning] skipping a company entry without name/source: {c}")
            continue
        c["name"] = str(c["name"]).strip()
        c["source"] = str(c["source"]).strip().lower()
        if c["source"] not in REQUIRED_FIELDS:
            print(f"[config warning] {c['name']}: unknown source '{c['source']}' "
                  f"(use one of: {', '.join(sorted(REQUIRED_FIELDS))}) - skipped")
            continue
        missing = [f for f in REQUIRED_FIELDS[c["source"]] if not c.get(f)]
        if missing:
            print(f"[config warning] {c['name']}: missing {', '.join(missing)} - skipped")
            continue
        for f in REQUIRED_FIELDS[c["source"]]:
            c[f] = str(c[f]).strip()
        if c["name"].casefold() in names:
            print(f"[config warning] {c['name']} is listed twice - second entry skipped")
            continue
        names.add(c["name"].casefold())
        good.append(c)
    cfg["companies"] = good
    return cfg


# ----------------------------------------------------------------- output
def add_hyperlink(paragraph, url, text):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    part = paragraph.part
    r_id = part.relate_to(url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
                          is_external=True)
    h = OxmlElement("w:hyperlink")
    h.set(qn("r:id"), r_id)
    run = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    u = OxmlElement("w:u"); u.set(qn("w:val"), "single"); rpr.append(u)
    col = OxmlElement("w:color"); col.set(qn("w:val"), "0563C1"); rpr.append(col)
    run.append(rpr)
    t = OxmlElement("w:t"); t.text = text; run.append(t)
    h.append(run)
    paragraph._p.append(h)


def write_docx(path, rows, manual=()):
    from docx import Document
    doc = Document()
    doc.add_heading(f"New jobs - {date.today():%d %b %Y}", level=1)
    doc.add_paragraph(f"{len(rows)} new listings. 'Match' = experience requirement fits; "
                      f"'Maybe' = experience not stated, read the JD.")
    for verdict in ("match", "maybe"):
        subset = [r for r in rows if r["verdict"] == verdict]
        if not subset:
            continue
        doc.add_heading("Likely matches" if verdict == "match" else "Maybe (check manually)", level=2)
        table = doc.add_table(rows=1, cols=4)
        table.style = "Light Grid Accent 1"
        for cell, label in zip(table.rows[0].cells, ["Company", "Title", "Location", "Link / note"]):
            cell.text = label
        for r in sorted(subset, key=lambda x: x["company"]):
            cells = table.add_row().cells
            cells[0].text, cells[1].text, cells[2].text = r["company"], r["title"], r["location"]
            p = cells[3].paragraphs[0]
            add_hyperlink(p, r["url"], "Open job")
            p.add_run(f"  ({r['note']})")

    doc.add_heading("MANUAL links - check these yourself", level=2)
    if manual:
        mtable = doc.add_table(rows=1, cols=3)
        mtable.style = "Light Grid Accent 1"
        for cell, label in zip(mtable.rows[0].cells, ["Tag", "Company", "Link"]):
            cell.text = label
        for c in manual:
            cells = mtable.add_row().cells
            cells[0].text = "MANUAL"
            cells[1].text = c["name"]
            p = cells[2].paragraphs[0]
            if c.get("url"):
                add_hyperlink(p, c["url"], "Open saved search")
            else:
                p.add_run("(add a url in config.yaml)")
    else:
        doc.add_paragraph("No manual companies in config.yaml.")
    doc.save(path)


# ----------------------------------------------------------------- main
def run_check(companies, cfg, workers):
    global CHECK_MODE
    CHECK_MODE = True

    def probe(c):
        if c["source"] == "manual":
            return f"[manual] {c['name']}: {c.get('url', '(add a url in config.yaml)')}"
        fetcher = FETCHERS.get(c["source"])
        if not fetcher:
            return f"[FAIL]   {c['name']}: unknown source '{c['source']}'"
        try:
            jobs = fetcher(c, cfg)
        except Exception as e:
            return f"[FAIL]   {c['name']}: {e}"
        if not jobs:
            return f"[FAIL]   {c['name']}: no jobs returned - fix board/host/site in config.yaml (see README)"
        return f"[OK]     {c['name']}: working (e.g. \"{jobs[0]['title']}\")"

    print("Quick check: one job per company is enough...\n")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for line in ex.map(probe, companies):
            print(line)


def explain(title, loc, desc, cfg):
    R = get_rules(cfg)
    t = norm(title)
    inc = [w for w, rx in R["include"] if rx.search(t)]
    exc = [w for w, rx in R["exclude"] if rx.search(t)]
    print(f"Title:                 {title}")
    print(f"Include words matched: {inc or 'none'}")
    print(f"Exclude words matched: {exc or 'none'}")
    verdict, note = evaluate({"title": title, "location": loc, "description": desc}, cfg)
    print(f"Result:                {verdict or 'REJECTED'} - {note}")


def process_company(c, cfg, seen, deadline, debug):
    """Runs in a worker thread. Returns (company_name, scanned_count, rows, status)."""
    if time.time() > deadline:
        return c["name"], 0, [], "timeout"
    fetcher = FETCHERS.get(c["source"])
    if not fetcher:
        return c["name"], 0, [], f"unknown source '{c['source']}'"
    try:
        jobs = fetcher(c, cfg)
    except Exception as e:
        return c["name"], 0, [], f"error: {e}"

    ready, need_detail = [], []
    for j in jobs:
        if j["url"] in seen:
            continue
        key = next((k for k in DETAIL_FETCHERS if k in j), None)
        if key and not j["description"]:
            # cheap title/location check first: only fetch the detail page for plausible jobs
            if evaluate({**j, "description": "x"}, cfg)[0] is None:
                continue
            need_detail.append((j, key))
        else:
            ready.append(j)

    if need_detail:
        def fill(item):
            j, key = item
            j["description"] = DETAIL_FETCHERS[key](j)
        with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as ex:
            list(ex.map(fill, need_detail))
        ready += [j for j, _ in need_detail]

    rows = []
    for j in ready:
        verdict, note = evaluate(j, cfg)
        if not verdict and debug and note != "title: no keyword match":
            print(f"   [dropped] {j['company']} | {j['title']} | {j['location']} | {note}")
        if verdict:
            rows.append({**j, "verdict": verdict, "note": note})
    return c["name"], len(jobs), rows, "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default=str(BASE / "config.yaml"))
    ap.add_argument("--check", action="store_true", help="test every company and exit")
    ap.add_argument("--test", metavar="TITLE",
                    help='explain how one job title is judged, e.g. --test "Software Development Manager"')
    ap.add_argument("--loc", default="", help="location to use with --test")
    ap.add_argument("--desc", default="", help="description text to use with --test")
    ap.add_argument("--debug", action="store_true",
                    help="print why jobs with a matching title were dropped")
    ap.add_argument("--workers", type=int, default=24,
                    help="companies fetched in parallel (default 24)")
    args = ap.parse_args()
    cfg = load_config(args.config)
    get_rules(cfg)  # compile regexes once, before threads start

    if args.test is not None:
        explain(args.test, args.loc, args.desc, cfg)
        return

    out_dir = BASE / cfg.get("output_dir", "output")
    out_dir.mkdir(exist_ok=True)
    seen_file = out_dir / "seen.json"
    seen = set(json.loads(seen_file.read_text())) if seen_file.exists() else set()

    deadline = time.time() + cfg.get("max_runtime_minutes", 90) * 60
    companies = cfg["companies"][:]
    random.shuffle(companies)

    if args.check:
        run_check(companies, cfg, args.workers)
        return

    manual = [c for c in companies if c["source"] == "manual"]
    companies = [c for c in companies if c["source"] != "manual"]
    new_rows, t0 = [], time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(process_company, c, cfg, seen, deadline, args.debug) for c in companies]
        done = 0
        for f in as_completed(futures):
            done += 1
            name, scanned, rows, status = f.result()
            if status != "ok":
                print(f"[{done}/{len(companies)}] [skip] {name}: {status}")
                continue
            fresh = []
            for r in rows:                 # de-dupe + record seen in the main thread only
                if r["url"] not in seen:
                    seen.add(r["url"])
                    fresh.append(r)
            new_rows += fresh
            print(f"[{done}/{len(companies)}] [ok] {name}: {scanned} jobs scanned, {len(fresh)} new matches")

    print(f"\nFinished in {time.time() - t0:.0f}s")
    if new_rows or manual:
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        write_docx(out_dir / f"jobs_{stamp}.docx", new_rows, manual)
        seen_file.write_text(json.dumps(sorted(seen)))
        print(f"Saved {len(new_rows)} new jobs to {out_dir}")
    else:
        print("No new matching jobs this run.")


if __name__ == "__main__":
    sys.exit(main())