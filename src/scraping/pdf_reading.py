#!/usr/bin/env python3
"""
Extract structured JSON from an Equibase "official past performances" PDF.
Works on both the standalone Keeneland PP sheet (10-16-26-kee-official-ppspdf.pdf) and the
full race-day program (e.g. tests/pdf_tests/10-16-26-kee-online-programpdf.pdf), where the
PP pages are found among the other pages and rescaled first (see find_pp_pages).

Usage:
    python src/scraping/pdf_reading.py path/to/pps.pdf                 # JSON to stdout
    python src/scraping/pdf_reading.py path/to/pps.pdf -o card.json    # whole card
    python src/scraping/pdf_reading.py path/to/pps.pdf --race 3 -o race3.json
    python src/scraping/pdf_reading.py path/to/pps.pdf --split-dir out/  # a file per race

The whole-card output is {"track": ..., "date": ..., "races": [race, ...]}.
Each race object (and the --race / --split-dir files) is shaped so that it can be
fed straight into src/model/pp_json_to_handicap.py:

    {
      "name": "Keeneland Race 1", "distance_f": 6.0, "surface": "dirt",
      "horses": [
        {"name": ..., "program_number": 1, "morning_line_odds": "10-1",
         "trainer": ..., "jockey": ..., "class_rating": 61.35,
         "speed_figures": [65, 70, ...], "pace": {"early_pace": 61.1},
         "past_performances": [{"finish_position": 1, "field_size": 5, ...}, ...],
         ...}
      ]
    }

How the sheet is read
---------------------
The PDF is laid out in fixed columns, so the parser works from text spans with
their x/y coordinates (PyMuPDF) instead of plain text.  Running positions,
beaten lengths, fractional times and a few other items are printed with the
"NorthernDancerNarrow" symbol fonts; GLYPH_DIGITS / GLYPH_FRACTIONS below
translate those glyphs back into numbers (e.g. "9¶¾£" -> 9th, 18 3/4 lengths back).

Every past-performance line carries three figures after the race class, e.g.
"53  45-65".  Keeneland's "How to read a program" guide
(keeneland.com/tutorial, details/details.json) documents the pair in the order
"Equibase Pace Figure, then Equibase Speed Figure in bold":
  * the first number of the pair (45) is the Equibase Pace Figure, "the horse's
    speed figure at the first call of the race" -> exported as ``early_pace``;
  * the second, bold number of the pair (65) is the Equibase Speed Figure ->
    exported as ``speed_figure`` (also verified: it moves with beaten lengths
    for horses that met in the same race);
  * the lone bold number in front (53) is a newer column the guide does not
    cover.  It is per-horse, absent on debut runs and on lines older than about
    a year, and is NOT lengths-consistent.  It is exported unchanged as
    ``bold_figure``; the handicap converter does not read it.
The italic number printed right after the horse's name is, per the legend printed
at the end of each race ("Equibase Rating - The italicized number by horse name."),
exported as ``equibase_rating``.

Only PyMuPDF (``pip install pymupdf``) is required.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

try:
    import pymupdf  # PyMuPDF >= 1.24
except ImportError:  # pragma: no cover - older installs expose the module as fitz
    import fitz as pymupdf  # type: ignore


# --------------------------------------------------------------------------
# Glyph tables for the NorthernDancerNarrow symbol fonts
# --------------------------------------------------------------------------
GLYPH_DIGITS = {
    "^": "0",
    "¶": "1",
    "¸": "2",
    "¹": "3",
    "º": "4",
    "»": "5",
    "¼": "6",
    "½": "7",
    "¾": "8",
    "¿": "9",
}
GLYPH_FRACTIONS = {"¡": 0.5, "¢": 0.25, "£": 0.75}
GLYPH_MARGINS = {"«¬": ("hd", 0.1), "¨μ": ("nk", 0.25), "¨©": ("no", 0.05)}
LENGTH_CHARS = "".join(GLYPH_DIGITS) + "".join(GLYPH_FRACTIONS) + "«¬¨μ©"

# Distance tokens in the past-performance lines.  The second symbol font prints the
# fraction part as a letter: "6K" = 6 1/2 furlongs, "1B" = 1 1/16 miles.  The letters were
# decoded from the data (2026-11-16): the median final time of every token across four
# Keeneland programs, divided by ~11.9 s per furlong, gives an ordered ladder
# B 8.5f < N 9f < C 9.5f < L 10f < D 10.5f < O 11f < K 12f < M 14f, i.e. sixteenths of a
# mile.
# ("*1M" is also printed for the San Juan Capistrano, a race at about 1 3/4 miles.)
DIST_FRACTION_GLYPHS = {
    "B": 1 / 16,
    "N": 2 / 16,
    "C": 3 / 16,
    "L": 4 / 16,
    "D": 5 / 16,
    "O": 6 / 16,
    "K": 8 / 16,
    "M": 12 / 16,
}
# Miles plus yards print the yards as superscript digits, which come out as the letters
# À..É (U+00C0 + digit): "1ÇÀ" = 1 mile 70 yards, "1ÄÀ" = 1 mile 40 yards (Tampa Bay
# Downs;
# its 1:40.26 final time sits between the mile and 1 mile 70 yard medians).
SUPERSCRIPT_DIGITS = {chr(0xC0 + i): str(i) for i in range(10)}

# Track codes used in the past-performance lines (extend freely).
TRACK_NAMES = {
    "Kee": "Keeneland",
    "CD": "Churchill Downs",
    "Elp": "Ellis Park",
    "TP": "Turfway Park",
    "KD": "Kentucky Downs",
    "Ind": "Horseshoe Indianapolis",
    "GP": "Gulfstream Park",
    "Sar": "Saratoga",
    "Bel": "Belmont Park",
    "Aqu": "Aqueduct",
    "Baq": "Belmont at Aqueduct",
    "OP": "Oaklawn Park",
    "FG": "Fair Grounds",
    "Prm": "Prairie Meadows",
    "Btp": "Belterra Park",
    "Cnl": "Colonial Downs",
    "Mth": "Monmouth Park",
    "WO": "Woodbine",
    "Lrl": "Laurel Park",
    "Pid": "Presque Isle Downs",
    "Emd": "Emerald Downs",
    "Cby": "Canterbury Park",
    "Del": "Delaware Park",
    "Pim": "Pimlico",
    "Tam": "Tampa Bay Downs",
    "SA": "Santa Anita",
    "Dmr": "Del Mar",
    "GG": "Golden Gate Fields",
    "Haw": "Hawthorne",
    "AP": "Arlington Park",
    "Mnr": "Mountaineer",
    "PRX": "Parx Racing",
    "Pen": "Penn National",
    "Lrc": "Los Alamitos",
    "Hou": "Sam Houston",
    "LS": "Lone Star Park",
    "RP": "Remington Park",
    "Fai": "Fair Hill",
    "Ctt": "Chattahoochee Hills",
    "Cdt": "Churchill Downs Trackside",
    "Ttc": "Thoroughbred Training Center",
    "Pmm": "Palm Meadows",
    "Mid": "Middleburg",
    "Cmr": "Camarero",
}

# Medication / equipment letters (Keeneland guide): L Lasix, B Bute, b blinkers, f front
# bandages
EQUIPMENT_CODES = {"L": "lasix", "B": "bute", "b": "blinkers", "f": "front_bandages"}
# Course symbols printed after the track condition, keyed by (symbol font, character).
# Meanings from the program's own "COURSE SYMBOLS" legend ("How to read the program"
# page),
# matched by rendering each glyph from the PP lines and comparing it with the legend:
#   p = circled T (main turf), h = boxed T (inner turf), y = diamond + (downhill turf),
#   u = circled X (off turf: moved from the grass to the main track, so run on dirt),
#   o in the third font = diamond A (all-weather / synthetic track).
# "o" in the second font prints as a box with a dot. It is not in the legend; it only
# appears
# at Colonial, Aqueduct and Belmont-at-Aqueduct and only with turf conditions (fm/gd), and
# all
# three tracks have an inner turf course, so it is read as inner turf (unconfirmed).
COURSE_SYMBOLS = {
    ("2", "p"): ("turf", "main turf"),
    ("2", "h"): ("turf", "inner turf"),
    ("2", "o"): ("turf", "inner turf (box-dot symbol)"),
    ("2", "y"): ("turf", "downhill turf"),
    ("2", "u"): ("dirt", "off turf"),
    ("3", "o"): ("synthetic", "all weather"),
}


def course_symbol_key(s):
    """(symbol font, character) for a span, e.g. ("2", "p"); the font's last character
    tells the NorthernDancer symbol fonts apart (...Narrow2, ...Dancer3)."""
    return (s.font[-1:], s.t) if s.symbol else (None, s.t)


# Medication printed after the horse's name, e.g. "(L)" (first-time Lasix may add a
# digit).
MEDICATION_RE = re.compile(r"^\([LB]+\d?\)$")

WORD_NUMBERS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "seventy": 70,
    "forty": 40,
    "half": 0.5,
    "quarter": 0.25,
    "sixteenth": 1 / 16,
    "eighth": 1 / 8,
}
MONTHS = {
    m: i + 1
    for i, m in enumerate(
        [
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        ]
    )
}

# x-coordinate windows (points) of the past-performance line columns.
COL = {
    "date": (0, 45),
    "race_track": (45, 66),
    "condition": (66, 92),
    "distance": (92, 108),
    "fractions": (108, 163),
    "age_sex": (163, 183),
    "class": (183, 243),
    "bold": (243, 254),
    "early": (254, 262),
    "speed": (262, 276),
    "post": (276, 284),
    "calls": (284, 349),
    "jockey": (349, 386),
    "weight": (386, 396),
    "equipment": (396, 411),
    "odds": (411, 437),
    "finishers": (437, 586),
    "field": (586, 612),
}

# The columns of a past-performance line in the layout COL was tuned on (the 2026
# Keeneland
# PPs), as (x0 where the column starts, is it printed bold, text shape). These are the
# stops shared by at least a quarter of all PP lines, measured with column_profile() on
# 10-16-26-kee-online-programpdf.pdf. "Shape" is the text with digit runs -> 9, letter
# runs
# -> a, running-line glyph runs -> g (e.g. "-58" -> "-9", "16Aug26" -> "9a9", ":22¼¸" ->
#  ":9g").
# Each new PDF measures its own profile and is aligned to this one (see
# learn_column_shift),
# so a layout that adds, drops or moves a column still lands in COL.
REFERENCE_COLUMNS = (
    (24, 0, "9a9"),
    (49, 0, "9 a"),
    (69, 0, "a"),
    (71, 0, "a"),
    (78, 0, "a"),
    (92, 0, "9a"),
    (95, 0, "a"),
    (109, 0, ":9g"),
    (125, 0, "9:9g"),
    (128, 0, ":9g"),
    (146, 0, "9:9g"),
    (165, 0, "9"),
    (168, 0, "a"),
    (183, 0, "a 9"),
    (245, 1, "9"),
    (257, 0, "9"),
    (262, 1, "-9"),
    (278, 0, "9"),
    (286, 0, "9"),
    (298, 0, "9g"),
    (311, 0, "9g"),
    (323, 0, "9g"),
    (336, 0, "9g"),
    (350, 0, "a a"),
    (387, 0, "9"),
    (400, 0, "a"),
    (411, 0, "9.9"),
    (414, 0, "9.9"),
    (440, 0, "a a"),
    (590, 0, "9"),
    (593, 0, "9"),
)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def _num(text):
    """int/float from a printed number ('$42,958', '61.35');
    None if not numeric ('N/A')."""
    if text is None:
        return None
    t = str(text).strip().replace(",", "").replace("$", "")
    if not t or t.upper() in {"N/A", "NA", "-", "--", "---"}:
        return None
    try:
        f = float(t)
    except ValueError:
        return None
    return int(f) if f.is_integer() and "." not in t else f


def decode_glyph_number(text):
    """'¼¼' -> '66', '^¾' -> '08'; unknown characters are dropped."""
    return "".join(GLYPH_DIGITS.get(c, "") for c in text)


def decode_time(token):
    """':22¼¼' -> '22.66', '1:11¾¿' -> '1:11.89', ':49' -> '49.00'."""
    m = re.match(r"^(\d*:\d{2})(.*)$", token.strip())
    if not m:
        return token.strip()
    base, hundredths = m.group(1), decode_glyph_number(m.group(2))
    base = base.lstrip(":")
    return f"{base}.{hundredths.ljust(2, '0')[:2]}"


def time_to_seconds(text):
    if not text:
        return None
    m = re.match(r"^(?:(\d+):)?(\d{1,2})\.(\d{2})$", text)
    if not m:
        return None
    minutes = int(m.group(1) or 0)
    return round(minutes * 60 + int(m.group(2)) + int(m.group(3)) / 100, 2)


def decode_margin(glyphs):
    """Lengths glyphs -> (text, float).
    '¶¾£' -> ('18 3/4', 18.75); '«¬' -> ('hd', 0.1)."""
    g = glyphs.strip()
    if not g:
        return "", 0.0
    if g in GLYPH_MARGINS:
        return GLYPH_MARGINS[g]
    whole = decode_glyph_number("".join(c for c in g if c in GLYPH_DIGITS))
    frac = sum(GLYPH_FRACTIONS[c] for c in g if c in GLYPH_FRACTIONS)
    value = (int(whole) if whole else 0) + frac
    frac_text = {0.5: "1/2", 0.25: "1/4", 0.75: "3/4"}.get(frac, "")
    text = f"{whole or ('0' if not frac_text else '')} {frac_text}".strip()
    return text, value


def split_calls(text):
    """'10º£9º' -> [('10', 'º£'), ('9', 'º')]."""
    return re.findall(r"(\d{1,2})([" + re.escape(LENGTH_CHARS) + r"]*)", text)


def words_to_furlongs(text):
    """'Six Furlongs' -> 6.0, 'Eight And One Half Furlongs' -> 8.5,
    'One Mile And One Sixteenth' -> 8.5, 'One Mile And Seventy Yards' -> 8.32."""
    if not text:
        return None
    t = text.lower()
    m = re.search(
        r"\b((?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
        r"(?:\s+and\s+(?:one|three)\s+(?:half|quarter|eighth|sixteenth))?)\s+furlongs?\b",
        t,
    )
    # Course chutes add a measured extra ("Seven Furlongs and 184 Feet", Keeneland's Beard
    # Course).
    extra = re.search(r"\band\s+(\d+)\s+(feet|yards)\b", t)
    extra_f = (
        int(extra.group(1)) / (660 if extra.group(2) == "feet" else 220) if extra else 0.0
    )
    if m:
        words = m.group(1).split()
        value = WORD_NUMBERS[words[0]]
        if len(words) == 4:
            value += WORD_NUMBERS[words[2]] * WORD_NUMBERS[words[3]]
        return round(float(value) + extra_f, 3)
    m = re.search(
        r"\b(one|two)\s+miles?\b(?:\s+and\s+(one|three|seventy|forty)\s+"
        r"(sixteenth|eighth|quarter|half|yards))?",
        t,
    )
    if m:
        value = WORD_NUMBERS[m.group(1)] * 8
        if m.group(2):
            if m.group(3) == "yards":
                value += WORD_NUMBERS[m.group(2)] / 220
            else:
                value += WORD_NUMBERS[m.group(2)] * WORD_NUMBERS[m.group(3)] * 8
        else:
            value += extra_f  # "One Mile and 70 Yards" written with digits
        return round(float(value), 3)
    m = re.search(r"(\d+(?:\.\d+)?)\s*furlongs?", t)
    if m:
        return float(m.group(1))
    return None


def line_distance_to_furlongs(token):
    """Past-performance distance token -> furlongs. '6f' -> 6, '1m' -> 8, '6K' -> 6.5,
    '1B' -> 8.5, '1N' -> 9, '1ÇÀ' -> 8.32 (1 mile 70 yards), '*7f' -> 7 (about)."""
    t = token.replace(" ", "")
    about = t.startswith("*")
    t = t.lstrip("*")
    ym = re.match("^(\\d+)([" + "".join(SUPERSCRIPT_DIGITS) + "]+)$", t)
    if ym:
        yards = int("".join(SUPERSCRIPT_DIGITS[c] for c in ym.group(2)))
        return round(int(ym.group(1)) * 8 + yards / 220, 3), about
    m = re.match(r"^(\d+)([fm]?)([A-Z]?)$", t)
    if not m:
        return None, about
    value = int(m.group(1))
    unit, frac = m.group(2), m.group(3)
    if unit == "m":
        value *= 8
    elif frac and frac in DIST_FRACTION_GLYPHS and value <= 2 and unit == "":
        # "1B" = one mile + 1/16 mile
        value = value * 8 + DIST_FRACTION_GLYPHS[frac] * 8
        return round(float(value), 3), about
    if frac in DIST_FRACTION_GLYPHS:
        value += DIST_FRACTION_GLYPHS[frac]
    return float(value), about


def parse_record(text):
    """'( 3-0-0-0 ) 0.00%' -> dict; also plain '5-3-2-0'."""
    m = re.search(r"(\d+)-(\d+)-(\d+)-(\d+)", text or "")
    if not m:
        return None
    out = {
        "starts": int(m.group(1)),
        "wins": int(m.group(2)),
        "places": int(m.group(3)),
        "shows": int(m.group(4)),
    }
    p = re.search(r"([\d.]+)%", text)
    if p:
        out["win_pct"] = float(p.group(1))
    return out


# --------------------------------------------------------------------------
# Span access
# --------------------------------------------------------------------------
# Characters that different PDFs extract differently for the SAME printed glyph. The neck
# margin "¨μ" comes out with GREEK SMALL MU (U+03BC) in some files and MICRO SIGN (U+00B5)
# in others (10-17-26); fold them to one spelling so the glyph tables only need one entry.
LOOKALIKE_CHARS = str.maketrans({"µ": "μ"})


class Span:
    __slots__ = (
        "text",
        "x0",
        "y0",
        "x1",
        "y1",
        "size",
        "font",
        "bold",
        "italic",
        "symbol",
        "chars",
        "base",
    )

    def __init__(self, s):
        self.text = s["text"].translate(LOOKALIKE_CHARS)
        # (character, x0, x1) of every character -- used to split a span that runs on
        # across two columns of a past-performance line (see split_at_columns)
        self.chars = [
            (c.translate(LOOKALIKE_CHARS), a, b) for c, a, b in s.get("chars_x", [])
        ]
        self.x0, self.y0, self.x1, self.y1 = s["bbox"]
        # Baseline: the line the characters sit on. Text of different sizes on one printed
        # line shares it, while its top edge (y0) moves with the font size (see
        # group_rows).
        self.base = s["origin"][1] if "origin" in s else self.y1
        self.size = round(s["size"], 1)
        self.font = s["font"]
        self.bold = "Bold" in self.font or ",Bo" in self.font or bool(s["flags"] & 16)
        self.italic = "Italic" in self.font or bool(s["flags"] & 2)
        self.symbol = "NorthernDancer" in self.font

    @property
    def t(self):
        return self.text.strip()

    def shifted(self, dx):
        """Copy of this span moved dx points to the right."""
        out = object.__new__(Span)
        for k in Span.__slots__:
            setattr(out, k, getattr(self, k))
        out.x0, out.x1 = self.x0 + dx, self.x1 + dx
        out.chars = [(c, a + dx, b + dx) for c, a, b in self.chars]
        return out

    def with_text(self, text):
        """Copy of this span with different text (same position and font)."""
        out = self.shifted(0)
        out.text = text
        return out

    def piece(self, chars):
        """Copy of this span holding only ``chars`` (a run of its characters)."""
        out = object.__new__(Span)
        for k in Span.__slots__:
            setattr(out, k, getattr(self, k))
        visible = [ch for ch in chars if ch[0].strip()]
        out.chars = chars
        out.text = "".join(c for c, _, _ in chars)
        out.x0, out.x1 = visible[0][1], visible[-1][2]
        return out

    def __repr__(self):  # pragma: no cover - debugging aid
        bold = "B" if self.bold else ""
        return f"<{self.y0:.0f},{self.x0:.0f} {self.size} {bold} {self.t!r}>"


def page_spans(page, layout=None):
    """All non-blank text spans on a page, sorted top-to-bottom, left-to-right.

    ``layout`` (a PageLayout from find_pp_pages) maps the page's coordinates back into
    the frame of the standalone PP sheet, so every x-window / font-size test below works
    unchanged on a full race-day program where the PP pages are printed smaller.
    """
    out = []
    # "rawdict" gives every character's box, so a span's left edge can be taken from its
    # first VISIBLE character. Spans often begin with a space (" Moc 62500", " e");
    # counting
    # the space put their left edge 1-2pt early, i.e. in the previous column.
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for raw in line["spans"]:
                chars = raw["chars"]
                s = {k: v for k, v in raw.items() if k != "chars"}
                s["text"] = "".join(c["c"] for c in chars)
                if not s["text"].strip():
                    continue
                first = next(c for c in chars if c["c"].strip())
                s["bbox"] = (first["bbox"][0],) + tuple(raw["bbox"][1:])
                s["chars_x"] = [(c["c"], c["bbox"][0], c["bbox"][2]) for c in chars]
                if layout is not None:
                    if any(f in s["font"] for f in PROGRAM_CHROME_FONTS):
                        continue  # the program's own running header / page number
                    s = layout.normalize(s)
                out.append(Span(s))
    out.sort(key=lambda s: (s.y0, s.x0))
    return out


# --------------------------------------------------------------------------
# Finding the past-performance pages (standalone PP sheet or full program)
# --------------------------------------------------------------------------
# Keeneland publishes the same Equibase PP pages two ways:
#   * the standalone "official PPs" PDF (612x792 pages, nothing else in it), and
#   * the full race-day program
#     (keeneland.com/files/programs/M-D-YY-kee-online-programpdf.pdf),
#     where the PP pages are mixed in with ads, wagering guides, simulcast pages, etc.,
#     and every PP element is printed at 92% size and shifted right/down a little.
# Measured on the 10-16-26 card (PP sheet vs program, ~400 matching spans per page):
# x' = 0.92 * x + dx exactly, where dx is 10.0-11.6 and differs from page to page
# (left/right page gutter). The program also re-flows the horse blocks onto taller
# pages, so its page breaks do not line up 1:1 with the PP sheet.
#
# A page is a PP page when it carries the Equibase footer "M/D/YYYY Race N" in the
# Arial Narrow font AND some NorthernDancer glyph text. The footer alone is a precise
# test (no ad or info page has it); the glyph check guards against a stray match.
# Glyph-font pages without the footer -- the "How to read the program" guide, the
# simulcast pages, the "Winners" page -- are rejected.
PP_FOOTER_X = 18.0  # footer's left edge on the standalone PP sheet (every page)
PP_LABEL_SIZE = 8.0  # size of the "Owner:" / "Silks:" / "Trainer:" labels there
PROGRAM_CHROME_FONTS = ("OpenSans",)  # program-only running header and page numbers


class PageLayout:
    """One detected PP page and how to map its coordinates back to the PP-sheet frame."""

    def __init__(self, index, footer_text, scale, dx, dy):
        self.index = index  # 0-based page index in the PDF
        self.footer_text = footer_text
        self.scale = scale
        self.dx = dx
        self.dy = dy

    def normalize(self, s):
        """Copy of a PyMuPDF span dict, with bbox and size mapped into the PP-sheet
        frame."""
        if self.scale == 1.0 and self.dx == 0.0 and self.dy == 0.0:
            return s
        x0, y0, x1, y1 = s["bbox"]
        out = dict(s)
        out["bbox"] = (
            (x0 - self.dx) / self.scale,
            (y0 - self.dy) / self.scale,
            (x1 - self.dx) / self.scale,
            (y1 - self.dy) / self.scale,
        )
        out["size"] = s["size"] / self.scale
        out["origin"] = (
            (s["origin"][0] - self.dx) / self.scale,
            (s["origin"][1] - self.dy) / self.scale,
        )
        out["chars_x"] = [
            (c, (a - self.dx) / self.scale, (b - self.dx) / self.scale)
            for c, a, b in s.get("chars_x", [])
        ]
        return out

    def __repr__(self):  # pragma: no cover - debugging aid
        return (
            f"<page {self.index + 1} {self.footer_text!r} "
            f"scale={self.scale:.3f} dx={self.dx:.2f}>"
        )


def _raw_spans(page):
    return [
        s
        for b in page.get_text("dict")["blocks"]
        for line in b.get("lines", [])
        for s in line["spans"]
        if s["text"].strip()
    ]


def find_pp_pages(doc):
    """Return a PageLayout for every past-performance page of ``doc``, in page order.

    The print scale is measured once per document from the 8pt field labels (it is
    1.0 for the standalone sheet, 0.92 for the 2025 and 2026 programs). The x shift
    is measured per page from the footer, whose left edge is always x=18 on the sheet.
    The y shift only matters for the "top of page" header band in parse_race_header,
    so it is set equal to the x shift (the program's top and left margins measured the
    same, about 10-11pt).
    """
    candidates, label_sizes = [], []
    for i, page in enumerate(doc):
        spans = _raw_spans(page)
        footer = next(
            (
                s
                for s in spans
                if "ArialNarrow" in s["font"] and FOOTER_RE.search(s["text"])
            ),
            None,
        )
        if footer is None or not any("NorthernDancer" in s["font"] for s in spans):
            continue
        candidates.append((i, footer))
        label_sizes += [
            s["size"]
            for s in spans
            if s["text"].strip() in ("Owner:", "Silks:", "Trainer:")
        ]

    if not candidates:
        return []
    label_sizes.sort()
    scale = label_sizes[len(label_sizes) // 2] / PP_LABEL_SIZE if label_sizes else 1.0
    if abs(scale - 1.0) < 0.005:
        scale = 1.0  # standalone sheet: keep coordinates exactly as printed

    layouts = []
    for i, footer in candidates:
        dx = footer["bbox"][0] - scale * PP_FOOTER_X
        if abs(dx) < 0.05:
            dx = 0.0
        layouts.append(
            PageLayout(i, FOOTER_RE.search(footer["text"]).group(0), scale, dx, dx)
        )
    return layouts


def group_rows(spans, tol=2.5):
    """Group spans into printed lines by their BASELINE.
    Returns [(top y0, spans sorted by x)].

    Grouping by the top edge (y0) loses text printed smaller than the rest of its line:
    a long race class shrunk to 4.8pt to fit its column sits on the same baseline as the
    7.4pt line but its top is ~3pt lower, past ``tol``. Measured on PP lines, baselines on
    one printed line agree within 1.3pt (a raised "s"), and lines are >= 7.9pt apart.
    """
    rows = []
    for s in sorted(spans, key=lambda s: s.base):
        if rows and abs(rows[-1][0] - s.base) <= tol:
            rows[-1][1].append(s)
        else:
            rows.append([s.base, [s]])
    return [(min(s.y0 for s in r), sorted(r, key=lambda s: s.x0)) for _, r in rows]


def row_text(spans):
    """Join a row's spans, inserting a space only where the glyphs don't touch."""
    out = ""
    last_x1 = None
    for s in spans:
        if (
            last_x1 is not None
            and (s.x0 - last_x1 > 1.0 or s.x0 < last_x1 - 1.0)
            and not out.endswith(" ")
        ):
            out += " "
        out += s.text
        last_x1 = s.x1
    return re.sub(r"\s+", " ", out).strip()


# A lone number such as "103" or "-113" (figures, post positions, field sizes).
NUMBER_RE = re.compile(r"^-?\d+$")


def is_number_span(s):
    return NUMBER_RE.match(s.t) is not None


def column_of(s):
    """The COL column a span belongs to.

    Text is printed left-aligned, so a text span belongs where it STARTS -- important for
    spans that run on across several columns ("M 115» bL" starts in the jockey column).
    Numbers are printed right-aligned, so a lone number belongs to the column it overlaps
    MOST: a 3-digit figure ("103") starts ~3pt left of where 2-digit figures start, and
    its left edge pokes into the previous column although nearly all of it is in its own.
    """
    return _column_at(s.t, s.x0, s.x1)


def _column_at(text, x0, x1):
    if NUMBER_RE.match(text.strip()):
        best, best_overlap = None, 0.0
        for name, (lo, hi) in COL.items():
            overlap = min(x1, hi) - max(x0, lo)
            if overlap > best_overlap:
                best, best_overlap = name, overlap
        if best is not None:
            return best
    return next((name for name, (lo, hi) in COL.items() if lo <= x0 < hi), None)


def split_at_columns(row, stops):
    """Split spans that run on from one column into the next.

    The PDF sometimes stores neighbouring columns as ONE span when they are printed close
    together, e.g. a long comment and a 2-digit field size: "4w, improved 10". ``stops``
    are
    this PDF's own columns as (x where it starts, bold, text shape) -- see column_profile.
    A span is cut at a space when a column starts inside that gap AND the next word looks
    like that column: "improved 10" crosses the field-size stop at x=588 and "10" has the
    column's shape "9". A long name that merely runs on into an EMPTY neighbour column
    ("... Stallion Farms EBF -21k" over the figure columns of a foreign line) is not cut,
    because "-21k" does not look like a figure.
    """
    out = []
    for s in row:
        if not s.chars:
            out.append(s)
            continue
        words, cur = [], []  # runs of non-space characters with their boxes
        for ch in s.chars:
            if ch[0].isspace():
                if cur:
                    words.append(cur)
                    cur = []
            else:
                cur.append(ch)
        if cur:
            words.append(cur)
        if len(words) < 2:
            out.append(s)
            continue
        pieces, start = [], 0
        for prev, w in zip(words, words[1:]):
            gap_lo, gap_hi = prev[-1][2], w[0][1]
            shape = text_shape("".join(ch[0] for ch in w))
            # 1pt of slack: a right-aligned number may start just before its stop
            if any(
                gap_lo + 0.5 < x <= gap_hi + 1.0 and shape == col_shape
                for x, _, col_shape in stops
            ):
                i = s.chars.index(w[0])  # where this word starts in s.chars
                pieces.append(s.chars[start:i])
                start = i
        pieces.append(s.chars[start:])
        out.extend(s.piece(p) for p in pieces if any(ch[0].strip() for ch in p))
    return out


# --------------------------------------------------------------------------
# Past-performance line
# --------------------------------------------------------------------------
DATE_RE = re.compile(r"^(\d{2})([A-Z][a-z]{2})(\d{2})(.*)$")


def is_pp_line(row):
    first = row[0]
    return first.symbol and first.x0 < 30 and DATE_RE.match(first.t) is not None


# --------------------------------------------------------------------------
# Adapting to column layout changes
# --------------------------------------------------------------------------
# COL is tuned on one layout. Rather than keeping a COL table per year, every PDF is
# mapped
# onto that one layout:
#   1. measure the PDF's own column profile: the x0 values shared by >= 25% of its PP
#      lines,
#      each with how it is printed (bold or not, text shape) -- see REFERENCE_COLUMNS;
#   2. line that profile up with REFERENCE_COLUMNS using sequence alignment (as in
#      `diff`):
#      either side may have columns the other lacks, matched columns should look alike,
#      and
#      the offset should stay steady, because a whole block of columns moves together when
#      one is added or removed;
#   3. shift every span by the offset of the matched column at or left of it.
# Example: the 2025 PPs lack the lone bold figure column that 2026 put at x=245, so pace,
# speed, post, calls, jockey ... all start ~11pt further left. Position alone cannot tell
# whether 2025's x=245 is the bold figure or the pace figure, but its look can (plain "9",
# like pace, not bold "9"), so it is matched 245 -> 257 (pace), 251 -> 262 (speed), ...
COLUMN_STOP_SHARE = 0.25  # a column must appear in this share of PP lines
COLUMN_SKIP_COST = (
    4.0  # cost of leaving a column unmatched, vs 1 per point of offset change
)
COLUMN_STYLE_COST = 6.0  # cost of matching columns that differ in boldness or text shape
MIN_LINES_TO_LEARN = 20  # fewer PP lines than this: too little evidence, use COL as is


def text_shape(text):
    """'-58' -> '-9', '16Aug26' -> '9a9', ':22¼¸' -> ':9g',
    'Clm 20000nw2/L' -> 'a 9a9/a'."""
    t = re.sub(r"\d+", "9", text.strip())
    t = re.sub(r"[A-Za-z]+", "a", t)
    t = re.sub("[" + re.escape(LENGTH_CHARS) + "]+", "g", t)
    return re.sub(r"\s+", " ", t)


def column_profile(rows):
    """[(x0, bold 0/1, most common text shape)] for every x0 (rounded) where at least
    COLUMN_STOP_SHARE of the PP-line rows have a span."""
    spans_at = defaultdict(list)
    lines_at = defaultdict(int)
    for row in rows:
        for x in {round(s.x0) for s in row}:
            lines_at[x] += 1
        for s in row:
            spans_at[round(s.x0)].append(s)
    out = []
    for x in sorted(x for x, n in lines_at.items() if n >= COLUMN_STOP_SHARE * len(rows)):
        spans = spans_at[x]
        bold = round(sum(s.bold for s in spans) / len(spans))
        shape = max(
            set(text_shape(s.text) for s in spans),
            key=[text_shape(s.text) for s in spans].count,
        )
        out.append((x, bold, shape))
    return out


def align_columns(ref, doc):
    """Match doc columns to reference columns: [(doc_x, ref_x), ...] left to right.

    Dynamic programming over matched pairs. Matching ref[i] with doc[j] right after the
    previous match (i', j') costs: |change in offset| + COLUMN_STYLE_COST per differing
    style (bold, shape) + COLUMN_SKIP_COST per column skipped on either side in between.
    The path starts from a virtual match at the left edge with offset 0.
    """

    def style(i, j):
        return COLUMN_STYLE_COST * ((ref[i][1] != doc[j][1]) + (ref[i][2] != doc[j][2]))

    best, back = {}, {}
    for i in range(len(ref)):
        for j in range(len(doc)):
            off = doc[j][0] - ref[i][0]
            cost, prev = abs(off) + COLUMN_SKIP_COST * (i + j), None  # first match
            for (pi, pj), pc in best.items():
                if pi < i and pj < j:
                    c = (
                        pc
                        + abs(off - (doc[pj][0] - ref[pi][0]))
                        + COLUMN_SKIP_COST * ((i - pi - 1) + (j - pj - 1))
                    )
                    if c < cost:
                        cost, prev = c, (pi, pj)
            best[(i, j)], back[(i, j)] = cost + style(i, j), prev
    if not best:
        return []
    end = min(
        best,
        key=lambda k: best[k]
        + COLUMN_SKIP_COST * ((len(ref) - 1 - k[0]) + (len(doc) - 1 - k[1])),
    )
    path = []
    while end is not None:
        path.append((doc[end[1]][0], ref[end[0]][0]))
        end = back[end]
    return path[::-1]


class ColumnShift:
    """Moves the spans of a PP line from this PDF's column positions onto the
    reference's."""

    def __init__(self, pairs, columns=None):
        self.pairs = pairs  # [(doc_x, ref_x)], left to right
        self.identity = all(d == r for d, r in pairs)
        # this PDF's own column profile; without a learned one, assume the reference's
        self.columns = columns or list(REFERENCE_COLUMNS)

    def offset_at(self, x):
        # A span takes the offset of the closest column stop at or left of x (1pt of slack
        # for rounding); spans left of every stop use the first stop's offset.
        d, r = self.pairs[0]
        for doc_x, ref_x in self.pairs:
            if doc_x <= x + 1.0:
                d, r = doc_x, ref_x
        return r - d

    def apply(self, row):
        if self.identity:
            return row
        # Text is judged by its left edge, a lone number by its centre: right-aligned
        # 3-digit figures start left of their column's stop (see column_of).
        return [
            s.shifted(self.offset_at((s.x0 + s.x1) / 2 if is_number_span(s) else s.x0))
            for s in row
        ]


def learn_column_shift(rows):
    """Learn a ColumnShift from all PP-line rows of one PDF (see the section comment)."""
    rows = [r for r in rows if is_pp_line(r)]
    if len(rows) < MIN_LINES_TO_LEARN:
        return ColumnShift([(0, 0)])
    profile = column_profile(rows)
    pairs = align_columns(REFERENCE_COLUMNS, profile)
    return ColumnShift(pairs or [(0, 0)], profile)


def parse_pp_line(row, shift=None):  # noqa: C901  (split later)
    # Split run-on spans in this PDF's own coordinates (its own column stops), then move
    # the pieces onto the reference layout.
    row = split_at_columns(row, shift.columns if shift is not None else REFERENCE_COLUMNS)
    if shift is not None:
        row = shift.apply(row)
    by = defaultdict(list)
    for s in row:
        name = column_of(s)
        if name is not None:
            by[name].append(s)

    out = {}
    m = DATE_RE.match(by["date"][0].t)
    out["date"] = f"20{m.group(3)}-{MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}"
    out["date_text"] = by["date"][0].t[:7]

    # The race number is occasionally glued onto the date span ("19Sep2611 CD").
    # A symbol-font glyph in front of the track marks a foreign race (no race number).
    foreign = any(s.font.endswith("2") for s in by["race_track"])
    rt = (
        m.group(4).strip()
        + " "
        + row_text([s for s in by["race_track"] if not s.font.endswith("2")])
    ).strip()
    m = re.match(r"^(\d+)\s*(\S+)$", rt)
    out["race_number"] = int(m.group(1)) if m else None
    out["track_code"] = m.group(2) if m else rt
    # Foreign tracks carry their country in small print: "Dun(IRE)", "Kar(KSA)".
    cm = re.match(r"^(.*?)\(([A-Z]{2,3})\)$", out["track_code"])
    out["country"] = cm.group(2) if cm else None
    if cm:
        out["track_code"] = cm.group(1)
    out["foreign"] = foreign
    out["track"] = TRACK_NAMES.get(out["track_code"], out["track_code"])

    # Condition ("ft", "fm"), course symbol (see COURSE_SYMBOLS) and rail setting
    cond_tokens = [s for s in by["condition"]]
    cond = "".join(
        s.t
        for s in cond_tokens
        if not s.font.endswith("2") and s.t.isalpha() and len(s.t) >= 2
    )
    rail = "".join(
        s.t for s in cond_tokens if s.t and all(c in GLYPH_DIGITS for c in s.t)
    )
    out["condition"] = cond or None
    symbol = next(
        (
            COURSE_SYMBOLS[k]
            for k in (course_symbol_key(s) for s in cond_tokens)
            if k in COURSE_SYMBOLS
        ),
        None,
    )
    # Symbol-font marks here that are neither a known course symbol nor a rail-setting
    # digit
    # are recorded (not guessed at) so check_parser.py can report them.
    out["unrecognized_symbols"] = [
        f"{s.font[-1:]}:{s.t}"
        for s in cond_tokens
        if s.symbol
        and course_symbol_key(s) not in COURSE_SYMBOLS
        and not all(c in GLYPH_DIGITS for c in s.t)
        and not (s.t.isalpha() and s.t.islower() and not s.font[-1:] in "23")
    ]
    out["course"] = symbol[1] if symbol else "main track"
    out["off_turf"] = out["course"] == "off turf"
    # No symbol means the main (dirt) track; turf-only conditions still mean turf.
    out["surface"] = (
        symbol[0] if symbol else ("turf" if cond in ("fm", "yl", "sf", "hd") else "dirt")
    )
    out["rail_setting"] = int(decode_glyph_number(rail)) if rail else None

    # A time always contains ":". In marathon races the first fraction ("1:38") is wide
    # enough to start inside the distance column, so route such spans to the fractions.
    timed = [s for s in by["distance"] if ":" in s.t]
    dist_raw = "".join(s.t for s in by["distance"] if s not in timed)
    out["distance"] = dist_raw
    out["distance_f"], out["about_distance"] = line_distance_to_furlongs(dist_raw)

    frac_text = "".join(s.text for s in timed + by["fractions"]).replace(" ", "")
    times = [decode_time(t) for t in re.findall(r"\d*:\d{2}[^\d:]{0,2}", frac_text)]
    out["fractions"] = times[:-1] if times else []
    out["final_time"] = times[-1] if times else None
    out["final_time_seconds"] = time_to_seconds(out["final_time"])

    age_raw = "".join(s.t for s in by["age_sex"])
    out["age_sex_code"] = age_raw
    # Glyphs (rendered and matched with the page-12 legend): g = up-arrow (and up), k = F
    # (fillies),
    # ½ = boxed A, l = S (state-bred), j = boxed R (restricted).
    out["age_sex"] = (
        age_raw.replace("g", "+")
        .replace("k", "F")
        .replace("½", "A")
        .replace("l", "S")
        .replace("j", "R")
    ) or None

    out["race_class"] = row_text(by["class"]) or None
    out.update(parse_class_string(out["race_class"] or ""))

    fig_spans = by["bold"] + by["early"]
    bold_sp = [s for s in fig_spans if s.bold]
    early_sp = [s for s in fig_spans if not s.bold]
    out["bold_figure"] = _num(bold_sp[0].t) if bold_sp else None
    out["early_pace"] = _num(early_sp[0].t.strip("-")) if early_sp else None
    # The speed figure is printed as "-65"; a two-digit post position ("12") can start
    # inside the speed column, so split the two by content rather than by x alone.
    speed_tokens = [s.t for s in by["speed"]]
    spd = next(
        (tk for tk in speed_tokens if tk.startswith("-") or not tk.isdigit()), None
    )
    post_tokens = [tk for tk in speed_tokens if tk != spd and tk.isdigit()] + [
        s.t for s in by["post"]
    ]
    out["speed_figure"] = (
        _num(re.sub(r"[^\d]", "", spd)) if spd and not spd[0].isalpha() else None
    )
    out["speed_figure_text"] = (
        spd if spd and spd[0].isalpha() else None
    )  # e.g. "T-86" on foreign lines
    out["post_position"] = _num(post_tokens[0]) if post_tokens else None

    # Running line, jockey and weight share one region: the jockey name is sometimes glued
    # to
    # the last call ("12º¸¡Asmussen K J") and the weight to the jockey ("Hernandez J J
    # 122").
    # A '0' glyph ('^') belonging to a 10 lb apprentice allowance can land in the
    # equipment column.
    stray = [
        s for s in by["equipment"] if s.font.endswith("2") and s.t == "^" and s.x0 < 400
    ]
    region = row_text(by["calls"] + by["jockey"] + by["weight"] + stray)
    region = re.sub(
        r"\s+(?=[" + re.escape(LENGTH_CHARS) + r"])", "", region
    )  # re-attach stray glyphs
    # A "*" after the finish ("1¹*") marks a result changed by the stewards; the comment
    # then
    # says "DQ3rd" (disqualified, placed 3rd) or "PL2nd" (moved up to 2nd).
    cm = re.match(
        r"^(?P<calls>(?:(?:\d{1,2}["
        + re.escape(LENGTH_CHARS)
        + r"]*\*?|--)\s*)*)(?P<rest>.*)$",
        region,
    )
    calls = []
    steward_mark = False
    for pos, g, star in re.findall(
        r"(\d{1,2}|--)([" + re.escape(LENGTH_CHARS) + r"]*)(\*?)", cm.group("calls")
    ):
        steward_mark = steward_mark or bool(star)
        if pos == "--":
            calls.append({"position": None, "margin": None, "margin_text": None})
            continue
        text, value = decode_margin(g)
        calls.append({"position": int(pos), "margin": value, "margin_text": text or None})
    if foreign and len(calls) == 2 and calls[0]["margin_text"] is None:
        # Foreign lines only print the post position and the finish.
        out["post_position"] = calls[0]["position"]
        calls = calls[1:]
    out["calls"] = calls
    jm = re.match(
        r"^(?P<jockey>.*?)\s*(?:(?P<weight>\d{3})(?P<bug>["
        + re.escape(LENGTH_CHARS)
        + r"]*))?"
        r"\s*(?P<equip>[A-Za-z]*)\s*$",
        cm.group("rest"),
    )
    # Always present (None when not printed). A horse that did not finish ("-- --" at the
    # end)
    # still has its early calls, which the converter uses for early-pace estimates.
    out.update(
        finish_position=None,
        lengths_behind=None,
        winning_margin=None,
        stretch_position=None,
        first_call_position=None,
    )
    if calls and calls[-1]["position"] is not None:
        fin = calls[-1]
        out["finish_position"] = fin["position"]
        out["lengths_behind"] = 0.0 if fin["position"] == 1 else fin["margin"]
        out["winning_margin"] = fin["margin"] if fin["position"] == 1 else None
    if len(calls) >= 2:
        out["stretch_position"] = calls[-2]["position"]
        # Sprints print a start position first (no margin); routes do not.
        valid = [c for c in calls if c["position"] is not None]
        # A sprint's first entry is the start position, so its first call is the next
        # entry
        # -- which can itself be "--" when the horse was pulled up early ("8 -- -- --
        #  --").
        sprint_start = len(calls) >= 5 and calls[0]["margin_text"] is None
        first = calls[1] if sprint_start else (valid[0] if valid else None)
        out["first_call_position"] = first["position"] if first else None

    out["jockey"] = (jm.group("jockey").strip() or None) if jm else None
    out["weight"] = int(jm.group("weight")) if jm and jm.group("weight") else None
    bug = jm.group("bug") if jm else ""
    out["apprentice_allowance"] = (
        int(decode_glyph_number(bug)) if bug else None
    )  # 5/7/10 lb bug

    eo = row_text([s for s in by["equipment"] if s not in stray] + by["odds"])
    if jm and jm.group("equip"):  # equipment glued to an apprentice weight ("119» fL")
        eo = jm.group("equip") + " " + eo
    om = re.search(r"(\*?)(\d*\.\d+)", eo)
    out["equipment"] = (
        re.sub(r"[^A-Za-z ]", "", eo[: om.start()] if om else eo).strip() or None
    )
    out["equipment_codes"] = [
        EQUIPMENT_CODES.get(c, c) for c in (out["equipment"] or "").replace(" ", "")
    ]
    out["odds_text"] = om.group(0) if om else None
    out["odds"] = float(om.group(2)) if om else None
    out["favorite"] = bool(om and om.group(1))
    # What can follow the odds:
    #   "®"  -- prints as a superscript "e": coupled entry;
    #   a lone 3-4 digit number ("995", "1074") -- only on lines from Santa Anita, Lone
    #   Star,
    #         Remington, Laurel and Golden Gate, range ~920-1260 and steady for one horse
    #         (995, 1010, 1010, 999, 1008): the horse's body weight in pounds on race day,
    #         which those tracks publish (confirmed with the user 2026-11-16);
    #   anything else -- the finishers, when the PDF stored them in the same span as the
    #   odds.
    tail = eo[om.end() :].strip() if om else ""
    out["coupled_entry"] = tail.startswith("®")
    tail = tail.lstrip("®").strip()
    tm = re.match(r"^(\d{3,4})\b\s*(.*)$", tail)
    out["body_weight_lb"] = int(tm.group(1)) if tm else None
    tail = tm.group(2) if tm else tail

    # A trouble icon -- a "w" in a third symbol font -- is printed in front of trouble
    # comments ("stmbld st", "off slow", "chckd turn"). Keep it out of the text: glued to
    # the
    # third finisher's margin it would swallow the comment's first word ("124¡woff slow").
    icons = [s for s in by["finishers"] if "NorthernDancer3" in s.font]
    out["trouble_icon"] = any(s.t == "w" for s in icons)
    small = [s for s in by["finishers"] if s not in icons]
    # NOTE: a "^" here (second symbol font) is the digit 0 of a 10+ length margin
    # ("Holly122¶^" = 10 lengths, "¶^£" = 10 3/4), so it stays part of the text.
    # An "n" in the second symbol font prints as a boxed "DH": the finisher after it
    # dead-heated
    # (tied) for that position. Turn it into a marker that split_finishers reads.
    small = [
        s.with_text(" " + DEAD_HEAT_MARK) if s.font.endswith("2") and s.t == "n" else s
        for s in small
    ]
    text = (tail + " " + row_text(small)).strip()
    out["top_finishers"], out["comment"] = split_finishers(text)
    # Official result when the stewards changed it (see steward_mark above):
    # finish_position is
    # the official place, finish_position_wire the order across the wire as printed.
    out["finish_position_wire"] = out["finish_position"]
    sm = re.search(r"\b(DQ|PL)(\d{1,2})(?:st|nd|rd|th)\b", out["comment"] or "")
    out["stewards_change"] = (sm.group(1) if sm else "?") if steward_mark else None
    if steward_mark and sm:
        out["finish_position"] = int(sm.group(2))
    # Guide: "Names in bold will be horses they are competing against in that race.
    # Names in italics indicate the horse won its next start."
    bold_text = " ".join(s.t for s in small if s.bold)
    italic_text = " ".join(s.t for s in small if s.italic)
    for f in out["top_finishers"]:
        f["in_todays_race"] = bool(f["name"]) and f["name"] in bold_text
        f["won_next_start"] = bool(f["name"]) and f["name"] in italic_text
    out["field_size"] = _num(by["field"][0].t) if by["field"] else None
    return out


def parse_class_string(text):
    """'Mcl 20000 (20-15)' -> type Mcl, value 20000;
    'Stk - FuturityG3 - 175k' -> stakes info.
    ``purse`` is only filled where the printed number is a purse (Msw/Alw/Stk/Hcp).
    For optional / starter claiming classes (Aoc, Moc, Str, SOC) the number is a
    claiming price: 10-10-26 R8 is "Allowance Optional Claiming $80,000" with a
    $130,000 purse, and its PP class prints "Aoc 80000"."""
    out = {
        "class_type": None,
        "class_value": None,
        "purse": None,
        "stakes_name": None,
        "stakes_grade": None,
    }
    t = text.strip()
    sm = re.match(
        r"^(?P<pre>[a-z]?)(?P<kind>Stk|Hcp|Msw|Alw|Aoc|Clm|Mcl)"
        r"\s*-\s*(?P<name>.+?)\s*-\s*(?P<purse>\d+)k\s*$",
        t,
    )
    if sm:
        out["class_type"] = sm.group("kind")
        out["purse"] = int(sm.group("purse")) * 1000
        name = sm.group("name")
        gm = re.search(r"(G[123]|L|B)$", name)
        out["stakes_grade"] = gm.group(1) if gm and sm.group("kind") == "Stk" else None
        out["stakes_name"] = (
            name[: gm.start()] if gm and sm.group("kind") == "Stk" else name
        )
        out["state_bred"] = sm.group("pre") == "l"
        return out
    cm = re.match(r"^(?P<kind>[A-Za-z]+)\s*(?P<value>\d+)?", t)
    if cm:
        out["class_type"] = cm.group("kind")
        if cm.group("value"):
            out["class_value"] = int(cm.group("value"))
            if out["class_type"] in ("Msw", "Alw", "Hcp", "Stk"):
                out["purse"] = out["class_value"]
    return out


DEAD_HEAT_MARK = "‡"  # stands in for the boxed "DH" glyph while splitting finishers
FINISHER_RE = re.compile(
    r"^\s*(?P<a>.+?\d{3}[^\s\d]*)\s+(?P<b>.+?\d{3}[^\s\d]*)"
    r"\s+(?P<c>.+?\d{3}[^\s\d]*)\s*(?P<comment>.*)$"
)


def split_finishers(text):
    """'Sudden Squall122½ Legit Chick122£ C V Holly122¶^ 3p,rally' -> ([...], '3p,rally').
    Each finisher's margin is the number of lengths it finished IN FRONT OF the next
    horse.
    """
    if not text:
        return [], None
    m = FINISHER_RE.match(text)
    if not m:
        return [], text
    finishers = []
    for key in ("a", "b", "c"):
        fm = re.match(r"^(.*?)(\d{3})(.*)$", m.group(key))
        name, weight, margin = fm.group(1).strip(), int(fm.group(2)), fm.group(3)
        mtext, mval = decode_margin(margin)
        finishers.append(
            {
                "name": name.replace(DEAD_HEAT_MARK, "").strip(),
                "weight": weight,
                "margin": mval,
                "margin_text": mtext or None,
                "dead_heat": DEAD_HEAT_MARK in name,
            }
        )
    return finishers, (m.group("comment").strip() or None)


# --------------------------------------------------------------------------
# Horse block
# --------------------------------------------------------------------------
BREEDING_RE = re.compile(
    r"(?P<color>[A-Za-z/ ]+?)\.(?P<sex>[a-z]+)\.(?P<age>\d+)\s+"
    r"(?P<sire>.+?)\s+-\s+(?P<dam>.+?)\s+by\s+(?P<damsire>.+?)"
    r"(?:\s+-\s+Bred\s+in\s+(?P<where>.+?)\s+by\s+(?P<breeder>.+?))?"
    r"\s*(?:\((?P<foaled>[^)]+)\))?\s*$"
)
WORKOUT_RE = re.compile(
    # Between track and distance: optional short markers such as a course symbol "o" (all
    # weather), "tt"/"tr" (training track) or both ("Kee o tr 4F").
    r"(?P<bullet>a?)\s*(?P<day>\d{1,2})\s*(?P<mon>[A-Z][a-z]{2})\s*(?P<yr>\d{2})\s+"
    r"(?P<track>[A-Za-z]+)\s*(?P<surf>(?:[a-z]{1,2}\s*)*?)\s*(?P<dist>\d+)F\s+"
    r"(?P<cond>[a-z]+)\s*(?P<time>\d*:?\d{2}(?:\.\d+)?)"
    r"(?P<kind>[a-zA-Z]*)\s+(?P<rank>\d+)/(?P<of>\d+)"
)


def parse_workouts(text):
    out = []
    for m in WORKOUT_RE.finditer(text):
        out.append(
            {
                "date": f"20{m.group('yr')}-{MONTHS.get(m.group('mon'), 0):02d}"
                f"-{int(m.group('day')):02d}",
                "track_code": m.group("track"),
                "surface_marker": " ".join(m.group("surf").split()) or None,
                "distance_f": int(m.group("dist")),
                "condition": m.group("cond"),
                "time": m.group("time"),
                "type": m.group("kind")
                or None,  # b = breezing, h = handily, g = from gate, d = dogs up
                "rank": int(m.group("rank")),
                "of": int(m.group("of")),
                "bullet": m.group("bullet") == "a",
            }
        )
    return out


def parse_horse(spans, shift=None):  # noqa: C901  (split later)
    """spans: all spans of one horse block (program number row .. before the next).
    shift: the PDF's ColumnShift, applied to its past-performance lines."""
    rows = group_rows(spans)
    horse = {"scratched": False, "breeding": None, "sale": None}

    prog = next((s for s in spans if s.bold and s.size >= 20 and s.x0 < 60), None)
    horse["program_number"] = _num(prog.t) if prog else None
    # Also-eligible horses have a stacked bold "A" / "E" (12pt) in the left margin beside
    # the
    # program number (62 of them in the five test programs, nothing else bold prints
    # there).
    margin = {s.t for s in spans if s.bold and s.x0 < 30 and 10 <= s.size <= 14}
    horse["ae_marker"] = {"A", "E"} <= margin
    color = next(
        (s for s in spans if not s.bold and s.size == 10 and s.x0 < 55 and not s.symbol),
        None,
    )
    horse["saddle_cloth_color"] = color.t if color else None
    odds = next(
        (s for s in spans if s.bold and 13 <= s.size <= 15 and 50 <= s.x0 <= 90), None
    )
    horse["morning_line_odds"] = odds.t if odds else None

    # Labelled lines (Owner / Silks / Trainer) and the Class rating on the owner row
    for y, row in rows:
        labels = [
            s
            for s in row
            if s.bold and s.t in ("Owner:", "Silks:", "Trainer:") and s.x0 < 100
        ]
        if not labels:
            continue
        label = labels[0]
        value = row_text(
            [s for s in row if s.x0 > label.x0 and not s.bold and s.x0 < 380]
        )
        key = label.t.rstrip(":").lower()
        if key == "trainer":
            rec = parse_record(value)
            horse["trainer"] = re.sub(r"\s*\(.*$", "", value).strip() or None
            horse["trainer_meet_record"] = rec
        else:
            horse[key] = value or None
        if key == "owner":
            cls = next((s for s in row if s.bold and 375 <= s.x0 <= 412), None)
            horse["class_rating"] = _num(cls.t) if cls else None
            horse["class_rating_text"] = cls.t if cls else None

    clm = [s for s in spans if s.bold and s.x0 < 84 and s.t.startswith("$")]
    horse["claiming_price"] = _num(clm[0].t) if clm else None

    # Name row: the ArialBlack text left of the weight. Long names are printed smaller
    # (12pt normally, 8.5pt for "Never Gonna Stop (IRE)"), so no size limit is applied.
    # In parentheses: a medication code from the race legend ("(L)" Lasix, "(B)" Bute), or
    # the
    # country of breeding, which is part of the registered name ("Never Gonna Stop
    # (IRE)").
    name_spans = [s for s in spans if "ArialBlack" in s.font and s.x0 < 290]
    meds = [s for s in name_spans if MEDICATION_RE.match(s.t)]
    horse["name"] = " ".join(s.t for s in name_spans if s not in meds) or None
    horse["medication"] = " ".join(s.t for s in meds) or None
    bred = re.search(r"\(([A-Z]{2,3})\)$", horse["name"] or "")
    horse["country_bred"] = bred.group(1) if bred else None
    if name_spans:
        nb = name_spans[0].base
        # The race-end legend reads: "Equibase Rating - The italicized number by horse
        # name."
        # It sits on the name's baseline, whatever size the name is printed at.
        fig = next(
            (
                s
                for s in spans
                if s.italic and abs(s.base - nb) < 2.5 and _num(s.t) is not None
            ),
            None,
        )
        horse["equibase_rating"] = _num(fig.t) if fig else None
        symbols = "".join(
            s.t
            for s in spans
            if s.symbol and s.font.endswith("2") and abs(s.base - nb) < 6 and s.x0 < 300
        )
        horse["name_symbols"] = symbols or None
        horse["kentucky_bred"] = "ï" in symbols  # legend: "ï - Kentucky Bred"
        horse["keeneland_sale"] = (
            "K" in symbols
        )  # legend: "K - most recent sale price at Keeneland"
    wt = next((s for s in spans if "ArialBlack" in s.font and s.x0 >= 290), None)
    horse["weight"] = _num(wt.t) if wt else None

    jk = [
        s
        for s in spans
        if s.bold and s.size == 10 and 300 <= s.x0 <= 400 and not s.symbol
    ]
    horse["jockey"] = jk[0].t if jk else None
    jr = [
        s for s in spans if s.bold and s.size == 7 and 300 <= s.x0 <= 400 and "-" in s.t
    ]
    horse["jockey_meet_record"] = parse_record(jr[0].t) if jr else None

    # Record table (right-hand side): label, starts, wins, places, shows, earnings
    records = {}
    for y, row in rows:
        right = [s for s in row if s.symbol and s.x0 >= 400 and s.size <= 8.1]
        if not right:
            continue
        for part in (
            sorted([s for s in right if s.x0 < 498], key=lambda s: s.x0),
            sorted([s for s in right if s.x0 >= 498], key=lambda s: s.x0),
        ):
            toks = [s.t for s in part]
            if not toks or not toks[0].endswith(":"):
                continue
            label = toks[0].rstrip(":")
            nums = [_num(t) for t in toks[1:]]
            if len(nums) >= 5:
                key = label.lower().replace(" ", "_")
                if re.match(
                    r"^[A-Z][A-Za-z]+ (Dirt|Turf|Synthetic)$", label
                ) and not label.startswith("Wet"):
                    key = "track_" + label.split()[1].lower()
                    records["track_code"] = label.split()[0]
                records[key] = {
                    "starts": nums[0],
                    "wins": nums[1],
                    "places": nums[2],
                    "shows": nums[3],
                    "earnings": nums[4],
                }
    horse["records"] = records

    # Breeding line + optional sale line
    for y, row in rows:
        text = row_text([s for s in row if not s.symbol and s.x0 < 420])
        if "ArialBlack" in row[0].font:
            continue
        bm = BREEDING_RE.search(text)
        if bm and " by " in text and not horse.get("breeding"):
            d = bm.groupdict()
            horse["breeding"] = {
                "color": d["color"],
                "sex": d["sex"],
                "age": int(d["age"]),
                "sire": d["sire"],
                "dam": d["dam"],
                "dam_sire": d["damsire"],
                "bred_in": d["where"],
                "breeder": d["breeder"],
                "foaled": d["foaled"],
            }
        sale = re.search(
            r"\b([A-Z]{2,4}\s+[A-Z]{3}\s+[A-Z ]+\d{4}\s+\$[\d,]+)",
            row_text([s for s in row if not s.symbol]),
        )
        if sale and not horse.get("sale"):
            horse["sale"] = sale.group(1)

    # Past-performance lines, "Claimed by" notes and workouts
    pps = []
    for y, row in rows:
        if is_pp_line(row):
            pps.append(parse_pp_line(row, shift))
        elif (
            pps
            and row[0].symbol
            and row[0].x0 < 100
            and row_text(row).startswith(("Claimed", "Previously"))
        ):
            pps[-1]["note"] = (pps[-1].get("note", "") + " " + row_text(row)).strip()
        elif (
            pps
            and row[0].symbol
            and row[0].x0 > 60
            and row[0].bold
            and not row_text(row).startswith("Workout")
        ):
            # continuation of a multi-line "Claimed by ..." note
            if pps[-1].get("note"):
                pps[-1]["note"] += " " + row_text(row)
    horse["past_performances"] = pps

    wo_rows = [row for y, row in rows if any(s.t.startswith("Workout") for s in row)]
    workouts = []
    for row in wo_rows:
        workouts.extend(
            parse_workouts(row_text([s for s in row if not s.t.startswith("Workout")]))
        )
    horse["workouts"] = workouts

    # Convenience summaries for the handicap converter (most recent race first)
    horse["speed_figures"] = [
        p["speed_figure"] for p in pps if p.get("speed_figure") is not None
    ]
    horse["early_pace_figures"] = [
        p["early_pace"] for p in pps if p.get("early_pace") is not None
    ]
    horse["bold_figures"] = [
        p["bold_figure"] for p in pps if p.get("bold_figure") is not None
    ]
    if horse["early_pace_figures"]:
        horse["pace"] = {
            "early_pace": round(
                sum(horse["early_pace_figures"]) / len(horse["early_pace_figures"]), 2
            )
        }
    life = records.get("life")
    if life:
        horse["life_record"] = (
            f"{life['starts']}-{life['wins']}-{life['places']}-{life['shows']}"
        )
    return horse


# --------------------------------------------------------------------------
# Race header
# --------------------------------------------------------------------------
def parse_race_header(spans, race_number):
    """spans: everything above the 'Program #' column header on a race's first page."""
    race = {"race_number": race_number}
    rows = group_rows(spans)
    texts = [(y, row_text(row), row) for y, row in rows]

    post = next((s.t for s in spans if "APPROX. POST" in s.t), None)
    race["post_time"] = (
        re.sub(r".*APPROX\. POST:\s*", "", post).split()[0] if post else None
    )
    wager_spans = [
        s for s in spans if s.bold and s.size == 10 and s.y0 < 45 and "APPROX" not in s.t
    ]
    race["wagers"] = row_text(wager_spans) or None
    big = next((s for s in spans if s.bold and s.size >= 40), None)
    if big and _num(big.t) is not None:
        race["race_number"] = _num(big.t)

    # Race type: 10pt bold at the left margin that is not the post-time line
    type_rows = [
        (y, t)
        for y, t, r in texts
        if r[0].bold
        and r[0].size == 10
        and r[0].x0 < 25
        and "APPROX" not in t
        and not t.startswith("RACE")
    ]
    race["race_type"] = type_rows[0][1] if type_rows else None

    # Stakes title: 12pt bold centred, optional sponsor line (10pt bold, centred) below
    # A title line is the text set in the title's own style: other text on the same
    # baseline
    # (the small "equibase.com/QR" caption beside the QR code) is not part of it.
    def styled(r):
        return row_text([s for s in r if s.bold == r[0].bold and s.size == r[0].size])

    title = [
        styled(r)
        for y, t, r in texts
        if r[0].bold and r[0].size == 12 and r[0].x0 > 100 and y > 45
    ]
    sponsor = [
        styled(r)
        for y, t, r in texts
        if r[0].bold
        and r[0].size == 10
        and r[0].x0 > 100
        and y > 45
        and not re.search(r"Exacta|Trifecta|Pick|Superfecta", t)
    ]
    race["stakes_name"] = " ".join(title + sponsor) if title else None

    cond_spans = [s for s in spans if s.size == 9 and s.x0 < 600]
    # Order line by line with a tolerance (group_rows), not by round(y0): two spans on one
    # printed line can be ~0.2pt apart and land on different sides of a rounding boundary.
    cond_spans = [s for _, row in group_rows(cond_spans) for s in row]
    race["conditions"] = row_text(cond_spans) if cond_spans else None
    # Distance / course: the bold spans that follow the first regular-weight span
    first_regular = next((i for i, s in enumerate(cond_spans) if not s.bold), None)
    dist_spans = (
        [s for s in cond_spans[first_regular:] if s.bold]
        if first_regular is not None
        else []
    )
    distance_text = row_text(dist_spans) if dist_spans else None
    if not distance_text and race["conditions"]:
        m = re.search(
            r"((?:\w+ ){1,5}Furlongs?|(?:\w+ ){1,6}Miles?[^.]*)\s*$", race["conditions"]
        )
        distance_text = m.group(1).strip() if m else None
    race["distance"] = distance_text
    race["distance_f"] = words_to_furlongs(distance_text or "")
    race["surface"] = (
        "turf" if distance_text and "turf" in distance_text.lower() else "dirt"
    )
    course = re.search(r"\(Rail at (\d+) Feet\)", distance_text or "")
    race["rail_setting"] = int(course.group(1)) if course else None
    cm = re.match(r"^\s*([A-Za-z ]+?) Course", distance_text or "")
    race["course"] = cm.group(1).strip() if cm else None
    off = re.search(
        r"taken off the turf it will be contested at ([^.]+?) on the main track",
        race["conditions"] or "",
    )
    race["off_turf_distance"] = off.group(1).strip() if off else None
    race["off_turf_distance_f"] = words_to_furlongs(off.group(1)) if off else None

    # Stakes races print no "Purse $..." phrase; the purse is the first dollar amount in
    # the title ("The Perryville (Grade III) $350,000 (Includes $100,000 from KTDF)").
    pm = re.search(r"Purse\s+\$([\d,]+)", race["conditions"] or "") or re.search(
        r"\$([\d,]+)", race["stakes_name"] or ""
    )
    race["purse"] = _num(pm.group(1)) if pm else None
    cp = re.search(r"Claiming Price\s+\$([\d,]+)", race["conditions"] or "")
    race["claiming_price"] = _num(cp.group(1)) if cp else None
    tr = next((t for y, t, r in texts if t.startswith("Track Record")), None)
    race["track_record"] = tr.replace("Track Record:", "").strip() if tr else None
    return race


# --------------------------------------------------------------------------
# Race-end footnotes (legend, equipment changes, mutuel ratings, also-eligibles)
# --------------------------------------------------------------------------
def extract_footnotes(body, race):
    """Remove legend / footnote spans from a page body and record them on the race.
    Returns (remaining spans, y of the ALSO ELIGIBLES banner or None)."""
    keep, also_y = [], None
    for s in body:
        t = s.t
        if t.startswith("MUTUEL RATINGS"):
            race["mutuel_ratings"] = [int(x) for x in re.findall(r"\d+", t)]
        elif t.startswith("Equipment Change:"):
            race.setdefault("equipment_changes", []).append(t.split(":", 1)[1].strip())
        elif t.startswith("ALSO ELIGIBLE"):
            also_y = s.y0
            race["also_eligible_note"] = t
        elif (
            s.size <= 7.6
            and s.x0 < 25
            and not s.symbol
            and re.match(
                r"^(L-Lasix|Equibase Rating|ï - |K - |B - |Scratched|Late Scratch)", t
            )
        ):
            race.setdefault("legend", []).append(t)
        else:
            keep.append(s)
    return keep, also_y


# --------------------------------------------------------------------------
# Document walk
# --------------------------------------------------------------------------
FOOTER_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})\s*Race\s*(\d+)")


def parse_pdf(path):  # noqa: C901  (split later)
    doc = pymupdf.open(str(path))
    races = []
    current = None
    card_date = None
    track_code = None

    # Only walk the pages that are actually past performances (see find_pp_pages); in a
    # full program that skips the ads, wagering guides and simulcast pages in between.
    layouts = find_pp_pages(doc)
    pages = [(layout, page_spans(doc[layout.index], layout)) for layout in layouts]
    # Learn how this PDF's PP-line columns line up with the layout COL was tuned on.
    shift = learn_column_shift(
        [row for _, spans in pages for _, row in group_rows(spans)]
    )
    for layout, spans in pages:
        pno = layout.index
        fm = FOOTER_RE.search(layout.footer_text)
        if fm and not card_date:
            card_date = (
                f"{int(fm.group(3))}-{int(fm.group(1)):02d}-{int(fm.group(2)):02d}"
            )

        big = next((s for s in spans if s.bold and s.size >= 40 and s.x0 < 40), None)
        header_row = next(
            (s for s in spans if s.bold and s.size == 7 and s.t == "Program #"), None
        )
        if big is not None:
            cutoff = header_row.y1 if header_row else big.y1 + 100
            # The race header ends at the "Program #" column-header row -- but never below
            # the
            # first horse's program number. The program's re-flowed pages put horse #1
            # closer
            # to that row (7pt vs 9.6pt on the PP sheet), so the row's bottom edge
            # overlaps it.
            first_prog = min(
                (
                    s.y0
                    for s in spans
                    if s.bold
                    and 20 <= s.size < 40
                    and s.x0 < 60
                    and s.y0 > big.y1
                    and _num(s.t) is not None
                ),
                default=None,
            )
            if first_prog is not None:
                cutoff = min(cutoff, first_prog - 1)
            current = parse_race_header([s for s in spans if s.y0 < cutoff], _num(big.t))
            current["horses"] = []
            current["pages"] = [pno + 1]
            races.append(current)
            body = [s for s in spans if s.y0 >= cutoff]
        else:
            if current is None:
                continue
            current["pages"].append(pno + 1)
            body = spans

        # Drop footer / continuation banners, pull out the race-end legend lines
        body = [
            s
            for s in body
            if not (FOOTER_RE.search(s.t) or "CONTINUED" in s.t or "equibase.com" in s.t)
        ]
        body, also_eligible_y = extract_footnotes(body, current)
        starts = sorted(
            [
                s
                for s in body
                if s.bold
                and s.size >= 20
                and s.size < 40
                and s.x0 < 60
                and _num(s.t) is not None
            ],
            key=lambda s: s.y0,
        )
        # Anything above the first program number on a continuation page belongs to the
        # previous
        # horse -- the whole page if no new horse starts on it.
        lead = [s for s in body if not starts or s.y0 < starts[0].y0 - 1]
        if lead and current["horses"]:
            extra = [
                parse_pp_line(row, shift)
                for y, row in group_rows(lead)
                if is_pp_line(row)
            ]
            for p in extra:
                p["page"] = pno + 1  # PDF page (1-based) the line is printed on
            current["horses"][-1]["past_performances"].extend(extra)
        for i, st in enumerate(starts):
            y_lo = st.y0 - 1
            y_hi = starts[i + 1].y0 - 1 if i + 1 < len(starts) else 10_000
            block = [s for s in body if y_lo <= s.y0 < y_hi]
            horse = parse_horse(block, shift)
            if horse.get("name"):
                # Every also-eligible horse carries its own bold "A"/"E" marker; the
                # banner
                # check is a backup and covers horses on later pages of the race too.
                below_banner = current.get("_ae_banner_seen") or (
                    also_eligible_y is not None and st.y0 > also_eligible_y
                )
                horse["also_eligible"] = horse.pop("ae_marker") or bool(below_banner)
                horse["page"] = pno + 1
                for p in horse["past_performances"]:
                    p["page"] = pno + 1
                current["horses"].append(horse)
                if not track_code and horse["records"].get("track_code"):
                    track_code = horse["records"]["track_code"]
        if also_eligible_y is not None:
            current["_ae_banner_seen"] = True

    track_name = TRACK_NAMES.get(track_code or "", track_code)
    for race in races:
        race.pop("_ae_banner_seen", None)
        race["track"] = track_name
        race["track_code"] = track_code
        race["date"] = card_date
        base = f"{track_name or 'Race'} Race {race['race_number']}"
        race["name"] = (
            f"{base} - {race['stakes_name']}" if race.get("stakes_name") else base
        )
        race["field_size"] = len(
            [h for h in race["horses"] if not h.get("also_eligible")]
        )
        changes = race.get("equipment_changes") or []
        for h in race["horses"]:
            # match on the name without a country suffix: "Navy Seal (IRE)" -> "navy seal"
            base_name = re.sub(r"\s*\([A-Z]{2,3}\)$", "", h["name"]).lower()
            h["equipment_change"] = next(
                (c for c in changes if c.lower().startswith(base_name)), None
            )
        # Re-derive per-horse summaries now that continuation lines were attached
        for h in race["horses"]:
            pps = h["past_performances"]
            h["speed_figures"] = [
                p["speed_figure"] for p in pps if p.get("speed_figure") is not None
            ]
            h["early_pace_figures"] = [
                p["early_pace"] for p in pps if p.get("early_pace") is not None
            ]
            h["bold_figures"] = [
                p["bold_figure"] for p in pps if p.get("bold_figure") is not None
            ]
            if h["early_pace_figures"]:
                h["pace"] = {
                    "early_pace": round(
                        sum(h["early_pace_figures"]) / len(h["early_pace_figures"]), 2
                    )
                }
            else:
                h.pop("pace", None)
    return {
        "source_pdf": str(path),
        "pp_pages": [lay.index + 1 for lay in layouts],
        "print_scale": layouts[0].scale if layouts else None,
        # Columns that sit somewhere else than in the reference layout: [[pdf_x,
        # reference_x]]
        "column_shift": [[d, r] for d, r in shift.pairs if d != r],
        "track": track_name,
        "track_code": track_code,
        "date": card_date,
        "race_count": len(races),
        "races": races,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Extract JSON from an Equibase past-performance PDF."
    )
    ap.add_argument("pdf", help="path to the past-performance PDF")
    ap.add_argument("-o", "--output", help="write JSON here instead of stdout")
    ap.add_argument(
        "--race", type=int, help="only output this race number (a single race object)"
    )
    ap.add_argument(
        "--split-dir", help="write one <stem>_race<N>.json per race into this folder"
    )
    ap.add_argument(
        "--summary", action="store_true", help="print a short per-race summary to stderr"
    )
    args = ap.parse_args(argv)

    card = parse_pdf(args.pdf)
    if args.summary:
        for r in card["races"]:
            print(
                f"Race {r['race_number']}: {r['race_type']} | {r['distance']} "
                f"({r['distance_f']}f {r['surface']})"
                f" | {len(r['horses'])} horses | pages {r['pages']}",
                file=sys.stderr,
            )
            for h in r["horses"]:
                print(
                    f"   #{h['program_number']:<3}{h['name']:<24} "
                    f"ML {h['morning_line_odds']:<5} cls {h['class_rating']!s:<6}"
                    f" spd {h['speed_figures'][:6]} pps={len(h['past_performances'])}"
                    f" wo={len(h['workouts'])}",
                    file=sys.stderr,
                )

    if args.split_dir:
        out_dir = Path(args.split_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(args.pdf).stem
        for r in card["races"]:
            p = out_dir / f"{stem}_race{r['race_number']}.json"
            p.write_text(
                json.dumps(r, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        print(f"Wrote {len(card['races'])} race files to {out_dir}", file=sys.stderr)
        if not args.output and not args.race:
            return 0

    payload = card
    if args.race is not None:
        payload = next((r for r in card["races"] if r["race_number"] == args.race), None)
        if payload is None:
            print(
                f"ERROR: race {args.race} not found "
                f"(have {[r['race_number'] for r in card['races']]})",
                file=sys.stderr,
            )
            return 1
    rendered = json.dumps(payload, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
