"""
Handicapping engine: v8 (locked), v9 (shadow), and PP-sheet derived mode.
All scores are on a 0-100 scale.

Reads and writes the handicap_output.jsonc layout: a list of races, each with a
"race_participants" list. The scoring fills in the "model_*" fields on every
horse; everything else in the file is echoed back as it was read.

Usage:
    python handicap.py                       # auto-run: finds handicap_output.jsonc and updates it
                                             # where it is (v8)
    python handicap.py path/to/race.jsonc    # scores that file and updates it where it is
    python handicap.py race.jsonc --v9       # same, using the v9 shadow model
    python handicap.py race.jsonc --out p    # write to a different path instead of in place
    python handicap.py race.jsonc --text     # printed table + confidence instead of JSONC

With no path, handicap_output.jsonc is looked for in the current folder, then next
to this script, then in a tests/ folder beside this script's folder.

Each horse also carries a race-level confidence score (0-100) and label
(HIGH / MEDIUM / LOW): how much to trust the top pick. It is a heuristic, NOT a
calibrated win probability; backtest the thresholds before relying on the labels.

How the JSONC fields feed the engine:
    race_distance_furlongs          -> distance_f
    race_track_surface              -> surface
    horse_trainer                   -> trainer
    race_horse_running_style        -> style (Early/Speed -> E, Presser/Stalker -> P,
                                              Closer -> S; E, E/P, P, S also accepted)
    race_horse_program_number       -> pp (post)
    race_horse_morning_line_number  -> pl (odds)
    race_horse_speed_figures        -> avgspd
    race_horse_distance_figures     -> avgdist (mean of the list)
    race_horse_class                -> primepower AND lastclass (one number feeds both)
    race_horse_pace.early_pace      -> ep1
    race_horse_pace.late_pace       -> lp
    race_scratched                  -> scratched
Ranks are computed automatically from the raw numbers (higher = better, ties share
a rank). The file has no field for bestspd, so the sprint best-speed bonus is off.
"""
import json
import math
import os
import re
import sys
from functools import partial
from typing import NamedTuple

# --------------------------------------------------------------------------
# Rank -> points tables
# --------------------------------------------------------------------------
def tbl(d, default):
    """Build a rank -> points lookup from dict d, falling back to default."""
    return lambda r: d.get(r, default)

ROUTE = {  # v8, /100
    "lp":   tbl({1:22,2:19,3:17,4:15,5:13,6:11,7:8,8:5,9:3}, 3),
    "cls":  tbl({1:17,2:16,3:15,4:14,5:13,6:12,7:10,8:8,9:5}, 5),
    "spd":  tbl({1:17,2:15,3:14,4:12,5:10,6:8,7:6,8:5,9:3}, 3),
    "dist": tbl({1:13,2:11,3:10,4:8,5:6,6:5,7:3,8:1,9:0}, 0),
    "form": tbl({1:13,2:12,3:11,4:10,5:9,6:8,7:6,8:4,9:0}, 0),
    "trainer_max": 9, "workout_max": 9,
    "presser": 4, "closer": 3, "lone": 4, "duel": {2: -3, 3: -3},
}
ROUTE_V9 = dict(ROUTE,  # class upweighted, speed/LP trimmed
    lp=tbl({1:20,2:17,3:15,4:13,5:11,6:9,7:7,8:4,9:2}, 2),
    cls=tbl({1:21,2:19,3:18,4:16,5:15,6:13,7:11,8:8,9:5}, 5),
    spd=tbl({1:15,2:13,3:12,4:10,5:8,6:7,7:5,8:4,9:2}, 2),
    dist=tbl({1:12,2:10,3:9,4:7,5:6,6:4,7:3,8:1,9:0}, 0),
    form=tbl({1:12,2:11,3:10,4:9,5:8,6:7,7:5,8:3,9:0}, 0),
    trainer_max=10, workout_max=10)
TURF = dict(ROUTE, duel={2: -5, 3: -5})

SPRINT = {  # v8, /100
    "spd":  tbl({1:19,2:17,3:14,4:12,5:9,6:7}, 5),
    "best": {1: 4, 2: 2, 3: 1},
    "cls":  tbl({1:19,2:17,3:15,4:13,5:10,6:8}, 5),
    "pace": tbl({1:15,2:13,3:11,4:8,5:6,6:4}, 2),
    "post": tbl({1:8,2:8,3:7,4:6,5:5,6:5,7:4,8:4}, 4),
    "drop": tbl({1:8,2:7,3:6,4:5,5:3,6:0}, 0),
    "style_cap": 12, "spd_cap": 19, "trainer_max": 8,
    "presser": 4, "duel": {2: -3, 3: -5},
}
SPRINT_V9 = dict(SPRINT,  # class upweighted
    spd=tbl({1:17,2:15,3:12,4:10,5:8,6:6}, 4),
    best={1: 3, 2: 2, 3: 1},
    cls=tbl({1:23,2:21,3:18,4:15,5:12,6:9}, 6),
    pace=tbl({1:14,2:12,3:10,4:8,5:6,6:4}, 2),
    post=tbl({1:7,2:7,3:6,4:5,5:5,6:4,7:4,8:4}, 4),
    drop=tbl({1:9,2:8,3:7,4:5,5:3,6:0}, 0),
    style_cap=11, spd_cap=17, trainer_max=7)

TRAINER_TIER = {  # fraction of max trainer points; edit freely
    "baffert": 1.0, "pletcher": 1.0, "brown": 1.0, "c.brown": 1.0, "cox": 1.0,
    "mott": 0.9, "ward": 0.9, "asmussen": 0.8, "d. ryan": 0.8, "sharp": 0.8,
    # --- added: tiers are judgment calls, not computed from stats ---
    "joseph": 0.9, "casse": 0.9, "mcgaughey": 0.9, "mcpeek": 0.9,
    "motion": 0.8, "clement": 0.8, "romans": 0.8, "amoss": 0.8, "maker": 0.8,
    "sadler": 0.8, "mandella": 0.8, "miller": 0.8, "d'amato": 0.8,
    "hollendorfer": 0.8, "o'neill": 0.8, "devaux": 0.8, "weaver": 0.8,
    "rice": 0.8, "brisset": 0.8, "walsh": 0.8, "mccarthy": 0.8,
}

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def ranks(horses, key):
    """Competition ranking, higher is better. Missing/zero values rank last."""
    vals = [(h.get(key) or 0) for h in horses]
    return [1 + sum(v2 > v for v2 in vals) for v in vals]

def rank_of(horse, all_ranks, i, key):
    """Rank of horse i for metric key: manual override first, else computed."""
    return horse.get("ranks", {}).get(key, all_ranks[key][i])

def market_pts(pl, top):
    """Points from the odds (pl); top = max points for the market/workout factor."""
    steps = [(2, 1.0), (3, .85), (5, .75), (8, .6), (12, .45), (20, .3)]
    for lim, frac in steps:
        if pl <= lim:
            return round(top * frac)
    return round(top * .1)

def trainer_pts(name, mx):
    """Trainer points: tier fraction (default .7 if unknown) of the max mx."""
    return round(mx * TRAINER_TIER.get((name or "").lower(), .7))

def pace_scenario(horses):
    """Returns (lone_speed_horse, duel_group). Thresholds from this session:
    lone = EP1 lead >= 8 over the next horse; duel = others within 4 of top EP1."""
    ep = sorted(((h.get("ep1") or 0, h["name"]) for h in horses), reverse=True)
    if len(ep) < 2:
        return None, []
    top, second = ep[0][0], ep[1][0]
    if top - second >= 8:
        return ep[0][1], []
    group = [n for v, n in ep if top - v <= 4]
    return None, (group if len(group) >= 2 else [])

# --------------------------------------------------------------------------
# Scorers
# --------------------------------------------------------------------------
def score_route_like(horses, cfg):
    """Score a route/turf race; returns (name, total, parts, tags) per horse."""
    lone, duel = pace_scenario(horses)
    all_ranks = {k: ranks(horses, k) for k in
                 ("lp", "avgspd", "primepower", "avgdist", "lastclass")}
    out = []
    for i, h in enumerate(horses):
        r = partial(rank_of, h, all_ranks, i)
        parts = {
            "lp": cfg["lp"](r("lp")), "class": cfg["cls"](r("primepower")),
            "speed": cfg["spd"](r("avgspd")), "dist": cfg["dist"](r("avgdist")),
            "form": cfg["form"](r("lastclass")),
            "trainer": trainer_pts(h.get("trainer"), cfg["trainer_max"]),
            "workout": market_pts(h.get("pl", 99), cfg["workout_max"]),
        }
        adj, tags = 0, []
        st = h.get("style", "")
        if st == "P":
            adj += cfg["presser"]
            tags.append("PRESSER")
        if st == "S":
            adj += cfg["closer"]
            tags.append("CLOSER")
        is_lone = h.get("lone", h["name"] == lone)
        if is_lone:
            adj += cfg["lone"]
            tags.append("LONE_SPEED")
        in_duel = h.get("duel", h["name"] in duel)
        if in_duel:
            adj += cfg["duel"].get(min(len(duel), 3), -3)
            tags.append("DUEL")
        parts["adj"] = adj
        out.append((h["name"], sum(parts.values()), parts, tags))
    return out

def score_sprint(horses, cfg):
    """Score a sprint; returns (name, total, parts, tags) per horse."""
    lone, duel = pace_scenario(horses)
    all_ranks = {k: ranks(horses, k) for k in
                 ("lp", "avgspd", "primepower", "lastclass", "bestspd")}
    out = []
    for i, h in enumerate(horses):
        r = partial(rank_of, h, all_ranks, i)
        best = cfg["best"].get(r("bestspd"), 0) if h.get("bestspd") else 0
        st = h.get("style", "E/P")
        base = {"E": 12, "E/P": 12, "P": 10, "S": 8}.get(st, 8)
        tags = []
        if st == "P":
            base += cfg["presser"]
            tags.append("PRESSER")
        if best:
            tags.append(f"BESTSPD{r('bestspd')}")
        parts = {
            "speed": min(cfg["spd"](r("avgspd")) + best, cfg["spd_cap"]),
            "class": cfg["cls"](r("primepower")),
            "pace": cfg["pace"](r("lp")),
            "post": cfg["post"](h.get("pp", 99)),
            "market": market_pts(h.get("pl", 99), 12),
            "style": min(base, cfg["style_cap"]),
            "drop": cfg["drop"](r("lastclass")),
            "trainer": trainer_pts(h.get("trainer"), cfg["trainer_max"]),
        }
        if h.get("lone", h["name"] == lone):
            parts["lone"] = 4
            tags.append("LONE_SPEED")
        if h.get("duel", h["name"] in duel):
            parts["duel"] = cfg["duel"].get(min(len(duel), 3), -3)
            tags.append("DUEL")
        out.append((h["name"], sum(parts.values()), parts, tags))
    return out

def pick_formula(distance_f, surface):
    """Choose the formula: 'turf', 'sprint' (< 8f dirt) or 'route'."""
    if surface == "turf":
        return "turf"
    return "sprint" if distance_f < 8 else "route"

def score_race(race, version="v8"):
    """Score a race; returns (formula kind, results sorted best-first)."""
    horses = [h for h in race["horses"] if not h.get("scratched")]
    kind = pick_formula(race["distance_f"], race.get("surface", "dirt"))
    v9 = version == "v9"
    if kind == "sprint":
        res = score_sprint(horses, SPRINT_V9 if v9 else SPRINT)
    else:
        res = score_route_like(horses, (ROUTE_V9 if v9 else (TURF if kind == "turf" else ROUTE)))
    res.sort(key=lambda x: -x[1])
    return kind, res

# --------------------------------------------------------------------------
# PP-sheet derived mode (lower confidence; see standing addendum)
# --------------------------------------------------------------------------
FOREIGN_FLOOR = {"G1": 100, "G2": 96, "G3": 92, "LISTED": 88, None: 85}
CLASS_PTS = {"stk750": 21, "stk500": 19, "stk300": 17, "stk225": 16, "stk200": 18,
             "stk170": 14, "stk125": 13, "msw120": 10, "msw100": 9, "msw32": 5}

def score_pp_sheet(h, route_distance=False):
    """h keys: spd (Equibase fig or None), foreign_grade, class (key of CLASS_PTS),
    style, trainer_pts, jockey_pts, odds, life ('5-3-2-0'), sprint_only (bool)."""
    spd = h.get("spd") or FOREIGN_FLOOR.get(h.get("foreign_grade"), 85)
    s = min(spd * .25, 26) + CLASS_PTS.get(h.get("class"), 8)
    s += {"E": 3, "E/P": 4, "P": 2, "S": 2}.get(h.get("style"), 0)
    s += h.get("trainer_pts", 5) + h.get("jockey_pts", 5)
    odds = h.get("odds", 99)
    s += next(p for lim, p in [(2,14),(4,12),(6,10),(9,8),(13,6),(16,4),(1e9,2)] if odds <= lim)
    starts, wins = (int(x) for x in h.get("life", "0-0").split("-")[:2])
    s += min((wins / starts if starts else 0) * 8 + min(starts, 4), 10)
    if route_distance and h.get("sprint_only"):  # standing rule: first route try
        s -= 4
    return round(s, 1)

# --------------------------------------------------------------------------
# Race confidence (0-100): how much to trust the top pick
#   margin     (35)  gap between #1 and #2 totals
#   agreement  (25)  v8 and v9 agree on the top pick / top three
#   data       (20)  how many key inputs are actually filled in
#   field      (10)  smaller fields are more predictable
#   pace       (10)  a speed duel makes the race shape less certain
# Each component returns 0.0-1.0 and is weighted so the weights sum to 100.
# Thresholds are starting guesses.
# --------------------------------------------------------------------------
W_MARGIN, W_AGREEMENT, W_DATA, W_FIELD, W_PACE = 35, 25, 20, 10, 10

MARGIN_FULL = 10.0                # point gap at which margin confidence maxes out
FIELD_SMALL, FIELD_BIG = 6, 14    # field size: <=6 full confidence, >=14 floor
FIELD_FLOOR = 0.4
DUEL2_CONF, DUEL3_CONF = 0.7, 0.4
HIGH_CUTOFF, MEDIUM_CUTOFF = 70, 45

# Inputs the scorers read; a missing one silently counts as 0 or a default.
DATA_FIELDS = ("ep1", "lp", "avgspd", "avgdist", "primepower",
               "lastclass", "pl", "trainer")

def conf_margin(res):
    """Gap between the top two totals, scaled so MARGIN_FULL points = 1.0."""
    if len(res) < 2:
        return 1.0
    return min((res[0][1] - res[1][1]) / MARGIN_FULL, 1.0)

def conf_agreement(res, res_other):
    """60% weight on same top pick, 40% on top-three overlap between versions."""
    same_top = 1.0 if res[0][0] == res_other[0][0] else 0.0
    overlap = len({r[0] for r in res[:3]} & {r[0] for r in res_other[:3]}) / min(3, len(res))
    return 0.6 * same_top + 0.4 * overlap

def conf_data(horses):
    """Fraction of DATA_FIELDS present across all running horses."""
    present = sum(1 for h in horses for f in DATA_FIELDS if h.get(f) is not None)
    return present / (len(horses) * len(DATA_FIELDS))

def conf_field(n_horses):
    """1.0 for small fields, sliding linearly down to FIELD_FLOOR for big ones."""
    if n_horses <= FIELD_SMALL:
        return 1.0
    if n_horses >= FIELD_BIG:
        return FIELD_FLOOR
    slide = (n_horses - FIELD_SMALL) / (FIELD_BIG - FIELD_SMALL)
    return 1.0 - (1.0 - FIELD_FLOOR) * slide

def conf_pace(horses):
    """Lone speed or no speed = clean pace picture; duels add uncertainty."""
    _, duel = pace_scenario(horses)
    if len(duel) >= 3:
        return DUEL3_CONF
    if len(duel) == 2:
        return DUEL2_CONF
    return 1.0

def confidence_label(score):
    """Map a 0-100 confidence score to HIGH / MEDIUM / LOW."""
    if score >= HIGH_CUTOFF:
        return "HIGH"
    if score >= MEDIUM_CUTOFF:
        return "MEDIUM"
    return "LOW"

def race_confidence(race, res, res_other):
    """res = ranked results for the version being reported, res_other = the other
    version (for the agreement check). Returns score, label and each component."""
    horses = [h for h in race["horses"] if not h.get("scratched")]
    parts = {
        "margin": conf_margin(res),
        "agreement": conf_agreement(res, res_other),
        "data": conf_data(horses),
        "field": conf_field(len(horses)),
        "pace": conf_pace(horses),
    }
    score = round(W_MARGIN * parts["margin"] + W_AGREEMENT * parts["agreement"]
                  + W_DATA * parts["data"] + W_FIELD * parts["field"]
                  + W_PACE * parts["pace"])
    return {"score": score, "label": confidence_label(score), "components": parts}

def analyze(race, version="v8"):
    """Score the race under `version` and the other version; returns
    (formula kind, ranked results, confidence dict)."""
    other = "v9" if version == "v8" else "v8"
    kind, res = score_race(race, version)
    _, res_other = score_race(race, other)
    return kind, res, race_confidence(race, res, res_other)

# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
def show(race, version="v8"):
    """Print the ranked table and confidence line; returns the results."""
    kind, res, conf = analyze(race, version)
    print(f"\n{race['name']}  |  {kind.upper()} {version}  |  /100")
    print("-" * 64)
    for i, (name, tot, _parts, tags) in enumerate(res, 1):
        print(f"{i:>2}. {name:<18}{tot:>5}   {' '.join(tags)}")
    print(f"Confidence: {conf['score']}/100 ({conf['label']})")
    return res

# --------------------------------------------------------------------------
# JSONC input
# --------------------------------------------------------------------------
class _JsoncParser:
    """Tolerant JSONC reader: // and /* */ comments, trailing commas, and the
    file's `[ "key": value, ... ]` arrays (read as objects)."""
    NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
    LITERALS = {"true": True, "false": False, "null": None}

    def __init__(self, text):
        """Start parsing text from the beginning."""
        self.s = text
        self.i = 0

    def fail(self, msg):
        """Fail."""
        line = self.s.count("\n", 0, self.i) + 1
        raise ValueError(f"JSONC parse error at line {line}: {msg}")

    def peek(self):
        """Current character, or '' at end of text."""
        return self.s[self.i] if self.i < len(self.s) else ""

    def skip(self):
        """Advance past whitespace and comments."""
        s = self.s
        while self.i < len(s):
            if s[self.i].isspace():
                self.i += 1
            elif s.startswith("//", self.i):
                end = s.find("\n", self.i)
                self.i = len(s) if end < 0 else end
            elif s.startswith("/*", self.i):
                end = s.find("*/", self.i + 2)
                if end < 0:
                    self.fail("unterminated comment")
                self.i = end + 2
            else:
                break

    def string(self):
        """Parse a quoted string at the cursor."""
        if self.peek() != '"':
            self.fail("expected a quoted string")
        s, j = self.s, self.i + 1
        while j < len(s) and s[j] != '"':
            j += 2 if s[j] == "\\" else 1
        if j >= len(s):
            self.fail("unterminated string")
        raw = s[self.i:j + 1]
        self.i = j + 1
        return json.loads(raw)

    def value(self):
        """Parse any JSONC value at the cursor."""
        self.skip()
        c = self.peek()
        if not c:
            self.fail("unexpected end of file")
        if c == "{":
            return self.obj()
        if c == "[":
            return self.arr()
        if c == '"':
            return self.string()
        m = self.NUMBER.match(self.s, self.i)
        if m:
            self.i = m.end()
            t = m.group()
            return float(t) if any(ch in t for ch in ".eE") else int(t)
        for word, val in self.LITERALS.items():
            if self.s.startswith(word, self.i):
                self.i += len(word)
                return val
        self.fail(f"unexpected {c!r}")
        return None

    def obj(self):
        """Parse an object (cursor on the opening brace)."""
        self.i += 1
        out = {}
        while True:
            self.skip()
            if self.peek() == "}":
                self.i += 1
                return out
            key = self.string()
            self.skip()
            if self.peek() != ":":
                self.fail("expected ':'")
            self.i += 1
            out[key] = self.value()
            self.skip()
            if self.peek() == ",":
                self.i += 1

    def arr(self):
        """Parse an array; key: value entries inside it make it an object."""
        self.i += 1
        items, keyed = [], {}
        while True:
            self.skip()
            if self.peek() == "]":
                self.i += 1
                return keyed if keyed else items
            item = self.value()
            self.skip()
            if self.peek() == ":" and isinstance(item, str):
                self.i += 1
                keyed[item] = self.value()
            else:
                items.append(item)
            self.skip()
            if self.peek() == ",":
                self.i += 1

def load_jsonc(path):
    """Read a JSONC file; returns a list of races."""
    with open(path, encoding="utf-8") as fh:
        data = _JsoncParser(fh.read()).value()
    return data if isinstance(data, list) else [data]

STYLE_CODES = {
    "e": "E", "early": "E", "speed": "E", "front runner": "E", "frontrunner": "E",
    "e/p": "E/P",
    "p": "P", "presser": "P", "stalker": "P",
    "s": "S", "closer": "S", "sustained": "S",
}

def _pace(p):
    """The race_horse_pace block as a dict ({} if absent or not keyed)."""
    pace = p.get("race_horse_pace")
    return pace if isinstance(pace, dict) else {}

def _put(h, key, value):
    """Set h[key] only when the file actually has a value for it."""
    if value is not None:
        h[key] = value

# --- race-level readers ---------------------------------------------------
def in_race_name(r):
    """Input: name of the race."""
    return r["race_name"]

def in_race_distance(r):
    """Input: distance of the race."""
    return r["race_distance_furlongs"]

def in_race_surface(r):
    """Input: surface of the race."""
    return (r.get("race_track_surface") or "dirt").lower()

def in_race_participants(r):
    """Input: participants of the race."""
    return r.get("race_participants") or []

# --- horse-level readers --------------------------------------------------
def in_horse_name(p):
    """Input: name of the horse."""
    return p["horse_name"]

def in_horse_trainer(p):
    """Input: trainer of the horse."""
    return p.get("horse_trainer")

def in_horse_style(p):
    """Input: style of the horse."""
    return STYLE_CODES.get(str(p.get("race_horse_running_style") or "").strip().lower())

def in_horse_post(p):
    """Input: post of the horse."""
    m = re.match(r"\d+", str(p.get("race_horse_program_number") or ""))
    return int(m.group()) if m else None

def in_horse_morning_line(p):
    """Input: morning line of the horse."""
    return p.get("race_horse_morning_line_number")

def in_horse_speed(p):
    """Input: speed of the horse."""
    return p.get("race_horse_speed_figures")

def in_horse_distance(p):
    """Input: distance of the horse."""
    figs = [f for f in (p.get("race_horse_distance_figures") or []) if f is not None]
    return sum(figs) / len(figs) if figs else None

def in_horse_class(p):
    """Input: class of the horse."""
    return p.get("race_horse_class")

def in_horse_early_pace(p):
    """Input: early pace of the horse."""
    return _pace(p).get("early_pace")

def in_horse_late_pace(p):
    """Input: late pace of the horse."""
    return _pace(p).get("late_pace")

def in_horse_scratched(p):
    """Input: scratched of the horse."""
    return bool(p.get("race_scratched"))

def engine_horse(p):
    """One participant in the dict shape the scorers expect."""
    h = {"name": in_horse_name(p), "scratched": in_horse_scratched(p)}
    _put(h, "trainer", in_horse_trainer(p))
    _put(h, "style", in_horse_style(p))
    _put(h, "pp", in_horse_post(p))
    _put(h, "pl", in_horse_morning_line(p))
    _put(h, "avgspd", in_horse_speed(p))
    _put(h, "avgdist", in_horse_distance(p))
    _put(h, "primepower", in_horse_class(p))
    _put(h, "lastclass", in_horse_class(p))
    _put(h, "ep1", in_horse_early_pace(p))
    _put(h, "lp", in_horse_late_pace(p))
    return h

def engine_race(r):
    """One JSONC race in the dict shape score_race / analyze expect."""
    return {
        "name": in_race_name(r),
        "distance_f": in_race_distance(r),
        "surface": in_race_surface(r),
        "horses": [engine_horse(p) for p in in_race_participants(r)],
    }

# --------------------------------------------------------------------------
# Win probability + per-column model values
#
# win_probability is a softmax over the 0-100 scores:
#     p_i = exp(score_i / T) / sum_j exp(score_j / T)
# T = PROB_TEMPERATURE. Smaller T -> favorite gets a bigger share.
# NOTE: this is a heuristic conversion, NOT a calibrated probability. To make it
# honest, fit T against past results (pick T that minimizes log-loss).
# --------------------------------------------------------------------------
PROB_TEMPERATURE = 8.0

class Pick(NamedTuple):
    """One horse's place in a scored race."""
    rank: int
    name: str
    score: float
    prob: float

def win_probabilities(scores, temp=PROB_TEMPERATURE):
    """Softmax of the scores at temperature temp; returns probabilities summing to 1."""
    top = max(scores)  # subtract max for numeric stability
    exps = [math.exp((s - top) / temp) for s in scores]
    total = sum(exps)
    return [e / total for e in exps]

def col_predicted_rank(pick):
    """Column: predicted finishing rank (1 = predicted winner)."""
    return pick.rank

def col_is_predicted_winner(pick):
    """Column: True for the top-ranked horse."""
    return pick.rank == 1

def col_score(pick):
    """Column: total score on the 0-100 scale, one decimal."""
    return round(float(pick.score), 1)

def col_win_probability(pick):
    """Column: softmax win probability, 4 decimals."""
    return round(pick.prob, 4)

def col_confidence_score(conf):
    """Column: race-level confidence score (0-100)."""
    return conf["score"]

def col_confidence_label(conf):
    """Column: race-level confidence label (HIGH / MEDIUM / LOW)."""
    return conf["label"]

def score_jsonc_race(r, version="v8"):
    """Score one JSONC race; returns (picks by horse name, confidence dict).
    Both are None when every horse is scratched."""
    race = engine_race(r)
    if all(h["scratched"] for h in race["horses"]):
        return None, None
    _kind, res, conf = analyze(race, version)
    probs = win_probabilities([entry[1] for entry in res])
    picks = {entry[0]: Pick(rank, entry[0], entry[1], prob)
             for rank, (entry, prob) in enumerate(zip(res, probs), 1)}
    return picks, conf

# --------------------------------------------------------------------------
# JSONC output: same layout and comments as handicap_output.jsonc
# --------------------------------------------------------------------------
def _jv(value):
    """ jv."""
    return json.dumps(value, ensure_ascii=False)

def _echo(ind, d, key, tail=""):
    """Echo d[key] back as a field line; None if the input did not have it."""
    if key not in d:
        return None
    return f'{ind}"{key}": {_jv(d[key])},{tail}'

# --- race metadata --------------------------------------------------------
def out_race_name(r, ind):
    """Output line(s) for race name."""
    return _echo(ind, r, "race_name")

def out_race_season(r, ind):
    """Output line(s) for race season."""
    return _echo(ind, r, "race_season")

def out_race_date(r, ind):
    """Output line(s) for race date."""
    return _echo(ind, r, "race_date")

def out_track_location(r, ind):
    """Output line(s) for track location."""
    return _echo(ind, r, "track_location")

def out_track_name(r, ind):
    """Output line(s) for track name."""
    return _echo(ind, r, "track_name")

def out_race_distance_furlongs(r, ind):
    """Output line(s) for race distance furlongs."""
    return _echo(ind, r, "race_distance_furlongs")

def out_race_track_surface(r, ind):
    """Output line(s) for race track surface."""
    return _echo(ind, r, "race_track_surface")

# --- individual horse data ------------------------------------------------
def out_horse_name(p, ind):
    """Output line(s) for horse name."""
    return _echo(ind, p, "horse_name")

def out_horse_trainer(p, ind):
    """Output line(s) for horse trainer."""
    return _echo(ind, p, "horse_trainer")

def out_race_jockey(p, ind):
    """Output line(s) for race jockey."""
    return _echo(ind, p, "race_jockey")

def out_horse_description(p, ind):
    """Output line(s) for horse description."""
    return _echo(ind, p, "horse_description")

def out_race_horse_running_style(p, ind):
    """Output line(s) for race horse running style."""
    return _echo(ind, p, "race_horse_running_style", "  // Subject to be on race basis")

def out_race_horse_program_number(p, ind):
    """Output line(s) for race horse program number."""
    return _echo(ind, p, "race_horse_program_number")

def out_race_horse_morning_line_number(p, ind):
    """Output line(s) for race horse morning line number."""
    return _echo(ind, p, "race_horse_morning_line_number")

def out_race_horse_speed_figures(p, ind):
    """Output line(s) for race horse speed figures."""
    return _echo(ind, p, "race_horse_speed_figures")

def out_race_horse_distance_figures(p, ind):
    """Output line(s) for race horse distance figures."""
    figs = p.get("race_horse_distance_figures")
    if not isinstance(figs, list):
        return _echo(ind, p, "race_horse_distance_figures")
    lines = [f'{ind}"race_horse_distance_figures": [']
    lines += [f"{ind}  {_jv(f)}," for f in figs]
    lines.append(f"{ind}],")
    return "\n".join(lines)

def out_race_horse_class(p, ind):
    """Output line(s) for race horse class."""
    return _echo(ind, p, "race_horse_class")

def out_race_horse_pace(p, ind):
    """Output line(s) for race horse pace."""
    # Kept exactly as the file has it: brackets around key/value pairs.
    # (Not valid JSON/JSONC; swap "[" / "]" for "{" / "}" here if you ever want it to be.)
    pace = _pace(p)
    if not pace:
        return None
    inner = ind + "  "
    lines = [f'{ind}"race_horse_pace": [',
             _echo(inner, pace, "early_pace"),
             _echo(inner, pace, "late_pace"),
             f"{ind}],"]
    return "\n".join(x for x in lines if x is not None)

def out_race_scratched(p, ind):
    """Output line(s) for race scratched."""
    return _echo(ind, p, "race_scratched")

# --- our model data -------------------------------------------------------
def out_model_predicted_rank(ind, pick):
    """Model column: predicted rank (null if scratched)."""
    value = None if pick is None else col_predicted_rank(pick)
    return f'{ind}"model_predicted_rank": {_jv(value)},'

def out_model_is_predicted_winner(ind, pick):
    """Model column: predicted-winner flag (null if scratched)."""
    value = None if pick is None else col_is_predicted_winner(pick)
    return f'{ind}"model_is_predicted_winner": {_jv(value)},'

def out_model_score(ind, pick):
    """Model column: 0-100 score (null if scratched)."""
    value = None if pick is None else col_score(pick)
    return f'{ind}"model_score": {_jv(value)},'

def out_model_win_probability(ind, pick):
    """Model column: softmax win probability (null if scratched)."""
    value = None if pick is None else col_win_probability(pick)
    return f'{ind}"model_win_probability": {_jv(value)},'

def out_model_confidence_score(ind, pick, conf):
    """Model column: race confidence score (null if scratched)."""
    value = None if pick is None or conf is None else col_confidence_score(conf)
    return f'{ind}"model_confidence_score": {_jv(value)},'

def out_model_confidence_label(ind, pick, conf):
    """Model column: race confidence label (null if scratched)."""
    value = None if pick is None or conf is None else col_confidence_label(conf)
    return f'{ind}"model_confidence_label": {_jv(value)},'

# --- assembly -------------------------------------------------------------
def render_participant(p, pick, conf, ind):
    """One horse: input fields echoed, model fields filled (null if scratched)."""
    i2 = ind + "  "
    lines = [
        ind + "{",
        i2 + "// Individual Horse Data",
        out_horse_name(p, i2),
        out_horse_trainer(p, i2),
        out_race_jockey(p, i2),
        out_horse_description(p, i2),
        out_race_horse_running_style(p, i2),
        out_race_horse_program_number(p, i2),
        out_race_horse_morning_line_number(p, i2),
        out_race_horse_speed_figures(p, i2),
        out_race_horse_distance_figures(p, i2),
        out_race_horse_class(p, i2),
        out_race_horse_pace(p, i2),
        out_race_scratched(p, i2),
        i2 + "// Our Model Data",
        out_model_predicted_rank(i2, pick),
        out_model_is_predicted_winner(i2, pick),
        out_model_score(i2, pick),
        out_model_win_probability(i2, pick),
        out_model_confidence_score(i2, pick, conf),
        out_model_confidence_label(i2, pick, conf),
        ind + "},",
    ]
    return "\n".join(x for x in lines if x is not None)

def render_race(r, version, ind="  "):
    """One race block, with model fields filled in for every participant."""
    picks, conf = score_jsonc_race(r, version)
    i2, i3 = ind + "  ", ind + "    "
    horses = []
    for p in in_race_participants(r):
        pick = picks.get(in_horse_name(p)) if picks else None
        if in_horse_scratched(p):
            pick = None
        horses.append(render_participant(p, pick, conf, i3))
    lines = [
        ind + "{",
        i2 + "// Race Metadata",
        out_race_name(r, i2),
        out_race_season(r, i2),
        out_race_date(r, i2),
        out_track_location(r, i2),
        out_track_name(r, i2),
        out_race_distance_furlongs(r, i2),
        out_race_track_surface(r, i2),
        i2 + '"race_participants": [',
        *horses,
        i2 + "],",
        ind + "},",
    ]
    return "\n".join(x for x in lines if x is not None)

DEFAULT_NAME = "handicap_output.jsonc"

def find_default_input():
    """Locate handicap_output.jsonc: current folder, this script's folder, then
    a tests/ folder beside the script's folder. Returns the first that exists,
    else the current-folder path."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [os.path.join(os.getcwd(), DEFAULT_NAME),
                  os.path.join(here, DEFAULT_NAME),
                  os.path.join(os.path.dirname(here), "tests", DEFAULT_NAME)]
    for path in candidates:
        if os.path.exists(path):
            return path
    return candidates[0]

def to_jsonc(races, version="v8"):
    """All races as a JSONC string in the handicap_output.jsonc layout."""
    blocks = [render_race(r, version) for r in races]
    return "[\n  // Most Numerical Values are Floats\n" + "\n".join(blocks) + "\n]\n"

def write_jsonc(races, version, path):
    """Write the scored races to path as JSONC; returns the horse count."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(to_jsonc(races, version))
    return sum(len(in_race_participants(r)) for r in races)

def parse_args(argv):
    """Split argv into (race jsonc path, output path, version, text flag).
    The output path defaults to the input path (updated where it is)."""
    out = None
    paths = []
    args = iter(argv)
    for a in args:
        if a == "--out":
            out = next(args, None)
            if not out:
                sys.exit("--out needs a file path")
        elif not a.startswith("--"):
            paths.append(a)
    version = "v9" if "--v9" in argv else "v8"
    race_path = paths[0] if paths else find_default_input()
    return race_path, (out or race_path), version, "--text" in argv

def main(argv):
    """CLI entry point: scored JSONC to a file by default, printed table with --text."""
    race_path, out, version, text = parse_args(argv)
    if not os.path.exists(race_path):
        sys.exit(f"input not found: {race_path}\n"
                 "usage: python handicap.py [race.jsonc] [--v9] [--out PATH] [--text]")
    races = load_jsonc(race_path)
    if text:                  # human-readable table
        for r in races:
            race = engine_race(r)
            if not all(h["scratched"] for h in race["horses"]):
                show(race, version)
    else:                     # default: JSONC written to a file
        n = write_jsonc(races, version, out)
        print(f"Wrote {n} horses across {len(races)} race(s) to {out}")

if __name__ == "__main__":
    main(sys.argv[1:])
