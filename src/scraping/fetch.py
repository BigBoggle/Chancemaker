#!/usr/bin/env python3
"""
Download a Keeneland race day's PDFs by date and turn the program into JSON.

Usage:
    python src/scraping/fetch.py                  # asks for the date
    python src/scraping/fetch.py 10/16/2026       # or 10-16-26, or 2026-10-16
    python src/scraping/fetch.py 10/16/26 --out some/folder --no-chart

What it does, for one race date:
  1. Downloads the race-day program (it contains the past-performance pages):
       https://www.keeneland.com/files/programs/M-D-YY-kee-online-programpdf.pdf
     Month and day are NOT zero-padded here: October 9, 2026 -> 10-9-26.

  2. Downloads the Equibase result chart for the same card (the page the program's
     QR codes link to). It only exists after the races have been run:
       https://tvg.equibase.com/static/chart/pdf/KEEMMDDYYUSA.pdf
     Here month and day ARE zero-padded: October 9, 2026 -> KEE100926USA.

  3. Parses the program with pdf_reading.parse_pdf, checks the result with
     check_parser.check_card, and writes the essentials as JSON.

Files go to <out>/<YYYY-MM-DD>/ (default out: src/scraping/downloads, which the repo's
.gitignore already ignores):
    program.pdf, chart.pdf,
    card.json             the whole day, essentials only (see pdf_reading.slim_card)
    race1.json, ...       one race each, the input pp_json_to_handicap.py expects
    check.json            the check findings

Needs the "requests" package (pip install requests).
Exit code: 0 if the program was downloaded, parsed and passed the checks; 1 otherwise.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

import requests

# Make the other scraper files importable however this script is started (they sit in
# the same folder, which is not an installed package).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_parser  # noqa: E402  (needs the path above)
import pdf_reading  # noqa: E402

# Where the PDFs live. {m}, {d}, {yy} are filled in by program_url / chart_url.
PROGRAM_URL = (
    "https://www.keeneland.com/files/programs/{m}-{d}-{yy}-kee-online-programpdf.pdf"
)
CHART_URL = "https://tvg.equibase.com/static/chart/pdf/KEE{m:02d}{d:02d}{yy}USA.pdf"
DEFAULT_OUT = Path(__file__).resolve().parent / "downloads"

# A plain, generic client name; nothing identifying about the user is sent.
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; race-program-fetcher)"}
TIMEOUT = 60  # seconds


# Turn the date the user typed into a date object (accepts several formats)
def parse_date(text):
    """'10/16/2026', '10/16/26', '10-16-26', '2026-10-16' -> datetime.date."""
    t = text.strip()

    # Year first: 2026-10-16
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # Month first: 10/16/2026, 10-16-26 (a two-digit year means 20xx)
    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2}|\d{4})", t)
    if m:
        year = int(m.group(3))
        year += 2000 if year < 100 else 0
        return dt.date(year, int(m.group(1)), int(m.group(2)))

    raise ValueError(f"unrecognised date {text!r}; use MM/DD/YYYY, e.g. 10/16/2026")


# Build the Keeneland program URL for a date
def program_url(day):
    """Keeneland program: no zero padding (10-9-26)."""
    return PROGRAM_URL.format(m=day.month, d=day.day, yy=f"{day.year % 100:02d}")


# Build the Equibase result-chart URL for a date
def chart_url(day):
    """Equibase chart: zero-padded (KEE100926USA)."""
    return CHART_URL.format(m=day.month, d=day.day, yy=f"{day.year % 100:02d}")


# Download one PDF and save it, refusing anything that is not really a PDF
def download_pdf(url, dest):
    """Download url to dest if it really is a PDF. Returns (ok, message)."""

    # Network errors (no connection, timeout) become a message instead of a crash
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException as exc:
        return False, f"request failed: {exc}"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code} (not published, or no racing that day?)"

    # Some sites answer a missing file with an HTML page and status 200; a PDF starts
    # with "%PDF".
    if not resp.content.startswith(b"%PDF"):
        kind = resp.headers.get("Content-Type", "unknown type")
        return False, f"response is not a PDF ({kind}, {len(resp.content)} bytes)"

    dest.write_bytes(resp.content)
    return True, f"saved {dest} ({len(resp.content) / 1e6:.1f} MB)"


# Save data as pretty-printed JSON (UTF-8, so names like "Señor" stay readable)
def write_json(path, data):
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


# The whole job for one race date: download, parse, check, save.
# Returns True when the program was parsed and no check FAILed.
def fetch(day, out_root, get_chart=True):
    folder = Path(out_root) / day.isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    print(f"Race date {day:%A %B %d, %Y} -> {folder}")

    # Result chart (optional, and only published after the races are run)
    ok_chart = None
    if get_chart:
        url = chart_url(day)
        ok_chart, msg = download_pdf(url, folder / "chart.pdf")
        print(f"  chart:   {url}\n           {msg}")
        if not ok_chart and day >= dt.date.today():
            print("           (charts are only published after the races are run)")

    # Program (required: without it there is nothing to parse)
    url = program_url(day)
    ok_prog, msg = download_pdf(url, folder / "program.pdf")
    print(f"  program: {url}\n           {msg}")
    if not ok_prog:
        print(
            "           (Keeneland only races in April and October; "
            "programs appear a few days ahead)"
        )
        return False

    # 1. Build: parse the program into a card (a plain dict).
    #    A crash is reported and stops here; there is no card to save.
    try:
        card = pdf_reading.parse_pdf(folder / "program.pdf")
    except Exception as exc:
        print(f"  parse:   FAILED -- parse_pdf crashed: {exc!r}")
        return False

    # 2. Inspect: check the card against the PDF before anyone relies on it.
    rep = check_parser.check_card(card, folder / "program.pdf")

    # 3. Save only the essentials: the whole day, plus one file per race that can be
    #    handed straight to src/model/pp_json_to_handicap.py.
    slim = pdf_reading.slim_card(card)
    write_json(folder / "card.json", slim)
    for race in slim["races"]:
        write_json(folder / f"race{race['race_number']}.json", race)

    # 4. Save the check findings and print the report
    findings = [
        dict(zip(("level", "check", "where", "message"), f)) for f in rep.findings
    ]
    write_json(folder / "check.json", findings)
    print(f"  parse:   {folder / 'card.json'} + {len(slim['races'])} race files")
    check_parser.print_report(rep, card, verbose_info=False)
    return rep.count("FAIL") == 0


# Command line: read the arguments, ask for the date if none was given, run fetch()
def main(argv=None):

    # Arguments
    ap = argparse.ArgumentParser(
        description="Download a Keeneland race day's program and chart by date."
    )
    ap.add_argument(
        "date", nargs="?", help="race date, e.g. 10/16/2026 (asked for if omitted)"
    )
    ap.add_argument(
        "--out", default=str(DEFAULT_OUT), help=f"output folder (default {DEFAULT_OUT})"
    )
    ap.add_argument(
        "--no-chart", action="store_true", help="skip the Equibase result chart"
    )
    args = ap.parse_args(argv)

    # Date, then the actual work; the exit code is 0 on success, 1 otherwise
    text = args.date or input("Race date (MM/DD/YYYY): ")
    try:
        day = parse_date(text)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0 if fetch(day, args.out, get_chart=not args.no_chart) else 1


if __name__ == "__main__":
    sys.exit(main())
