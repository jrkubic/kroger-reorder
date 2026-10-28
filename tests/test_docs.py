# tests/test_docs.py
"""README drift guard. Fails when code grows a route, setting, module, design doc, or version that the
README does not mention, so the docs stay honest as features land. Each assertion names what is missing."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi.routing import APIRoute

from kroger_reorder import __version__
from kroger_reorder.main import create_app
from kroger_reorder.storage import Storage

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")


def section(title: str) -> str:
    """Text of a `## title` section (up to the next `## `)."""
    match = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", README, re.S | re.M)
    assert match, f"README has no '## {title}' section"
    return match.group(1)


def test_every_env_setting_is_in_the_configuration_table():
    names = re.findall(r"^#?([A-Z][A-Z0-9_]+)=", (ROOT / ".env.example").read_text(encoding="utf-8"), re.M)
    assert names, ".env.example lists no settings"
    table = section("Configuration")
    missing = [n for n in names if f"`{n}`" not in table]
    assert not missing, f"Settings in .env.example but not in README Configuration table: {missing}"


def test_every_route_is_in_the_http_api_section(tmp_path):
    app = create_app(Storage(tmp_path), kroger=object())
    documented = {
        (method, re.sub(r"\{[^}]+\}", "{}", path))
        for method, path in re.findall(r"^\| `(GET|POST|PUT|PATCH|DELETE)` \| `([^`?]+)", section("HTTP API"), re.M)
    }
    actual = {
        (method, re.sub(r"\{[^}]+\}", "{}", route.path))
        for route in app.routes if isinstance(route, APIRoute)
        for method in route.methods
        if route.path.startswith(("/api", "/auth", "/callback"))
    }
    missing = sorted(actual - documented)
    assert not missing, f"Routes in main.py but not in README HTTP API tables: {missing}"
    stale = sorted(documented - actual)
    assert not stale, f"Routes in README HTTP API tables that no longer exist: {stale}"


def test_every_module_is_in_the_project_structure():
    modules = sorted(p.name for p in (ROOT / "kroger_reorder").glob("*.py"))
    structure = section("Project structure")
    missing = [m for m in modules if m not in structure]
    assert not missing, f"Modules in kroger_reorder/ but not in README Project structure: {missing}"


def test_every_design_doc_is_in_the_design_documents_table():
    docs = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "docs" / "plans").glob("*-design.md"))
    table = section("Design documents")
    missing = [d for d in docs if d not in table]
    assert not missing, f"Design docs under docs/plans/ but not in README Design documents table: {missing}"


def test_version_history_matches_package_version():
    rows = re.findall(r"^\| \d{4}-\d{2}-\d{2} \| v(\d+) \|", section("Version history"), re.M)
    assert rows, "README Version history has no rows"
    major = __version__.split(".")[0]
    assert max(int(r) for r in rows) == int(major), (
        f"kroger_reorder.__version__ is {__version__} but the newest README Version history row is v{max(rows)}; "
        "bump one or the other")


def test_agents_guide_exists_and_is_linked():
    assert (ROOT / "AGENTS.md").exists(), "AGENTS.md is missing"
    assert "@AGENTS.md" in (ROOT / "CLAUDE.md").read_text(encoding="utf-8"), "CLAUDE.md should import AGENTS.md"
    assert "AGENTS.md" in README, "README should point contributors and agents at AGENTS.md"
