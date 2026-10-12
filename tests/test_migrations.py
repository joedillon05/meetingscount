"""
The migrations themselves, run the way the container runs them.

Every other test builds its tables straight from the models, which says
nothing about whether `alembic upgrade head` works. A deployment depends on
that command and nothing else, so it gets a test of its own.
"""
import os
import subprocess
import sys

from sqlalchemy import create_engine, inspect

from app.models import Base

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_upgrade_head_builds_every_table_and_column_the_models_expect(tmp_path):
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT,
                       env=os.environ | {"MC_DATABASE_URL": url}, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-2000:]

    db = inspect(create_engine(url))
    missing = [f"{t.name}.{c.name}" for t in Base.metadata.sorted_tables
               for c in t.columns
               if t.name not in db.get_table_names()
               or c.name not in {x["name"] for x in db.get_columns(t.name)}]
    assert missing == [], f"the models have columns the migrations never create: {missing}"
