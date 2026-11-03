import sqlite3
import json
from flask_sqlalchemy import SQLAlchemy

# DB Model


# Database Class for Races, which takes valid JSON inputs and imports them into the SQL DB.
class RaceDB:
    def __init__(self, db):
        # Maybe wrap this in a try block
        self.db = db  # Pass SQLALCHEMY instance to the class in app.

    # TODO: create functions to create database from json input.

    # Get access to database connection
    def get_db(self):
        return self.db

    def import_json(self, raw):
        data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        return
