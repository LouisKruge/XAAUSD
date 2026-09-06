"""The parallel sweep must start on Windows, where `fork` does not exist.

The bug this was written after: `_run_configs` called `mp.get_context("fork")` behind a
guard that tested `mp.get_start_method(allow_none=True) not in (None, "fork")`. That
guard is wrong in the one place it matters — `allow_none=True` returns None when no
start method has been chosen yet, which is the normal state on a fresh interpreter, so
it read as "fork is fine" on a platform that has never had fork.

It passed every test on Linux because Linux took the fork branch and the fallback was
never executed. The first Windows run died immediately:

    ValueError: cannot find context for 'fork'

after the plan had printed and before a single configuration ran. A guard that is both
wrong and unreachable on the development platform is the shape of bug that only the user
finds, so these tests reach for the two things that actually differ across platforms.
"""

from __future__ import annotations

import ast
import multiprocessing as mp
from pathlib import Path

import pytest

SWEEP = Path(__file__).resolve().parents[2] / "scripts" / "scalp_sweep.py"


@pytest.fixture(scope="module")
def source() -> str:
    return SWEEP.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def tree(source: str) -> ast.Module:
    return ast.parse(source)


class TestItDoesNotDemandAStartMethodThePlatformMayLack:
    def test_no_hardcoded_fork_context(self, tree: ast.Module) -> None:
        """`get_context("fork")` raises on Windows. The default context always exists.

        Checked against the parsed source rather than by string search so a comment
        mentioning fork — and this module's own explanation does — cannot mask it.
        """
        offenders = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get_context"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "fork"
        ]
        assert not offenders, (
            "mp.get_context('fork') raises ValueError on Windows. Use the platform "
            "default context and give workers their state through an initializer."
        )

    def test_the_default_context_exists_here(self) -> None:
        """Whatever platform this runs on, the default context is startable."""
        assert mp.get_context() is not None

    def test_fork_is_not_assumed_to_be_available(self) -> None:
        """Documents the actual platform fact the old guard got wrong.

        On Windows `get_all_start_methods()` has no "fork", while
        `get_start_method(allow_none=True)` is None until something forces a choice —
        so None must never be read as "fork is available".
        """
        methods = mp.get_all_start_methods()
        assert "spawn" in methods, "spawn is available on every supported platform"
        unset = mp.get_start_method(allow_none=True)
        assert unset is None or unset in methods


class TestWorkersCanRebuildTheirOwnState:
    def test_an_initializer_is_passed_to_the_pool(self, tree: ast.Module) -> None:
        """Under spawn the child is a fresh interpreter with an empty module namespace.

        Without an initializer the worker's `_SHARED` is empty and the first job dies on
        a KeyError — so the pool must be constructed with one. This is the half of the
        fix that the crash itself did not reveal: removing the hardcoded fork would have
        stopped the ValueError and produced a KeyError instead.
        """
        pools = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ProcessPoolExecutor"
        ]
        assert pools, "the sweep no longer constructs a process pool"
        for pool in pools:
            kwargs = {kw.arg for kw in pool.keywords}
            assert "initializer" in kwargs, (
                "ProcessPoolExecutor needs an initializer, or a spawn-based platform "
                "starts workers with no history loaded"
            )

    def test_the_initializer_is_idempotent(self) -> None:
        """Under fork the child already has the data; re-loading it would be wasteful.

        Calling it with state already present must return without touching the database,
        which also means the test needs no database.
        """
        import sys

        sys.path.insert(0, str(SWEEP.parent))
        import scalp_sweep

        sentinel = object()
        previous = scalp_sweep._SHARED.get("data")
        scalp_sweep._SHARED["data"] = sentinel
        try:
            scalp_sweep._worker_init("mt5")
            assert scalp_sweep._SHARED["data"] is sentinel
        finally:
            if previous is None:
                scalp_sweep._SHARED.pop("data", None)
            else:
                scalp_sweep._SHARED["data"] = previous


class TestFailureToParalleliseIsNotFailureToRun:
    def test_a_serial_fallback_exists(self, tree: ast.Module) -> None:
        """A pool that will not start must degrade to serial, not abort the sweep.

        The serial path produces identical numbers; losing an hour of someone's time to
        an environment quirk is a worse outcome than running slowly.
        """
        fn = next(
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_run_configs"
        )
        handlers = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]
        assert handlers, "_run_configs must survive a pool that cannot start"
