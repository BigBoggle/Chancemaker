import sys
from pathlib import Path
from flask import Flask, render_template, request
from flask_sqlalchemy import SQLAlchemy
from db import RaceDB

ROOT = Path(__file__).resolve().parent.parent

app = Flask(
    __name__,
    template_folder=str(ROOT / "templates"),
    static_folder=str(ROOT / "static"),
)

app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///race_data.db"


@app.route("/")
def index():
    return render_template("index.html")


# Main Application
if __name__ == "__main__":
    # TODO: CLI Arguments for DB input
    RaceDB(SQLAlchemy(app))  # Connect SQLAlchemy to our DB Management class

    app.run(host="0.0.0.0")
