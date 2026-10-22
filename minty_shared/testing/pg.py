"""Postgres test mode for a Django API, as a pytest plugin: ``pytest_plugins = ["minty_shared.testing.pg"]``
in the repo's root ``conftest.py``.

With ``MINTY_TEST_PG_URI`` set, the test database is built from the Minty repo's
``docs/schema/01_schema_rebased.sql`` by Minty's ``tests/pg_harness.py`` (found through
``MINTY_REPO``, default ``C:\\Github\\Minty``; CI checks Minty out beside the code) and handed to
pytest-django instead of letting it create one - so the model mirrors are checked against the
real schema, which SQLite (tables from the models) never can. Unset, this plugin does nothing.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

#: The fewest tables a real build of 01 has; fewer means the harness built the wrong thing.
MIN_TABLES = 50


def load_harness() -> ModuleType:
    """Minty's ``tests/pg_harness.py``, loaded by path (it imports nothing from the app)."""
    minty = Path(os.environ.get("MINTY_REPO", r"C:\Github\Minty"))
    path = minty / "tests" / "pg_harness.py"
    if not path.exists():
        raise RuntimeError(f"Minty repo not found at {minty} (set MINTY_REPO); need {path}")
    spec = importlib.util.spec_from_file_location("minty_pg_harness", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(mod)
    return mod


if os.environ.get("MINTY_TEST_PG_URI"):

    @pytest.fixture(scope="session")
    def django_db_setup(django_db_blocker: Any) -> Iterator[None]:
        """Replace pytest-django's database creation with the schema-file build."""
        from django.conf import settings
        from django.db import connections

        harness = load_harness()
        built = harness.build()
        try:
            for alias in connections:
                connections[alias].close()
            settings.DATABASES["default"]["NAME"] = built.dbname
            connections["default"].settings_dict["NAME"] = built.dbname
            connections["default"].settings_dict["TEST"]["NAME"] = built.dbname
            with django_db_blocker.unblock():
                with connections["default"].cursor() as cur:
                    cur.execute(
                        "SELECT count(*) FROM information_schema.tables WHERE table_schema=%s",
                        [settings.DB_SCHEMA],
                    )
                    (n,) = cur.fetchone()
                    assert n >= MIN_TABLES, f"harness database has only {n} tables"
            yield
        finally:
            for alias in connections:
                connections[alias].close()
            built.drop()
