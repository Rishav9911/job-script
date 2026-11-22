#!/usr/bin/env python3
"""
Job finder: reads companies from config.yaml, pulls their open jobs from the
company's public job-board feed, filters by title / experience / location, and
saves NEW matches to a single .docx. It never applies to anything.

Run:  python job_finder.py            (uses config.yaml next to this file)
      python job_finder.py -c other.yaml
"""
import argparse
import csv
import html
import json
import random
import re
import sys
import time
import unicodedata
import difflib
from datetime import date, datetime
from pathlib import Path

import requests
import yaml

# HEADERS = {"User-Agent": "Mozilla/5.0 (personal job search script)"}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (personal job search script)",
    "Accept-Encoding": "gzip, deflate",
}
BASE = Path(__file__).parent
CHECK_MODE = False  # set by --check: fetch only enough to prove a company works


# ----------------------------------------------------------------- helpers
def get(url, **kw):
    err = None
    for attempt in range(1 if CHECK_MODE else 3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30, **kw)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            err = e
            time.sleep(2 * (attempt + 1))
    print(f"   [fetch failed] {url.split('?')[0]} -> {err}")
    return None


def post(url, payload):
    err = None
    for attempt in range(1 if CHECK_MODE else 3):
        try:
            r = requests.post(url, json=payload, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            err = e
            time.sleep(2 * (attempt + 1))
    print(f"   [fetch failed] {url} -> {err}")
    return None


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


# ----------------------------------------------------------------- sources
# Each fetcher returns a list of job dicts. `c` is the company's config block.

def fetch_greenhouse(c, cfg):
    suffix = "" if CHECK_MODE else "?content=true"
    r = get(f"https://boards-api.greenhouse.io/v1/boards/{c['board']}/jobs{suffix}")
    if not r:
        return []
    return [
        job(c["name"], j["title"], j["absolute_url"],
            (j.get("location") or {}).get("name", ""),
            j.get("updated_at", ""), j.get("content", ""))
        for j in r.json().get("jobs", [])
    ]


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
    out = []
    for query in cfg["titles"]:
        offset = 0
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
            if CHECK_MODE and out:
                return out
            offset += 100
            time.sleep(1)
    return out


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
    out, seen = [], set()
    for query in cfg["titles"]:
        offset = 0
        while offset < 200:
            r = post(f"{api}/jobs", {"appliedFacets": {}, "limit": 1 if CHECK_MODE else 20,
                                     "offset": offset, "searchText": query})
            if not r:
                break
            postings = r.json().get("jobPostings", [])
            if not postings:
                break
            for p in postings:
                path = p.get("externalPath", "")
                if path in seen:
                    continue
                seen.add(path)
                out.append(job(c["name"], p["title"],
                               f"https://{host}/en-US/{site}{path}",
                               p.get("locationsText", ""), p.get("postedOn", ""),
                               ""))  # description fetched later, only if title matches
                out[-1]["_workday_detail"] = f"{api}{path}"
            if CHECK_MODE and out:
                return out
            offset += 20
            time.sleep(1)
    return out


def workday_description(j):
    r = get(j["_workday_detail"])
    if not r:
        return ""
    info = r.json().get("jobPostingInfo", {})
    return clean(info.get("jobDescription", ""))


def fetch_oracle(c, cfg):
    """Oracle Cloud HCM 'Candidate Experience' sites (e.g. JPMorgan Chase).
    c needs: host (e.g. jpmc.fa.oraclecloud.com) and site (e.g. CX_1001)."""
    host, site = c["host"], c["site"]
    api = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    page = 1 if CHECK_MODE else 25
    out, seen = [], set()
    for query in cfg["titles"]:
        offset = 0
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
                if not rid or rid in seen:
                    continue
                seen.add(rid)
                locs = [q.get("PrimaryLocation", "")] + [
                    s_.get("Name", "") for s_ in (q.get("secondaryLocations") or [])]
                j = job(c["name"], q.get("Title", ""),
                        f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{rid}",
                        "; ".join(x for x in locs if x), q.get("PostedDate", ""), "")
                j["_oracle"] = (host, site, rid)  # description fetched later, only if title matches
                out.append(j)
            if CHECK_MODE and out:
                return out
            offset += page
            time.sleep(1)
    return out


def oracle_description(j):
    host, site, rid = j["_oracle"]
    r = get(f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails",
            params={"expand": "all", "onlyData": "true",
                    "finder": f'ById;Id="{rid}",siteNumber={site}'})
    if not r:
        return ""
    items = r.json().get("items") or [{}]
    d = items[0]
    return clean(" ".join(str(d.get(k) or "") for k in (
        "ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")))


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "amazon": fetch_amazon,
    "workday": fetch_workday,
    "atlassian": fetch_atlassian,
    "oracle": fetch_oracle,
}


# ----------------------------------------------------------------- filtering
def norm(s):
    """Lower-case (casefold), strip accents, collapse whitespace - so matching ignores case/spacing."""
    s = unicodedata.normalize("NFKD", str(s if s is not None else ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("\xa0", " ").casefold()
    return re.sub(r"\s+", " ", s).strip()


def as_list(v):
    """Config value -> clean list of normalised, non-empty strings (accepts a lone string too)."""
    if v is None:
        return []
    if isinstance(v, (str, int, float)):
        v = [v]
    return [t for t in (norm(x) for x in v) if t]


def prefix_re(term):
    """term must start at a word boundary ('sde' won't match inside another word)."""
    return re.compile(r"(?<![a-z0-9])" + r"\s+".join(re.escape(p) for p in term.split()))


def word_re(term):
    """term must be a whole word (used for short exclusions like 'sr', 'vp')."""
    return re.compile(r"(?<![a-z0-9])" + r"\s+".join(re.escape(p) for p in term.split()) + r"(?![a-z0-9])")


BUILTIN_EXCLUDES = ["senior", "sr", "sr.", "staff", "principal", "lead", "manager", "director",
                    "head of", "architect", "vp", "vice president", "distinguished", "fellow"]
# 'Software Engineer II', 'SDE 2', 'Engineer III' ... but not 'Engineer I' or 'Engineer - 2024 Graduates'
LEVEL_RE = re.compile(r"(?<![a-z0-9])(?:engineer|developer|sde|swe|sdet|programmer|scientist)"
                      r"\s*[-,:]?\s*(?:ii|iii|iv|v|2|3|4|5|6)(?![a-z0-9])")
# bengaluru/bangalore and gurugram/gurgaon are the same place: listing either matches both
LOCATION_ALIASES = [("bengaluru", "bangalore", "bengalooru"), ("gurugram", "gurgaon")]

# equivalent job titles: listing one matches all of them
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
    """Smallest 'N years' figure near the word 'experience' (digits or words, ranges, decimals)."""
    text = norm(text)
    found = []
    for m in EXP_RE.finditer(text):
        window = text[max(0, m.start() - 80): m.end() + 80]
        if re.search(r"experience|\bexp\b", window):
            v = m.group(1)
            found.append(float(_NUMW.get(v, v)))
    return min(found) if found else None


def is_vague_location(loc):
    """Locations too vague to reject: empty, plain 'India', '2 Locations', 'Remote - India'..."""
    return (not loc
            or loc in {"india", "pan india", "anywhere in india", "multiple locations",
                       "various locations", "multiple", "various"}
            or re.fullmatch(r"\d+\s+locations?", loc) is not None
            or re.fullmatch(r"(remote\W+)?india(\W+remote)?", loc) is not None)


def get_rules(cfg):
    """Compile the filter settings once (cached inside cfg)."""
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
                   "workday": ["host", "tenant", "site"], "oracle": ["host", "site"],
                   "amazon": [], "atlassian": [], "manual": []}


def load_config(path):
    """Read config.yaml and catch typos/missing fields early, with clear messages."""
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

    # Always-present, separate table of manual-check links
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
def run_check(companies, cfg):
    """Quick check: one real job per company is enough to prove it works."""
    global CHECK_MODE
    CHECK_MODE = True
    from concurrent.futures import ThreadPoolExecutor

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
    with ThreadPoolExecutor(max_workers=8) as ex:
        for line in ex.map(probe, companies):
            print(line)


def explain(title, loc, desc, cfg):
    """Show exactly which config words hit a title, and the final verdict."""
    R = get_rules(cfg)
    t = norm(title)
    inc = [w for w, rx in R["include"] if rx.search(t)]
    exc = [w for w, rx in R["exclude"] if rx.search(t)]
    print(f"Title:                 {title}")
    print(f"Include words matched: {inc or 'none'}")
    print(f"Exclude words matched: {exc or 'none'}")
    verdict, note = evaluate({"title": title, "location": loc, "description": desc}, cfg)
    print(f"Result:                {verdict or 'REJECTED'} - {note}")


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
    args = ap.parse_args()
    cfg = load_config(args.config)

    if args.test is not None:
        explain(args.test, args.loc, args.desc, cfg)
        return

    out_dir = BASE / cfg.get("output_dir", "output")
    out_dir.mkdir(exist_ok=True)
    seen_file = out_dir / "seen.json"
    seen = set(json.loads(seen_file.read_text())) if seen_file.exists() else set()

    deadline = time.time() + cfg.get("max_runtime_minutes", 90) * 60
    companies = cfg["companies"][:]
    random.shuffle(companies)  # so a time cut-off doesn't always skip the same companies

    if args.check:
        run_check(companies, cfg)
        return

    manual = [c for c in companies if c["source"] == "manual"]
    companies = [c for c in companies if c["source"] != "manual"]
    new_rows = []
    for c in companies:
        if time.time() > deadline:
            print("Time limit reached; stopping.")
            break
        fetcher = FETCHERS.get(c["source"])
        if not fetcher:
            print(f"[skip] {c['name']}: unknown source '{c['source']}'")
            continue
        try:
            jobs = fetcher(c, cfg)
        except Exception as e:  # one broken company shouldn't kill the run
            print(f"[error] {c['name']}: {e}")
            continue
        hits = 0
        for j in jobs:
            if j["url"] in seen:
                continue
            # cheap title/location check first, so we only fetch the detail page when needed
            if ("_workday_detail" in j or "_oracle" in j) and not j["description"]:
                if evaluate({**j, "description": "x"}, cfg)[0] is None:
                    continue
                j["description"] = (workday_description(j) if "_workday_detail" in j
                                    else oracle_description(j))
                time.sleep(0.5)
            verdict, note = evaluate(j, cfg)
            if not verdict and args.debug and note != "title: no keyword match":
                print(f"   [dropped] {j['title']} | {j['location']} | {note}")
            if verdict:
                seen.add(j["url"])
                new_rows.append({**j, "verdict": verdict, "note": note})
                hits += 1
        print(f"[ok] {c['name']}: {len(jobs)} jobs scanned, {hits} new matches")
        time.sleep(random.uniform(1, 3))

    if new_rows or manual:
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        write_docx(out_dir / f"jobs_{stamp}.docx", new_rows, manual)
        seen_file.write_text(json.dumps(sorted(seen)))
        print(f"\nSaved {len(new_rows)} new jobs to {out_dir}")
    else:
        print("\nNo new matching jobs this run.")


if __name__ == "__main__":
    sys.exit(main())