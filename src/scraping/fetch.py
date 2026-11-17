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
  2. Downloads the Equibase result chart for the same card (the page the program's QR
  codes
     link to). It only exists after the races have been run:
       https://tvg.equibase.com/static/chart/pdf/KEEMMDDYYUSA.pdf
     Here month and day ARE zero-padded: October 9, 2026 -> KEE100926USA.
  3. Parses the program with pdf_reading.parse_pdf, writes the card JSON, and runs the
     check_parser.py checks on it, so a bad parse is reported right away.

Files go to <out>/<YYYY-MM-DD>/ (default out: src/scraping/downloads, which the repo's
.gitignore already ignores):
    program.pdf, chart.pdf, card.json, check.json

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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_parser  # noqa: E402  (needs the path above)

PROGRAM_URL = (
    "https://www.keeneland.com/files/programs/{m}-{d}-{yy}-kee-online-programpdf.pdf"
)
CHART_URL = "https://tvg.equibase.com/static/chart/pdf/KEE{m:02d}{d:02d}{yy}USA.pdf"
DEFAULT_OUT = Path(__file__).resolve().parent / "downloads"
# A plain, generic client name; nothing identifying about the user is sent.
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; race-program-fetcher)"}
TIMEOUT = 60  # seconds


def parse_date(text):
    """'10/16/2026', '10/16/26', '10-16-26', '2026-10-16' -> datetime.date."""
    t = text.strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)  # 2026-10-16
    if m:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.fullmatch(
        r"(\d{1,2})[/-](\d{1,2})[/-](\d{2}|\d{4})", t
    )  # 10/16/2026, 10-16-26
    if m:
        year = int(m.group(3))
        year += 2000 if year < 100 else 0
        return dt.date(year, int(m.group(1)), int(m.group(2)))
    raise ValueError(f"unrecognised date {text!r}; use MM/DD/YYYY, e.g. 10/16/2026")


def program_url(day):
    """Keeneland program: no zero padding (10-9-26)."""
    return PROGRAM_URL.format(m=day.month, d=day.day, yy=f"{day.year % 100:02d}")


def chart_url(day):
    """Equibase chart: zero-padded (KEE100926USA)."""
    return CHART_URL.format(m=day.month, d=day.day, yy=f"{day.year % 100:02d}")


def download_pdf(url, dest):
    """Download url to dest if it really is a PDF. Returns (ok, message)."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException as exc:
        return False, f"request failed: {exc}"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code} (not published, or no racing that day?)"
    # Some sites answer a missing file with an HTML page and status 200; a PDF starts with
    # "%PDF".
    if not resp.content.startswith(b"%PDF"):
        kind = resp.headers.get("Content-Type", "unknown type")
        return False, f"response is not a PDF ({kind}, {len(resp.content)} bytes)"
    dest.write_bytes(resp.content)
    return True, f"saved {dest} ({len(resp.content) / 1e6:.1f} MB)"


def fetch(day, out_root, get_chart=True):
    folder = Path(out_root) / day.isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    print(f"Race date {day:%A %B %d, %Y} -> {folder}")

    ok_chart = None
    if get_chart:
        url = chart_url(day)
        ok_chart, msg = download_pdf(url, folder / "chart.pdf")
        print(f"  chart:   {url}\n           {msg}")
        if not ok_chart and day >= dt.date.today():
            print("           (charts are only published after the races are run)")

    url = program_url(day)
    ok_prog, msg = download_pdf(url, folder / "program.pdf")
    print(f"  program: {url}\n           {msg}")
    if not ok_prog:
        print(
            "           (Keeneland only races in April and October; "
            "programs appear a few days ahead)"
        )
        return False

    # Parse the program and check the result before anyone relies on it.
    rep, card = check_parser.check_pdf(folder / "program.pdf")
    if card is None:
        print("  parse:   FAILED -- see the check output below")
        check_parser.print_report(rep, card, verbose_info=False)
        return False
    (folder / "card.json").write_text(
        json.dumps(card, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (folder / "check.json").write_text(
        json.dumps(
            [dict(zip(("level", "check", "where", "message"), f)) for f in rep.findings],
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"  parse:   {folder / 'card.json'}")
    check_parser.print_report(rep, card, verbose_info=False)
    return rep.count("FAIL") == 0


def main(argv=None):
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

    text = args.date or input("Race date (MM/DD/YYYY): ")
    try:
        day = parse_date(text)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0 if fetch(day, args.out, get_chart=not args.no_chart) else 1


if __name__ == "__main__":
    sys.exit(main())
