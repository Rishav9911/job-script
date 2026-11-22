#!/usr/bin/env python3
"""
Job finder: reads companies from config.yaml, pulls their open jobs from the
company's public job-board feed, filters by title / experience / location, and
saves NEW matches to a .docx (plus a running CSV). It never applies to anything.

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


# ----------------------------------------------------------------- helpers
def get(url, **kw):
    err = None
    for attempt in range(3):
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
    for attempt in range(3):
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
    r = get(f"https://boards-api.greenhouse.io/v1/boards/{c['board']}/jobs?content=true")
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
                "result_limit": 100,
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
            r = post(f"{api}/jobs", {"appliedFacets": {}, "limit": 20,
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
            offset += 20
            time.sleep(1)
    return out


def workday_description(j):
    r = get(j["_workday_detail"])
    if not r:
        return ""
    info = r.json().get("jobPostingInfo", {})
    return clean(info.get("jobDescription", ""))


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "amazon": fetch_amazon,
    "workday": fetch_workday,
    "atlassian": fetch_atlassian,
}


# ----------------------------------------------------------------- filtering
EXP_RE = re.compile(r"(\d{1,2})\s*(?:\+|\s*(?:-|–|to)\s*\d{1,2})?\s*(?:years|year|yrs|yr)\b", re.I)
ENTRY_HINTS = re.compile(r"\b(fresher|new grad|graduate|entry[- ]level|early career|"
                         r"campus|intern|sde[- ]?1|sde[- ]?i\b|engineer i\b|associate)\b", re.I)


def min_years_required(text):
    """Smallest 'N years' figure that appears near the word 'experience'."""
    found = []
    for m in EXP_RE.finditer(text):
        window = text[max(0, m.start() - 80): m.end() + 80].lower()
        if "experience" in window or "exp" in window:
            found.append(int(m.group(1)))
    return min(found) if found else None


def evaluate(j, cfg):
    """Return (verdict, note). verdict is 'match', 'maybe' or None."""
    title = j["title"].lower()
    if not any(t.lower() in title for t in cfg["titles"]):
        return None, ""
    if any(x.lower() in title for x in cfg.get("exclude_title_words", [])):
        return None, ""
    locs = cfg.get("locations", [])
    loc = j["location"].lower().strip()
    vague = (not loc or loc in ("india", "remote india", "remote - india", "remote, india")
             or re.fullmatch(r"\d+\s+locations?", loc))
    if locs and not vague and not any(l.lower() in loc for l in locs):
        return None, ""
    if any(k.lower() in j["description"].lower() for k in cfg.get("exclude_description_words", [])):
        return None, ""

    max_years = cfg.get("max_years_experience", 1)
    yrs = min_years_required(j["description"])
    if yrs is not None:
        if yrs <= max_years:
            return "match", f"asks {yrs}+ yrs"
        return None, ""
    if ENTRY_HINTS.search(j["title"]) or ENTRY_HINTS.search(j["description"]):
        return "match", "entry-level wording"
    return "maybe", "experience not stated - check manually"


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
    if manual:
        doc.add_heading("Check these manually", level=2)
        for c in manual:
            p = doc.add_paragraph(f"{c['name']}: ")
            if c.get("url"):
                add_hyperlink(p, c["url"], "open saved search")
            else:
                p.add_run("(add a url in config.yaml)")
    doc.save(path)


# ----------------------------------------------------------------- main
def run_check(companies, cfg):
    print("Checking every company (can take a few minutes)...\n")
    for c in companies:
        if c["source"] == "manual":
            print(f"[manual] {c['name']}: {c.get('url', '(add a url in config.yaml)')}")
            continue
        fetcher = FETCHERS.get(c["source"])
        try:
            jobs = fetcher(c, cfg) if fetcher else []
        except Exception as e:
            print(f"[FAIL]   {c['name']}: {e}")
            continue
        if not jobs:
            print(f"[FAIL]   {c['name']}: no jobs returned - fix board/host/site in config.yaml (see README)")
        else:
            n = sum(1 for j in jobs if any(t.lower() in j["title"].lower() for t in cfg["titles"]))
            print(f"[OK]     {c['name']}: {len(jobs)} jobs, {n} with a matching title")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default=str(BASE / "config.yaml"))
    ap.add_argument("--check", action="store_true", help="test every company and exit")
    args = ap.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text())

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
            # cheap title/location check first, so we only fetch Workday detail when needed
            if "_workday_detail" in j and not j["description"]:
                if evaluate({**j, "description": "x"}, cfg)[0] is None:
                    continue
                j["description"] = workday_description(j)
                time.sleep(0.5)
            verdict, note = evaluate(j, cfg)
            if verdict:
                seen.add(j["url"])
                new_rows.append({**j, "verdict": verdict, "note": note})
                hits += 1
        print(f"[ok] {c['name']}: {len(jobs)} jobs scanned, {hits} new matches")
        time.sleep(random.uniform(1, 3))

    if new_rows or manual:
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        write_docx(out_dir / f"jobs_{stamp}.docx", new_rows, manual)
        csv_path = out_dir / "all_jobs.csv"
        is_new = not csv_path.exists()
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow(["date", "company", "title", "location", "verdict", "note", "url"])
            for r in new_rows:
                w.writerow([date.today(), r["company"], r["title"], r["location"],
                            r["verdict"], r["note"], r["url"]])
        seen_file.write_text(json.dumps(sorted(seen)))
        print(f"\nSaved {len(new_rows)} new jobs to {out_dir}")
    else:
        print("\nNo new matching jobs this run.")


if __name__ == "__main__":
    sys.exit(main())
