# Job finder - setup guide

Finds new software / AI engineer jobs (0-1 yrs) at top-paying companies in
Bengaluru, Gurugram/Gurgaon, Noida, Pune and Hyderabad, and saves them to a
Word file. It never applies to anything.

## 1. Install Python
Download Python 3.10+ from python.org. On Windows, tick "Add Python to PATH".
Check: `python --version` (Mac/Linux: `python3 --version`).

## 2. Put the files in one folder
`job_finder.py`, `config.yaml`, `requirements.txt` (e.g. `C:\job-finder` or `~/job-finder`).

## 3. Install the packages
Open a terminal in that folder:
    pip install -r requirements.txt

## 4. Test every company (one time)
    python job_finder.py --check

Each line shows [OK], [FAIL] or [manual].

## 5. Fix any [FAIL] (one time)
- Greenhouse: open the company's careers page, click any job. If the URL has
  `greenhouse.io/<name>/jobs/...`, put `<name>` in `board:`.
- Workday: open the careers page. The URL looks like
  `https://<host>.myworkdayjobs.com/<site>`. Put the full host in `host:`, the
  first part of the host in `tenant:`, and `<site>` in `site:`
  (the part after `/en-US/` if present).
- If a company moved to another system, switch it to `source: manual`.

Re-run `--check` until the companies you care about show [OK].

## 6. Manual companies (one time, ~2 minutes each)
Google, Microsoft, LinkedIn, Rippling, Uber, Goldman Sachs use custom sites.
For each: open its careers search, apply Location = India (or your cities),
keyword = software engineer, and an early-career / 0-2 yrs filter if offered.
Copy the resulting address-bar URL into `url:` in config.yaml. You can also
switch on the site's job-alert emails as a backup.
Each daily Word file ends with a "Check these manually" list of those links.

## 7. Run it
    python job_finder.py

Results land in the `output` folder: `jobs_<date>.docx` (new jobs only, with
clickable links) and `all_jobs.csv` (running history).

## 8. Run it automatically
Your computer must be on (or able to wake) at the scheduled time.

Windows (Task Scheduler):
1. Create Basic Task -> name it "Job finder" -> Daily -> pick e.g. 1:00 AM.
2. Action: Start a program.
   Program: full path to python.exe (find it with `where python`).
   Arguments: `job_finder.py`
   Start in: your job-finder folder.
3. Open the task's Properties: tick "Run task as soon as possible after a
   scheduled start is missed" and "Wake the computer to run this task".

Mac / Linux (cron):
    crontab -e
    0 1 * * * cd /path/to/job-finder && /usr/bin/python3 job_finder.py >> run.log 2>&1
(Mac: keep the laptop plugged in and not asleep, or use `pmset schedule wake`.)

## 9. Daily routine
Open the newest file in `output`. "Likely matches" first, then "Maybe",
then the manual links. Apply by hand.

## Tuning
- Too many results: add words to `exclude_title_words`.
- Too few: raise `max_years_experience` to 2.
- Titles: edit `titles` in config.yaml.
