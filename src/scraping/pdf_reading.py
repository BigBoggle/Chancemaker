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

    python src/scraping/pdf_reading.py path/to/pps.pdf --full    # every field read

The whole-card output is {"track": ..., "date": ..., "races": [race, ...]}.
Only the essentials are written (see "Output: only the essentials" near the end:
CARD_FIELDS, RACE_FIELDS, HORSE_FIELDS, PP_FIELDS); --full writes everything.
Each race object (and the --race / --split-dir files) can be fed straight into
src/model/pp_json_to_handicap.py:

    {
      "date": "2026-10-10", "track": "Keeneland", "race_number": 1,
      "name": "Keeneland Race 1", "distance_f": 6.0, "surface": "dirt",
      "horses": [
        {"program_number": 1, "name": ..., "morning_line_odds": "10-1",
         "trainer": ..., "scratched": false, "also_eligible": false,
         "class_rating": 61.35, "speed_figures": [65, 70, ...],
         "pace": {"early_pace": 61.1},
         "past_performances": [{"date": "2026-09-13", "track_code": "CD",
                                "race_number": 2, "finish_position": 1,
                                "field_size": 5, ...}, ...]}
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
    a year, and is NOT lengths-consistent.  It is not read.

Only what the model needs is read (see "Output: only the essentials"), plus a few
fields the checks in check_parser.py use (pages, the lifetime record, the printed
distance and final time, ...). Other columns -- jockey, weight, equipment, odds, the
top three finishers -- are still located on the line, because they decide where the
neighbouring fields end, but their values are not kept.

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
# The symbol fonts print digits as other characters; this maps them back.
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

# Fractions of a length (½, ¼, ¾) and the short margins: head, neck, nose
GLYPH_FRACTIONS = {"¡": 0.5, "¢": 0.25, "£": 0.75}
GLYPH_MARGINS = {"«¬": ("hd", 0.1), "¨μ": ("nk", 0.25), "¨©": ("no", 0.05)}

# Every character that can be part of a beaten-lengths margin
LENGTH_CHARS = "".join(GLYPH_DIGITS) + "".join(GLYPH_FRACTIONS) + "«¬¨μ©"

# Distance tokens in the past-performance lines.  The second symbol font prints the
# fraction part as a letter: "6K" = 6 1/2 furlongs, "1B" = 1 1/16 miles.  The letters were
# decoded from the data (2026-11-16): the median final time of every token across four
# Keeneland programs, divided by ~11.9 s per furlong, gives an ordered ladder
# B 8.5f < N 9f < C 9.5f < L 10f < D 10.5f < O 11f < K 12f < M 14f, i.e. sixteenths of a
# mile. ("*1M" is also printed for the San Juan Capistrano, a race at about 1 3/4 miles.)
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
# Downs; its 1:40.26 final time sits between the mile and 1 mile 70 yard medians).
SUPERSCRIPT_DIGITS = {chr(0xC0 + i): str(i) for i in range(10)}


# --------------------------------------------------------------------------
# Lookup tables: tracks, course symbols, medication, number words, months
# --------------------------------------------------------------------------
# Track codes used in the past-performance lines (extend freely).
# Only the card's own track name is written ("Kee" -> "Keeneland").
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

# Course symbols printed after the track condition, keyed by (symbol font, character).
# Meanings from the program's own "COURSE SYMBOLS" legend (on the "How to read the
# program" page), matched by rendering each glyph from the PP lines and comparing:
#   p = circled T (main turf), h = boxed T (inner turf), y = diamond + (downhill turf),
#   u = circled X (off turf: moved from the grass to the main track, so run on dirt),
#   o in the third font = diamond A (all-weather / synthetic track).
# "o" in the second font prints as a box with a dot. It is not in the legend (the
# legend's "inner dirt" is a circle with a dot). It only appears at Colonial, Aqueduct
# and Belmont-at-Aqueduct, only with turf conditions (fm/gd), and all three tracks have
# an inner turf course, so it is read as inner turf (unconfirmed).
COURSE_SYMBOLS = {
    ("2", "p"): ("turf", "main turf"),
    ("2", "h"): ("turf", "inner turf"),
    ("2", "o"): ("turf", "inner turf (box-dot symbol)"),
    ("2", "y"): ("turf", "downhill turf"),
    ("2", "u"): ("dirt", "off turf"),
    ("3", "o"): ("synthetic", "all weather"),
}


# The key a span has in COURSE_SYMBOLS: which symbol font, and which character
def course_symbol_key(s):
    """(symbol font, character) for a span, e.g. ("2", "p"); the font's last character
    tells the NorthernDancer symbol fonts apart (...Narrow2, ...Dancer3)."""
    return (s.font[-1:], s.t) if s.symbol else (None, s.t)


# Medication printed after the horse's name, e.g. "(L)" (first-time Lasix may add
# a digit). It is dropped from the name.
MEDICATION_RE = re.compile(r"^\([LB]+\d?\)$")

# Number words in race distances ("Six Furlongs", "One Mile And One Sixteenth")
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

# "Aug" -> 8, for the dates printed on PP lines ("16Aug26")
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


# --------------------------------------------------------------------------
# Column layout of a past-performance line
# --------------------------------------------------------------------------
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
# Keeneland PPs), as (x0 where the column starts, is it printed bold, text shape).
# These are the stops shared by at least a quarter of all PP lines, measured with
# column_profile() on 10-16-26-kee-online-programpdf.pdf.
# "Shape" is the text with digit runs -> 9, letter runs -> a and running-line glyph
# runs -> g, e.g. "-58" -> "-9", "16Aug26" -> "9a9", ":22¼¸" -> ":9g".
# Each new PDF measures its own profile and is aligned to this one (see
# learn_column_shift), so a layout that adds, drops or moves a column still lands in COL.
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
# Small helpers: decoding numbers, times, margins and distances
# --------------------------------------------------------------------------
# Read a printed number; None when it is not a number
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


# Turn symbol-font digit glyphs back into digits
def decode_glyph_number(text):
    """'¼¼' -> '66', '^¾' -> '08'; unknown characters are dropped."""
    return "".join(GLYPH_DIGITS.get(c, "") for c in text)


# Turn a printed time (hundredths in glyphs) into a plain time string
def decode_time(token):
    """':22¼¼' -> '22.66', '1:11¾¿' -> '1:11.89', ':49' -> '49.00'."""
    m = re.match(r"^(\d*:\d{2})(.*)$", token.strip())
    if not m:
        return token.strip()
    base, hundredths = m.group(1), decode_glyph_number(m.group(2))
    base = base.lstrip(":")
    return f"{base}.{hundredths.ljust(2, '0')[:2]}"


# "1:11.89" -> 71.89 seconds
def time_to_seconds(text):
    if not text:
        return None
    m = re.match(r"^(?:(\d+):)?(\d{1,2})\.(\d{2})$", text)
    if not m:
        return None
    minutes = int(m.group(1) or 0)
    return round(minutes * 60 + int(m.group(2)) + int(m.group(3)) / 100, 2)


# Turn margin glyphs into lengths, as text and as a number
def decode_margin(glyphs):
    """Lengths glyphs -> (text, float).
    '¶¾£' -> ('18 3/4', 18.75); '«¬' -> ('hd', 0.1)."""
    g = glyphs.strip()
    if not g:
        return "", 0.0

    # Head / neck / nose
    if g in GLYPH_MARGINS:
        return GLYPH_MARGINS[g]

    # Whole lengths (digit glyphs) plus a fraction glyph
    whole = decode_glyph_number("".join(c for c in g if c in GLYPH_DIGITS))
    frac = sum(GLYPH_FRACTIONS[c] for c in g if c in GLYPH_FRACTIONS)
    value = (int(whole) if whole else 0) + frac
    frac_text = {0.5: "1/2", 0.25: "1/4", 0.75: "3/4"}.get(frac, "")
    text = f"{whole or ('0' if not frac_text else '')} {frac_text}".strip()
    return text, value


# A race distance written in words (race header) -> furlongs
def words_to_furlongs(text):
    """'Six Furlongs' -> 6.0, 'Eight And One Half Furlongs' -> 8.5,
    'One Mile And One Sixteenth' -> 8.5, 'One Mile And Seventy Yards' -> 8.32."""
    if not text:
        return None
    t = text.lower()

    # "<number> [and one half / one quarter ...] furlongs"
    m = re.search(
        r"\b((?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
        r"(?:\s+and\s+(?:one|three)\s+(?:half|quarter|eighth|sixteenth))?)\s+furlongs?\b",
        t,
    )

    # Course chutes add a measured extra ("Seven Furlongs and 184 Feet", Keeneland's
    # Beard Course).
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

    # "<one|two> mile(s) [and one sixteenth / seventy yards ...]" (1 mile = 8 furlongs)
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

    # Digits: "6.5 furlongs"
    m = re.search(r"(\d+(?:\.\d+)?)\s*furlongs?", t)
    if m:
        return float(m.group(1))
    return None


# A distance token from a PP line ("6K", "1B", "1ÇÀ") -> furlongs
def line_distance_to_furlongs(token):
    """Past-performance distance token -> furlongs. '6f' -> 6, '1m' -> 8, '6K' -> 6.5,
    '1B' -> 8.5, '1N' -> 9, '1ÇÀ' -> 8.32 (1 mile 70 yards), '*7f' -> 7 (a leading
    '*' means "about" that distance)."""
    t = token.replace(" ", "").lstrip("*")

    # Miles + yards in superscript digits
    ym = re.match("^(\\d+)([" + "".join(SUPERSCRIPT_DIGITS) + "]+)$", t)
    if ym:
        yards = int("".join(SUPERSCRIPT_DIGITS[c] for c in ym.group(2)))
        return round(int(ym.group(1)) * 8 + yards / 220, 3)

    # Number, optional unit (f / m), optional fraction letter
    m = re.match(r"^(\d+)([fm]?)([A-Z]?)$", t)
    if not m:
        return None
    value = int(m.group(1))
    unit, frac = m.group(2), m.group(3)
    if unit == "m":
        value *= 8
    elif frac and frac in DIST_FRACTION_GLYPHS and value <= 2 and unit == "":
        # "1B" = one mile + 1/16 mile
        value = value * 8 + DIST_FRACTION_GLYPHS[frac] * 8
        return round(float(value), 3)
    if frac in DIST_FRACTION_GLYPHS:
        value += DIST_FRACTION_GLYPHS[frac]
    return float(value)


# --------------------------------------------------------------------------
# Text spans: the pieces of text PyMuPDF reads from a page
# --------------------------------------------------------------------------
# Characters that different PDFs extract differently for the SAME printed glyph. The neck
# margin "¨μ" comes out with GREEK SMALL MU (U+03BC) in some files and MICRO SIGN (U+00B5)
# in others (10-17-26); fold them to one spelling so the glyph tables only need one entry.
LOOKALIKE_CHARS = str.maketrans({"µ": "μ"})


# One run of text in one font: its text, position box, size, font and character boxes
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
        "symbol",
        "chars",
        "base",
    )

    # Build a Span from one of PyMuPDF's span dicts
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

        # Font: size, bold, and whether it is one of the NorthernDancer symbol fonts
        self.size = round(s["size"], 1)
        self.font = s["font"]
        self.bold = "Bold" in self.font or ",Bo" in self.font or bool(s["flags"] & 16)
        self.symbol = "NorthernDancer" in self.font

    # The text without surrounding spaces
    @property
    def t(self):
        return self.text.strip()

    # A copy moved sideways (used to map columns onto the reference layout)
    def shifted(self, dx):
        """Copy of this span moved dx points to the right."""
        out = object.__new__(Span)
        for k in Span.__slots__:
            setattr(out, k, getattr(self, k))
        out.x0, out.x1 = self.x0 + dx, self.x1 + dx
        out.chars = [(c, a + dx, b + dx) for c, a, b in self.chars]
        return out

    # A copy with different text
    def with_text(self, text):
        """Copy of this span with different text (same position and font)."""
        out = self.shifted(0)
        out.text = text
        return out

    # A copy holding only part of the characters (used to cut a run-on span)
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

    # How a span prints when debugging, e.g. <120,24 7.4 B '16Aug26'>
    def __repr__(self):  # pragma: no cover - debugging aid
        bold = "B" if self.bold else ""
        return f"<{self.y0:.0f},{self.x0:.0f} {self.size} {bold} {self.t!r}>"


# Read every piece of text on a page as Spans, in reading order
def page_spans(page, layout=None):
    """All non-blank text spans on a page, sorted top-to-bottom, left-to-right.

    ``layout`` (a PageLayout from find_pp_pages) maps the page's coordinates back into
    the frame of the standalone PP sheet, so every x-window / font-size test below works
    unchanged on a full race-day program where the PP pages are printed smaller.
    """
    out = []

    # "rawdict" gives every character's box, so a span's left edge can be taken from its
    # first VISIBLE character. Spans often begin with a space (" Moc 62500", " e");
    # counting the space put their left edge 1-2pt early, i.e. in the previous column.
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

                # In a program: skip its own header / page number, and rescale the rest
                if layout is not None:
                    if any(f in s["font"] for f in PROGRAM_CHROME_FONTS):
                        continue
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


# One PP page, and how to map its coordinates back to the standalone sheet's
class PageLayout:
    """One detected PP page and how to map its coordinates back to the PP-sheet frame."""

    # Remember the page index, footer text, print scale and offsets
    def __init__(self, index, footer_text, scale, dx, dy):
        self.index = index  # 0-based page index in the PDF
        self.footer_text = footer_text
        self.scale = scale
        self.dx = dx
        self.dy = dy

    # Undo the program's shrink and shift on one span: x = (x' - dx) / scale
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

    # How a page layout prints when debugging
    def __repr__(self):  # pragma: no cover - debugging aid
        return (
            f"<page {self.index + 1} {self.footer_text!r} "
            f"scale={self.scale:.3f} dx={self.dx:.2f}>"
        )


# All non-blank spans of a page as PyMuPDF's plain dicts (quick, no character boxes)
def _raw_spans(page):
    return [
        s
        for b in page.get_text("dict")["blocks"]
        for line in b.get("lines", [])
        for s in line["spans"]
        if s["text"].strip()
    ]


# Find the PP pages of a PDF and measure how each one is scaled and shifted
def find_pp_pages(doc):
    """Return a PageLayout for every past-performance page of ``doc``, in page order.

    The print scale is measured once per document from the 8pt field labels (it is
    1.0 for the standalone sheet, 0.92 for the 2025 and 2026 programs). The x shift
    is measured per page from the footer, whose left edge is always x=18 on the sheet.
    The y shift only matters for the "top of page" header band in parse_race_header,
    so it is set equal to the x shift (the program's top and left margins measured the
    same, about 10-11pt).
    """

    # PP pages: the Equibase footer plus glyph-font text. Collect their label sizes.
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

    # Print scale: the median label size compared with the sheet's 8pt
    label_sizes.sort()
    scale = label_sizes[len(label_sizes) // 2] / PP_LABEL_SIZE if label_sizes else 1.0
    if abs(scale - 1.0) < 0.005:
        scale = 1.0  # standalone sheet: keep coordinates exactly as printed

    # Per-page shift: where the footer starts compared with the sheet's x=18
    layouts = []
    for i, footer in candidates:
        dx = footer["bbox"][0] - scale * PP_FOOTER_X
        if abs(dx) < 0.05:
            dx = 0.0
        layouts.append(
            PageLayout(i, FOOTER_RE.search(footer["text"]).group(0), scale, dx, dx)
        )
    return layouts


# --------------------------------------------------------------------------
# Rows and columns: grouping spans into printed lines and PP-line columns
# --------------------------------------------------------------------------
# Group spans into the printed lines they belong to
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


# Join spans into one string, with a space only where there is a visible gap
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


# Is this span a lone number?
def is_number_span(s):
    return NUMBER_RE.match(s.t) is not None


# Which PP-line column (a COL name) a span belongs to
def column_of(s):
    """The COL column a span belongs to.

    Text is printed left-aligned, so a text span belongs where it STARTS -- important for
    spans that run on across several columns ("M 115» bL" starts in the jockey column).
    Numbers are printed right-aligned, so a lone number belongs to the column it overlaps
    MOST: a 3-digit figure ("103") starts ~3pt left of where 2-digit figures start, and
    its left edge pokes into the previous column although nearly all of it is in its own.
    """
    return _column_at(s.t, s.x0, s.x1)


# The column for some text at x0..x1 (see column_of for the rules)
def _column_at(text, x0, x1):

    # A lone number: the column it overlaps most
    if NUMBER_RE.match(text.strip()):
        best, best_overlap = None, 0.0
        for name, (lo, hi) in COL.items():
            overlap = min(x1, hi) - max(x0, lo)
            if overlap > best_overlap:
                best, best_overlap = name, overlap
        if best is not None:
            return best

    # Text: the column it starts in
    return next((name for name, (lo, hi) in COL.items() if lo <= x0 < hi), None)


# Cut spans that run on from one column into the next
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

        # Split the span into words: runs of non-space characters with their boxes
        words, cur = [], []
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

        # Cut before a word when a column stop lies in the gap before it and the word
        # looks like that column. 1pt of slack: a right-aligned number may start just
        # before its stop.
        pieces, start = [], 0
        for prev, w in zip(words, words[1:]):
            gap_lo, gap_hi = prev[-1][2], w[0][1]
            shape = text_shape("".join(ch[0] for ch in w))
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
# Recognising a past-performance line
# --------------------------------------------------------------------------
# A PP line starts with its date: "16Aug26" (the race number can be glued on)
DATE_RE = re.compile(r"^(\d{2})([A-Z][a-z]{2})(\d{2})(.*)$")


# Is this row a PP line? (it starts at the left margin with a date in the glyph font)
def is_pp_line(row):
    first = row[0]
    return first.symbol and first.x0 < 30 and DATE_RE.match(first.t) is not None


# --------------------------------------------------------------------------
# Adapting to column layout changes
# --------------------------------------------------------------------------
# COL is tuned on one layout. Rather than keeping a COL table per year, every PDF is
# mapped onto that one layout:
#   1. measure the PDF's own column profile: the x0 values shared by >= 25% of its PP
#      lines, each with how it is printed (bold or not, text shape) -- see
#      REFERENCE_COLUMNS;
#   2. line that profile up with REFERENCE_COLUMNS using sequence alignment (as in
#      `diff`): either side may have columns the other lacks, matched columns should look
#      alike, and the offset should stay steady, because a whole block of columns moves
#      together when one is added or removed;
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


# Reduce text to its "shape", so columns can be compared by how they look
def text_shape(text):
    """'-58' -> '-9', '16Aug26' -> '9a9', ':22¼¸' -> ':9g',
    'Clm 20000nw2/L' -> 'a 9a9/a'."""
    t = re.sub(r"\d+", "9", text.strip())
    t = re.sub(r"[A-Za-z]+", "a", t)
    t = re.sub("[" + re.escape(LENGTH_CHARS) + "]+", "g", t)
    return re.sub(r"\s+", " ", t)


# Measure where this PDF's PP-line columns start, and how each one looks
def column_profile(rows):
    """[(x0, bold 0/1, most common text shape)] for every x0 (rounded) where at least
    COLUMN_STOP_SHARE of the PP-line rows have a span."""

    # Per x position: how many lines have a span there, and the spans themselves
    spans_at = defaultdict(list)
    lines_at = defaultdict(int)
    for row in rows:
        for x in {round(s.x0) for s in row}:
            lines_at[x] += 1
        for s in row:
            spans_at[round(s.x0)].append(s)

    # Keep the common positions, each with its usual boldness and shape
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


# Pair this PDF's columns with the reference columns (cheapest matching)
def align_columns(ref, doc):
    """Match doc columns to reference columns: [(doc_x, ref_x), ...] left to right.

    Dynamic programming over matched pairs. Matching ref[i] with doc[j] right after the
    previous match (i', j') costs: |change in offset| + COLUMN_STYLE_COST per differing
    style (bold, shape) + COLUMN_SKIP_COST per column skipped on either side in between.
    The path starts from a virtual match at the left edge with offset 0.
    """

    # Extra cost when two matched columns look different (bold, shape)
    def style(i, j):
        return COLUMN_STYLE_COST * ((ref[i][1] != doc[j][1]) + (ref[i][2] != doc[j][2]))

    # best[(i, j)]: cheapest cost of a matching that ends with ref[i] <-> doc[j];
    # back[(i, j)]: the match before it on that path
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

    # Cheapest end point (columns left over on the right also count as skipped),
    # then walk back to the start to read off the matched pairs
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


# Moves a PP line's spans from this PDF's column positions onto the reference's
class ColumnShift:
    """Moves the spans of a PP line from this PDF's column positions onto the
    reference's."""

    # pairs: the matched (this PDF's x, reference x) columns from align_columns
    def __init__(self, pairs, columns=None):
        self.pairs = pairs  # [(doc_x, ref_x)], left to right
        self.identity = all(d == r for d, r in pairs)
        # this PDF's own column profile; without a learned one, assume the reference's
        self.columns = columns or list(REFERENCE_COLUMNS)

    # How far to move something at x
    def offset_at(self, x):
        # A span takes the offset of the closest column stop at or left of x (1pt of slack
        # for rounding); spans left of every stop use the first stop's offset.
        d, r = self.pairs[0]
        for doc_x, ref_x in self.pairs:
            if doc_x <= x + 1.0:
                d, r = doc_x, ref_x
        return r - d

    # Move every span of a row
    def apply(self, row):
        if self.identity:
            return row
        # Text is judged by its left edge, a lone number by its centre: right-aligned
        # 3-digit figures start left of their column's stop (see column_of).
        return [
            s.shifted(self.offset_at((s.x0 + s.x1) / 2 if is_number_span(s) else s.x0))
            for s in row
        ]


# Measure this PDF's columns and match them to the reference layout
def learn_column_shift(rows):
    """Learn a ColumnShift from all PP-line rows of one PDF (see the section comment)."""
    rows = [r for r in rows if is_pp_line(r)]
    if len(rows) < MIN_LINES_TO_LEARN:
        return ColumnShift([(0, 0)])
    profile = column_profile(rows)
    pairs = align_columns(REFERENCE_COLUMNS, profile)
    return ColumnShift(pairs or [(0, 0)], profile)


# --------------------------------------------------------------------------
# Reading one past-performance line
# --------------------------------------------------------------------------
# One PP line -> dict, decoded left to right by the helpers below
def parse_pp_line(row, shift=None):
    """One past-performance line -> dict. Each helper below decodes one stretch of the
    line, left to right, and adds its fields to ``out``."""
    by = _line_columns(row, shift)
    out = {}
    foreign = _parse_date_and_track(by, out)
    _parse_condition_and_course(by, out)
    _parse_distance_and_times(by, out)
    _parse_class(by, out)
    _parse_figures(by, out)
    jockey_match, stray, steward_mark = _parse_running_line(by, out, foreign)
    tail = _text_after_odds(by, jockey_match, stray)
    _apply_stewards_change(by, out, tail, steward_mark)
    out["field_size"] = _num(by["field"][0].t) if by["field"] else None
    return out


# Sort a PP line's spans into its columns: {"date": [...], "calls": [...], ...}
def _line_columns(row, shift):
    """Spans of a PP line grouped by COL column name."""

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
    return by


# Date, race number and track code of the earlier race
def _parse_date_and_track(by, out):
    """Date, race number and track. Returns whether the race was run abroad."""
    m = DATE_RE.match(by["date"][0].t)
    out["date"] = f"20{m.group(3)}-{MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}"
    out["date_text"] = by["date"][0].t[:7]  # as printed ("16Aug26"), for the checks

    # The race number is occasionally glued onto the date span ("19Sep2611 CD").
    # A symbol-font glyph in front of the track marks a foreign race (no race number).
    foreign = any(s.font.endswith("2") for s in by["race_track"])
    track_spans = [s for s in by["race_track"] if not s.font.endswith("2")]
    rt = (m.group(4).strip() + " " + row_text(track_spans)).strip()
    m = re.match(r"^(\d+)\s*(\S+)$", rt)
    out["race_number"] = int(m.group(1)) if m else None
    out["track_code"] = m.group(2) if m else rt

    # Foreign tracks carry their country in small print: "Dun(IRE)" -> "Dun".
    cm = re.match(r"^(.*?)\(([A-Z]{2,3})\)$", out["track_code"])
    if cm:
        out["track_code"] = cm.group(1)
    out["foreign"] = foreign  # foreign lines print no US figures (the checks allow it)
    return foreign


# Is this a symbol in the condition column that the parser does not know?
def _is_unrecognized_symbol(s):
    """A symbol-font mark in the condition column that is neither a known course symbol,
    a rail-setting digit, nor (in the regular glyph font) part of the condition text."""
    return (
        s.symbol
        and course_symbol_key(s) not in COURSE_SYMBOLS
        and not all(c in GLYPH_DIGITS for c in s.t)
        and not (s.t.isalpha() and s.t.islower() and s.font[-1:] not in "23")
    )


# Surface (dirt / turf / synthetic) of the earlier race
def _parse_condition_and_course(by, out):
    """Surface, from the course symbol (see COURSE_SYMBOLS) or the track condition
    ("ft", "fm")."""
    cond_tokens = list(by["condition"])

    # Track condition text ("ft", "fm", ...) and the first known course symbol
    cond = "".join(
        s.t
        for s in cond_tokens
        if not s.font.endswith("2") and s.t.isalpha() and len(s.t) >= 2
    )
    keys = (course_symbol_key(s) for s in cond_tokens)
    symbol = next((COURSE_SYMBOLS[k] for k in keys if k in COURSE_SYMBOLS), None)

    # Unknown marks are recorded (not guessed at) so check_parser.py can report them.
    out["unrecognized_symbols"] = [
        f"{s.font[-1:]}:{s.t}" for s in cond_tokens if _is_unrecognized_symbol(s)
    ]

    # No symbol means the main (dirt) track; turf-only conditions still mean turf.
    turf_conditions = ("fm", "yl", "sf", "hd")
    out["surface"] = (
        symbol[0] if symbol else ("turf" if cond in turf_conditions else "dirt")
    )


# Distance of the earlier race, and its final time
def _parse_distance_and_times(by, out):
    """Distance, and the final time (used by the checks to test the distance)."""

    # A time always contains ":". In marathon races the first fraction ("1:38") is wide
    # enough to start inside the distance column, so route such spans to the fractions.
    timed = [s for s in by["distance"] if ":" in s.t]
    dist_raw = "".join(s.t for s in by["distance"] if s not in timed)
    out["distance"] = dist_raw  # as printed, for the checks
    out["distance_f"] = line_distance_to_furlongs(dist_raw)

    # The fractional times are followed by the final time; only the last one is kept.
    frac_text = "".join(s.text for s in timed + by["fractions"]).replace(" ", "")
    times = [decode_time(t) for t in re.findall(r"\d*:\d{2}[^\d:]{0,2}", frac_text)]
    out["final_time"] = times[-1] if times else None
    out["final_time_seconds"] = time_to_seconds(out["final_time"])


# Race class text and the purse printed in it
def _parse_class(by, out):
    """Race class ("Clm 20000nw2/L", "Stk - ... - 175k") and the purse in it."""
    out["race_class"] = row_text(by["class"]) or None
    out["purse"] = class_purse(out["race_class"] or "")


# Early pace figure and speed figure
def _parse_figures(by, out):
    """Early pace figure and speed figure."""

    # The bold figure in front of the pair (a newer column, not used) is told apart
    # from the early pace figure by weight: it is bold, the pace figure is not.
    fig_spans = by["bold"] + by["early"]
    early_sp = [s for s in fig_spans if not s.bold]
    out["early_pace"] = _num(early_sp[0].t.strip("-")) if early_sp else None

    # The speed figure is printed as "-65"; a two-digit post position ("12") can start
    # inside the speed column, so pick the speed figure by content rather than by x.
    speed_tokens = [s.t for s in by["speed"]]
    spd = next(
        (tk for tk in speed_tokens if tk.startswith("-") or not tk.isdigit()), None
    )

    # Foreign lines print a text rating instead ("T-86"): no speed figure.
    numeric = spd and not spd[0].isalpha()
    out["speed_figure"] = _num(re.sub(r"[^\d]", "", spd)) if numeric else None


# The running line ("1¹ 2¡ 3* --") -> a list of calls (position + margin)
def _decode_calls(calls_text):
    """'1¹ 2¡ 3* -- ' -> ([{position, margin, margin_text}, ...], stewards' mark seen)."""
    calls = []
    steward_mark = False
    glyphs = re.escape(LENGTH_CHARS)
    for pos, g, star in re.findall(r"(\d{1,2}|--)([" + glyphs + r"]*)(\*?)", calls_text):
        steward_mark = steward_mark or bool(star)

        # "--": no position at that call (e.g. the horse did not finish)
        if pos == "--":
            calls.append({"position": None, "margin": None, "margin_text": None})
            continue

        text, value = decode_margin(g)
        calls.append({"position": int(pos), "margin": value, "margin_text": text or None})
    return calls, steward_mark


# Finish, beaten lengths, stretch and first-call positions from the calls
def _set_positions(out, calls):
    """Finish, beaten lengths, stretch and first-call positions from the calls.
    Always present (None when not printed): a horse that did not finish ("-- --" at the
    end) still has its early calls, which the converter uses for early-pace estimates."""
    out.update(
        finish_position=None,
        lengths_behind=None,
        stretch_position=None,
        first_call_position=None,
    )

    # The last call is the finish
    if calls and calls[-1]["position"] is not None:
        fin = calls[-1]
        out["finish_position"] = fin["position"]
        # A winner's margin is how far it WON by, so a winner is 0 lengths behind.
        out["lengths_behind"] = 0.0 if fin["position"] == 1 else fin["margin"]

    # The one before it is the stretch call; the first call depends on the race type
    if len(calls) >= 2:
        out["stretch_position"] = calls[-2]["position"]
        # Sprints print a start position first (no margin), so the first call is the
        # next entry -- which can itself be "--" when the horse was pulled up early
        # ("8 -- -- -- --"). Routes print no start position.
        valid = [c for c in calls if c["position"] is not None]
        sprint_start = len(calls) >= 5 and calls[0]["margin_text"] is None
        first = calls[1] if sprint_start else (valid[0] if valid else None)
        out["first_call_position"] = first["position"] if first else None


# Positions from the running line; also finds where the jockey / weight part ends
def _parse_running_line(by, out, foreign):
    """Positions and beaten lengths from the calls. The jockey and weight that follow
    are matched only to find where the calls end and the equipment begins.
    Returns (jockey/weight regex match, stray '0' spans, stewards' mark seen)."""

    # Running line, jockey and weight share one region: the jockey name is sometimes
    # glued to the last call ("12º¸¡Asmussen K J") and the weight to the jockey
    # ("Hernandez J J 122"). A '0' glyph ('^') belonging to a 10 lb apprentice allowance
    # can land in the equipment column.
    stray = [
        s for s in by["equipment"] if s.font.endswith("2") and s.t == "^" and s.x0 < 400
    ]
    region = row_text(by["calls"] + by["jockey"] + by["weight"] + stray)
    glyphs = re.escape(LENGTH_CHARS)
    region = re.sub(r"\s+(?=[" + glyphs + r"])", "", region)  # re-attach stray glyphs

    # Split the region into the calls and the rest.
    # A "*" after the finish ("1¹*") marks a result changed by the stewards; the comment
    # then says "DQ3rd" (disqualified, placed 3rd) or "PL2nd" (moved up to 2nd).
    cm = re.match(
        r"^(?P<calls>(?:(?:\d{1,2}[" + glyphs + r"]*\*?|--)\s*)*)(?P<rest>.*)$", region
    )
    calls, steward_mark = _decode_calls(cm.group("calls"))
    if foreign and len(calls) == 2 and calls[0]["margin_text"] is None:
        # Foreign lines only print the post position and the finish: drop the post.
        calls = calls[1:]

    # Jockey, weight (+ apprentice allowance glyphs), and equipment letters glued on.
    jm = re.match(
        r"^(?P<jockey>.*?)\s*(?:(?P<weight>\d{3})(?P<bug>[" + glyphs + r"]*))?"
        r"\s*(?P<equip>[A-Za-z]*)\s*$",
        cm.group("rest"),
    )

    _set_positions(out, calls)
    return jm, stray, steward_mark


# The text after the odds: the top three finishers and the comment
def _text_after_odds(by, jockey_match, stray):
    """The finishers-and-comment text that follows the odds. The equipment and odds
    are not kept; the odds ("*2.10") are only found to know where that text starts,
    because some PDFs store the first finisher in the same span as the odds."""

    # Equipment + odds text, then find the odds in it
    eo = row_text([s for s in by["equipment"] if s not in stray] + by["odds"])
    if jockey_match and jockey_match.group("equip"):
        # equipment glued to an apprentice weight ("119» fL")
        eo = jockey_match.group("equip") + " " + eo
    om = re.search(r"(\*?)(\d*\.\d+)", eo)

    # What can follow the odds:
    #   "®"  -- prints as a superscript "e": coupled entry;
    #   a lone 3-4 digit number ("995", "1074") -- only on lines from Santa Anita, Lone
    #         Star, Remington, Laurel and Golden Gate, range ~920-1260 and steady for one
    #         horse (995, 1010, 1010, 999, 1008): the horse's body weight in pounds on
    #         race day, which those tracks publish (confirmed with the user 2026-11-16);
    #   anything else -- the finishers, when the PDF stored them in the same span as
    #         the odds.
    tail = eo[om.end() :].strip() if om else ""
    tail = tail.lstrip("®").strip()
    tm = re.match(r"^(\d{3,4})\b\s*(.*)$", tail)
    return tm.group(2) if tm else tail


# Use the official finish when the stewards changed the result (DQ / PL)
def _apply_stewards_change(by, out, tail, steward_mark):
    """Official finish when the stewards changed the result: a "*" after the finish
    in the calls, and "DQ3rd" / "PL2nd" in the comment after the top three finishers.
    finish_position becomes the official place."""

    # A trouble icon -- a "w" in a third symbol font -- is printed in front of trouble
    # comments ("stmbld st", "off slow", "chckd turn"). Keep it out of the text: glued to
    # the third finisher's margin it would swallow the comment's first word
    # ("124¡woff slow").
    small = [s for s in by["finishers"] if "NorthernDancer3" not in s.font]

    # NOTE: a "^" here (second symbol font) is the digit 0 of a 10+ length margin
    # ("Holly122¶^" = 10 lengths, "¶^£" = 10 3/4), so it stays part of the text.
    # An "n" in the second symbol font prints as a boxed "DH" (dead heat) in front of a
    # finisher's name. Turn it into a separate marker so it does not glue the name to
    # the previous finisher's margin, which would shift where the comment starts.
    small = [
        s.with_text(" " + DEAD_HEAT_MARK) if s.font.endswith("2") and s.t == "n" else s
        for s in small
    ]

    # Look for "DQ3rd" / "PL2nd" in the comment
    comment = comment_after_finishers((tail + " " + row_text(small)).strip())
    sm = re.search(r"\b(DQ|PL)(\d{1,2})(?:st|nd|rd|th)\b", comment or "")
    # "?" = the "*" is printed but the comment names no DQ/PL (reported by the checks).
    out["stewards_change"] = (sm.group(1) if sm else "?") if steward_mark else None
    if steward_mark and sm:
        out["finish_position"] = int(sm.group(2))


# The purse printed in a race class, if that class prints one
def class_purse(text):
    """The purse printed in a race class, or None.
    'Alw 100000nw1/x' -> 100000; 'Stk - FuturityG3 - 175k' -> 175000.
    Only Msw/Alw/Stk/Hcp print a purse. For claiming classes (Clm, Mcl) and optional /
    starter claiming classes (Aoc, Moc, Str, SOC) the number is a claiming price:
    10-10-26 R8 is "Allowance Optional Claiming $80,000" with a $130,000 purse, and
    its PP class prints "Aoc 80000"."""
    t = text.strip()

    # Stakes-style class with the purse in thousands at the end
    sm = re.match(
        r"^[a-z]?(?:Stk|Hcp|Msw|Alw|Aoc|Clm|Mcl)\s*-\s*.+?\s*-\s*(?P<purse>\d+)k\s*$", t
    )
    if sm:
        return int(sm.group("purse")) * 1000

    # "<class> <number>": the number is a purse only for these classes
    cm = re.match(r"^(?P<kind>[A-Za-z]+)\s*(?P<value>\d+)?", t)
    if cm and cm.group("value") and cm.group("kind") in ("Msw", "Alw", "Hcp", "Stk"):
        return int(cm.group("value"))
    return None


DEAD_HEAT_MARK = "‡"  # stands in for the boxed "DH" glyph while splitting finishers
# Three finishers, each "Name" + 3-digit weight + margin glyphs, then the comment.
FINISHER_RE = re.compile(
    r"^\s*(?P<a>.+?\d{3}[^\s\d]*)\s+(?P<b>.+?\d{3}[^\s\d]*)"
    r"\s+(?P<c>.+?\d{3}[^\s\d]*)\s*(?P<comment>.*)$"
)


# The comment that follows the top three finishers
def comment_after_finishers(text):
    """'Sudden Squall122½ Legit Chick122£ C V Holly122¶^ 3p,rally' -> '3p,rally'.
    The whole text when the three finishers cannot be found; None when empty."""
    if not text:
        return None
    m = FINISHER_RE.match(text)
    if not m:
        return text
    return m.group("comment").strip() or None


# --------------------------------------------------------------------------
# Horse block
# --------------------------------------------------------------------------
# One horse's block (header, labelled lines, record table, PP lines) -> dict
def parse_horse(spans, shift=None):
    """spans: all spans of one horse block (program number row .. before the next).
    shift: the PDF's ColumnShift, applied to its past-performance lines."""
    rows = group_rows(spans)

    # The program is printed before scratches are known, so nothing here is ever
    # scratched; the field is kept because the converter reads it.
    horse = {"scratched": False}

    _parse_horse_header(spans, horse)
    _parse_labelled_lines(rows, horse)
    _parse_horse_name(spans, horse)

    # Only the lifetime starts (a check) and the track code (the card's track) are used.
    horse["records"] = parse_record_table(rows)

    horse["past_performances"] = _parse_pp_lines(rows, shift)
    set_figure_summaries(horse)
    return horse


# Program number, also-eligible marker and morning-line odds
def _parse_horse_header(spans, horse):
    """Program number, also-eligible marker and morning line."""
    prog = next((s for s in spans if s.bold and s.size >= 20 and s.x0 < 60), None)
    horse["program_number"] = _num(prog.t) if prog else None

    # Also-eligible horses have a stacked bold "A" / "E" (12pt) in the left margin
    # beside the program number (62 of them in the five test programs, nothing else
    # bold prints there).
    margin = {s.t for s in spans if s.bold and s.x0 < 30 and 10 <= s.size <= 14}
    horse["ae_marker"] = {"A", "E"} <= margin

    # Morning line: bold, 13-15pt, just right of the program number
    odds = next(
        (s for s in spans if s.bold and 13 <= s.size <= 15 and 50 <= s.x0 <= 90), None
    )
    horse["morning_line_odds"] = odds.t if odds else None


# Trainer, and the class rating printed on the Owner row
def _parse_labelled_lines(rows, horse):
    """The Trainer line, and the Class rating printed on the Owner row."""
    for y, row in rows:
        labels = [
            s for s in row if s.bold and s.t in ("Owner:", "Trainer:") and s.x0 < 100
        ]
        if not labels:
            continue
        label = labels[0]

        # Trainer row
        if label.t == "Trainer:":
            value = row_text(
                [s for s in row if s.x0 > label.x0 and not s.bold and s.x0 < 380]
            )
            # "Brad H. Cox ( 12-3-2-1 ) 25.00%" -> "Brad H. Cox". Some programs print
            # "Owner" here: the owner trains the horse.
            horse["trainer"] = re.sub(r"\s*\(.*$", "", value).strip() or None

        # Owner row: the class rating is the bold number at its right end
        else:
            cls = next((s for s in row if s.bold and 375 <= s.x0 <= 412), None)
            horse["class_rating"] = _num(cls.t) if cls else None
            horse["class_rating_text"] = cls.t if cls else None  # for the checks


# The horse's name
def _parse_horse_name(spans, horse):
    """Name, without a medication code."""
    # Name row: the ArialBlack text left of the weight. Long names are printed smaller
    # (12pt normally, 8.5pt for "Never Gonna Stop (IRE)"), so no size limit is applied.
    # In parentheses: a medication code from the race legend ("(L)" Lasix, "(B)" Bute),
    # which is dropped, or the country of breeding, which is part of the registered
    # name ("Never Gonna Stop (IRE)") and kept.
    name_spans = [s for s in spans if "ArialBlack" in s.font and s.x0 < 290]
    meds = [s for s in name_spans if MEDICATION_RE.match(s.t)]
    horse["name"] = " ".join(s.t for s in name_spans if s not in meds) or None


# One "Label: starts wins places shows earnings" group -> records[label]
def _record_entry(toks, records):
    """One 'Label: starts wins places shows earnings' group of the record table."""
    if not toks or not toks[0].endswith(":"):
        return
    label = toks[0].rstrip(":")
    nums = [_num(t) for t in toks[1:]]
    if len(nums) < 5:
        return
    key = label.lower().replace(" ", "_")

    # "Kee Dirt" etc. is the record at today's track; it also tells us the track code
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


# The record table on the right of the horse block ("Life:", "Dirt:", "Kee Dirt:", ...)
def parse_record_table(rows):
    """Record table (right-hand side): label, starts, wins, places, shows, earnings,
    printed as two groups per row (e.g. 'Life:' and 'Dirt:')."""
    records = {}
    for y, row in rows:
        right = [s for s in row if s.symbol and s.x0 >= 400 and s.size <= 8.1]
        for part in (
            sorted([s for s in right if s.x0 < 498], key=lambda s: s.x0),
            sorted([s for s in right if s.x0 >= 498], key=lambda s: s.x0),
        ):
            _record_entry([s.t for s in part], records)
    return records


# All PP lines in a horse block
def _parse_pp_lines(rows, shift):
    """The horse's past-performance lines (other rows, such as workouts or "Claimed
    by" notes, are skipped)."""
    return [parse_pp_line(row, shift) for y, row in rows if is_pp_line(row)]


# The speed-figure list and average early pace that the converter reads
def set_figure_summaries(horse):
    """Summaries for the handicap converter (most recent race first): the list of
    speed figures and the average early pace figure. Called again after
    continuation-page lines are attached to a horse."""
    pps = horse["past_performances"]
    horse["speed_figures"] = [
        p["speed_figure"] for p in pps if p.get("speed_figure") is not None
    ]

    figs = [p["early_pace"] for p in pps if p.get("early_pace") is not None]
    if figs:
        horse["pace"] = {"early_pace": round(sum(figs) / len(figs), 2)}
    else:
        horse.pop("pace", None)


# --------------------------------------------------------------------------
# Race header
# --------------------------------------------------------------------------
# Race number, stakes name, distance and surface from the top of a race's first page
def parse_race_header(spans, race_number):
    """spans: everything above the 'Program #' column header on a race's first page."""
    race = {"race_number": race_number}
    rows = group_rows(spans)
    texts = [(y, row_text(row), row) for y, row in rows]

    # The big race number
    big = next((s for s in spans if s.bold and s.size >= 40), None)
    if big and _num(big.t) is not None:
        race["race_number"] = _num(big.t)

    # Stakes title (part of the race name): 12pt bold centred, optional sponsor line
    # (10pt bold, centred) below.
    # A title line is the text set in the title's own style: other text on the same
    # baseline (the small "equibase.com/QR" caption beside the QR code) is not part of it.
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

    # Race conditions: the 9pt text block
    cond_spans = [s for s in spans if s.size == 9 and s.x0 < 600]
    # Order line by line with a tolerance (group_rows), not by round(y0): two spans on one
    # printed line can be ~0.2pt apart and land on different sides of a rounding boundary.
    cond_spans = [s for _, row in group_rows(cond_spans) for s in row]
    conditions = row_text(cond_spans) if cond_spans else None

    # Distance / course: the bold spans that follow the first regular-weight span
    first_regular = next((i for i, s in enumerate(cond_spans) if not s.bold), None)
    dist_spans = (
        [s for s in cond_spans[first_regular:] if s.bold]
        if first_regular is not None
        else []
    )
    distance_text = row_text(dist_spans) if dist_spans else None
    if not distance_text and conditions:
        # Fallback: the distance is the last phrase of the race conditions.
        m = re.search(
            r"((?:\w+ ){1,5}Furlongs?|(?:\w+ ){1,6}Miles?[^.]*)\s*$", conditions
        )
        distance_text = m.group(1).strip() if m else None

    race["distance"] = distance_text  # the printed words, shown by the checks
    race["distance_f"] = words_to_furlongs(distance_text or "")
    race["surface"] = (
        "turf" if distance_text and "turf" in distance_text.lower() else "dirt"
    )
    return race


# --------------------------------------------------------------------------
# Race-end footnotes (legend, equipment changes, mutuel ratings, also-eligibles)
# --------------------------------------------------------------------------
# Remove the footnotes printed after the last horse; note where ALSO ELIGIBLES starts
def remove_footnotes(body):
    """Drop the footnote / legend spans printed at the end of a race, so they are not
    read as part of the last horse. Returns (remaining spans, y of the ALSO ELIGIBLES
    banner or None)."""
    keep, also_y = [], None
    for s in body:
        t = s.t
        if t.startswith("ALSO ELIGIBLE"):
            also_y = s.y0
        elif t.startswith(("MUTUEL RATINGS", "Equipment Change:")) or (
            s.size <= 7.6
            and s.x0 < 25
            and not s.symbol
            and re.match(
                r"^(L-Lasix|Equibase Rating|ï - |K - |B - |Scratched|Late Scratch)", t
            )
        ):
            continue
        else:
            keep.append(s)
    return keep, also_y


# --------------------------------------------------------------------------
# Document walk
# --------------------------------------------------------------------------
# The Equibase footer on every PP page: "10/16/2026 Race 3"
FOOTER_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})\s*Race\s*(\d+)")


# The whole PDF -> the full card (every race, horse and PP line)
def parse_pdf(path):
    """Whole PDF -> {"track", "date", "races": [race, ...], ...}
    (see the module docstring)."""
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

        # The card's date, from the first page footer
        fm = FOOTER_RE.search(layout.footer_text)
        if fm and not card_date:
            card_date = (
                f"{int(fm.group(3))}-{int(fm.group(1)):02d}-{int(fm.group(2)):02d}"
            )

        # A new race starts on a page with the big race number; other pages continue it.
        big = next((s for s in spans if s.bold and s.size >= 40 and s.x0 < 40), None)
        if big is not None:
            cutoff = _race_header_cutoff(spans, big)
            current = parse_race_header([s for s in spans if s.y0 < cutoff], _num(big.t))
            current["horses"] = []
            current["pages"] = [pno + 1]
            races.append(current)
            body = [s for s in spans if s.y0 >= cutoff]
        elif current is None:
            continue
        else:
            current["pages"].append(pno + 1)
            body = spans

        # The horses on this page
        found = _parse_page_body(body, current, shift, pno)
        track_code = track_code or found

    # Card-level fields, then the full card
    track_name = TRACK_NAMES.get(track_code or "", track_code)
    _finish_races(races, track_name, card_date)
    return {
        "track": track_name,
        "date": card_date,
        "races": races,
        # For the check report: which pages were read, and the columns that sit
        # somewhere else than in the reference layout ([[pdf_x, reference_x]]).
        "pp_pages": [lay.index + 1 for lay in layouts],
        "column_shift": [[d, r] for d, r in shift.pairs if d != r],
    }


# Is this span the big program number that starts a horse block?
def _is_program_number(s):
    """The big bold program number that starts each horse's block."""
    return s.bold and 20 <= s.size < 40 and s.x0 < 60 and _num(s.t) is not None


# Where the race header ends and the horses begin on a race's first page
def _race_header_cutoff(spans, big):
    """y below which a race's first page holds horses rather than the race header."""
    header_row = next(
        (s for s in spans if s.bold and s.size == 7 and s.t == "Program #"), None
    )
    cutoff = header_row.y1 if header_row else big.y1 + 100

    # The race header ends at the "Program #" column-header row -- but never below the
    # first horse's program number. The program's re-flowed pages put horse #1 closer to
    # that row (7pt vs 9.6pt on the PP sheet), so the row's bottom edge overlaps it.
    first_prog = min(
        (s.y0 for s in spans if _is_program_number(s) and s.y0 > big.y1), default=None
    )
    if first_prog is not None:
        cutoff = min(cutoff, first_prog - 1)
    return cutoff


# Read the horses on one page of a race (and lines continued from the page before)
def _parse_page_body(body, race, shift, pno):
    """Horses (and continuation lines) on one page of a race. Returns the card's track
    code when a horse's record table names it, else None."""

    # Drop footer / continuation banners, pull out the race-end legend lines
    body = [
        s
        for s in body
        if not (FOOTER_RE.search(s.t) or "CONTINUED" in s.t or "equibase.com" in s.t)
    ]
    body, also_eligible_y = remove_footnotes(body)
    starts = sorted([s for s in body if _is_program_number(s)], key=lambda s: s.y0)

    # Anything above the first program number on a continuation page belongs to the
    # previous horse -- the whole page if no new horse starts on it.
    lead = [s for s in body if not starts or s.y0 < starts[0].y0 - 1]
    if lead and race["horses"]:
        extra = [
            parse_pp_line(row, shift) for y, row in group_rows(lead) if is_pp_line(row)
        ]
        for p in extra:
            p["page"] = pno + 1  # PDF page (1-based) the line is printed on
        race["horses"][-1]["past_performances"].extend(extra)

    # Each horse block runs from its program number down to the next one
    track_code = None
    for i, st in enumerate(starts):
        y_hi = starts[i + 1].y0 - 1 if i + 1 < len(starts) else 10_000
        block = [s for s in body if st.y0 - 1 <= s.y0 < y_hi]
        horse = parse_horse(block, shift)
        if not horse.get("name"):
            continue

        # Every also-eligible horse carries its own bold "A"/"E" marker; the banner check
        # is a backup and covers horses on later pages of the race too.
        below_banner = race.get("_ae_banner_seen") or (
            also_eligible_y is not None and st.y0 > also_eligible_y
        )
        horse["also_eligible"] = horse.pop("ae_marker") or bool(below_banner)

        # Page numbers, for the check report
        horse["page"] = pno + 1
        for p in horse["past_performances"]:
            p["page"] = pno + 1

        race["horses"].append(horse)
        track_code = track_code or horse["records"].get("track_code")

    if also_eligible_y is not None:
        race["_ae_banner_seen"] = True
    return track_code


# After all pages are read: track, date and name on every race; refresh the summaries
def _finish_races(races, track_name, card_date):
    """Card-level fields, race name and figure summaries per race."""
    for race in races:
        race.pop("_ae_banner_seen", None)
        race["track"] = track_name
        race["date"] = card_date

        # "Keeneland Race 8", plus " - <stakes name>" for a stakes race
        base = f"{track_name or 'Race'} Race {race['race_number']}"
        stakes = race.pop("stakes_name", None)
        race["name"] = f"{base} - {stakes}" if stakes else base

        for h in race["horses"]:
            # Re-derive now that continuation-page lines are attached
            set_figure_summaries(h)


# --------------------------------------------------------------------------
# Output: only the essentials
# --------------------------------------------------------------------------
# parse_pdf's full card also holds fields that only check_parser.py uses (each race's
# pages, a horse's record table, the printed distance and final time, ...). Only this
# slim version is written to disk. It keeps
#   (a) what src/model/pp_json_to_handicap.py reads (see its ALIASES and
#       HISTORY_ALIASES), and
#   (b) identifiers, so a file or PP line says which race it is (date, track, race
#       number), which is also what results will be matched on later.
CARD_FIELDS = ("track", "date")
RACE_FIELDS = ("date", "track", "race_number", "name", "distance_f", "surface")
HORSE_FIELDS = (
    "program_number",
    "name",
    "morning_line_odds",
    "trainer",
    "scratched",
    "also_eligible",  # not read by the converter, but says who actually runs
    "class_rating",
    "speed_figures",
    "pace",
)
PP_FIELDS = (
    # identifiers: which earlier race this line is
    "date",
    "track_code",
    "race_number",
    # read by the converter
    "finish_position",
    "field_size",
    "lengths_behind",
    "first_call_position",
    "stretch_position",
    "speed_figure",
    "early_pace",
    "distance_f",
    "surface",
    "purse",
    "race_class",
)


# Copy only some keys of a dict
def _pick(d, keys):
    """Copy only `keys` out of dict `d` (missing keys come out as None)."""
    return {k: d.get(k) for k in keys}


# One race, cut down to the essentials (this is a race<N>.json file)
def slim_race(race):
    """One full race -> the essentials; this is what the converter is given."""
    out = _pick(race, RACE_FIELDS)
    out["horses"] = []
    for h in race["horses"]:
        horse = _pick(h, HORSE_FIELDS)
        horse["past_performances"] = [_pick(p, PP_FIELDS) for p in h["past_performances"]]
        out["horses"].append(horse)
    return out


# The whole card, cut down to the essentials (this is card.json)
def slim_card(card):
    """Full card from parse_pdf -> the essentials that are written to disk."""
    out = _pick(card, CARD_FIELDS)
    out["races"] = [slim_race(r) for r in card["races"]]
    return out


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------
# Parse one PDF and write the JSON (whole card, one race, or a file per race)
def main(argv=None):

    # Arguments
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
    ap.add_argument(
        "--full",
        action="store_true",
        help="write everything the parser read, not just the essentials (debugging)",
    )
    args = ap.parse_args(argv)

    # Parse; keep the essentials unless --full
    full_card = parse_pdf(args.pdf)
    card = full_card if args.full else slim_card(full_card)

    # --summary: one line per race and per horse, to stderr
    if args.summary:  # uses fields only the full card has
        for r in full_card["races"]:
            print(
                f"Race {r['race_number']}: {r['distance']} "
                f"({r['distance_f']}f {r['surface']})"
                f" | {len(r['horses'])} horses | pages {r['pages']}",
                file=sys.stderr,
            )
            for h in r["horses"]:
                print(
                    f"   #{h['program_number']:<3}{h['name']:<24} "
                    f"ML {h['morning_line_odds']:<5} cls {h['class_rating']!s:<6}"
                    f" spd {h['speed_figures'][:6]} pps={len(h['past_performances'])}",
                    file=sys.stderr,
                )

    # --split-dir: one file per race
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

    # --race: just one race instead of the whole card
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

    # Write to the -o file, or print
    rendered = json.dumps(payload, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
