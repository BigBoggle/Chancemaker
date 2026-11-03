import enum
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import ForeignKey, String, JSON, UniqueConstraint, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import date, datetime

db = SQLAlchemy()


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
    track_id: Mapped[int] = mapped_column(primary_key=True)
    track_name: Mapped[str] = mapped_column(String(120))
    track_location: Mapped[str] = mapped_column(String(120))
    races: Mapped[list["Race"]] = relationship(back_populates="track")


class Race(db.Model):
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
        back_populates="race", cascade="all, delete-orphan"
    )

    track: Mapped["Track"] = relationship(back_populates="races")


class Horse(db.Model):
    horse_id: Mapped[int] = mapped_column(primary_key=True, unique=True, nullable=False)
    horse_name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    horse_trainer: Mapped[str] = mapped_column(
        String(120), unique=False, nullable=False
    )
    horse_description: Mapped[str] = mapped_column(
        String(120), unique=False, nullable=True
    )
    horse_entries: Mapped[list["Entry"]] = relationship(back_populates="horse")
    # Any Other Horse Data can also go here.


# Seperate Table of Jockeys
class Jockey(db.Model):
    jockey_id: Mapped[int] = mapped_column(primary_key=True)
    jockey_name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    jockey_entries: Mapped[list["Entry"]] = relationship(back_populates="jockey")


class Entry(db.Model):
    __table_args__ = UniqueConstraint(
        "entry_race_id", "entry_program_number", name="unique_race_program_number"
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
    entry_speed_figures: Mapped[list[float]] = mapped_column(JSON, default=list)
    entry_distance_figures: Mapped[list[float]] = mapped_column(JSON, default=list)
    entry_class_figures: Mapped[list[float]] = mapped_column(JSON, default=list)
    entry_pace: Mapped[dict] = mapped_column(JSON, default=dict)

    # Relationships
    race: Mapped["Race"] = relationship(back_populates="race_entries")
    horse: Mapped["Horse"] = relationship(back_populates="horse_entries")
    jockey: Mapped["Jockey"] = relationship(back_populates="jockey_entries")
    predictions: Mapped[list["Prediction"]] = relationship(
        back_populates="entry", cascade="all, delete-orphan"
    )


class Prediction(db.Model):
    prediction_id: Mapped[int] = mapped_column(primary_key=True)

    prediction_entry_id: Mapped[int] = mapped_column(
        ForeignKey("entry.entry_id"), nullable=False
    )

    # Prediction results
    prediction_rank: Mapped[int] = mapped_column(nullable=False)
    prediction_model_version: Mapped[str] = mapped_column(String(50), nullable=False)
    prediction_is_winner: Mapped[bool] = mapped_column(default=False, nullable=False)
    prediction_score: Mapped[float] = mapped_column(nullable=False)
    prediction_win_probability: Mapped[float] = mapped_column(nullable=False)
    prediction_confidence_score: Mapped[float] = mapped_column(nullable=False)
    prediction_confidence_label: Mapped[str] = mapped_column(String(20), nullable=False)

    # Relates back to race entry
    entry: Mapped["Entry"] = relationship(back_populates="predictions")
