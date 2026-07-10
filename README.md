# Job Hunter — automated SWE posting emailer

Emails you brand-new software-engineering internship/job postings **four times a
day** (8am, 12pm, 4pm, 8pm US Eastern) and never re-sends anything you've already
seen. Runs entirely on GitHub Actions' free tier — no machine of yours needs to
be on.

## How it works

- **Schedule:** GitHub cron fires hourly (UTC). `scraper.py` uses
  `zoneinfo("America/New_York")` to check whether the current Eastern hour is one
  of `{8, 12, 16, 20}`; if not, it exits immediately. This tracks EST/EDT
  automatically.
- **Sources:** two JSON feeds + five markdown-table READMEs (see `scraper.py`).
- **Dedup:** JSON listings dedupe on their `id`; markdown listings dedupe on the
  extracted apply URL. Seen keys are stored in `seen.json`, which the workflow
  commits back to the repo after each run (runners are ephemeral, so state lives
  in the repo).
- **Filtering:** JSON sources filter on `category` containing "Software" (or, when
  a feed has no category field, on SWE title keywords). The all-disciplines
  jobright *Engineer* repo is title-keyword filtered; the other markdown sources
  are already SWE-scoped.
- **First run is silent:** when `seen.json` is empty, the first run records every
  current posting *without* emailing, so you don't get one giant 1000-item blast.
  From then on you only get genuinely new postings.

## Setup

1. **Create a GitHub repo** and push these files to it (must include the
   `.github/workflows/job-alert.yml` path).

2. **Add repository secrets** (Settings → Secrets and variables → Actions → New
   repository secret):

   | Secret | Value |
   | --- | --- |
   | `SMTP_HOST` | e.g. `smtp.gmail.com` |
   | `SMTP_PORT` | `587` |
   | `SMTP_USER` | your sending email address |
   | `SMTP_PASS` | app password (see below) |
   | `EMAIL_FROM` | (optional) defaults to `SMTP_USER` |
   | `EMAIL_TO` | where alerts go — defaults to `thomas.hung@outlook.com`; comma-separate for multiple |

   **Gmail is easiest:** enable 2-Step Verification, then create an
   [App Password](https://myaccount.google.com/apppasswords) and use that as
   `SMTP_PASS` (not your normal password). Outlook/Microsoft accounts have largely
   disabled SMTP basic-auth, so a Gmail sender is recommended even if `EMAIL_TO`
   is your Outlook address.

3. **Enable Actions write permission** (so the workflow can commit `seen.json`):
   Settings → Actions → General → Workflow permissions → **Read and write
   permissions**. (The workflow also declares `permissions: contents: write`.)

4. **Done.** The hourly cron takes over.

## Running it manually

**From GitHub (no local setup needed):** Actions tab → *Job Alert* → **Run
workflow**. Two toggles:
- **force** (default on) — run right now, ignoring the 8/12/16/20 ET gate.
- **dry_run** — show what *would* be emailed without sending or touching
  `seen.json`. Good for a safe test.

**From your terminal:**

```bash
export SMTP_HOST=smtp.gmail.com SMTP_PORT=587 \
       SMTP_USER=you@gmail.com SMTP_PASS='your-app-password' \
       EMAIL_TO=thomas.hung@outlook.com
python3 scraper.py --force            # run now, ignore the time gate
python3 scraper.py --force --dry-run  # preview only: no email, no state change
python3 scraper.py --help             # list all flags
```

Flags:
- `--force` / `--now` — bypass the Eastern-time gate (or set `FORCE_RUN=1`).
- `--dry-run` — do everything except send email / update `seen.json`.
- `--seed-send` — on a first/empty run, email the batch instead of seeding silently.

Delete/empty `seen.json` to re-seed. No third-party packages required —
standard library only (Python 3.9+).

## Files

- `scraper.py` — fetch, parse, filter, dedupe, email.
- `.github/workflows/job-alert.yml` — hourly cron + commits `seen.json`.
- `seen.json` — dedupe state (starts empty; maintained automatically).
