import json
from database.race_db_model import db
from datetime import date, datetime

# DB Model
from database.race_db_model import (
    Track,
    Race,
    Horse,
    Jockey,
    Entry,
    Prediction,
    Seasons,
    RunningStyle,
)


# Helper Function for Parsing Race Dates in the format "YYYY-MM-DD"
def _parse_race_date(value: str) -> date:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"race_date must be a valid YYYY-MM-DD date, got {value!r}")
    if parsed.isoformat() != value:  # rejects "2026-1-5"
        raise ValueError(f"race_date must be zero-padded YYYY-MM-DD, got {value!r}")
    return parsed


def _parse_running_style(value: str) -> RunningStyle:
    try:
        return RunningStyle(value)
    except ValueError:
        raise ValueError(
            f"Invalid running style: {value!r}. "
            f"Expected one of: {[style.value for style in RunningStyle]}"
        )


# Database Class for Races, which takes valid JSON inputs
# and imports them into the SQL DB.
class RaceDB:
    def __init__(self):
        self.db = db

    def create_database(self):
        self.db.create_all()

    # Get access to database connection.
    def get_db(self):
        return self.db

    # Import Valid JSON file into the db.
    def import_json(self, raw):
        try:
            data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ValueError("Invalid JSON input") from e

        imported_races = []

        try:
            for race_data in data:
                imported_races.append(self._import_race(race_data))
            self.db.session.commit()
        # If import fails, rollback db and raise and error
        except Exception:
            self.db.session.rollback()
            raise

        return imported_races

    # Helper Functions
    def _get_or_create_track(self, name: str, location: str) -> Track:
        track = self.db.session.scalar(
            self.db.select(Track).where(
                Track.track_name == name, Track.track_location == location
            )
        )
        if track is None:
            track = Track(track_name=name, track_location=location)
            self.db.session.add(track)
            self.db.session.flush()
        return track

    def _get_or_create_horse(self, data: dict) -> Horse:
        horse = self.db.session.scalar(
            self.db.select(Horse).where(Horse.horse_name == data["horse_name"])
        )
        if horse is None:
            horse = Horse(
                horse_name=data["horse_name"],
                horse_trainer=data["horse_trainer"],
                horse_description=data.get("horse_description"),
            )
            self.db.session.add(horse)
            self.db.session.flush()
        return horse

    def _get_or_create_jockey(self, name: str) -> Jockey:
        jockey = self.db.session.scalar(
            self.db.select(Jockey).where(Jockey.jockey_name == name)
        )
        if jockey is None:
            jockey = Jockey(jockey_name=name)
            self.db.session.add(jockey)
            self.db.session.flush()
        return jockey

    # Function to import json data into db
    def _import_race(self, data):
        track = self._get_or_create_track(data["track_name"], data["track_location"])
        race_date = _parse_race_date(data["race_date"])

        # Already imported? Reuse it instead of inserting a duplicate.
        existing = self.db.session.scalar(
            self.db.select(Race).where(
                Race.race_track_id == track.track_id,
                Race.race_name == data["race_name"],
                Race.race_date == race_date,
            )
        )
        if existing is not None:
            return existing

        race = Race(
            race_name=data["race_name"],
            race_date=race_date,
            race_season=Seasons[data["race_season"].upper()],
            race_track_surface=data["race_track_surface"],
            race_distance_f=data["race_distance_furlongs"],
            race_track=track,
        )
        self.db.session.add(race)

        for participant in data["race_participants"]:
            self._import_participants(race, participant)

        return race

    def _import_participants(self, race: Race, data: dict) -> Entry:

        horse = self._get_or_create_horse(data)
        jockey = self._get_or_create_jockey(data["race_jockey"])

        prediction = Prediction(
            prediction_rank=data["model_predicted_rank"],
            prediction_is_winner=data["model_is_predicted_winner"],
            prediction_score=data["model_score"],
            prediction_win_probability=data["model_win_probability"],
            prediction_confidence_score=data["model_confidence_score"],
            prediction_confidence_label=data["model_confidence_label"],
        )

        entry = Entry(
            entry_race=race,
            entry_horse=horse,
            entry_jockey=jockey,
            entry_predictions=[prediction],
            entry_program_number=int(data["race_horse_program_number"]),
            entry_morning_line_odds=data["race_horse_morning_line_number"],
            entry_running_style=_parse_running_style(data["race_horse_running_style"]),
            entry_speed_figures=data["race_horse_speed_figures"],
            entry_distance_figures=data["race_horse_distance_figures"],
            entry_class_figures=data["race_horse_class"],
            entry_pace=data["race_horse_pace"],
            entry_scratched=data["race_scratched"],
        )
        self.db.session.add(entry)
        return entry

    def list_races(
        self, track: str | None = None, race_date: str | None = None
    ) -> list[dict]:
        stmt = self.db.select(Race).order_by(Race.race_date.desc())
        if track:
            stmt = stmt.join(Race.race_track).where(Track.track_name == track)
        if race_date:
            stmt = stmt.where(Race.race_date == _parse_race_date(race_date))
        return [self.serialize_race(r) for r in self.db.session.scalars(stmt)]

    @staticmethod
    def serialize_race(race: Race) -> dict:
        return {
            "race_id": race.race_id,
            "race_name": race.race_name,
            "race_date": race.race_date.isoformat(),
            "race_season": race.race_season.value,
            "race_distance_f": race.race_distance_f,
            "race_track_surface": race.race_track_surface,
            "track_name": race.race_track.track_name,
            "track_location": race.race_track.track_location,
            "entries": [
                {
                    "program_number": e.entry_program_number,
                    "horse_name": e.entry_horse.horse_name,
                    "horse_trainer": e.entry_horse.horse_trainer,
                    "jockey_name": e.entry_jockey.jockey_name,
                    "running_style": e.entry_running_style.value,
                    "morning_line_odds": e.entry_morning_line_odds,
                    "speed_figures": e.entry_speed_figures,
                    "distance_figures": e.entry_distance_figures,
                    "class_figures": e.entry_class_figures,
                    "pace": e.entry_pace,
                    "scratched": e.entry_scratched,
                    "predictions": [
                        {
                            "rank": p.prediction_rank,
                            "is_winner": p.prediction_is_winner,
                            "score": p.prediction_score,
                            "win_probability": p.prediction_win_probability,
                            "confidence_score": p.prediction_confidence_score,
                            "confidence_label": p.prediction_confidence_label,
                        }
                        for p in e.entry_predictions
                    ],
                }
                for e in race.race_entries
            ],
        }

    def get_race(self, race_id: int) -> dict | None:
        race = self.db.session.get(Race, race_id)
        return self.serialize_race(race) if race else None
