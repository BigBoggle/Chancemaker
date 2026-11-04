from datetime import date
from database.race_db_model import (
    Prediction,
    Track,
    Race,
    Horse,
    Jockey,
    Entry,
    Seasons,
    RunningStyle,
)

# Test Cases for testing the structure of the database model.


def test_create_track(database):
    track = Track(track_name="Keeneland", track_location="Lexington, Kentucky")

    database.session.add(track)
    database.session.commit()

    saved_track = database.session.get(Track, track.track_id)

    assert saved_track is not None
    assert saved_track.track_name == "Keeneland"


def test_create_race(database):
    track = Track(track_name="Keeneland", track_location="Lexington, Kentucky")

    race = Race(
        race_name="Fall Championship Stakes",
        race_date=date(2026, 11, 7),
        race_season=Seasons.FALL,
        race_track_surface="Dirt",
        race_distance_f=8,
        race_track=track,
    )

    database.session.add(race)
    database.session.commit()

    saved_race = database.session.get(Race, race.race_id)

    assert saved_race.race_distance_f == 8


def test_create_horse(database):
    horse = Horse(
        horse_name="Thunder Strike",
        horse_trainer="John Smith",
        horse_description="Experienced front runner",
    )

    database.session.add(horse)
    database.session.commit()

    saved_horse = database.session.get(Horse, horse.horse_id)

    assert saved_horse is not None
    assert saved_horse.horse_name == "Thunder Strike"


def test_create_jockey(database):
    jockey = Jockey(jockey_name="Alex Johnson")

    database.session.add(jockey)
    database.session.commit()

    saved_jockey = database.session.get(Jockey, jockey.jockey_id)

    assert saved_jockey is not None
    assert saved_jockey.jockey_name == "Alex Johnson"


def test_create_entry(database):
    track = Track(track_name="Keeneland", track_location="Lexington, Kentucky")
    race = Race(
        race_name="Fall Championship Stakes",
        race_date=date(2026, 11, 7),
        race_season=Seasons.FALL,
        race_track_surface="Dirt",
        race_distance_f=8,
        race_track=track,
    )
    horse = Horse(
        horse_name="Thunder Strike",
        horse_trainer="John Smith",
        horse_description="Experienced front runner",
    )
    jockey = Jockey(jockey_name="Alex Johnson")

    entry = Entry(
        entry_race=race,
        entry_horse=horse,
        entry_speed_figures=68.0,
        entry_pace={"early_pace": 40.0, "late_pace": 30.0},
        entry_distance_figures=[98.0, 10.0, 20.0, 20.0, 16.0, 10.0],
        entry_class_figures=20.0,
        entry_scratched=False,
        entry_jockey=jockey,
        entry_program_number=1,
        entry_morning_line_odds=2.5,
        entry_running_style=RunningStyle.FRONT_RUNNER,
    )

    database.session.add(entry)
    database.session.commit()

    saved_entry = database.session.get(Entry, entry.entry_id)

    assert saved_entry is not None
    assert saved_entry.entry_race.race_track.track_name == "Keeneland"
    assert saved_entry.entry_scratched == False
    assert saved_entry.entry_horse.horse_name == "Thunder Strike"
    assert saved_entry.entry_jockey.jockey_name == "Alex Johnson"


def test_create_prediction(database):

    track = Track(track_name="Keeneland", track_location="Lexington, Kentucky")
    race = Race(
        race_name="Fall Championship Stakes",
        race_date=date(2026, 11, 7),
        race_season=Seasons.FALL,
        race_track_surface="Dirt",
        race_distance_f=8,
        race_track=track,
    )
    horse = Horse(
        horse_name="Thunder Strike",
        horse_trainer="John Smith",
        horse_description="Experienced front runner",
    )
    jockey = Jockey(jockey_name="Alex Johnson")

    prediction = Prediction(
        prediction_rank=3,
        prediction_is_winner=True,
        prediction_confidence_label="HIGH",
        prediction_score=97,
        prediction_win_probability=0.525,
        prediction_confidence_score=83,
    )

    entry = Entry(
        entry_race=race,
        entry_horse=horse,
        entry_speed_figures=68.0,
        entry_pace={"early_pace": 40.0, "late_pace": 30.0},
        entry_distance_figures=[98.0, 10.0, 20.0, 20.0, 16.0, 10.0],
        entry_class_figures=20.0,
        entry_scratched=False,
        entry_jockey=jockey,
        entry_program_number=1,
        entry_morning_line_odds=2.5,
        entry_running_style=RunningStyle.FRONT_RUNNER,
        entry_predictions=[prediction],
    )

    database.session.add(entry)
    database.session.commit()

    saved_prediction = database.session.get(Prediction, prediction.prediction_id)

    assert saved_prediction is not None
    assert saved_prediction.prediction_is_winner == True
    assert saved_prediction.prediction_score == 97
    assert saved_prediction.prediction_win_probability == 0.525
