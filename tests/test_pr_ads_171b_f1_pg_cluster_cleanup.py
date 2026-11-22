"""PR-ADS-171B-F1 — throwaway PostgreSQL clusters must not leak their data.

Each PG test module starts its own cluster whose data directory is owned by
the ``postgres`` user. On CI the runner is an unprivileged user, so
``shutil.rmtree(tmp, ignore_errors=True)`` could not delete those files and
silently left about 50 MB behind per cluster. With the PG step and the full
suite both starting clusters, the runner's disk filled during PR 223's full
suite (55 errors, "No space left on device" from initdb).

Locally the suite usually runs as root, where rmtree works, so a behavioural
test here would pass with or without the fix. This guard is therefore
structural: every cluster helper's ``stop`` must delete its directory with
the same non-interactive sudo the helper already uses to run ``pg_ctl``.
"""
from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def _cluster_stop_methods():
    found = []
    for path in sorted(TESTS.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "_PgCluster":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "stop":
                        found.append((path.name, item))
    return found


def _calls_sudo_rm(func: ast.FunctionDef) -> bool:
    for node in ast.walk(func):
        if isinstance(node, ast.List):
            values = [e.value for e in node.elts
                      if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if values[:4] == ["sudo", "-n", "rm", "-rf"]:
                return True
    return False


def test_01_there_are_cluster_helpers_to_check():
    # A guard that finds nothing is not a guard.
    assert len(_cluster_stop_methods()) >= 12


def test_02_every_cluster_stop_deletes_its_postgres_owned_directory():
    missing = [name for name, func in _cluster_stop_methods()
               if not _calls_sudo_rm(func)]
    assert missing == [], (
        "these _PgCluster.stop methods leave the postgres-owned data "
        f"directory behind on an unprivileged runner: {missing}")
