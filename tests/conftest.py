import os
import tempfile

import pytest

# Point every test at a throwaway database before the app imports anything.
_tmp = tempfile.mkdtemp()
os.environ["MC_DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["MC_DEMO"] = "1"
os.environ["MC_DEV_AUTH"] = "1"


@pytest.fixture(scope="session")
def seeded():
    from app.seed import seed
    return seed(reset=True)


@pytest.fixture(scope="session")
def client(seeded):
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


@pytest.fixture(scope="session")
def ids(client):
    users = client.get("/api/users").json()
    return {
        "admin": next(u["id"] for u in users if u["role"] == "admin"),
        "officer": next(u["id"] for u in users if u["role"] == "officer"),
        "participant": next(u["id"] for u in users if u["role"] == "participant"),
    }


@pytest.fixture(autouse=True)
def no_leftover_session(client):
    """
    A session cookie outranks the dev-auth header, so one test signing in
    must not quietly make the next test somebody else.
    """
    client.cookies.clear()
    yield
    client.cookies.clear()


def as_(uid):
    return {"X-MC-User": str(uid)}
