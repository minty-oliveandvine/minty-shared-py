"""minty_shared.testing: the guards walk source correctly; the harness loader fails loudly."""

from pathlib import Path

import pytest

from minty_shared.testing import guards


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def test_forbidden_imports_finds_flask_world_imports_only(tmp_path):
    root = _tree(
        tmp_path,
        {
            "core/a.py": "import flask\nfrom loguru import logger\nimport json\n",
            "core/b.py": "def f():\n    from blueprints.x import y\n",
            "core/c.py": "from . import models\nimport django\n",
        },
    )
    hits = guards.forbidden_imports(root, ["core"])
    assert hits == [
        "core/a.py:1 imports flask",
        "core/a.py:2 imports loguru",
        "core/b.py:2 imports blueprints",
    ]


def test_string_literals_skips_docstrings_allowed_files_tests_and_minty(tmp_path):
    root = _tree(
        tmp_path,
        {
            "app/a.py": '"""Says pettycashv3 in a docstring."""\nX = "pettycashv3"\n',
            "app/b.py": 'def f():\n    """pettycashv3"""\n    return "set search_path=pettycashv3"\n',
            "config/settings.py": 'S = "pettycashv3"\n',
            "tests/test_x.py": 'S = "pettycashv3"\n',
            "Minty/env.py": 'S = "pettycashv3"\n',
        },
    )
    hits = guards.string_literals(root, "pettycashv3", allowed={"config/settings.py"})
    assert hits == ["app/a.py:2", "app/b.py:3"]


def test_load_harness_says_where_it_looked(monkeypatch, tmp_path):
    from minty_shared.testing import pg

    monkeypatch.setenv("MINTY_REPO", str(tmp_path))
    with pytest.raises(RuntimeError, match="set MINTY_REPO"):
        pg.load_harness()
