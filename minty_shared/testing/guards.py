"""Source-walking guards, shared so every API checks the same thing the same way. Each returns
the offending ``path:line`` strings; a repo's test asserts the list is empty.

* :func:`forbidden_imports` - nothing imports the Flask world (a verbatim port carries its
  imports, and a function-local one fails only when first run, months later).
* :func:`string_literals` - an application string never spells a value that belongs to one
  place: the schema name (``settings.DB_SCHEMA``), the default zone (``minty_shared.time``).
  Docstrings may say it.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator
from pathlib import Path

#: What a Django API must never import: Flask, its extensions, and Flask's own top-level
#: packages (``models``, ``blueprints``, ``services``, ``cli``).
FLASK_WORLD = frozenset(
    {
        "flask",
        "flask_mail",
        "flask_sqlalchemy",
        "flask_migrate",
        "flask_login",
        "flask_wtf",
        "sqlalchemy",
        "alembic",
        "loguru",
        "werkzeug",
        "models",
        "blueprints",
        "services",
        "cli",
    }
)

_ALWAYS_SKIP = (".venv", "node_modules", "site-packages")


def python_files(root: Path, packages: Iterable[str]) -> list[Path]:
    files: list[Path] = []
    for package in packages:
        files.extend(sorted((root / package).rglob("*.py")))
    return [p for p in files if not any(s in p.as_posix() for s in _ALWAYS_SKIP)]


def _imported_roots(path: Path) -> Iterator[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.lineno, node.module.split(".")[0]


def forbidden_imports(
    root: Path, packages: Iterable[str], forbidden: Iterable[str] = FLASK_WORLD
) -> list[str]:
    banned = set(forbidden)
    return [
        f"{path.relative_to(root).as_posix()}:{lineno} imports {name}"
        for path in python_files(root, packages)
        for lineno, name in _imported_roots(path)
        if name in banned
    ]


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def string_literals(
    root: Path,
    needle: str,
    *,
    allowed: Iterable[str] = (),
    skip: Iterable[str] = ("tests/", "e2e/", "migrations/"),
) -> list[str]:
    """Every string constant (not a docstring) under ``root`` that contains ``needle``,
    outside the ``allowed`` files (repo-relative posix paths) and any path containing a
    ``skip`` fragment. A checkout of Minty beside the code (CI's harness) is skipped."""
    allow, skips = set(allowed), tuple(skip)
    hits: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith("Minty/") or rel in allow or any(s in rel for s in skips):
            continue
        if any(s in rel for s in _ALWAYS_SKIP):
            continue
        source = path.read_text(encoding="utf-8-sig")
        if needle not in source:
            continue
        tree = ast.parse(source)
        docs = _docstring_ids(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and needle in node.value
                and id(node) not in docs
            ):
                hits.append(f"{rel}:{node.lineno}")
    return hits
