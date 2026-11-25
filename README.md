# Job Finder

Finds new **fresher-level (0-1 years)** software, AI/ML and data engineering jobs in **India** at 100+ companies, and saves only the *new* ones to a Word file with clickable links.

It reads the companies' public job-board feeds (Greenhouse, Workday, Oracle, Lever, SmartRecruiters and more), filters with simple rules, and can optionally use a small AI model that runs on your own computer to double-check the experience and location. **It never applies to anything.** You open the Word file and apply by hand.

## Contents
- [How it works](#how-it-works)
- [Pick your setup](#pick-your-setup)
- [1. Install](#1-install)
- [2. Check the company list](#2-check-the-company-list-one-time)
- [3. Run it](#3-run-it)
- [Option A: rules only](#option-a-rules-only-no-ai-check)
- [Option B: AI check on your own computer (Ollama)](#option-b-ai-check-on-your-own-computer-ollama)
- [Option C: AI check on Google Colab's GPU](#option-c-ai-check-on-google-colabs-gpu)
- [Configuration](#configuration-configyaml)
- [Output files](#output-files)
- [Command-line options](#command-line-options)
- [Run it automatically](#run-it-automatically)
- [Troubleshooting](#troubleshooting)
- [Notes and limits](#notes-and-limits)
- [Contributing](#contributing)

## How it works

1. **Fetch.** Pulls open jobs from every company in `config.yaml`, in parallel.
2. **Rules (no AI).** Cheap checks drop most jobs straight away:
   - the title must match one of your `titles` and none of your `exclude_title_words` (senior, lead, manager...)
   - titles with a level above entry level ("Engineer II", "Level 5") are dropped
   - the location must be in your `locations` list (whole-word match, so "India" never matches "Indiana")
   - if the location is vague ("2 Locations"), the job text must mention one of your locations
   - the job text is scanned for the experience wording ("3+ years of experience" is dropped, "freshers welcome" is kept)
3. **AI check (optional).** Jobs that pass the rules are read by a small local model (`qwen2.5:3b` through [Ollama](https://ollama.com)). It answers only two questions: *is this for freshers / under 1 year?* and *is the job in India?*
4. **Verdict.**

| Verdict | Meaning |
|---|---|
| **Match** | Clearly fits. Listed under "Likely matches". |
| **Maybe** | The job text doesn't say, or the model was unsure or unavailable. Listed under "Maybe (check manually)". |
| *(dropped)* | Clearly doesn't fit. Not listed. |

Built-in safety: the model can only drop a job if the exact sentence it quotes really appears in the job text. A made-up quote, a timeout or a bad answer turns into "Maybe", never a silent drop.

## Pick your setup

| | Option A: rules only | Option B: Ollama on your computer | Option C: Ollama on Colab GPU |
|---|---|---|---|
| Setup effort | none | install Ollama + download a 2 GB model | run a notebook + paste a URL |
| Accuracy | good, more "Maybe"s | best | best |
| Speed | fastest | seconds per job with a GPU or Apple Silicon Mac, about a minute or more per job on a CPU | seconds per job |
| Needs | Python | Python + about 4 GB free memory | Python + a Google account |

You only need the model for the **first run**, which checks every job that passes the rules. After that, answers are saved, so later runs only check *new* postings and are quick even on a CPU.

## 1. Install

You need **Python 3.10+** from [python.org](https://www.python.org/downloads/) (on Windows, tick "Add Python to PATH"). Check with `python --version` (Mac/Linux: `python3 --version`).

```bash
git clone https://github.com/Rishav9911/job-script.git
cd job-script
pip install -r requirements.txt
```

## 2. Check the company list (one time)

```bash
python job_finder.py --check
```

Each company shows `[OK]`, `[FAIL]` or `[manual]`. To fix a `[FAIL]`, see [Adding or fixing companies](#adding-or-fixing-companies).

## 3. Run it

Pick one option below, then run:

```bash
python job_finder.py
```

Results are written to the `output` folder.

### Option A: rules only (no AI check)

Nothing to set up:

```bash
python job_finder.py --no-model
```

Jobs that don't state the experience end up under "Maybe".

If you run the script *without* `--no-model` and Ollama isn't running, it prints a warning and continues with the rules only, so you always get results.

### Option B: AI check on your own computer (Ollama)

**One-time setup**

1. Install Ollama from [ollama.com/download](https://ollama.com/download) (Windows, Mac, Linux). On Windows and Mac it runs in the background after install. On Linux, run `ollama serve` if it isn't already running.
2. Download the model (about 2 GB, needs roughly 4 GB of free memory):
   ```bash
   ollama pull qwen2.5:3b
   ```
3. In `config.yaml`, make sure these are set:
   ```yaml
   use_model: true
   ollama_model: qwen2.5:3b
   ollama_url: http://localhost:11434
   max_runtime_minutes: 600      # a slow first run can take hours
   ```
4. Check it works, and see how fast it is on your computer:
   ```bash
   python job_finder.py --test "Software Engineer" --loc "Bengaluru, India" --desc "Requirements: 0-1 years of experience. Bengaluru office."
   ```
   Run it twice. The line `[model #1] 12s ...` shows the seconds per job. The second run is the real speed, because the first one includes loading the model.

**How long will it take?**

- **First run:** the model checks every job that passes the rules (about 90 in the author's run). With a GPU or an Apple Silicon Mac that is seconds per job. On a CPU it can be about a minute or more per job, so leave it running (an hour or more is possible).
- **Later runs:** only new postings are checked, so a run every few days is quick. Keep `output/model_cache.json` and `output/seen.json` (see [Output files](#output-files)).

**Too slow?** Lower `model_jd_chars` (for example `2500`) so less text is sent per job, or use [Option C](#option-c-ai-check-on-google-colabs-gpu) for the first run.

### Option C: AI check on Google Colab's GPU

Colab gives you a free GPU, so the model answers in seconds. The job fetching still runs **on your own computer**. Only the model runs on Colab, and a small tunnel connects the two. (Don't run the whole script on Colab: Workday often blocks Colab's shared IP addresses.)

1. Open [colab.research.google.com](https://colab.research.google.com), then **File > Upload notebook** and choose `job_finder.ipynb` from this repo.
2. Set **Runtime > Change runtime type > T4 GPU**, then save.
3. Run the cells in order, waiting for each to finish:
   - **Cell 1:** installs Ollama
   - **Cell 2:** starts Ollama and downloads the model
   - **Cell 3:** a watchdog that restarts Ollama if it crashes
   - **Cell 4:** opens the tunnel and **prints an address** like `https://something-random.trycloudflare.com`
4. Copy that address into `config.yaml` on your computer:
   ```yaml
   ollama_url: https://something-random.trycloudflare.com
   ```
5. Run `python job_finder.py` on your computer. You should see `[model] ON` and `[model #N] 1s ...` lines.

**Things to know about Colab**
- **Keep the Colab tab open** while the script runs. Free Colab disconnects idle sessions and limits GPU time.
- The address **changes every time** you restart the tunnel, so update `ollama_url` each time.
- **Don't share or commit the address.** Anyone who has it can use your Colab model while it's open. After you finish, switch `ollama_url` back to `http://localhost:11434`.
- If the GPU isn't available, try again later, or use Option A or B.

## Configuration (`config.yaml`)

```yaml
titles:                      # a job title must contain one of these
  - software engineer
  - machine learning engineer
exclude_title_words:         # skip titles containing any of these
  - senior
  - manager
max_years_experience: 1
locations:                   # job location must contain one of these ([] = no filter)
  - bengaluru
  - india
companies:
  - {name: Stripe,  source: greenhouse, board: stripe}
  - {name: NVIDIA,  source: workday, host: nvidia.wd5.myworkdayjobs.com, tenant: nvidia, site: NVIDIAExternalCareerSite}
  - {name: Google,  source: manual, url: "https://www.google.com/about/careers/applications/jobs/results?q=software%20engineer&location=India"}
```

| Setting | What it does | Default |
|---|---|---|
| `titles` | Job-title keywords to look for | required |
| `exclude_title_words` | Skip titles containing any of these | none |
| `max_years_experience` | Experience limit. See the note below. | `1` |
| `locations` | Allowed locations (whole-word match) | none (all kept) |
| `exclude_description_words` | Skip jobs whose text contains any of these | none |
| `max_runtime_minutes` | Stop starting new work after this long | `90` |
| `output_dir` | Where results are written | `output` |
| `use_model` | Use the AI check (`false` = rules only) | `true` |
| `ollama_model` | Model to use | `qwen2.5:3b` |
| `ollama_url` | Where Ollama is running | `http://localhost:11434` |
| `model_timeout_seconds` | Give up on one model call after this long | `180` |
| `model_jd_chars` | How much job text is sent to the model | `3500` |
| `workday_parallel` | Workday requests at once (all Workday companies share this) | `6` |
| `workday_gap_seconds` | Minimum pause between Workday requests | `0.15` |
| `exclude_senior_levels` | Drop titles like "Engineer II" or "Level 5" | `true` |
| `use_builtin_exclusions` | Also skip senior/staff/principal/lead/manager/director... titles | `true` |

**About `max_years_experience`:** with the AI check on, a job needs *fewer than* this many years, so `1` keeps fresher and 0-1 year jobs and drops "1+ years". In rules-only mode the limit is inclusive (up to and including this number). Set it to `2` if you want jobs asking for 1 year too.

### Adding or fixing companies

| `source` | Fields | Where to find them |
|---|---|---|
| `greenhouse` | `board` | Careers URL like `boards.greenhouse.io/<board>/jobs/...` |
| `lever` | `board` | `jobs.lever.co/<board>` |
| `ashby` | `board` | `jobs.ashbyhq.com/<board>` |
| `smartrecruiters` | `board`, optional `country: in` | `jobs.smartrecruiters.com/<board>` |
| `workday` | `host`, `tenant`, `site` | URL `https://<host>/<site>`. `tenant` is the first part of `host`; `site` is the part after `/en-US/` if present |
| `oracle` | `host`, `site` | URL `https://<host>/hcmUI/CandidateExperience/en/sites/<site>/...` |
| `amazon` | optional `location`, `country` | Built in |
| `atlassian` | none | Built in |
| `manual` | `url` | Any careers search page. It is listed at the end of the Word file for you to open yourself. |

Re-run `python job_finder.py --check` until the companies you care about show `[OK]`. If a company moved to another system, switch it to `source: manual`.

## Output files

Everything is written to the `output` folder. It is git-ignored, so it stays on your computer.

| File | What it is |
|---|---|
| `jobs_<date>_<time>.docx` | New jobs only: **Likely matches**, **Maybe (check manually)**, and a **MANUAL links** list |
| `seen.json` | Links of jobs already shown to you. They never appear again. Delete this file to see everything again. |
| `model_cache.json` | The model's saved answers, so a job is never asked twice. Delete it to re-ask everything. |

Jobs that were *dropped* are not saved in `seen.json`, so they are re-checked on every run. This is fast thanks to the cache, and it means a rule change can bring them back.

## Command-line options

| Option | What it does |
|---|---|
| `--no-model` | Rules only, no AI check |
| `--check` | Test every company and exit |
| `--test "TITLE" --loc "..." --desc "..."` | Show how one job would be judged (also times the model) |
| `--debug` | Print why each matching-title job was dropped |
| `--workers N` | Companies fetched in parallel (default 24) |
| `-c other.yaml` | Use a different config file |
| `--inspect <Oracle job link>` | Show exactly what the script reads for one Oracle job (for debugging) |

## Run it automatically

Your computer must be on at the scheduled time. If you use Option B, Ollama must be running too. If it isn't, the script warns and falls back to rules only.

**Windows (Task Scheduler)**
1. Create Basic Task, name it "Job finder", choose Daily (or every few days), pick a time.
2. Action: Start a program. Program: the full path to `python.exe` (find it with `where python`). Arguments: `job_finder.py`. Start in: your project folder.
3. In the task's Properties, tick "Run task as soon as possible after a scheduled start is missed".

**Mac / Linux (cron)**
```bash
crontab -e
0 1 * * * cd /path/to/job-script && /usr/bin/python3 job_finder.py >> run.log 2>&1
```

## Troubleshooting

| What you see | What to do |
|---|---|
| `[model WARNING] cannot reach Ollama` | Start Ollama (open the app, or `ollama serve`), then run again. Or keep going with the rules only. |
| `[model WARNING] model ... is not downloaded` | Run `ollama pull qwen2.5:3b` |
| Many `429 Too Many Requests` from Workday | Lower `workday_parallel` (try `2`) and raise `workday_gap_seconds` (try `0.4`). Run from your own computer, not Colab. |
| `[model failed, try 1/3]` | Ollama crashed or is busy. The script retries 3 times. After 5 jobs fail in a row the model is switched off for that run and the rest become "Maybe". Fix Ollama, delete `output/seen.json`, and run again. |
| The first run stopped early | `max_runtime_minutes` ran out. Unchecked jobs become "Maybe". Raise it (for example `600`) and delete `output/seen.json`. |
| A company shows `[FAIL]` | Fix `board` / `host` / `site` in `config.yaml` (see [Adding or fixing companies](#adding-or-fixing-companies)) |
| A wrong job slipped through, or a good one was dropped | Run with `--debug`, or `--test` with the job's title, location and text, and open an issue |

## Notes and limits

- It uses companies' **public** job-board feeds. Keep `--workers` and the Workday settings modest, and be kind to their servers.
- A small model can still be wrong. That is why unclear jobs go to "Maybe" instead of being hidden.
- Job sites change. If a company stops returning jobs, `--check` will show it.
- Always read the job description before you apply.

## Contributing

Suggestions and pull requests are welcome. Good things to contribute:

- new companies, or fixes for ones that stopped working
- support for another job-board system
- better rules for titles, locations or experience wording
- bug reports with the company name and the `--test` or `--debug` output

To contribute: fork the repo, create a branch, make your change, run `python job_finder.py --check` and a `--test` example, then open a pull request. For bigger changes, please open an issue first so we can talk it through.