import enum
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import ForeignKey, String, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import date

db = SQLAlchemy()


# Database model


class Seasons(enum.Enum):
    SPRING = "SPRING"
    SUMMER = "SUMMER"
    FALL = "FALL"
    WINTER = "WINTER"


# Enum Representation of All Possible Horse Running Styles
class RunningStyle(enum.Enum):
    FRONT_RUNNER = "Front Runner"
    PRESSER = "Presser"
    STALKER = "Stalker"
    CLOSER = "Closer"


# Database Models
class Track(db.Model):
    __table_args__ = (UniqueConstraint("track_name", "track_location"),)
    track_id: Mapped[int] = mapped_column(primary_key=True)
    track_name: Mapped[str] = mapped_column(String(120))
    track_location: Mapped[str] = mapped_column(String(120))
    track_races: Mapped[list["Race"]] = relationship(back_populates="race_track")

    def __init__(
        self,
        *,
        track_name: str,
        track_location: str,
    ) -> None:
        self.track_name = track_name
        self.track_location = track_location


class Race(db.Model):
    __table_args__ = (
        UniqueConstraint("race_track_id", "race_name", "race_date", name="unique_race"),
    )
    race_id: Mapped[int] = mapped_column(primary_key=True)
    race_track_id: Mapped[int] = mapped_column(
        ForeignKey("track.track_id"), nullable=False
    )
    race_name: Mapped[str] = mapped_column(String(120), nullable=False)
    race_date: Mapped[date] = mapped_column(nullable=False)
    race_season: Mapped[Seasons] = mapped_column(nullable=False)
    race_track_surface: Mapped[str] = mapped_column(String(20), nullable=False)
    race_distance_f: Mapped[int] = mapped_column(nullable=False)  # Distance in furlongs
    race_entries: Mapped[list["Entry"]] = relationship(
        back_populates="entry_race", cascade="all, delete-orphan"
    )
    race_track: Mapped["Track"] = relationship(back_populates="track_races")

    def __init__(
        self,
        *,
        race_name: str,
        race_date: date,
        race_season: Seasons,
        race_track_surface: str,
        race_distance_f: int,
        race_track: Track,
    ) -> None:
        self.race_name = race_name
        self.race_date = race_date
        self.race_season = race_season
        self.race_track_surface = race_track_surface
        self.race_distance_f = race_distance_f
        self.race_track = race_track


class Horse(db.Model):
    horse_id: Mapped[int] = mapped_column(primary_key=True, unique=True, nullable=False)
    horse_name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    horse_trainer: Mapped[str] = mapped_column(
        String(120), unique=False, nullable=False
    )
    horse_description: Mapped[str] = mapped_column(
        String(120), unique=False, nullable=True
    )
    horse_entries: Mapped[list["Entry"]] = relationship(back_populates="entry_horse")
    # Any Other Horse Data can also go here.

    def __init__(
        self,
        *,
        horse_name: str,
        horse_trainer: str,
        horse_description: str | None = None,
    ) -> None:
        self.horse_name = horse_name
        self.horse_trainer = horse_trainer
        self.horse_description = horse_description or ""


# Seperate Table of Jockeys
class Jockey(db.Model):
    jockey_id: Mapped[int] = mapped_column(primary_key=True)
    jockey_name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    jockey_entries: Mapped[list["Entry"]] = relationship(back_populates="entry_jockey")

    def __init__(
        self,
        *,
        jockey_name: str,
    ) -> None:
        self.jockey_name = jockey_name


class Entry(db.Model):
    __table_args__ = (
        UniqueConstraint(
            "entry_race_id", "entry_program_number", name="unique_race_program_number"
        ),
    )

    entry_id: Mapped[int] = mapped_column(primary_key=True)

    entry_race_id: Mapped[int] = mapped_column(
        ForeignKey("race.race_id"), nullable=False
    )

    entry_horse_id: Mapped[int] = mapped_column(
        ForeignKey("horse.horse_id"), nullable=False
    )

    entry_jockey_id: Mapped[int] = mapped_column(
        ForeignKey("jockey.jockey_id"), nullable=False
    )

    # Race-specific information
    entry_program_number: Mapped[int] = mapped_column(nullable=False)
    entry_morning_line_odds: Mapped[float] = mapped_column(nullable=False)
    entry_running_style: Mapped[RunningStyle] = mapped_column(nullable=False)
    entry_scratched: Mapped[bool] = mapped_column(default=False, nullable=False)

    # Horse performance figures
    entry_speed_figures: Mapped[float] = mapped_column(nullable=False)
    entry_distance_figures: Mapped[list[float]] = mapped_column(JSON, default=list)
    entry_class_figures: Mapped[float] = mapped_column(nullable=False)
    entry_pace: Mapped[dict] = mapped_column(JSON, default=dict)

    # Relationships
    entry_race: Mapped["Race"] = relationship(back_populates="race_entries")
    entry_horse: Mapped["Horse"] = relationship(back_populates="horse_entries")
    entry_jockey: Mapped["Jockey"] = relationship(back_populates="jockey_entries")
    entry_predictions: Mapped[list["Prediction"]] = relationship(
        back_populates="prediction_entry", cascade="all, delete-orphan"
    )

    def __init__(
        self,
        *,
        entry_race: Race,
        entry_horse: Horse,
        entry_jockey: Jockey,
        entry_predictions: list[Prediction] | None = None,
        entry_program_number: int,
        entry_morning_line_odds: float,
        entry_running_style: RunningStyle,
        entry_scratched: bool,
        entry_speed_figures: float,
        entry_distance_figures: list[float],
        entry_class_figures: float,
        entry_pace: dict,
    ) -> None:
        self.entry_race = entry_race
        self.entry_horse = entry_horse
        self.entry_jockey = entry_jockey
        self.entry_predictions = entry_predictions or []
        self.entry_program_number = entry_program_number
        self.entry_morning_line_odds = entry_morning_line_odds
        self.entry_running_style = entry_running_style
        self.entry_scratched = entry_scratched
        self.entry_speed_figures = entry_speed_figures
        self.entry_distance_figures = entry_distance_figures
        self.entry_class_figures = entry_class_figures
        self.entry_pace = entry_pace


class Prediction(db.Model):
    prediction_id: Mapped[int] = mapped_column(primary_key=True)

    prediction_entry_id: Mapped[int] = mapped_column(
        ForeignKey("entry.entry_id"), nullable=False
    )

    # Prediction results
    prediction_rank: Mapped[int] = mapped_column(nullable=False)
    prediction_is_winner: Mapped[bool] = mapped_column(default=False, nullable=False)
    prediction_score: Mapped[float] = mapped_column(nullable=False)
    prediction_win_probability: Mapped[float] = mapped_column(nullable=False)
    prediction_confidence_score: Mapped[float] = mapped_column(nullable=False)
    prediction_confidence_label: Mapped[str] = mapped_column(String(20), nullable=False)

    # Relates back to race entry
    prediction_entry: Mapped["Entry"] = relationship(back_populates="entry_predictions")

    def __init__(
        self,
        *,
        prediction_rank: int,
        prediction_is_winner: bool,
        prediction_score: float,
        prediction_win_probability: float,
        prediction_confidence_score: float,
        prediction_confidence_label: str,
    ) -> None:
        self.prediction_rank = prediction_rank
        self.prediction_is_winner = prediction_is_winner
        self.prediction_score = prediction_score
        self.prediction_win_probability = prediction_win_probability
        self.prediction_confidence_score = prediction_confidence_score
        self.prediction_confidence_label = prediction_confidence_label
