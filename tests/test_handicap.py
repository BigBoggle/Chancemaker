"""Tests for handicap.py: scoring, confidence, JSONC reading/writing, CLI."""

# One test per function makes this long; the size limit is not useful here.
# pylint: disable=too-many-lines

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Callable, cast

import pytest

import handicap

STYLE_TAIL = "  // Subject to be on race basis"


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def lookup(cfg: dict[str, Any], key: str) -> Callable[[int], int]:
    """Fetch a rank -> points table from a scoring config."""
    return cast(Callable[[int], int], cfg[key])


def result_entry(name: str, total: float) -> tuple[str, float, dict[str, int], list[str]]:
    """A scorer result row with empty parts and tags."""
    return (name, total, {}, [])


def ranked(*names: str) -> list[tuple[str, float, dict[str, int], list[str]]]:
    """Result rows for names, best first, with strictly falling totals."""
    return [result_entry(name, 100.0 - 10 * i) for i, name in enumerate(names)]


def route_field() -> list[dict[str, Any]]:
    """Three engine horses with a clear order and a lone speed horse (A)."""
    return [
        {
            "name": "A",
            "lp": 90,
            "avgspd": 100,
            "primepower": 95,
            "avgdist": 90,
            "lastclass": 95,
            "pl": 2,
            "trainer": "baffert",
            "style": "P",
            "ep1": 50,
        },
        {
            "name": "B",
            "lp": 80,
            "avgspd": 90,
            "primepower": 85,
            "avgdist": 80,
            "lastclass": 85,
            "pl": 6,
            "trainer": "nobody",
            "style": "S",
            "ep1": 40,
        },
        {
            "name": "C",
            "lp": 70,
            "avgspd": 80,
            "primepower": 75,
            "avgdist": 70,
            "lastclass": 75,
            "pl": 15,
            "ep1": 30,
        },
    ]


def sprint_field() -> list[dict[str, Any]]:
    """Two engine horses for the sprint scorer; A has a best-speed figure."""
    return [
        {
            "name": "A",
            "avgspd": 100,
            "primepower": 90,
            "lp": 80,
            "lastclass": 90,
            "bestspd": 105,
            "pp": 1,
            "pl": 2,
            "style": "P",
            "trainer": "baffert",
            "ep1": 40,
        },
        {
            "name": "B",
            "avgspd": 90,
            "primepower": 80,
            "lp": 70,
            "lastclass": 80,
            "pp": 5,
            "pl": 10,
            "style": "S",
            "trainer": "nobody",
            "ep1": 30,
        },
    ]


def pace_only(*ep_values: int) -> list[dict[str, Any]]:
    """Engine horses A, B, C... that only carry an early-pace figure."""
    return [{"name": chr(65 + i), "ep1": ep} for i, ep in enumerate(ep_values)]


def make_participant(name: str, **extra: Any) -> dict[str, Any]:
    """A JSONC participant with every input field filled in."""
    participant: dict[str, Any] = {
        "horse_name": name,
        "horse_trainer": "Baffert",
        "race_jockey": "Smith",
        "horse_description": "bay colt",
        "race_horse_running_style": "Presser",
        "race_horse_program_number": "1",
        "race_horse_morning_line_number": 3.0,
        "race_horse_speed_figures": 90,
        "race_horse_distance_figures": [88, 92],
        "race_horse_class": 85,
        "race_horse_pace": {"early_pace": 80, "late_pace": 85},
        "race_scratched": False,
    }
    participant.update(extra)
    return participant


def make_race(*participants: dict[str, Any], distance: float = 6) -> dict[str, Any]:
    """A JSONC race holding the given participants."""
    return {
        "race_name": "Test Stakes",
        "race_season": "2026",
        "race_date": "2026-11-05",
        "track_location": "Louisville",
        "track_name": "Churchill Downs",
        "race_distance_furlongs": distance,
        "race_track_surface": "Dirt",
        "race_participants": list(participants),
    }


@pytest.fixture(name="race")
def race_fixture() -> dict[str, Any]:
    """Alpha > Bravo > Charlie on every figure, plus a scratched Delta."""
    return make_race(
        make_participant(
            "Alpha",
            race_horse_speed_figures=100,
            race_horse_class=95,
            race_horse_program_number="1",
            race_horse_morning_line_number=2.0,
            race_horse_distance_figures=[95, 97],
            race_horse_pace={"early_pace": 90, "late_pace": 85},
        ),
        make_participant(
            "Bravo",
            race_horse_speed_figures=90,
            race_horse_class=85,
            race_horse_program_number="2",
            race_horse_morning_line_number=4.0,
            race_horse_distance_figures=[85, 87],
            race_horse_pace={"early_pace": 70, "late_pace": 75},
        ),
        make_participant(
            "Charlie",
            race_horse_speed_figures=80,
            race_horse_class=75,
            race_horse_program_number="3",
            race_horse_morning_line_number=12.0,
            race_horse_distance_figures=[75, 77],
            race_horse_pace={"early_pace": 60, "late_pace": 65},
        ),
        make_participant("Delta", race_scratched=True),
    )


@pytest.fixture(name="races_file")
def races_file_fixture(tmp_path: Path, race: dict[str, Any]) -> Path:
    """The fixture race written to disk as a one-race JSONC input file."""
    path = tmp_path / "race.jsonc"
    path.write_text(json.dumps([race]), encoding="utf-8")
    return path


def parse(tmp_path: Path, text: str) -> list[Any]:
    """Run text through load_jsonc."""
    path = tmp_path / "parse.jsonc"
    path.write_text(text, encoding="utf-8")
    return handicap.load_jsonc(str(path))


# --------------------------------------------------------------------------
# Rank -> points tables
# --------------------------------------------------------------------------


def test_tbl_returns_mapped_value() -> None:
    """tbl looks the rank up in the dict."""
    assert handicap.tbl({1: 22, 2: 19}, 3)(2) == 19


def test_tbl_falls_back_to_default() -> None:
    """tbl returns the default for ranks it has no entry for."""
    assert handicap.tbl({1: 22}, 3)(10) == 3


def test_route_table_values() -> None:
    """Spot-check the locked v8 route table."""
    assert lookup(handicap.ROUTE, "lp")(1) == 22
    assert lookup(handicap.ROUTE, "cls")(9) == 5
    assert lookup(handicap.ROUTE, "form")(12) == 0
    assert handicap.ROUTE["trainer_max"] == 9


def test_route_v9_overrides_and_inherits() -> None:
    """v9 reweights class, but keeps untouched keys from v8."""
    assert lookup(handicap.ROUTE_V9, "cls")(1) == 21
    assert handicap.ROUTE_V9["trainer_max"] == 10
    assert handicap.ROUTE_V9["presser"] == handicap.ROUTE["presser"]


def test_turf_only_changes_duel_penalty() -> None:
    """Turf is the route table with a harsher duel penalty."""
    assert handicap.TURF["duel"] == {2: -5, 3: -5}
    assert lookup(handicap.TURF, "lp")(1) == lookup(handicap.ROUTE, "lp")(1)


def test_sprint_v9_caps_are_tighter() -> None:
    """v9 sprint lowers the style, speed and trainer caps."""
    assert handicap.SPRINT["style_cap"] == 12
    assert handicap.SPRINT_V9["style_cap"] == 11
    assert handicap.SPRINT_V9["spd_cap"] == 17
    assert handicap.SPRINT_V9["trainer_max"] == 7


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def test_ranks_handles_ties_and_order() -> None:
    """Higher is better; ties share a rank and the next rank is skipped."""
    horses = [{"x": 10}, {"x": 20}, {"x": 20}, {"x": 5}]
    assert handicap.ranks(horses, "x") == [3, 1, 1, 4]


def test_ranks_treats_missing_and_none_as_last() -> None:
    """Missing, None and zero all count as 0."""
    assert handicap.ranks([{"x": 5}, {}, {"x": None}], "x") == [1, 2, 2]


def test_rank_of_prefers_manual_override() -> None:
    """A horse's own ranks dict beats the computed rank."""
    horse = {"ranks": {"lp": 1}}
    assert handicap.rank_of(horse, {"lp": [4]}, 0, "lp") == 1


def test_rank_of_falls_back_to_computed() -> None:
    """Without an override for that key the computed rank is used."""
    assert handicap.rank_of({}, {"lp": [4, 2]}, 1, "lp") == 2
    assert handicap.rank_of({"ranks": {"spd": 1}}, {"lp": [4]}, 0, "lp") == 4


@pytest.mark.parametrize(
    ("odds", "expected"),
    [(1.5, 12), (2, 12), (3, 10), (5, 9), (8, 7), (12, 5), (20, 4), (50, 1)],
)
def test_market_pts_steps(odds: float, expected: int) -> None:
    """Each odds bracket scales the top points (top=12)."""
    assert handicap.market_pts(odds, 12) == expected


def test_trainer_pts_known_tier() -> None:
    """Top-tier trainers get the full max; lookup ignores case."""
    assert handicap.trainer_pts("baffert", 9) == 9
    assert handicap.trainer_pts("BAFFERT", 9) == 9
    assert handicap.trainer_pts("mott", 9) == 8


def test_trainer_pts_unknown_or_missing_defaults_to_70_percent() -> None:
    """Unknown names and None both get 0.7 of the max."""
    assert handicap.trainer_pts("nobody", 9) == 6
    assert handicap.trainer_pts(None, 9) == 6


def test_pace_scenario_single_horse() -> None:
    """One horse cannot be a lone speed or in a duel."""
    assert handicap.pace_scenario(pace_only(50)) == (None, [])


def test_pace_scenario_lone_speed_at_threshold() -> None:
    """A lead of exactly 8 makes the top horse the lone speed."""
    assert handicap.pace_scenario(pace_only(58, 50, 40)) == ("A", [])


def test_pace_scenario_duel_group() -> None:
    """Horses within 4 of the top EP1 form the duel group, best first."""
    assert handicap.pace_scenario(pace_only(90, 88, 87, 70)) == (
        None,
        ["A", "B", "C"],
    )


def test_pace_scenario_no_duel_when_only_one_near_top() -> None:
    """A gap of 5-7 is neither a lone speed nor a duel."""
    assert handicap.pace_scenario(pace_only(90, 85)) == (None, [])


def test_pace_scenario_missing_pace_counts_as_zero() -> None:
    """Horses without ep1 all sit at 0, which makes them a duel group."""
    horses: list[dict[str, Any]] = [{"name": "A"}, {"name": "B"}]
    assert handicap.pace_scenario(horses) == (None, ["B", "A"])


# --------------------------------------------------------------------------
# Scorers
# --------------------------------------------------------------------------


def test_route_like_scores_each_horse() -> None:
    """Every component for a three-horse route field (A is lone presser)."""
    results = {
        row[0]: row for row in handicap.score_route_like(route_field(), handicap.ROUTE)
    }
    _, total, parts, tags = results["A"]
    assert parts == {
        "lp": 22,
        "class": 17,
        "speed": 17,
        "dist": 13,
        "form": 13,
        "trainer": 9,
        "workout": 9,
        "adj": 8,
    }
    assert total == sum(parts.values())
    assert tags == ["PRESSER", "LONE_SPEED"]
    _, _, parts, tags = results["B"]
    assert parts == {
        "lp": 19,
        "class": 16,
        "speed": 15,
        "dist": 11,
        "form": 12,
        "trainer": 6,
        "workout": 5,
        "adj": 3,
    }
    assert tags == ["CLOSER"]
    _, _, parts, tags = results["C"]
    assert parts == {
        "lp": 17,
        "class": 15,
        "speed": 14,
        "dist": 10,
        "form": 11,
        "trainer": 6,
        "workout": 3,
        "adj": 0,
    }
    assert not tags


def test_route_like_v9_uses_v9_tables() -> None:
    """The v9 config upweights class for the top horse."""
    results = handicap.score_route_like(route_field(), handicap.ROUTE_V9)
    parts = next(row[2] for row in results if row[0] == "A")
    assert parts["class"] == 21
    assert parts["trainer"] == 10


def test_route_like_duel_penalty_route_vs_turf() -> None:
    """A two-horse duel costs 3 on route and 5 on turf."""
    for cfg, penalty in ((handicap.ROUTE, -3), (handicap.TURF, -5)):
        results = handicap.score_route_like(pace_only(50, 49), cfg)
        for _, _, parts, tags in results:
            assert parts["adj"] == penalty
            assert "DUEL" in tags


def test_route_like_manual_lone_and_duel_overrides() -> None:
    """Per-horse lone/duel keys override the computed pace scenario."""
    horses: list[dict[str, Any]] = [
        {"name": "A", "lone": True, "duel": False},
        {"name": "B"},
    ]
    by_name = {row[0]: row for row in handicap.score_route_like(horses, handicap.ROUTE)}
    assert by_name["A"][3] == ["LONE_SPEED"]
    assert by_name["B"][3] == ["DUEL"]


def test_route_like_manual_rank_override() -> None:
    """A horse's own ranks dict replaces the computed rank for that metric."""
    horses: list[dict[str, Any]] = [
        {"name": "A", "avgspd": 10, "ranks": {"avgspd": 5}},
        {"name": "B", "avgspd": 20},
    ]
    by_name = {row[0]: row for row in handicap.score_route_like(horses, handicap.ROUTE)}
    assert by_name["A"][2]["speed"] == 10
    assert by_name["B"][2]["speed"] == 17


def test_sprint_scores_each_horse() -> None:
    """Every component for a two-horse sprint field."""
    results = {
        row[0]: row for row in handicap.score_sprint(sprint_field(), handicap.SPRINT)
    }
    _, total, parts, tags = results["A"]
    assert parts == {
        "speed": 19,
        "class": 19,
        "pace": 15,
        "post": 8,
        "market": 12,
        "style": 12,
        "drop": 8,
        "trainer": 8,
        "lone": 4,
    }
    assert total == sum(parts.values())
    assert tags == ["PRESSER", "BESTSPD1", "LONE_SPEED"]
    _, _, parts, tags = results["B"]
    assert parts == {
        "speed": 17,
        "class": 17,
        "pace": 13,
        "post": 5,
        "market": 5,
        "style": 8,
        "drop": 7,
        "trainer": 6,
    }
    assert not tags


def test_sprint_v9_caps_speed_and_style() -> None:
    """v9 caps A's speed at 17 and style at 11, and reweights class/trainer."""
    results = handicap.score_sprint(sprint_field(), handicap.SPRINT_V9)
    parts = next(row[2] for row in results if row[0] == "A")
    assert parts["speed"] == 17
    assert parts["style"] == 11
    assert parts["class"] == 23
    assert parts["trainer"] == 7


def test_sprint_best_speed_bonus_stops_after_rank_three() -> None:
    """Only the top three best-speed ranks earn the bonus and tag."""
    horses: list[dict[str, Any]] = [
        {"name": "A", "bestspd": 100},
        {"name": "B", "bestspd": 90},
        {"name": "C", "bestspd": 80},
        {"name": "D", "bestspd": 70},
    ]
    tags = {row[0]: row[3] for row in handicap.score_sprint(horses, handicap.SPRINT)}
    assert "BESTSPD3" in tags["C"]
    assert not any(tag.startswith("BESTSPD") for tag in tags["D"])


def test_sprint_style_points_default_and_cap() -> None:
    """Missing style counts as E/P (12), unknown as 8, a presser is capped."""
    horses: list[dict[str, Any]] = [
        {"name": "A"},
        {"name": "B", "style": "S"},
        {"name": "C", "style": "X"},
        {"name": "D", "style": "P"},
    ]
    style = {
        row[0]: row[2]["style"] for row in handicap.score_sprint(horses, handicap.SPRINT)
    }
    assert style == {"A": 12, "B": 8, "C": 8, "D": 12}


def test_sprint_duel_penalty_scales_with_group_size() -> None:
    """A two-horse duel costs 3; a three-horse duel costs 5."""
    two = handicap.score_sprint(pace_only(50, 49), handicap.SPRINT)
    three = handicap.score_sprint(pace_only(50, 49, 48), handicap.SPRINT)
    assert all(row[2]["duel"] == -3 for row in two)
    assert all(row[2]["duel"] == -5 for row in three)


def test_sprint_manual_lone_override() -> None:
    """A horse's lone key adds the lone-speed bonus and tag."""
    horses: list[dict[str, Any]] = [
        {"name": "A", "lone": True, "duel": False},
        {"name": "B"},
    ]
    row = next(
        row for row in handicap.score_sprint(horses, handicap.SPRINT) if row[0] == "A"
    )
    assert row[2]["lone"] == 4
    assert "LONE_SPEED" in row[3]
    assert "duel" not in row[2]


@pytest.mark.parametrize(
    ("distance", "surface", "expected"),
    [
        (6, "dirt", "sprint"),
        (7.5, "dirt", "sprint"),
        (8, "dirt", "route"),
        (8.5, "synthetic", "route"),
        (6, "turf", "turf"),
        (10, "turf", "turf"),
    ],
)
def test_pick_formula(distance: float, surface: str, expected: str) -> None:
    """Turf always wins; otherwise under 8f is a sprint."""
    assert handicap.pick_formula(distance, surface) == expected


def test_score_race_sprint_sorted_best_first() -> None:
    """A sprint race returns its kind and horses ordered by total."""
    race = {"name": "R", "distance_f": 6, "horses": sprint_field()}
    kind, results = handicap.score_race(race)
    assert kind == "sprint"
    assert [row[0] for row in results] == ["A", "B"]
    assert results[0][1] > results[1][1]


def test_score_race_skips_scratched_horses() -> None:
    """Scratched horses are not scored."""
    horses = sprint_field()
    horses[1]["scratched"] = True
    _, results = handicap.score_race({"name": "R", "distance_f": 6, "horses": horses})
    assert [row[0] for row in results] == ["A"]


def test_score_race_route_and_turf_kinds() -> None:
    """Distance and surface pick route or turf; surface defaults to dirt."""
    route = {"name": "R", "distance_f": 8.5, "horses": route_field()}
    turf = {
        "name": "R",
        "distance_f": 8.5,
        "surface": "turf",
        "horses": route_field(),
    }
    assert handicap.score_race(route)[0] == "route"
    assert handicap.score_race(turf)[0] == "turf"


def test_score_race_v9_matches_direct_v9_scoring() -> None:
    """version='v9' scores with SPRINT_V9."""
    race = {"name": "R", "distance_f": 6, "horses": sprint_field()}
    expected = sorted(
        handicap.score_sprint(sprint_field(), handicap.SPRINT_V9),
        key=lambda row: -row[1],
    )
    assert handicap.score_race(race, "v9")[1] == expected


def test_score_race_unknown_version_uses_v8() -> None:
    """Anything other than 'v9' falls back to the v8 tables."""
    race = {"name": "R", "distance_f": 6, "horses": sprint_field()}
    assert handicap.score_race(race, "v7")[1] == handicap.score_race(race, "v8")[1]


# --------------------------------------------------------------------------
# PP-sheet derived mode
# --------------------------------------------------------------------------

PP_BASE: dict[str, Any] = {
    "foreign_grade": "G1"
}  # 25 + 8 class + 10 + 10 trainer/jockey + 2 odds


def test_pp_sheet_full_example() -> None:
    """Every term of the PP-sheet score adds up."""
    horse = {
        "spd": 100,
        "class": "stk300",
        "style": "E/P",
        "trainer_pts": 8,
        "jockey_pts": 7,
        "odds": 3,
        "life": "5-3-2-0",
    }
    assert handicap.score_pp_sheet(horse) == pytest.approx(81.8)


def test_pp_sheet_defaults() -> None:
    """With only a foreign grade, defaults fill every other term."""
    assert handicap.score_pp_sheet(PP_BASE) == pytest.approx(45.0)


def test_pp_sheet_foreign_floor_grades() -> None:
    """Foreign grades replace a missing speed figure; zero is missing."""
    assert handicap.score_pp_sheet({"foreign_grade": "G2"}) == pytest.approx(44.0)
    assert handicap.score_pp_sheet({"foreign_grade": "G3"}) == pytest.approx(43.0)
    assert handicap.score_pp_sheet({"foreign_grade": "LISTED"}) == pytest.approx(42.0)
    assert handicap.score_pp_sheet({"spd": 0, "foreign_grade": "G1"}) == pytest.approx(
        45.0
    )


def test_pp_sheet_speed_points_are_capped_at_26() -> None:
    """spd * 0.25 stops counting past 26 points."""
    assert handicap.score_pp_sheet({"spd": 200}) == handicap.score_pp_sheet({"spd": 104})


def test_pp_sheet_style_points() -> None:
    """Running style adds 3 / 4 / 2 / 2 points and unknown styles add none."""
    assert handicap.score_pp_sheet({**PP_BASE, "style": "E"}) == pytest.approx(48.0)
    assert handicap.score_pp_sheet({**PP_BASE, "style": "E/P"}) == pytest.approx(49.0)
    assert handicap.score_pp_sheet({**PP_BASE, "style": "P"}) == pytest.approx(47.0)
    assert handicap.score_pp_sheet({**PP_BASE, "style": "S"}) == pytest.approx(47.0)
    assert handicap.score_pp_sheet({**PP_BASE, "style": "X"}) == pytest.approx(45.0)


@pytest.mark.parametrize(
    ("odds", "expected"),
    [
        (1, 57.0),
        (2, 57.0),
        (4, 55.0),
        (6, 53.0),
        (9, 51.0),
        (13, 49.0),
        (16, 47.0),
        (30, 45.0),
    ],
)
def test_pp_sheet_odds_brackets(odds: float, expected: float) -> None:
    """Shorter odds earn more points."""
    assert handicap.score_pp_sheet({**PP_BASE, "odds": odds}) == pytest.approx(expected)


def test_pp_sheet_life_record() -> None:
    """Win rate and starts add up to at most 10 points."""
    assert handicap.score_pp_sheet({**PP_BASE, "life": "10-0"}) == pytest.approx(49.0)
    assert handicap.score_pp_sheet({**PP_BASE, "life": "4-4"}) == pytest.approx(55.0)


def test_pp_sheet_first_route_try_penalty() -> None:
    """Sprint-only horses lose 4 points when the race is a route."""
    horse = {**PP_BASE, "sprint_only": True}
    assert handicap.score_pp_sheet(horse, route_distance=True) == pytest.approx(41.0)
    assert handicap.score_pp_sheet(horse) == pytest.approx(45.0)
    assert handicap.score_pp_sheet(PP_BASE, route_distance=True) == pytest.approx(45.0)


# --------------------------------------------------------------------------
# Race confidence
# --------------------------------------------------------------------------


def test_conf_margin() -> None:
    """The top-two gap is scaled by MARGIN_FULL and capped at 1."""
    assert handicap.conf_margin(ranked("A")) == 1.0
    assert handicap.conf_margin([result_entry("A", 80), result_entry("B", 75)]) == 0.5
    assert handicap.conf_margin([result_entry("A", 80), result_entry("B", 80)]) == 0.0
    assert handicap.conf_margin([result_entry("A", 99), result_entry("B", 50)]) == 1.0


def test_conf_agreement_identical_rankings() -> None:
    """Same top pick and same top three is full agreement."""
    assert handicap.conf_agreement(ranked("A", "B", "C"), ranked("A", "B", "C")) == 1.0
    assert handicap.conf_agreement(ranked("A", "B"), ranked("A", "B")) == 1.0


def test_conf_agreement_partial() -> None:
    """Same winner with a different supporting cast, or a swapped winner."""
    same_top = handicap.conf_agreement(ranked("A", "B", "C"), ranked("A", "D", "E"))
    assert same_top == pytest.approx(0.6 + 0.4 / 3)
    swapped = handicap.conf_agreement(ranked("A", "B", "C"), ranked("B", "A", "C"))
    assert swapped == pytest.approx(0.4)
    assert handicap.conf_agreement(ranked("A", "B", "C"), ranked("D", "E", "F")) == 0.0


def test_conf_data_counts_present_fields() -> None:
    """Zero is a present value; None and missing are not."""
    full = {field: 1 for field in handicap.DATA_FIELDS}
    half = {field: 0 for field in handicap.DATA_FIELDS[:4]}
    assert handicap.conf_data([full]) == 1.0
    assert handicap.conf_data([half]) == 0.5
    assert handicap.conf_data([{"name": "A", "lp": None}]) == 0.0
    assert handicap.conf_data([full, {}]) == 0.5


def test_conf_field_slides_with_field_size() -> None:
    """Small fields get 1.0, big fields the floor, linear slide between."""
    assert handicap.conf_field(6) == 1.0
    assert handicap.conf_field(14) == handicap.FIELD_FLOOR
    assert handicap.conf_field(20) == handicap.FIELD_FLOOR
    assert handicap.conf_field(10) == pytest.approx(0.7)


def test_conf_pace() -> None:
    """Lone speed is clean; two-way and three-way duels lower confidence."""
    assert handicap.conf_pace(pace_only(60, 50)) == 1.0
    assert handicap.conf_pace(pace_only(50, 48, 30)) == handicap.DUEL2_CONF
    assert handicap.conf_pace(pace_only(50, 49, 48)) == handicap.DUEL3_CONF
    assert handicap.conf_pace(pace_only(50, 45)) == 1.0


@pytest.mark.parametrize(
    ("score", "label"),
    [
        (100, "HIGH"),
        (70, "HIGH"),
        (69, "MEDIUM"),
        (45, "MEDIUM"),
        (44, "LOW"),
        (0, "LOW"),
    ],
)
def test_confidence_label_cutoffs(score: int, label: str) -> None:
    """Cutoffs are inclusive at 70 and 45."""
    assert handicap.confidence_label(score) == label


def test_race_confidence_maxes_out() -> None:
    """A clear, complete, agreed, small, clean-pace race scores 100."""
    full = {field: 1 for field in handicap.DATA_FIELDS}
    horses = [
        {"name": "A", **full, "ep1": 60},
        {"name": "B", **full, "ep1": 50},
    ]
    race = {"horses": horses}
    res = [result_entry("A", 90), result_entry("B", 70)]
    conf = handicap.race_confidence(race, res, res)
    assert conf["score"] == 100
    assert conf["label"] == "HIGH"
    assert set(conf["components"]) == {
        "margin",
        "agreement",
        "data",
        "field",
        "pace",
    }


def test_race_confidence_low_case_ignores_scratched() -> None:
    """Tied totals, swapped picks, no data and a duel give a LOW 27."""
    horses = [{"name": "A"}, {"name": "B"}, {"name": "C", "scratched": True}]
    race = {"horses": horses}
    res = [result_entry("A", 50), result_entry("B", 50)]
    other = [result_entry("B", 50), result_entry("A", 50)]
    conf = handicap.race_confidence(race, res, other)
    assert conf["score"] == 27
    assert conf["label"] == "LOW"


def test_analyze_returns_kind_results_and_confidence() -> None:
    """analyze bundles the formula kind, ranked results and confidence."""
    race = {"name": "R", "distance_f": 6, "horses": sprint_field()}
    for version in ("v8", "v9"):
        kind, results, conf = handicap.analyze(race, version)
        assert kind == "sprint"
        assert [row[0] for row in results] == ["A", "B"]
        assert conf["label"] in {"HIGH", "MEDIUM", "LOW"}


def test_show_prints_table_and_returns_results(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """show prints the header, ranked rows and confidence line."""
    race = {"name": "Test Stakes", "distance_f": 6, "horses": sprint_field()}
    results = handicap.show(race)
    out = capsys.readouterr().out
    assert "Test Stakes  |  SPRINT v8  |  /100" in out
    assert " 1. A" in out
    assert " 2. B" in out
    assert "PRESSER BESTSPD1 LONE_SPEED" in out
    assert "Confidence:" in out
    assert [row[0] for row in results] == ["A", "B"]


# --------------------------------------------------------------------------
# JSONC reader
# --------------------------------------------------------------------------


def test_load_jsonc_plain_json(tmp_path: Path) -> None:
    """Ordinary JSON loads unchanged."""
    assert parse(tmp_path, '[{"a": 1, "b": [1, 2]}]') == [{"a": 1, "b": [1, 2]}]


def test_load_jsonc_skips_comments(tmp_path: Path) -> None:
    """Line and block comments are ignored."""
    text = '[\n  // line\n  {"a": 1, /* block */ "b": 2}, // tail\n]'
    assert parse(tmp_path, text) == [{"a": 1, "b": 2}]


def test_load_jsonc_allows_trailing_commas(tmp_path: Path) -> None:
    """Trailing commas in objects and arrays are fine."""
    assert parse(tmp_path, '[{"a": [1, 2,],},]') == [{"a": [1, 2]}]


def test_load_jsonc_reads_keyed_arrays_as_objects(tmp_path: Path) -> None:
    """The file's [ "key": value ] pace blocks become dicts."""
    text = '[{"pace": ["early_pace": 80, "late_pace": 75]}]'
    assert parse(tmp_path, text) == [{"pace": {"early_pace": 80, "late_pace": 75}}]


def test_load_jsonc_wraps_a_single_object(tmp_path: Path) -> None:
    """A top-level object comes back as a one-race list."""
    assert parse(tmp_path, '{"a": 1}') == [{"a": 1}]


def test_load_jsonc_numbers_and_literals(tmp_path: Path) -> None:
    """Ints stay ints, decimals/exponents become floats, literals map over."""
    values = parse(tmp_path, "[1, -2, 3.5, 1e3, 2E-1, true, false, null]")
    assert values == [1, -2, 3.5, 1000.0, 0.2, True, False, None]
    assert isinstance(values[0], int)
    assert isinstance(values[3], float)


def test_load_jsonc_string_escapes_and_unicode(tmp_path: Path) -> None:
    """Escapes are decoded and the file is read as UTF-8."""
    text = '["a\\"b", "x\\ty", "\\u00e9", "caf\u00e9"]'
    assert parse(tmp_path, text) == ['a"b', "x\ty", "\u00e9", "caf\u00e9"]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[1 /* oops", "unterminated comment"),
        ('["abc', "unterminated string"),
        ("   ", "unexpected end of file"),
        ("[@]", "unexpected '@'"),
        ('[{"a" 1}]', "expected ':'"),
        ("[{1: 2}]", "expected a quoted string"),
    ],
)
def test_load_jsonc_errors(tmp_path: Path, text: str, message: str) -> None:
    """Malformed input raises ValueError naming the problem."""
    with pytest.raises(ValueError, match=message):
        parse(tmp_path, text)


def test_load_jsonc_error_reports_line_number(tmp_path: Path) -> None:
    """Parse errors say which line they hit."""
    with pytest.raises(ValueError, match="line 3"):
        parse(tmp_path, "[\n\n@]")


# --------------------------------------------------------------------------
# Input readers
# --------------------------------------------------------------------------


def test_in_race_name() -> None:
    """Race name is required."""
    assert handicap.in_race_name({"race_name": "Derby"}) == "Derby"
    with pytest.raises(KeyError):
        handicap.in_race_name({})


def test_in_race_distance() -> None:
    """Distance is required."""
    assert handicap.in_race_distance({"race_distance_furlongs": 8.5}) == 8.5
    with pytest.raises(KeyError):
        handicap.in_race_distance({})


def test_in_race_surface() -> None:
    """Surface is lower-cased and defaults to dirt."""
    assert handicap.in_race_surface({"race_track_surface": "Turf"}) == "turf"
    assert handicap.in_race_surface({}) == "dirt"
    assert handicap.in_race_surface({"race_track_surface": None}) == "dirt"


def test_in_race_participants() -> None:
    """Missing or null participants read as an empty list."""
    assert handicap.in_race_participants({"race_participants": [{"a": 1}]}) == [{"a": 1}]
    assert handicap.in_race_participants({}) == []
    assert handicap.in_race_participants({"race_participants": None}) == []


def test_in_horse_name() -> None:
    """Horse name is required."""
    assert handicap.in_horse_name({"horse_name": "Alpha"}) == "Alpha"
    with pytest.raises(KeyError):
        handicap.in_horse_name({})


def test_in_horse_trainer() -> None:
    """Trainer is optional."""
    assert handicap.in_horse_trainer({"horse_trainer": "Mott"}) == "Mott"
    assert handicap.in_horse_trainer({}) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Early", "E"),
        ("Speed", "E"),
        ("Front Runner", "E"),
        ("E/P", "E/P"),
        ("Presser", "P"),
        ("Stalker", "P"),
        ("Closer", "S"),
        ("Sustained", "S"),
        ("  s  ", "S"),
        ("Sprinter", None),
        ("", None),
        (None, None),
    ],
)
def test_in_horse_style(raw: str | None, expected: str | None) -> None:
    """Running styles map onto E, E/P, P and S; anything else is None."""
    assert handicap.in_horse_style({"race_horse_running_style": raw}) == expected


def test_in_horse_style_missing_key() -> None:
    """No style key reads as None."""
    assert handicap.in_horse_style({}) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("5", 5), ("12A", 12), (7, 7), ("A5", None), ("", None), (None, None)],
)
def test_in_horse_post(raw: str | int | None, expected: int | None) -> None:
    """The leading digits of the program number become the post."""
    assert handicap.in_horse_post({"race_horse_program_number": raw}) == expected


def test_in_horse_morning_line() -> None:
    """Morning line is passed through."""
    assert handicap.in_horse_morning_line({"race_horse_morning_line_number": 2.5}) == 2.5
    assert handicap.in_horse_morning_line({}) is None


def test_in_horse_speed() -> None:
    """Speed figure is passed through."""
    assert handicap.in_horse_speed({"race_horse_speed_figures": 95}) == 95
    assert handicap.in_horse_speed({}) is None


def test_in_horse_distance() -> None:
    """Distance figures average, ignoring None; empty or missing is None."""
    assert handicap.in_horse_distance({"race_horse_distance_figures": [80, 90]}) == 85
    assert handicap.in_horse_distance({"race_horse_distance_figures": [80, None]}) == 80
    assert handicap.in_horse_distance({"race_horse_distance_figures": []}) is None
    assert handicap.in_horse_distance({"race_horse_distance_figures": [None]}) is None
    assert handicap.in_horse_distance({}) is None


def test_in_horse_class() -> None:
    """Class is passed through."""
    assert handicap.in_horse_class({"race_horse_class": 88}) == 88
    assert handicap.in_horse_class({}) is None


def test_in_horse_early_pace() -> None:
    """Early pace comes from the pace block; list or missing gives None."""
    assert handicap.in_horse_early_pace({"race_horse_pace": {"early_pace": 80}}) == 80
    assert handicap.in_horse_early_pace({"race_horse_pace": [80, 85]}) is None
    assert handicap.in_horse_early_pace({}) is None


def test_in_horse_late_pace() -> None:
    """Late pace comes from the pace block; list or missing gives None."""
    assert handicap.in_horse_late_pace({"race_horse_pace": {"late_pace": 85}}) == 85
    assert handicap.in_horse_late_pace({"race_horse_pace": [80, 85]}) is None
    assert handicap.in_horse_late_pace({}) is None


def test_in_horse_scratched() -> None:
    """Scratched is read as a bool."""
    assert handicap.in_horse_scratched({"race_scratched": True}) is True
    assert handicap.in_horse_scratched({"race_scratched": False}) is False
    assert handicap.in_horse_scratched({}) is False


def test_engine_horse_maps_every_field() -> None:
    """A full participant becomes the dict shape the scorers expect."""
    horse = handicap.engine_horse(make_participant("Alpha"))
    assert horse == {
        "name": "Alpha",
        "scratched": False,
        "trainer": "Baffert",
        "style": "P",
        "pp": 1,
        "pl": 3.0,
        "avgspd": 90,
        "avgdist": 90,
        "primepower": 85,
        "lastclass": 85,
        "ep1": 80,
        "lp": 85,
    }


def test_engine_horse_omits_missing_values() -> None:
    """Only name and scratched survive when the file has nothing else."""
    horse = handicap.engine_horse({"horse_name": "Bare"})
    assert horse == {"name": "Bare", "scratched": False}


def test_engine_race_shape() -> None:
    """The race dict carries name, distance, surface and engine horses."""
    race = handicap.engine_race(make_race(make_participant("Alpha"), distance=8))
    assert race["name"] == "Test Stakes"
    assert race["distance_f"] == 8
    assert race["surface"] == "dirt"
    assert [horse["name"] for horse in race["horses"]] == ["Alpha"]


# --------------------------------------------------------------------------
# Win probability and model columns
# --------------------------------------------------------------------------


def test_win_probabilities_sum_to_one_and_order() -> None:
    """Softmax probabilities sum to 1 and follow the score order."""
    probs = handicap.win_probabilities([80, 70, 60])
    assert sum(probs) == pytest.approx(1.0)
    assert probs[0] > probs[1] > probs[2]


def test_win_probabilities_known_value() -> None:
    """A gap of exactly one temperature gives 1 / (1 + e^-1)."""
    probs = handicap.win_probabilities([8, 0], temp=8.0)
    assert probs[0] == pytest.approx(1 / (1 + math.exp(-1)))


def test_win_probabilities_edge_cases() -> None:
    """Single horse, ties and huge scores stay well-behaved."""
    assert handicap.win_probabilities([42]) == [1.0]
    assert handicap.win_probabilities([50, 50]) == [0.5, 0.5]
    assert handicap.win_probabilities([1000, 1000]) == [0.5, 0.5]


def test_lower_temperature_favours_the_favourite() -> None:
    """A smaller temperature gives the top horse a bigger share."""
    cold = handicap.win_probabilities([80, 70], temp=2.0)
    hot = handicap.win_probabilities([80, 70], temp=20.0)
    assert cold[0] > hot[0]


def test_pick_is_a_named_tuple() -> None:
    """Pick exposes rank, name, score and prob."""
    pick = handicap.Pick(1, "Alpha", 85.26, 0.45678)
    assert (pick.rank, pick.name, pick.score, pick.prob) == (
        1,
        "Alpha",
        85.26,
        0.45678,
    )


def test_col_predicted_rank() -> None:
    """The column is the pick's rank."""
    assert handicap.col_predicted_rank(handicap.Pick(3, "A", 70.0, 0.2)) == 3


def test_col_is_predicted_winner() -> None:
    """Only rank 1 is the predicted winner."""
    assert handicap.col_is_predicted_winner(handicap.Pick(1, "A", 70.0, 0.2)) is True
    assert handicap.col_is_predicted_winner(handicap.Pick(2, "A", 70.0, 0.2)) is False


def test_col_score() -> None:
    """Scores round to one decimal."""
    assert handicap.col_score(handicap.Pick(1, "A", 85.26, 0.2)) == 85.3
    assert isinstance(handicap.col_score(handicap.Pick(1, "A", 85, 0.2)), float)


def test_col_win_probability() -> None:
    """Probabilities round to four decimals."""
    assert handicap.col_win_probability(handicap.Pick(1, "A", 70.0, 0.45678)) == 0.4568


def test_col_confidence_score_and_label() -> None:
    """Confidence columns read straight from the confidence dict."""
    conf = {"score": 55, "label": "MEDIUM"}
    assert handicap.col_confidence_score(conf) == 55
    assert handicap.col_confidence_label(conf) == "MEDIUM"


def test_score_jsonc_race_all_scratched_is_none() -> None:
    """With every horse scratched there are no picks and no confidence."""
    race = make_race(make_participant("Solo", race_scratched=True))
    assert handicap.score_jsonc_race(race) == (None, None)


def test_score_jsonc_race_picks(race: dict[str, Any]) -> None:
    """Running horses get ranks 1..n and probabilities that sum to 1."""
    picks, conf = handicap.score_jsonc_race(race)
    assert picks is not None
    assert conf is not None
    assert set(picks) == {"Alpha", "Bravo", "Charlie"}
    assert [picks[name].rank for name in ("Alpha", "Bravo", "Charlie")] == [
        1,
        2,
        3,
    ]
    assert picks["Alpha"].score > picks["Bravo"].score > picks["Charlie"].score
    assert sum(pick.prob for pick in picks.values()) == pytest.approx(1.0)
    assert conf["label"] in {"HIGH", "MEDIUM", "LOW"}


def test_score_jsonc_race_v9_still_ranks_alpha_first(
    race: dict[str, Any],
) -> None:
    """The v9 shadow model also runs end to end."""
    picks, _ = handicap.score_jsonc_race(race, "v9")
    assert picks is not None
    assert picks["Alpha"].rank == 1


# --------------------------------------------------------------------------
# JSONC writer: one test per output function
# --------------------------------------------------------------------------

IND = "  "


def test_out_race_name() -> None:
    """Echoes race_name, or None when absent."""
    assert (
        handicap.out_race_name({"race_name": "Derby"}, IND) == '  "race_name": "Derby",'
    )
    assert handicap.out_race_name({}, IND) is None


def test_out_race_season() -> None:
    """Echoes race_season, or None when absent."""
    assert (
        handicap.out_race_season({"race_season": "2026"}, IND)
        == '  "race_season": "2026",'
    )
    assert handicap.out_race_season({}, IND) is None


def test_out_race_date() -> None:
    """Echoes race_date, or None when absent."""
    line = handicap.out_race_date({"race_date": "2026-11-05"}, IND)
    assert line == '  "race_date": "2026-11-05",'
    assert handicap.out_race_date({}, IND) is None


def test_out_track_location() -> None:
    """Echoes track_location, or None when absent."""
    line = handicap.out_track_location({"track_location": "Louisville"}, IND)
    assert line == '  "track_location": "Louisville",'
    assert handicap.out_track_location({}, IND) is None


def test_out_track_name() -> None:
    """Echoes track_name, or None when absent."""
    line = handicap.out_track_name({"track_name": "Churchill Downs"}, IND)
    assert line == '  "track_name": "Churchill Downs",'
    assert handicap.out_track_name({}, IND) is None


def test_out_race_distance_furlongs() -> None:
    """Echoes race_distance_furlongs as a bare number, or None when absent."""
    line = handicap.out_race_distance_furlongs({"race_distance_furlongs": 6}, IND)
    assert line == '  "race_distance_furlongs": 6,'
    assert handicap.out_race_distance_furlongs({}, IND) is None


def test_out_race_track_surface() -> None:
    """Echoes race_track_surface as it was read, or None when absent."""
    line = handicap.out_race_track_surface({"race_track_surface": "Dirt"}, IND)
    assert line == '  "race_track_surface": "Dirt",'
    assert handicap.out_race_track_surface({}, IND) is None


def test_out_horse_name_keeps_non_ascii() -> None:
    """Echoes horse_name without escaping non-ASCII characters."""
    line = handicap.out_horse_name({"horse_name": "Caf\u00e9"}, IND)
    assert line == '  "horse_name": "Caf\u00e9",'
    assert handicap.out_horse_name({}, IND) is None


def test_out_horse_trainer() -> None:
    """Echoes horse_trainer, or None when absent."""
    line = handicap.out_horse_trainer({"horse_trainer": "Baffert"}, IND)
    assert line == '  "horse_trainer": "Baffert",'
    assert handicap.out_horse_trainer({}, IND) is None


def test_out_race_jockey() -> None:
    """Echoes race_jockey, or None when absent."""
    assert (
        handicap.out_race_jockey({"race_jockey": "Smith"}, IND)
        == '  "race_jockey": "Smith",'
    )
    assert handicap.out_race_jockey({}, IND) is None


def test_out_horse_description() -> None:
    """Echoes horse_description, or None when absent."""
    line = handicap.out_horse_description({"horse_description": "bay colt"}, IND)
    assert line == '  "horse_description": "bay colt",'
    assert handicap.out_horse_description({}, IND) is None


def test_out_race_horse_running_style_has_comment_tail() -> None:
    """Echoes the style with the trailing 'race basis' comment, or None."""
    line = handicap.out_race_horse_running_style(
        {"race_horse_running_style": "Presser"}, IND
    )
    assert line == f'  "race_horse_running_style": "Presser",{STYLE_TAIL}'
    assert handicap.out_race_horse_running_style({}, IND) is None


def test_out_race_horse_program_number() -> None:
    """Echoes the program number as read, or None when absent."""
    line = handicap.out_race_horse_program_number(
        {"race_horse_program_number": "5A"}, IND
    )
    assert line == '  "race_horse_program_number": "5A",'
    assert handicap.out_race_horse_program_number({}, IND) is None


def test_out_race_horse_morning_line_number() -> None:
    """Echoes the morning line, or None when absent."""
    line = handicap.out_race_horse_morning_line_number(
        {"race_horse_morning_line_number": 2.5}, IND
    )
    assert line == '  "race_horse_morning_line_number": 2.5,'
    assert handicap.out_race_horse_morning_line_number({}, IND) is None


def test_out_race_horse_speed_figures() -> None:
    """Echoes the speed figures, or None when absent."""
    line = handicap.out_race_horse_speed_figures({"race_horse_speed_figures": 95}, IND)
    assert line == '  "race_horse_speed_figures": 95,'
    assert handicap.out_race_horse_speed_figures({}, IND) is None


def test_out_race_horse_distance_figures_list() -> None:
    """A list of distance figures is written one figure per line."""
    participant = {"race_horse_distance_figures": [90, None, 92.5]}
    text = handicap.out_race_horse_distance_figures(participant, IND)
    assert text == (
        '  "race_horse_distance_figures": [\n'
        "    90,\n"
        "    null,\n"
        "    92.5,\n"
        "  ],"
    )


def test_out_race_horse_distance_figures_non_list() -> None:
    """A non-list value is echoed as-is and a missing one is None."""
    line = handicap.out_race_horse_distance_figures(
        {"race_horse_distance_figures": 90}, IND
    )
    assert line == '  "race_horse_distance_figures": 90,'
    assert handicap.out_race_horse_distance_figures({}, IND) is None


def test_out_race_horse_class() -> None:
    """Echoes the class figure, or None when absent."""
    line = handicap.out_race_horse_class({"race_horse_class": 88}, IND)
    assert line == '  "race_horse_class": 88,'
    assert handicap.out_race_horse_class({}, IND) is None


def test_out_race_horse_pace_block() -> None:
    """The pace block is written as a bracketed key/value group."""
    participant = {"race_horse_pace": {"early_pace": 80, "late_pace": 85}}
    assert handicap.out_race_horse_pace(participant, IND) == (
        '  "race_horse_pace": [\n'
        '    "early_pace": 80,\n'
        '    "late_pace": 85,\n'
        "  ],"
    )


def test_out_race_horse_pace_partial_and_empty() -> None:
    """Missing halves are dropped; empty, list or absent blocks give None."""
    partial = handicap.out_race_horse_pace({"race_horse_pace": {"late_pace": 85}}, IND)
    assert partial == '  "race_horse_pace": [\n    "late_pace": 85,\n  ],'
    assert handicap.out_race_horse_pace({"race_horse_pace": {}}, IND) is None
    assert handicap.out_race_horse_pace({"race_horse_pace": [80, 85]}, IND) is None
    assert handicap.out_race_horse_pace({}, IND) is None


def test_out_race_scratched() -> None:
    """Echoes the scratched flag as a JSON boolean, or None when absent."""
    assert handicap.out_race_scratched({"race_scratched": True}, IND) == (
        '  "race_scratched": true,'
    )
    assert handicap.out_race_scratched({}, IND) is None


PICK = handicap.Pick(1, "Alpha", 85.26, 0.45678)
CONF = {"score": 72, "label": "HIGH", "components": {}}


def test_out_model_predicted_rank() -> None:
    """Writes the rank, or null for a scratched horse."""
    assert handicap.out_model_predicted_rank(IND, PICK) == '  "model_predicted_rank": 1,'
    assert (
        handicap.out_model_predicted_rank(IND, None) == '  "model_predicted_rank": null,'
    )


def test_out_model_is_predicted_winner() -> None:
    """Writes a JSON boolean, or null for a scratched horse."""
    line = handicap.out_model_is_predicted_winner(IND, PICK)
    assert line == '  "model_is_predicted_winner": true,'
    second = handicap.Pick(2, "Bravo", 80.0, 0.3)
    assert handicap.out_model_is_predicted_winner(IND, second) == (
        '  "model_is_predicted_winner": false,'
    )
    assert handicap.out_model_is_predicted_winner(IND, None) == (
        '  "model_is_predicted_winner": null,'
    )


def test_out_model_score() -> None:
    """Writes the score to one decimal, or null for a scratched horse."""
    assert handicap.out_model_score(IND, PICK) == '  "model_score": 85.3,'
    assert handicap.out_model_score(IND, None) == '  "model_score": null,'


def test_out_model_win_probability() -> None:
    """Writes the probability to four decimals, or null if scratched."""
    assert (
        handicap.out_model_win_probability(IND, PICK)
        == '  "model_win_probability": 0.4568,'
    )
    assert (
        handicap.out_model_win_probability(IND, None)
        == '  "model_win_probability": null,'
    )


def test_out_model_confidence_score() -> None:
    """Writes the score, or null when the pick or the confidence is missing."""
    assert handicap.out_model_confidence_score(IND, PICK, CONF) == (
        '  "model_confidence_score": 72,'
    )
    assert handicap.out_model_confidence_score(IND, None, CONF) == (
        '  "model_confidence_score": null,'
    )
    assert handicap.out_model_confidence_score(IND, PICK, None) == (
        '  "model_confidence_score": null,'
    )


def test_out_model_confidence_label() -> None:
    """Writes the label, or null when the pick or the confidence is missing."""
    assert handicap.out_model_confidence_label(IND, PICK, CONF) == (
        '  "model_confidence_label": "HIGH",'
    )
    assert handicap.out_model_confidence_label(IND, None, CONF) == (
        '  "model_confidence_label": null,'
    )
    assert handicap.out_model_confidence_label(IND, PICK, None) == (
        '  "model_confidence_label": null,'
    )


# --------------------------------------------------------------------------
# JSONC writer: assembly
# --------------------------------------------------------------------------


def test_render_participant_running_horse() -> None:
    """Input fields are echoed in order, then the model fields are filled."""
    text = handicap.render_participant(make_participant("Alpha"), PICK, CONF, "    ")
    lines = text.splitlines()
    assert lines[0] == "    {"
    assert lines[-1] == "    },"
    assert lines[1] == "      // Individual Horse Data"
    assert '      "horse_name": "Alpha",' in lines
    assert '      "race_scratched": false,' in lines
    assert lines.index("      // Our Model Data") > lines.index(
        '      "race_scratched": false,'
    )
    assert '      "model_score": 85.3,' in lines
    assert '      "model_confidence_label": "HIGH",' in lines


def test_render_participant_scratched_horse_gets_nulls() -> None:
    """A scratched horse has null model values."""
    participant = make_participant("Delta", race_scratched=True)
    text = handicap.render_participant(participant, None, CONF, "    ")
    assert '      "model_predicted_rank": null,' in text
    assert '      "model_score": null,' in text
    assert '      "model_confidence_label": null,' in text


def test_render_participant_skips_missing_input_fields() -> None:
    """Fields the input lacked are left out, with no blank lines."""
    text = handicap.render_participant({"horse_name": "Bare"}, PICK, CONF, "")
    assert "race_jockey" not in text
    assert "race_horse_pace" not in text
    assert all(line.strip() for line in text.splitlines())


def test_render_race(race: dict[str, Any]) -> None:
    """A race block lists metadata, then each participant with model fields."""
    text = handicap.render_race(race, "v8")
    lines = text.splitlines()
    assert lines[0] == "  {"
    assert lines[-1] == "  },"
    assert "    // Race Metadata" in lines
    assert '    "race_name": "Test Stakes",' in lines
    assert '    "race_participants": [' in lines
    assert lines.count('        "model_score": null,') == 1  # Delta, scratched
    assert lines.count('        "model_predicted_rank": 1,') == 1


def test_render_race_all_scratched() -> None:
    """With nothing running, every model field is null."""
    race = make_race(make_participant("Solo", race_scratched=True))
    text = handicap.render_race(race, "v8")
    assert '"model_score": null,' in text
    assert '"model_confidence_score": null,' in text


def test_to_jsonc_wrapper(race: dict[str, Any]) -> None:
    """The document opens with the float comment and closes the list."""
    text = handicap.to_jsonc([race])
    assert text.startswith("[\n  // Most Numerical Values are Floats\n")
    assert text.endswith("\n]\n")


def test_write_jsonc_round_trips(tmp_path: Path, race: dict[str, Any]) -> None:
    """Written output reloads with inputs echoed and model fields filled."""
    out = tmp_path / "out.jsonc"
    assert handicap.write_jsonc([race], "v8", str(out)) == 4
    loaded = cast(list[dict[str, Any]], handicap.load_jsonc(str(out)))
    assert len(loaded) == 1
    assert loaded[0]["race_name"] == "Test Stakes"
    participants = loaded[0]["race_participants"]
    running = [item for item in participants if not item["race_scratched"]]
    assert sorted(item["model_predicted_rank"] for item in running) == [
        1,
        2,
        3,
    ]
    assert sum(item["model_is_predicted_winner"] for item in running) == 1
    assert participants[0]["race_horse_pace"] == {
        "early_pace": 90,
        "late_pace": 85,
    }
    assert participants[0]["race_horse_distance_figures"] == [95, 97]
    assert participants[3]["model_score"] is None


def test_write_jsonc_counts_horses_across_races(
    tmp_path: Path, race: dict[str, Any]
) -> None:
    """The return value counts participants over all races."""
    out = tmp_path / "out.jsonc"
    assert handicap.write_jsonc([race, race], "v9", str(out)) == 8


# --------------------------------------------------------------------------
# find_default_input and the CLI
# --------------------------------------------------------------------------


@pytest.fixture(name="sandbox")
def sandbox_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Fake cwd, script folder and tests folder, all under tmp_path."""
    folders = {
        "cwd": tmp_path / "cwd",
        "script": tmp_path / "src",
        "tests": tmp_path / "tests",
    }
    for folder in folders.values():
        folder.mkdir()
    monkeypatch.chdir(folders["cwd"])
    monkeypatch.setattr(handicap, "__file__", str(folders["script"] / "handicap.py"))
    return folders


def test_find_default_input_prefers_current_folder(
    sandbox: dict[str, Path],
) -> None:
    """A file in the current folder wins over the other locations."""
    for key in ("cwd", "script", "tests"):
        (sandbox[key] / handicap.DEFAULT_NAME).write_text("[]", encoding="utf-8")
    found = Path(handicap.find_default_input())
    assert found.resolve() == (sandbox["cwd"] / handicap.DEFAULT_NAME).resolve()


def test_find_default_input_then_script_folder(
    sandbox: dict[str, Path],
) -> None:
    """Next it looks beside the script."""
    for key in ("script", "tests"):
        (sandbox[key] / handicap.DEFAULT_NAME).write_text("[]", encoding="utf-8")
    found = Path(handicap.find_default_input())
    assert found.resolve() == (sandbox["script"] / handicap.DEFAULT_NAME).resolve()


def test_find_default_input_then_tests_folder(
    sandbox: dict[str, Path],
) -> None:
    """Last it tries a tests/ folder beside the script's folder."""
    (sandbox["tests"] / handicap.DEFAULT_NAME).write_text("[]", encoding="utf-8")
    found = Path(handicap.find_default_input())
    assert found.resolve() == (sandbox["tests"] / handicap.DEFAULT_NAME).resolve()


def test_find_default_input_falls_back_to_current_folder_path(
    sandbox: dict[str, Path],
) -> None:
    """When nothing exists it returns the current-folder path anyway."""
    found = Path(handicap.find_default_input())
    assert found.resolve() == (sandbox["cwd"] / handicap.DEFAULT_NAME).resolve()
    assert not found.exists()


def test_parse_args_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no path it uses find_default_input and writes in place with v8."""
    monkeypatch.setattr(handicap, "find_default_input", lambda: "default.jsonc")
    assert handicap.parse_args([]) == (
        "default.jsonc",
        "default.jsonc",
        "v8",
        False,
    )


def test_parse_args_path_only() -> None:
    """A single path is both input and output."""
    assert handicap.parse_args(["race.jsonc"]) == (
        "race.jsonc",
        "race.jsonc",
        "v8",
        False,
    )


def test_parse_args_flags() -> None:
    """--v9, --text and --out are all picked up, in any order."""
    argv = ["--out", "other.jsonc", "race.jsonc", "--v9", "--text"]
    assert handicap.parse_args(argv) == (
        "race.jsonc",
        "other.jsonc",
        "v9",
        True,
    )


def test_parse_args_ignores_unknown_flags() -> None:
    """Unknown --flags are skipped rather than treated as paths."""
    assert handicap.parse_args(["--bogus", "race.jsonc"])[0] == "race.jsonc"


def test_parse_args_out_needs_a_value() -> None:
    """--out with nothing after it exits with a message."""
    with pytest.raises(SystemExit, match="--out needs a file path"):
        handicap.parse_args(["race.jsonc", "--out"])


def test_main_missing_input_exits(tmp_path: Path) -> None:
    """A missing input file exits with a usage message."""
    with pytest.raises(SystemExit, match="input not found"):
        handicap.main([str(tmp_path / "nope.jsonc")])


def test_main_updates_file_in_place(
    races_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """By default the input file is rewritten with model fields."""
    handicap.main([str(races_file)])
    assert capsys.readouterr().out == f"Wrote 4 horses across 1 race(s) to {races_file}\n"
    assert '"model_score"' in races_file.read_text(encoding="utf-8")


def test_main_out_writes_elsewhere(
    races_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--out leaves the input untouched and writes to the new path."""
    out = tmp_path / "scored.jsonc"
    handicap.main([str(races_file), "--out", str(out)])
    assert f"to {out}" in capsys.readouterr().out
    assert '"model_score"' in out.read_text(encoding="utf-8")
    assert '"model_score"' not in races_file.read_text(encoding="utf-8")


def test_main_text_prints_table_without_writing(
    races_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--text prints the ranked table and leaves the file alone."""
    before = races_file.read_text(encoding="utf-8")
    handicap.main([str(races_file), "--text"])
    out = capsys.readouterr().out
    assert "Test Stakes  |  SPRINT v8  |  /100" in out
    assert "Confidence:" in out
    assert "Wrote" not in out
    assert races_file.read_text(encoding="utf-8") == before


def test_main_text_with_v9(races_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--v9 switches the printed table to the shadow model."""
    handicap.main([str(races_file), "--text", "--v9"])
    assert "SPRINT v9" in capsys.readouterr().out


def test_main_text_skips_fully_scratched_races(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A race with every horse scratched prints nothing in --text mode."""
    path = tmp_path / "scratched.jsonc"
    race = make_race(make_participant("Solo", race_scratched=True))
    path.write_text(json.dumps([race]), encoding="utf-8")
    handicap.main([str(path), "--text"])
    assert capsys.readouterr().out == ""


def test_script_dir_helper_uses_real_paths() -> None:
    """DEFAULT_NAME is a bare file name, so it joins onto any folder."""
    assert os.path.basename(handicap.DEFAULT_NAME) == handicap.DEFAULT_NAME
