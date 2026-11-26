import pytest

from orchestrator import db


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "orch.db")
    db.init_db(path)
    return path


@pytest.fixture
def conn(db_path):
    c = db.connect(db_path)
    yield c
    c.close()
