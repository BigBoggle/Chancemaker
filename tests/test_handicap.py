import sys
import os
import pytest

from handicap import (
    tbl,
    ranks,
    rank_of,
    market_pts,
    trainer_pts,
    pace_scenario,
    pick_formula,
    score_pp_sheet,
    conf_margin,
    conf_agreement,
    conf_data,
    conf_field,
    conf_pace,
    confidence_label,
    _JsoncParser,
    win_probabilities,
    score_race,
)

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src/model"))
)


def test_tbl():
    lookup = tbl({1: 10, 2: 5}, default=2)
    assert lookup(1) == 10
    assert lookup(2) == 5
    assert lookup(3) == 2


def test_ranks():
    horses = [{"score": 10}, {"score": 25}, {"score": 10}, {"score": 5}]
    assert ranks(horses, "score") == [2, 1, 2, 4]


def test_rank_of_override():
    horse = {"ranks": {"avgspd": 1}}
    all_ranks = {"avgspd": [3, 2]}
    assert rank_of(horse, all_ranks, 0, "avgspd") == 1
    horse_no_override = {}
    assert rank_of(horse_no_override, all_ranks, 1, "avgspd") == 2


def test_market_pts():
    assert market_pts(1.5, 10) == 10
    assert market_pts(2.5, 10) == 8
    assert market_pts(25.0, 10) == 1


def test_trainer_pts():
    assert trainer_pts("baffert", 10) == 10
    assert trainer_pts("unknown_trainer", 10) == 7


def test_pace_scenario():
    horses_lone = [
        {"name": "Speedy", "ep1": 95},
        {"name": "Stalker", "ep1": 80},
    ]
    lone, duel = pace_scenario(horses_lone)
    assert lone == "Speedy"
    assert duel == []

    horses_duel = [
        {"name": "A", "ep1": 90},
        {"name": "B", "ep1": 88},
        {"name": "C", "ep1": 70},
    ]
    lone, duel = pace_scenario(horses_duel)
    assert lone is None
    assert set(duel) == {"A", "B"}


def test_pick_formula():
    assert pick_formula(6.0, "dirt") == "sprint"
    assert pick_formula(8.5, "dirt") == "route"
    assert pick_formula(8.0, "turf") == "turf"


def test_score_pp_sheet():
    h = {
        "spd": 90,
        "foreign_grade": None,
        "class": "stk300",
        "style": "E",
        "trainer_pts": 5,
        "jockey_pts": 5,
        "odds": 3,
        "life": "10-4-2-1",
        "sprint_only": True,
    }
    score = score_pp_sheet(h, route_distance=True)
    assert isinstance(score, float)
    assert score > 0


def test_confidence_calculations():
    res_high = [("A", 80.0), ("B", 65.0)]
    assert conf_margin(res_high) == 1.0

    res_low = [("A", 70.0), ("B", 69.0)]
    assert conf_margin(res_low) == 0.1

    assert conf_agreement([("A", 80), ("B", 70)], [("A", 80), ("B", 70)]) == 1.0
    assert (
        conf_data(
            [
                {
                    "ep1": 90,
                    "lp": 80,
                    "avgspd": 85,
                    "avgdist": 80,
                    "primepower": 85,
                    "lastclass": 80,
                    "pl": 3,
                    "trainer": "baffert",
                }
            ]
        )
        == 1.0
    )
    assert conf_field(5) == 1.0
    assert conf_field(15) == 0.4

    horses_duel = [
        {"name": "A", "ep1": 90},
        {"name": "B", "ep1": 89},
        {"name": "C", "ep1": 88},
    ]
    assert conf_pace(horses_duel) == 0.4

    assert confidence_label(75) == "HIGH"
    assert confidence_label(50) == "MEDIUM"
    assert confidence_label(30) == "LOW"


def test_jsonc_parser():
    sample_jsonc = """
    // Comment line
    [
      {
        "race_name": "Test Race", /* Block comment */
        "race_distance_furlongs": 6.0,
        "race_participants": [
          { "horse_name": "Horse 1", "race_scratched": false }
        ]
      }
    ]
    """
    parser = _JsoncParser(sample_jsonc)
    data = parser.value()
    assert isinstance(data, list)
    assert data[0]["race_name"] == "Test Race"
    assert data[0]["race_distance_furlongs"] == 6.0


def test_win_probabilities():
    scores = [90.0, 70.0, 50.0]
    probs = win_probabilities(scores)
    assert len(probs) == 3
    assert pytest.approx(sum(probs), 0.0001) == 1.0
    assert probs[0] > probs[1] > probs[2]


def test_score_race():
    race = {
        "name": "Sprint Test",
        "distance_f": 6.0,
        "surface": "dirt",
        "horses": [
            {
                "name": "Fast Runner",
                "race_horse_running_style": "E",
                "race_horse_speed_figures": 90,
                "primepower": 85,
                "lastclass": 85,
                "race_horse_program_number": 1,
                "pl": 2,
                "ep1": 95,
                "lp": 70,
            },
            {
                "name": "Late Charger",
                "race_horse_running_style": "S",
                "race_horse_speed_figures": 80,
                "primepower": 80,
                "lastclass": 80,
                "race_horse_program_number": 2,
                "pl": 6,
                "ep1": 65,
                "lp": 95,
            },
        ],
    }
    kind, res = score_race(race, version="v8")
    assert kind == "sprint"
    assert len(res) == 2
    assert res[0][1] >= res[1][1]
