import pytest
from app import create_app
from database.race_db_model import db


@pytest.fixture
def app():
    app = create_app("sqlite://")
    app.config["TESTING"] = True
    with app.app_context():
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def database(app):
    return db


@pytest.fixture
def client(app):
    return app.test_client()
