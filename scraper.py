#!/usr/bin/env python3
"""
Automated SWE internship/job alert emailer.

Runs on an hourly GitHub Actions cron. On each run it:
  1. Checks whether the current US-Eastern hour is within SEND_HOURS_ET
     (8am-10pm ET); if not, exits immediately (GitHub cron is UTC-only, so
     we gate here using zoneinfo which handles EST/EDT automatically).
  2. Fetches JSON + Markdown-table job sources.
  3. Parses, filters for SWE relevance, and dedupes against seen.json.
  4. Emails any brand-new postings, then records them in seen.json.

State (seen.json) is persisted by committing it back to the repo from the
workflow, since GitHub Actions runners are ephemeral.
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Every hour from 8am through 10pm ET, inclusive.
SEND_HOURS_ET = set(range(8, 23))
EASTERN = ZoneInfo("America/New_York")

SEEN_FILE = Path(__file__).parent / "seen.json"
USER_AGENT = "job-hunter-bot/1.0 (+https://github.com/actions)"

# JSON sources: flat array of listing objects. Dedupe on "id".
JSON_SOURCES = [
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/dev/.github/scripts/listings.json",
]

# Markdown-table sources. `title_filter=True` means the repo mixes disciplines
# and we must keyword-filter titles for SWE relevance; False means the repo is
# already scoped to software roles.
MARKDOWN_SOURCES = [
    {"url": "https://raw.githubusercontent.com/sndsh404/summer-2027-internships/main/README.md",
     "title_filter": False},
    {"url": "https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/README.md",
     "title_filter": False},
    {"url": "https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/INTERN_INTL.md",
     "title_filter": False},
    {"url": "https://raw.githubusercontent.com/jobright-ai/2026-Software-Engineer-Internship/master/README.md",
     "title_filter": False},
    {"url": "https://raw.githubusercontent.com/jobright-ai/2026-Engineer-Internship/master/README.md",
     "title_filter": True},
]

# Category substrings (JSON sources) that count as relevant. Matched against
# SimplifyJobs-style categories: "Software", "Software Engineering",
# "AI/ML/Data", "Data Science, AI & Machine Learning", "Quant",
# "Quantitative Finance" all match; "Hardware"/"Product" do not.
CATEGORY_KEYWORDS = ["software", "ai", "machine learning", "quant", "data science"]

# Keywords used to decide relevance when title filtering is required
# (markdown sources, and JSON sources with no category field). Covers
# software engineering, AI/ML, and quant roles.
RELEVANT_KEYWORDS = [
    "software", "swe", "developer", "programming", "full stack", "full-stack",
    "fullstack", "backend", "back-end", "back end", "frontend", "front-end",
    "front end", "web", "mobile", "ios", "android", "devops", "site reliability",
    "sre", "data engineer", "machine learning", "ml engineer", "ai engineer",
    "computer vision", "embedded software", "firmware", "platform engineer",
    "systems engineer", "cloud", "security engineer", "qa", "sdet", "test engineer",
    # AI/ML
    "artificial intelligence", "deep learning", "nlp", "llm", "data scientist",
    "data science", "research engineer", "applied scientist", "ai researcher",
    # Quant
    "quant", "quantitative", "algo trading", "algorithmic trading",
]

# Matches BOTH markdown links [text](url) AND raw HTML <a href="url">text</a>.
LINK_RE = re.compile(
    r'\[(?P<mdtext>[^\]]*)\]\((?P<mdurl>[^)]+)\)'      # [text](url)
    r'|<a\s+href="(?P<hturl>[^"]+)"[^>]*>(?P<httext>.*?)</a>',  # <a href="url">text</a>
    re.IGNORECASE | re.DOTALL,
)
TAG_RE = re.compile(r"<[^>]+>")
SEPARATOR_CELL_RE = re.compile(r"^:?-{2,}:?$")


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

def log(msg: str, level: str = "INFO") -> None:
    """Timestamped line to stdout (flushed so it streams in the Actions log)."""
    ts = datetime.now(EASTERN).strftime("%H:%M:%S")
    print(f"[{ts} ET] [{level:5}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def fetch(url: str) -> str:
    log(f"Fetching {url}", "FETCH")
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=60) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    log(f"  -> {len(body):,} bytes (HTTP {resp.status})", "FETCH")
    return body


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #

def extract_links(cell: str) -> list[tuple[str, str]]:
    """Return [(text, url), ...] for every markdown/HTML link in a cell."""
    links = []
    for m in LINK_RE.finditer(cell):
        if m.group("mdurl") is not None:
            text, url = m.group("mdtext"), m.group("mdurl")
        else:
            text, url = m.group("httext"), m.group("hturl")
        text = TAG_RE.sub("", text or "").strip()   # drop nested <strong>/<img>
        links.append((text, url.strip()))
    return links


def cell_text(cell: str) -> str:
    """Human-readable text of a cell: markdown links -> their text, HTML stripped."""
    text = LINK_RE.sub(lambda m: TAG_RE.sub("", (m.group("mdtext") or m.group("httext") or "")), cell)
    text = TAG_RE.sub("", text)
    return text.replace("**", "").strip()


def is_separator_row(cells: list[str]) -> bool:
    non_empty = [c for c in cells if c.strip()]
    return bool(non_empty) and all(SEPARATOR_CELL_RE.match(c.strip()) for c in non_empty)


def parse_markdown_table(text: str, title_filter: bool) -> list[dict]:
    """Parse every table row across a markdown document into listing dicts."""
    results = []
    prev_company = ""
    table_rows = 0
    kw_dropped = 0
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        # Split on pipes and drop the empty edge cells created by leading/trailing |.
        cells = [c.strip() for c in line.split("|")]
        if cells and cells[0] == "":
            cells = cells[1:]
        if cells and cells[-1] == "":
            cells = cells[:-1]
        if len(cells) < 2 or is_separator_row(cells):
            continue

        # --- Company (always first cell). "↳" == same company as previous row.
        company_links = extract_links(cells[0])
        raw_company = cell_text(cells[0])
        if raw_company in ("↳", "⤷", "") or raw_company.startswith("↳"):
            company = prev_company
        else:
            company = company_links[0][0] if company_links and company_links[0][0] else raw_company
            prev_company = company

        # --- Title (second cell), plain text or link text.
        title = cell_text(cells[1])
        if not title:
            continue

        # --- Apply link: if cell[1] itself contains a link (jobright), use it;
        #     otherwise use the last link found anywhere in the row.
        cell1_links = extract_links(cells[1])
        if cell1_links:
            apply_url = cell1_links[-1][1]
        else:
            row_links = extract_links(line)
            if not row_links:
                continue
            apply_url = row_links[-1][1]

        # Skip header rows that slipped through (no real URL).
        if not apply_url.lower().startswith("http"):
            continue

        table_rows += 1
        location = cell_text(cells[2]) if len(cells) > 2 else ""

        if title_filter and not is_relevant_title(title):
            kw_dropped += 1
            continue

        results.append({
            "key": f"url:{apply_url}",
            "company": company or "Unknown",
            "title": title,
            "location": location,
            "url": apply_url,
        })
    kw_note = f", dropped {kw_dropped} non-relevant title" if title_filter else ""
    log(f"  parsed {table_rows} table rows -> kept {len(results)}{kw_note}", "PARSE")
    return results


def parse_json_source(text: str) -> list[dict]:
    results = []
    data = json.loads(text)
    dropped = {"inactive": 0, "category": 0, "keyword": 0, "no_id": 0}
    for item in data:
        if item.get("active") is False or item.get("is_visible") is False:
            dropped["inactive"] += 1
            continue
        category = item.get("category")
        if category:
            # Structured category field -> filter on Software/AI/Quant categories.
            if not any(kw in category.lower() for kw in CATEGORY_KEYWORDS):
                dropped["category"] += 1
                continue
        else:
            # Some repos (e.g. vanshb03) omit category entirely; fall back to
            # keyword-filtering the title for relevance.
            if not is_relevant_title(item.get("title") or ""):
                dropped["keyword"] += 1
                continue
        listing_id = item.get("id")
        if not listing_id:
            dropped["no_id"] += 1
            continue
        locations = item.get("locations") or []
        results.append({
            "key": f"json:{listing_id}",
            "company": item.get("company_name") or "Unknown",
            "title": item.get("title") or "Software role",
            "location": ", ".join(locations) if isinstance(locations, list) else str(locations),
            "url": item.get("url") or "",
        })
    log(f"  parsed {len(data)} items -> kept {len(results)} relevant "
        f"(dropped: {dropped['inactive']} inactive, {dropped['category']} irrelevant cat, "
        f"{dropped['keyword']} irrelevant title, {dropped['no_id']} missing id)", "PARSE")
    return results


def is_relevant_title(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in RELEVANT_KEYWORDS)


# --------------------------------------------------------------------------- #
# Seen-state persistence
# --------------------------------------------------------------------------- #

def load_seen() -> set[str]:
    if not SEEN_FILE.exists():
        return set()
    try:
        data = json.loads(SEEN_FILE.read_text())
        return set(data.get("seen", []))
    except (json.JSONDecodeError, ValueError):
        return set()


def save_seen(seen: set[str]) -> None:
    SEEN_FILE.write_text(json.dumps({"seen": sorted(seen)}, indent=0))


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #

def build_email_html(postings: list[dict]) -> str:
    rows = []
    for p in postings:
        company = escape(p["company"])
        title = escape(p["title"])
        location = escape(p["location"] or "—")
        url = escape(p["url"], quote=True)
        rows.append(
            f'<tr>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;"><b>{company}</b></td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;">{title}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;color:#555;">{location}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;">'
            f'<a href="{url}">Apply</a></td>'
            f'</tr>'
        )
    now = datetime.now(EASTERN).strftime("%b %d, %Y %I:%M %p ET")
    return (
        f'<html><body style="font-family:-apple-system,Segoe UI,Arial,sans-serif;">'
        f'<h2>{len(postings)} new SWE posting(s)</h2>'
        f'<p style="color:#777;">Generated {now}</p>'
        f'<table style="border-collapse:collapse;width:100%;font-size:14px;">'
        f'<thead><tr style="text-align:left;background:#f6f6f6;">'
        f'<th style="padding:8px 12px;">Company</th><th style="padding:8px 12px;">Title</th>'
        f'<th style="padding:8px 12px;">Location</th><th style="padding:8px 12px;">Link</th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table>'
        f'</body></html>'
    )


def send_email(postings: list[dict]) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    email_from = os.environ.get("EMAIL_FROM", user)
    email_to = os.environ.get("EMAIL_TO", "thomas.hung@outlook.com")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Job Alert] {len(postings)} new SWE posting(s)"
    msg["From"] = email_from
    msg["To"] = email_to

    plain = "\n".join(
        f"- {p['company']} — {p['title']} ({p['location'] or 'n/a'})\n  {p['url']}"
        for p in postings
    )
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(build_email_html(postings), "html"))

    recipients = [a.strip() for a in email_to.split(",")]
    log(f"Connecting to SMTP {host}:{port} as {user}", "EMAIL")
    with smtplib.SMTP(host, port, timeout=60) as server:
        server.starttls()
        server.login(user, password)
        log(f"Sending to {recipients} (from {email_from})", "EMAIL")
        server.sendmail(email_from, recipients, msg.as_string())
    log(f"Email delivered: {len(postings)} posting(s)", "EMAIL")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def gather_listings() -> list[dict]:
    listings: list[dict] = []
    total = len(JSON_SOURCES) + len(MARKDOWN_SOURCES)
    for i, url in enumerate(JSON_SOURCES, 1):
        log(f"Source {i}/{total} (JSON): {url.split('/')[4]}", "SOURCE")
        try:
            listings += parse_json_source(fetch(url))
        except Exception as e:  # noqa: BLE001 - one bad source shouldn't kill the run
            log(f"JSON source failed {url}: {e}", "WARN")
    for j, src in enumerate(MARKDOWN_SOURCES, len(JSON_SOURCES) + 1):
        log(f"Source {j}/{total} (MD, title_filter={src['title_filter']}): "
            f"{src['url'].split('/')[4]}", "SOURCE")
        try:
            listings += parse_markdown_table(fetch(src["url"]), src["title_filter"])
        except Exception as e:  # noqa: BLE001
            log(f"Markdown source failed {src['url']}: {e}", "WARN")
    return listings


def parse_args(argv: list[str] | None = None):
    import argparse
    p = argparse.ArgumentParser(
        description="Fetch, filter and email new SWE job/internship postings.")
    p.add_argument("--force", "--now", action="store_true", dest="force",
                   help="Run immediately, ignoring the 8/12/16/20 ET time gate. "
                        "Also enabled by the FORCE_RUN=1 env var.")
    p.add_argument("--dry-run", action="store_true",
                   help="Do everything except send email and update seen.json "
                        "(prints what WOULD be sent). Great for a manual test.")
    p.add_argument("--seed-send", action="store_true",
                   help="On a first/empty run, email the current batch instead of "
                        "silently seeding it.")
    p.add_argument("--test-email", action="store_true",
                   help="Send a single sample email to verify SMTP/Gmail setup, "
                        "then exit. Touches nothing else.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.test_email:
        log("Test-email mode: sending one sample posting to verify SMTP setup.", "EMAIL")
        sample = [{
            "company": "Test Company",
            "title": "Sample SWE Intern (delivery test)",
            "location": "Remote",
            "url": "https://github.com/thung19/job-hunter",
        }]
        send_email(sample)
        log("Test email sent. Check your inbox (and spam folder).", "EMAIL")
        return 0

    force = args.force or os.environ.get("FORCE_RUN") == "1"
    now_et = datetime.now(EASTERN)
    log(f"Run start. Eastern time = {now_et:%Y-%m-%d %H:%M:%S %Z}, "
        f"force={force}, dry_run={args.dry_run}, send-hours={sorted(SEND_HOURS_ET)}")
    if not force and now_et.hour not in SEND_HOURS_ET:
        log(f"Eastern hour {now_et.hour} not in {sorted(SEND_HOURS_ET)}; exiting without emailing.", "SKIP")
        return 0

    seen = load_seen()
    first_run = not SEEN_FILE.exists() or not seen
    log(f"Loaded seen.json: {len(seen)} known keys (first_run={first_run})")

    listings = gather_listings()
    log(f"Gathered {len(listings)} SWE listings from all sources.")

    # De-dupe within this batch as well as against history.
    new_postings = []
    batch_keys = set()
    dup_seen = dup_batch = 0
    for item in listings:
        k = item["key"]
        if k in seen:
            dup_seen += 1
            continue
        if k in batch_keys:
            dup_batch += 1
            continue
        batch_keys.add(k)
        new_postings.append(item)
    log(f"Dedupe: {len(new_postings)} new, {dup_seen} already-seen, {dup_batch} intra-batch dupes.")

    if first_run and not args.seed_send:
        # Seed silently so the very first run doesn't blast hundreds of emails.
        if args.dry_run:
            log(f"[dry-run] Would seed {len(batch_keys)} postings without emailing.", "SEED")
            return 0
        for item in listings:
            seen.add(item["key"])
        save_seen(seen)
        log(f"First run: recorded {len(seen)} existing postings without emailing.", "SEED")
        return 0

    if not new_postings:
        log("No new postings to send. Done.")
        return 0

    log(f"{len(new_postings)} new posting(s) to email:")
    for p in new_postings:
        log(f"  + {p['company']} — {p['title']} ({p['location'] or 'n/a'})", "NEW")

    if args.dry_run:
        log(f"[dry-run] Would email {len(new_postings)} posting(s) and record them. "
            f"No email sent, seen.json untouched.", "EMAIL")
        return 0

    send_email(new_postings)
    for item in new_postings:
        seen.add(item["key"])
    save_seen(seen)
    log(f"Done. seen.json now has {len(seen)} keys.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
