import sys
import json
from pathlib import Path
from flask import Flask, render_template, jsonify, request
from flask_sqlalchemy import SQLAlchemy
from models import models, RaceDB, RaceModel

ROOT = Path(__file__).resolve().parent.parent

app = Flask(
    __name__,
    template_folder=str(ROOT / "templates"),
    static_folder=str(ROOT / "static"),
)

app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///race_data.db"

# Make the SQLAlchemy and RaceDB classes global for all request handling
models = SQLAlchemy(app)
raceManager = RaceDB(app)

# The Main webpage displayed
@app.route("/")
def index():
    return render_template("index.html") # Helps pass all the data to index.html to be used

# JSON endpoint requested by the site
@app.route("/races", methods=["GET"]) #Note: Methods is not defined for some reason
def getRacesJSON():
    races = raceManager.getallrecords() # Have to change "getallrecords" with whatever function we actually have in the db.py, or even use request.json 
    # Take the SQL model and format back into JSON structure
    data = []
    for race in races: # Race metadata
        racedict = {
            "race_name": race.race_name,
            "race_season": race.race_season,
            "race_date": race.race_date,
            "track_location": race.track_location,
            "track_name": race.track_name,
            "race_distance_furlongs": race.race_distance_furlongs,
            "race_track_surface": race.race_track_surface, 
            "race_participants": []
        }
        for horse in race.horses: # Individual horse data
            horsedict = {
                "horse_name": horse.horse_name,
                "horse_trainer": horse.horse_trainer,
                "race_jockey": horse.race_jockey,
                "horse_description": horse.horse_description,
                "race_horse_running_style": horse.race_horse_running_style,
                "race_horse_program_number": horse.race_horse_program_number,
                "race_horse_morning_line_number": horse.race_horse_morning_line_number,
                "race_horse_speed_figures": horse.race_horse_speed_figures,
                "race_horse_distance_figures": json.loads(horse.race_horse_distance_figures or "[]"),
                "race_horse_class" : horse.race_horse_class, 
                "race_horse_pace": {"early_pace": horse.early_pace,"late_pace": horse.late_pace},
                "race_scratched" : horse.race_scratched,
                # Our Model Data
                "model_predicted_rank": horse.model_predicted_rank,
                "model_is_predicted_winner": horse.model_is_predicted_winner,
                "model_score": horse.model_score,
                "model_win_probability": horse.model_win_probability,
                "model_confidence_score": horse.model_confidence_score,
                "model_confidence_label": horse.model_confidence_label
            }
            racedict["race_participants"].append(horsedict)
        data.append(racedict)
    return jsonify(data)


# Main Application
if __name__ == "__main__":
    # TODO: CLI Arguments for DB input
    #RaceDB(SQLAlchemy(app))  # Connect SQLAlchemy to our DB Management class # Should initialize the # I already made these into globabl variables to handle requests better
    with app.app_context(): # This helps tell SQLAlchemy which tables we need to build from the db after waiting for Flask to load
        models.create_all()
    app.run(host="0.0.0.0")
