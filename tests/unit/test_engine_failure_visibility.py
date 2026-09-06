"""A failure inside the engine must be loud, and must not report success.

Two defects found by audit while all 680 tests passed (BUG_REGISTER 003, 004). They
share a shape: the system behaved SAFELY and said nothing, so a safe failure was
indistinguishable from normal quiet operation — the confusion this project has already
paid for in FINDINGS 37 and 41.

BUG-003  `asyncio.gather(..., return_exceptions=True)` discarded its results, so a dead
         loop was invisible: the engine kept ticking with no decision loop, `run()`
         returned normally, and the CLI exited 0 reporting success.

BUG-004  `_strategy_status` swallowed every database error and returned `{}`. That fails
         CLOSED — every strategy reads DEV and live routing is refused — but nothing
         said the table could not be read, so a bot that had quietly stopped taking
         scalps looked exactly like a market with no setups.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "xauusd"


@pytest.fixture(scope="module")
def orchestrator_tree() -> ast.Module:
    return ast.parse((SRC / "engine" / "orchestrator.py").read_text(encoding="utf-8"))


class TestADeadLoopIsNotSilent:
    def test_the_gather_result_is_inspected(self, orchestrator_tree: ast.Module) -> None:
        """`gather(..., return_exceptions=True)` whose result is thrown away is the bug.

        Collecting exceptions instead of propagating them is deliberate — one loop dying
        must not cancel the others mid-trade — but only if something then LOOKS at them.
        """
        gathers = [
            node
            for node in ast.walk(orchestrator_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "gather"
        ]
        assert gathers, "the engine no longer gathers its loops"
        for call in gathers:
            returns_exceptions = any(
                kw.arg == "return_exceptions"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
                for kw in call.keywords
            )
            if not returns_exceptions:
                continue
            parent = next(
                (
                    n
                    for n in ast.walk(orchestrator_tree)
                    if isinstance(n, ast.Expr) and n.value is call
                ),
                None,
            )
            assert parent is None, (
                "gather(return_exceptions=True) used as a bare statement discards every "
                "task exception — a crashed loop becomes invisible"
            )

    @pytest.mark.parametrize("mode", ["crash", "clean"])
    def test_gather_semantics_this_relies_on(self, mode: str) -> None:
        """Pin the asyncio behaviour the fix depends on, so an upgrade cannot shift it.

        With return_exceptions=True the exception is RETURNED, not raised, and the other
        tasks still complete. That is exactly why the discarded result was silent.
        """

        async def boom() -> str:
            raise RuntimeError("loop died")

        async def fine() -> str:
            return "ok"

        async def go() -> list:
            tasks = [boom(), fine()] if mode == "crash" else [fine(), fine()]
            return await asyncio.gather(*tasks, return_exceptions=True)

        results = asyncio.run(go())
        if mode == "crash":
            assert isinstance(results[0], RuntimeError)
            assert results[1] == "ok", "a sibling task keeps running after one dies"
        else:
            assert results == ["ok", "ok"]

    def test_a_crashed_loop_is_recorded_for_the_caller(self, orchestrator_tree: ast.Module) -> None:
        """The CLI needs something to read; exiting 0 after a crash is the real damage."""
        assigned = {
            t.attr
            for node in ast.walk(orchestrator_tree)
            if isinstance(node, ast.AnnAssign | ast.Assign)
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(t, ast.Attribute)
        }
        assert "crashed_loops" in assigned

    def test_cancellation_is_not_treated_as_a_crash(self) -> None:
        """Stopping the engine cancels its loops. That is a clean shutdown, not a fault,
        and must not raise a CRITICAL alert every time someone closes the bot."""
        source = (SRC / "engine" / "orchestrator.py").read_text(encoding="utf-8")
        assert "CancelledError" in source, (
            "cancellation must be excluded, or every normal shutdown reports a crash"
        )


class TestTheCliReportsACrashedEngine:
    def test_cmd_run_returns_non_zero_when_a_loop_died(self) -> None:
        """A supervisor, a shortcut and a log reader all trust the exit code."""
        tree = ast.parse((SRC / "cli.py").read_text(encoding="utf-8"))
        fn = next(
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "cmd_run"
        )
        source = ast.unparse(fn)
        assert "crashed_loops" in source, "cmd_run must consult the engine's crash state"
        assert "return 1" in source, "a crashed engine must not exit 0"


class TestASilentSafeFailureIsStillAFailure:
    def test_strategy_status_logs_when_the_database_is_unavailable(
        self, orchestrator_tree: ast.Module
    ) -> None:
        fn = next(
            n
            for n in ast.walk(orchestrator_tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_strategy_status"
        )
        source = ast.unparse(fn)
        assert "log.error" in source, (
            "a database failure that silently disables live scalp routing is "
            "indistinguishable from a market with no setups"
        )
        assert "return {}" in source, "it must still fail CLOSED — every strategy reads DEV"

    def test_it_still_fails_closed(self) -> None:
        """Behaviour must not have changed: the empty map is what refuses live routing."""
        from xauusd.domain.enums import ValidationStatus

        assert ValidationStatus.DEV.live_eligible is False
        assert {}.get("scalp_sweep_reversal", ValidationStatus.DEV) is ValidationStatus.DEV
