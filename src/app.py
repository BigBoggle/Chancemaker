import sys
import json

from pathlib import Path
from flask import Flask, render_template, request, abort
from database.race_db_model import db, Race
from database.race_db import RaceDB

ROOT = Path(__file__).resolve().parent.parent

race_db = RaceDB()


def create_app(db_uri="sqlite:///race_data.db"):
    app = Flask(
        __name__,
        template_folder=str(ROOT / "templates"),
        static_folder=str(ROOT / "static"),
        instance_path=str(ROOT / "instance"),
    )

    app.config["SQLALCHEMY_DATABASE_URI"] = db_uri
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    with app.app_context():
        db.create_all()

        # Initalize the App Database
        @app.route("/")
        def index():
            return render_template("index.html")

        @app.route("/db")
        def test_db():
            from sqlalchemy import inspect, func

            tables = inspect(db.engine).get_table_names()
            counts = {
                "races": db.session.scalar(db.select(func.count()).select_from(Race)),
            }
            return {"database_connected": True, "tables": tables, "counts": counts}

        @app.route("/races")
        def list_races():
            try:
                return race_db.list_races(
                    track=request.args.get("track"),
                    race_date=request.args.get("date"),
                )
            except ValueError as e:
                return {"error": str(e)}, 400

        @app.route("/races/<int:race_id>")
        def get_race(race_id):
            race = race_db.get_race(race_id)
            if race is None:
                abort(404)
            return race

    return app


# Main Application
if __name__ == "__main__":

    # For testing loading a json manually
    # Remove later on.
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as f:
            data = json.load(f)

        app = create_app()

        with app.app_context():
            races = race_db.import_json(data)
            print(f"Imported {len(races)} race(s).")

        app.run(host="0.0.0.0")
