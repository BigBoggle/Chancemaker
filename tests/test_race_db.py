from pathlib import Path
from database.race_db import RaceDB

# from database.race_db_model import (
#     Track,
#     Race,
#     Horse,
#     Jockey,
#     Entry,
#     Prediction,
#     Seasons,
#     RunningStyle,
# )

# Test Cases for the database

FIXTURE_DIR = Path(__file__).parent / "fixtures"

VALID_JSON_FILES = list((FIXTURE_DIR / "valid").glob("*.json"))
INVALID_JSON_FILES = list((FIXTURE_DIR / "invalid").glob("*.json"))


def test_race_db_initalization(database):
    race_db = RaceDB()
    assert race_db.db is database


def test_get_db(database):
    race_db = RaceDB()
    assert race_db.get_db() is database


def test_create_database(database):
    race_db = RaceDB()
    database.drop_all()

    race_db.create_database()
    tables = database.metadata.tables.keys()

    for table in tables:
        assert table in database.engine.dialect.get_table_names(
            database.engine.connect()
        )


# Redo this test case
# @pytest.mark.parametrize("json_file", VALID_JSON_FILES)
# def test_valid_json_import(database, json_file):

#     with json_file.open("r", encoding="utf-8") as file:
#         data = json.load(file)

#     race_db = RaceDB()
#     imported_races = race_db.import_json(data)

#     assert len(imported_races) == 1
#     assert database.session.query(Track).count() == 1
#     assert database.session.query(Race).count() == 1
#     assert database.session.query(Horse).count() == 1
#     assert database.session.query(Jockey).count() == 1
#     assert database.session.query(Entry).count() == 1
#     assert database.session.query(Prediction).count() == 1
