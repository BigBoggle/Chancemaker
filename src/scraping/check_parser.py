#!/usr/bin/env python3
"""
Check pdf_reading.py's output for a past-performance PDF and report anything suspicious.

Usage:
    python src/scraping/check_parser.py tests/pdf_tests/10-2-26-kee-online-programpdf.pdf
    python src/scraping/check_parser.py tests/pdf_tests/*.pdf
    python src/scraping/check_parser.py tests/pdf_tests/*.pdf --compare-golden GOLDEN
    python src/scraping/check_parser.py tests/pdf_tests/*.pdf --save-golden GOLDEN
    (GOLDEN = the folder of known-good outputs, src/scraping/golden)
    python src/scraping/check_parser.py some.pdf --json report.json

Exit code: 0 when no check FAILs, 1 otherwise.

Why this exists
---------------
Parsing a PDF by text position fails in two ways: loudly (a crash) or silently (a horse,
a line or a value quietly missing or wrong). Most of the bugs found while building the
parser were silent. These checks compare the parser's output with things that can be
counted on the page independently of how the parser assigns text to fields, and with
consistency rules the data must satisfy.

Levels
------
FAIL  the output is known to be incomplete or inconsistent (e.g. a printed PP line was not
      parsed, a horse is missing, the same earlier race disagrees between two horses).
WARN  needs a human look: a value the model uses is empty, a value is implausible, or a
      symbol was not recognised. Often a genuine blank in the PDF ("---" speed figure).
INFO  worth knowing, not a problem by itself (e.g. the program printed fewer PP lines than
      the horse's starts; a track code without a full name).

What the checks can NOT see: a value that is wrong but plausible (a misread figure that
still looks like a figure). Those need spot checks against the PDF; --compare-golden at
least guarantees that values verified once do not change unnoticed.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pdf_reading as pr  # noqa: E402  (needs the path above)

REPO = Path(__file__).resolve().parents[2]
CONVERTER = REPO / "src" / "model" / "pp_json_to_handicap.py"

# Fields the handicap converter reads (see ALIASES / HISTORY_ALIASES in
# pp_json_to_handicap.py).
MODEL_HORSE_FIELDS = ("name", "program_number", "morning_line_odds", "trainer")
MODEL_LINE_FIELDS = (
    "finish_position",
    "field_size",
    "distance_f",
    "final_time",
    "race_class",
    "odds",
    "speed_figure",
    "early_pace",
    "first_call_position",
)
# Fields that must agree between horses whose histories contain the same earlier race.
SHARED_RACE_FIELDS = (
    "distance_f",
    "surface",
    "course",
    "condition",
    "final_time",
    "fractions",
    "field_size",
)
# Glyph characters that should never survive into decoded text.
LEFTOVER_GLYPHS = re.compile(
    "["
    + re.escape(pr.LENGTH_CHARS + "".join(pr.GLYPH_DIGITS) + pr.DEAD_HEAT_MARK + "®")
    + "]"
)
WORKOUT_DATE = re.compile(
    r"\b\d{1,2} [A-Z][a-z]{2} \d{2}\b"
)  # "4 Oct 26" (PP lines use "04Oct26")
AGE_SEX_OK = set("0123456789gkl½j")  # decoded in parse_pp_line


class Report:
    def __init__(self, pdf):
        self.pdf = str(pdf)
        self.findings = []  # (level, check, where, message)

    def add(self, level, check, where, message):
        self.findings.append((level, check, where, message))

    def count(self, level):
        return sum(1 for f in self.findings if f[0] == level)


def where(race=None, horse=None, line=None, page=None):
    """Where to look in the PDF, e.g.
    'p32 R5 #6 Never Gonna Stop (IRE) line 03Sep26 Sar'."""
    parts = []
    page = page or (line or {}).get("page") or (horse or {}).get("page")
    if page:
        parts.append(f"p{page}")
    if race is not None:
        parts.append(f"R{race['race_number']}")
    if horse is not None:
        parts.append(f"#{horse.get('program_number')} {horse.get('name')}")
    if line is not None:
        parts.append(
            f"line {line.get('date_text')} "
            f"{line.get('race_number') or ''}{line.get('track_code') or ''}"
        )
    return " ".join(parts)


# --------------------------------------------------------------------------
# Counts taken straight from the page (independent of horse/field assignment)
# --------------------------------------------------------------------------
def page_counts(doc, layouts):
    """Per page: printed PP lines, printed workouts and also-eligible markers."""
    out = {}
    for lay in layouts:
        spans = pr.page_spans(doc[lay.index], lay)
        # A PP line starts with a date like "16Aug26" at the left margin; count
        # distinct baselines.
        pp_lines = {round(s.base) for s in spans if s.x0 < 45 and pr.DATE_RE.match(s.t)}
        workouts = sum(len(WORKOUT_DATE.findall(s.text)) for s in spans)
        markers = sum(
            1 for s in spans if s.bold and s.x0 < 30 and 10 <= s.size <= 14 and s.t == "A"
        )
        out[lay.index + 1] = {
            "pp_lines": len(pp_lines),
            "workouts": workouts,
            "ae_markers": markers,
        }
    return out


def check_pages(rep, doc, card, layouts):
    if not layouts:
        rep.add("FAIL", "pages", "", "no past-performance pages found")
        return
    found = {lay.index for lay in layouts}
    for i, page in enumerate(doc):
        if i in found:
            continue
        spans = [
            s
            for b in page.get_text("dict")["blocks"]
            for ln in b.get("lines", [])
            for s in ln["spans"]
            if s["text"].strip()
        ]
        dates = sum(
            1
            for s in spans
            if pr.DATE_RE.match(s["text"].strip()) and "NorthernDancer" in s["font"]
        )
        # The "How to read the program" guide page shows example PP lines on purpose.
        if "HOW TO READ" in page.get_text().upper():
            continue
        if dates >= 6:
            rep.add(
                "WARN",
                "pages",
                f"p{i + 1}",
                f"{dates} PP-style dated lines but not treated as a PP page "
                "(no 'M/D/YYYY Race N' footer?)",
            )


def check_races(rep, card):
    nums = [r["race_number"] for r in card["races"]]
    for n in sorted({n for n in nums if nums.count(n) > 1}, key=str):
        rep.add("FAIL", "races", f"R{n}", "race number appears more than once")
    ints = sorted(n for n in nums if isinstance(n, int))
    if ints and ints != list(range(ints[0], ints[-1] + 1)):
        rep.add("WARN", "races", "", f"race numbers are not consecutive: {nums}")
    for r in card["races"]:
        if not r["horses"]:
            rep.add("FAIL", "races", where(r), "race has no horses")
        if r.get("distance_f") is None:
            rep.add(
                "WARN",
                "races",
                where(r),
                f"race distance not decoded: {r.get('distance')!r}",
            )


def check_horses(rep, card):
    for r in card["races"]:
        progs = [h.get("program_number") for h in r["horses"]]
        ints = sorted(p for p in progs if isinstance(p, int))
        missing = sorted(set(range(1, (ints[-1] if ints else 0) + 1)) - set(ints))
        if missing:
            rep.add(
                "FAIL",
                "horses",
                where(r),
                f"program numbers missing (horse not parsed?): {missing}",
            )
        for h in r["horses"]:
            for k in MODEL_HORSE_FIELDS:
                if h.get(k) in (None, ""):
                    level = "FAIL" if k in ("name", "program_number") else "WARN"
                    rep.add(level, "horses", where(r, h), f"{k} is empty")
            if h["past_performances"] and h.get("class_rating") is None:
                printed = h.get("class_rating_text")
                rep.add(
                    "INFO" if printed == "N/A" else "WARN",
                    "horses",
                    where(r, h),
                    f"no class rating (printed: {printed!r})",
                )


def check_line_counts(rep, card, counts):
    """Printed vs parsed PP lines, workouts and also-eligible markers, per race."""
    for r in card["races"]:
        printed = {
            k: sum(counts[p][k] for p in r["pages"] if p in counts)
            for k in ("pp_lines", "workouts", "ae_markers")
        }
        parsed_lines = sum(len(h["past_performances"]) for h in r["horses"])
        parsed_wo = sum(len(h["workouts"]) for h in r["horses"])
        parsed_ae = sum(1 for h in r["horses"] if h.get("also_eligible"))
        if parsed_lines != printed["pp_lines"]:
            rep.add(
                "FAIL",
                "pp lines",
                where(r),
                f"{printed['pp_lines']} PP lines printed on pages {r['pages']}, "
                f"{parsed_lines} parsed",
            )
        if parsed_wo != printed["workouts"]:
            rep.add(
                "FAIL",
                "workouts",
                where(r),
                f"{printed['workouts']} workouts printed, {parsed_wo} parsed",
            )
        if parsed_ae != printed["ae_markers"]:
            rep.add(
                "FAIL",
                "also eligible",
                where(r),
                f"{printed['ae_markers']} A/E markers printed, "
                f"{parsed_ae} horses flagged",
            )
        for h in r["horses"]:
            starts = ((h.get("records") or {}).get("life") or {}).get("starts")
            n = len(h["past_performances"])
            if starts is not None and n > starts:
                rep.add(
                    "FAIL",
                    "pp lines",
                    where(r, h),
                    f"{n} PP lines but only {starts} lifetime starts",
                )
            elif starts is not None and n < min(starts, 10):
                rep.add(
                    "INFO",
                    "pp lines",
                    where(r, h),
                    f"{n} PP lines printed for {starts} starts "
                    "(program shortened the history?)",
                )


def check_shared_races(rep, card):
    """The same earlier race in several horses' histories must agree."""
    seen = defaultdict(list)
    for r in card["races"]:
        for h in r["horses"]:
            for p in h["past_performances"]:
                if p.get("race_number") is not None:
                    seen[(p["date"], p["track_code"], p["race_number"])].append((r, h, p))
    for (date, track, num), group in seen.items():
        if len(group) < 2:
            continue
        for k in SHARED_RACE_FIELDS:
            values = {json.dumps(p.get(k)) for _, _, p in group}
            if len(values) > 1:
                detail = "; ".join(
                    f"{h['name']}={p.get(k)!r} (p{p.get('page')})" for _, h, p in group
                )
                rep.add(
                    "FAIL",
                    "shared races",
                    f"{date} {track} race {num}",
                    f"{k} disagrees: {detail}",
                )
        finishes = [p["finish_position"] for _, _, p in group if p.get("finish_position")]
        if len(finishes) != len(set(finishes)):
            detail = "; ".join(
                f"{h['name']}={p.get('finish_position')}" for _, h, p in group
            )
            rep.add(
                "WARN",
                "shared races",
                f"{date} {track} race {num}",
                f"same finish position twice (dead heat?): {detail}",
            )


def check_values(rep, card):
    """Per PP line: empty model fields, decoding problems and implausible values."""
    for r in card["races"]:
        for h in r["horses"]:
            for p in h["past_performances"]:
                at = where(r, h, p)
                _check_empty_fields(rep, p, at)
                _check_decoding(rep, p, at)
                _check_plausibility(rep, p, at)
                _check_ranges(rep, p, at)
                if p.get("track_code") and p["track_code"] not in pr.TRACK_NAMES:
                    rep.add(
                        "INFO",
                        "track names",
                        p["track_code"],
                        "track code has no full name in TRACK_NAMES",
                    )


# Lines from races abroad print no US figures and only the post and the finish.
FOREIGN_BLANK = ("speed_figure", "early_pace", "odds", "first_call_position")


def _check_empty_fields(rep, p, at):
    for k in MODEL_LINE_FIELDS:
        if p.get(k) is None and not (p.get("foreign") and k in FOREIGN_BLANK):
            rep.add("WARN", "empty model field", at, f"{k} is empty")


def _check_decoding(rep, p, at):
    """Distance, course symbols, age/sex glyphs, leftover glyphs, stewards' marks."""
    if p.get("distance") and p.get("distance_f") is None:
        rep.add("FAIL", "decoding", at, f"distance {p['distance']!r} not decoded")
    for sym in p.get("unrecognized_symbols") or []:
        rep.add("WARN", "decoding", at, f"unrecognised course symbol {sym!r}")
    odd = set(p.get("age_sex_code") or "") - AGE_SEX_OK
    if odd:
        rep.add(
            "WARN",
            "decoding",
            at,
            f"unknown age/sex glyph(s) {sorted(odd)} in {p['age_sex_code']!r}",
        )
    texts = [p.get("comment"), p.get("jockey"), p.get("race_class"), p.get("equipment")]
    texts += [f.get("name") for f in p.get("top_finishers") or []]
    for t in texts:
        if t and LEFTOVER_GLYPHS.search(t):
            rep.add("FAIL", "decoding", at, f"undecoded glyph left in text: {t!r}")
    if p.get("stewards_change") == "?":
        rep.add(
            "WARN",
            "decoding",
            at,
            "'*' (stewards' change) printed but no DQ/PL in the comment",
        )


def _check_plausibility(rep, p, at):
    """Time per furlong, and finish position vs field size."""
    sec, dist = p.get("final_time_seconds"), p.get("distance_f")
    if sec and dist and not 10.5 <= sec / dist <= 14.5:
        rep.add(
            "WARN",
            "plausibility",
            at,
            f"{p['final_time']} for {dist}f = {sec / dist:.1f} s/furlong",
        )
    fs, fin = p.get("field_size"), p.get("finish_position")
    if fs and fin and fin > fs:
        rep.add("WARN", "plausibility", at, f"finish {fin} > field size {fs}")


# (field, lowest plausible, highest plausible, how to show the printed value)
RANGES = (
    ("field_size", 2, 20, lambda p: f"field size {p['field_size']}"),
    ("weight", 100, 135, lambda p: f"weight {p['weight']}"),
    ("odds", 0.05, 999, lambda p: f"odds {p['odds_text']}"),
    ("speed_figure", -20, 140, lambda p: f"speed figure {p['speed_figure']}"),
)


def _check_ranges(rep, p, at):
    for key, low, high, show in RANGES:
        if p.get(key) is not None and not low <= p[key] <= high:
            rep.add("WARN", "plausibility", at, show(p))


def check_converter(rep, card):
    """Every race must convert with the handicap converter
    (teammates' code, imported read-only)."""
    if not CONVERTER.exists():
        rep.add("INFO", "converter", "", f"{CONVERTER} not found; skipped")
        return
    spec = importlib.util.spec_from_file_location("pp_json_to_handicap", CONVERTER)
    conv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conv)
    for r in card["races"]:
        try:
            conv.convert_race(r)
        except (
            Exception
        ) as exc:  # report any converter failure instead of stopping the check
            rep.add("FAIL", "converter", where(r), f"convert_race failed: {exc}")


# --------------------------------------------------------------------------
# Known-good ("golden") outputs
# --------------------------------------------------------------------------
def comparable(card):
    """Drop fields that legitimately differ between runs (the file path)."""
    return {k: v for k, v in card.items() if k != "source_pdf"}


def diff(a, b, path, out, limit):
    if len(out) >= limit:
        return
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            diff(a.get(k, "<missing>"), b.get(k, "<missing>"), f"{path}.{k}", out, limit)
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b)):
            diff(x, y, f"{path}[{i}]", out, limit)
    elif a != b:
        out.append(
            f"{path}: {json.dumps(a, ensure_ascii=False)[:80]} -> "
            f"{json.dumps(b, ensure_ascii=False)[:80]}"
        )


def compare_golden(rep, card, golden_dir):
    path = Path(golden_dir) / (Path(rep.pdf).stem + ".json")
    if not path.exists():
        rep.add(
            "INFO", "golden", path.name, "no known-good output saved for this PDF yet"
        )
        return
    old = json.loads(path.read_text(encoding="utf-8"))
    changes = []
    diff(comparable(old), comparable(card), "card", changes, limit=40)
    if changes:
        rep.add(
            "WARN",
            "golden",
            path.name,
            f"output differs from the saved known-good version "
            f"({len(changes)}{'+' if len(changes) >= 40 else ''} differences)",
        )
        for c in changes:
            rep.add("WARN", "golden", path.name, c)


# --------------------------------------------------------------------------
def check_pdf(pdf, golden_dir=None):
    rep = Report(pdf)
    try:
        card = pr.parse_pdf(pdf)
    except Exception as exc:  # a crash is itself a finding
        rep.add("FAIL", "parser", "", f"parse_pdf crashed: {exc!r}")
        return rep, None
    doc = pr.pymupdf.open(str(pdf))
    layouts = pr.find_pp_pages(doc)
    check_pages(rep, doc, card, layouts)
    check_races(rep, card)
    check_horses(rep, card)
    check_line_counts(rep, card, page_counts(doc, layouts))
    check_shared_races(rep, card)
    check_values(rep, card)
    check_converter(rep, card)
    if golden_dir:
        compare_golden(rep, card, golden_dir)
    return rep, card


def print_report(rep, card, verbose_info):
    print(f"\n=== {rep.pdf}")
    if card:
        n_h = sum(len(r["horses"]) for r in card["races"])
        n_l = sum(len(h["past_performances"]) for r in card["races"] for h in r["horses"])
        print(
            f"    {card.get('date')} {card.get('track')}: {len(card['races'])} races, "
            f"{n_h} horses, {n_l} PP lines, PP pages {card.get('pp_pages')}, "
            f"column shift {card.get('column_shift') or 'none'}"
        )
    print(
        f"    FAIL {rep.count('FAIL')}   WARN {rep.count('WARN')}   "
        f"INFO {rep.count('INFO')}"
    )
    by_check = defaultdict(list)
    for f in rep.findings:
        by_check[(f[0], f[1])].append(f)
    for level in ("FAIL", "WARN", "INFO"):
        for (lvl, check), items in sorted(by_check.items()):
            if lvl != level:
                continue
            if level == "INFO" and not verbose_info:
                print(f"    INFO {check}: {len(items)} (use --info to list)")
                continue
            print(f"    {level} {check} ({len(items)}):")
            seen = set()
            for _, _, at, msg in items:
                if (at, msg) in seen:  # e.g. the same unknown track code on many lines
                    continue
                seen.add((at, msg))
                print(f"        {at}: {msg}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Check pdf_reading.py output against the PDF itself."
    )
    ap.add_argument("pdfs", nargs="+", help="past-performance PDF(s)")
    ap.add_argument(
        "--compare-golden",
        metavar="DIR",
        help="compare with known-good outputs saved in DIR",
    )
    ap.add_argument(
        "--save-golden",
        metavar="DIR",
        help="save this run's outputs as the known-good versions in DIR",
    )
    ap.add_argument(
        "--json", metavar="FILE", help="also write all findings to FILE as JSON"
    )
    ap.add_argument(
        "--info",
        action="store_true",
        help="list INFO findings instead of only counting them",
    )
    args = ap.parse_args(argv)

    reports, any_fail = [], False
    for pdf in args.pdfs:
        rep, card = check_pdf(pdf, args.compare_golden)
        print_report(rep, card, args.info)
        reports.append(rep)
        any_fail = any_fail or rep.count("FAIL") > 0
        if args.save_golden and card is not None:
            out = Path(args.save_golden)
            out.mkdir(parents=True, exist_ok=True)
            (out / (Path(pdf).stem + ".json")).write_text(
                json.dumps(card, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            print(f"    saved known-good output to {out / (Path(pdf).stem + '.json')}")
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                [
                    {
                        "pdf": r.pdf,
                        "findings": [
                            dict(zip(("level", "check", "where", "message"), f))
                            for f in r.findings
                        ],
                    }
                    for r in reports
                ],
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
    print(f"\n{'FAILED' if any_fail else 'OK'}: {len(reports)} PDF(s) checked")
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
