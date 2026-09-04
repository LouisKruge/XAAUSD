"""Running operations from the dashboard instead of a terminal.

This module starts processes on behalf of a network request, so most of what is worth
asserting is about what it REFUSES: no caller-supplied command, no caller-supplied
argument, no shell, and no unbounded parameter reaching argv.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from xauusd.config.settings import Settings
from xauusd.dashboard.jobs import JOBS, JobAlreadyRunning, JobRunner, UnknownJob

TOKEN = "j" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def runner() -> JobRunner:
    return JobRunner(max_history=3)


class TestOnlyAllowlistedWorkRuns:
    def test_an_unknown_job_is_refused(self, runner: JobRunner) -> None:
        with pytest.raises(UnknownJob):
            runner.start("rm -rf /")

    def test_the_catalogue_is_a_closed_set(self, runner: JobRunner) -> None:
        assert {j["key"] for j in runner.catalogue()} == {
            "sample_data",
            "doctor",
            "validate",
            "backtest",
            "harvest",
            "scalp_sweep",
        }

    def test_no_job_reaches_a_shell(self) -> None:
        """Every argv is a list whose first entry is the interpreter, so nothing is
        parsed as a command line even if a value somehow contained a metacharacter."""
        for spec in JOBS.values():
            argv = spec.argv({name: d for name, (d, _, _) in spec.params.items()})
            assert isinstance(argv, list)
            assert argv[0].endswith("python") or "python" in argv[0]
            assert all(isinstance(a, str) for a in argv)
            assert not any(";" in a or "&&" in a or "|" in a for a in argv)


class TestABacktestCanReachRealData:
    """The backtest button could only ever pass --synthetic, and the pipeline suite
    asserts synthetic data produces no trades. So the single backtest an operator could
    reach was guaranteed to report 0 trades and an empty equity curve — which reads as a
    broken strategy, not as the absent data it actually was."""

    def test_zero_means_use_the_harvested_history(self) -> None:
        argv = JOBS["backtest"].argv({"synthetic": 0, "step": 6})
        assert "--synthetic" not in argv, "0 must fall through to the real database"

    def test_a_nonzero_value_still_runs_the_smoke_test(self) -> None:
        argv = JOBS["backtest"].argv({"synthetic": 30000, "step": 6})
        assert argv[argv.index("--synthetic") + 1] == "30000"

    def test_the_default_is_real_data(self) -> None:
        """The default a browser gets when it sends no parameters. Pointing it at
        generated data by default is what made "0 trades" look like a verdict."""
        assert JOBS["backtest"].params["synthetic"][0] == 0

    def test_harvest_asks_for_bars(self) -> None:
        argv = JOBS["harvest"].argv({"bars": 60000})
        assert argv[-2:] == ["--bars", "60000"]


class TestParametersCannotEscapeTheirBounds:
    def test_an_oversized_value_is_clamped(self, runner: JobRunner) -> None:
        spec = JOBS["validate"]
        values = runner._validate_params(spec, {"synthetic": 10**9, "step": 10**6})
        assert values["synthetic"] == spec.params["synthetic"][2]
        assert values["step"] == spec.params["step"][2]

    def test_a_negative_value_is_clamped(self, runner: JobRunner) -> None:
        spec = JOBS["validate"]
        values = runner._validate_params(spec, {"synthetic": -5, "step": 0})
        assert values["synthetic"] == spec.params["synthetic"][1]
        assert values["step"] == spec.params["step"][1]

    @pytest.mark.parametrize("hostile", ["; rm -rf /", "$(whoami)", "1 || true", None, {}])
    def test_a_non_integer_falls_back_to_the_default(
        self, runner: JobRunner, hostile: object
    ) -> None:
        """A string never becomes an argv entry: it cannot be coerced, so the default
        integer is used instead."""
        spec = JOBS["validate"]
        values = runner._validate_params(spec, {"synthetic": hostile})
        assert values["synthetic"] == spec.params["synthetic"][0]
        assert isinstance(values["synthetic"], int)

    def test_an_unknown_parameter_is_dropped(self, runner: JobRunner) -> None:
        values = runner._validate_params(JOBS["validate"], {"--output-file": "/etc/passwd"})
        assert set(values) == {"synthetic", "step"}


class TestOneAtATime:
    def test_a_second_job_is_refused_while_one_runs(self, runner: JobRunner) -> None:
        """Two heavy runs competing for the same cores makes both slower and the
        machine unresponsive."""
        runner.start("doctor")
        try:
            with pytest.raises(JobAlreadyRunning):
                runner.start("doctor")
        finally:
            runner.cancel()

    def test_the_runner_frees_up_when_the_job_ends(self, runner: JobRunner) -> None:
        job = runner.start("doctor")
        deadline = time.monotonic() + 60
        while runner.busy and time.monotonic() < deadline:
            time.sleep(0.2)
        assert not runner.busy, "the runner never released after the job finished"
        assert runner.get(job.id) is not None
        assert job.status in {"PASSED", "FAILED", "ERROR"}

    def test_history_is_bounded(self, runner: JobRunner) -> None:
        for _ in range(5):
            job = runner.start("doctor")
            deadline = time.monotonic() + 60
            while runner.busy and time.monotonic() < deadline:
                time.sleep(0.1)
            assert job.finished_at is not None
        assert len(runner.recent()) == 3, "history must not grow without bound"


class TestDoctorActuallyRuns:
    def test_the_preflight_job_produces_its_report(self, runner: JobRunner) -> None:
        """The point of the button: the same output the terminal would have shown."""
        job = runner.start("doctor")
        deadline = time.monotonic() + 120
        while runner.busy and time.monotonic() < deadline:
            time.sleep(0.2)
        text = "\n".join(job.output)
        assert job.status != "ERROR", text
        assert "symbol spec" in text, text
        assert "dashboard" in text, text


@pytest.fixture
def client(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    from xauusd.dashboard import api

    settings = Settings(
        database={"url": f"sqlite:///{tmp_path}/jobs.db"},
        dashboard={"host": "0.0.0.0", "auth_token": TOKEN},
    )
    monkeypatch.setattr(api, "_settings", settings)
    monkeypatch.setattr(api, "_db", None)
    yield TestClient(api.app)
    monkeypatch.setattr(api, "_db", None)


class TestTheJobApiIsGuardedToo:
    def test_starting_a_job_requires_the_token(self, client) -> None:  # type: ignore[no-untyped-def]
        """Unauthenticated process execution would be the worst hole in the system."""
        r = client.post("/api/jobs", json={"key": "validate", "params": {}})
        assert r.status_code == 401

    def test_reading_the_catalogue_requires_the_token(self, client) -> None:  # type: ignore[no-untyped-def]
        assert client.get("/api/jobs/catalogue").status_code == 401

    def test_an_unknown_job_is_a_400_not_a_500(self, client) -> None:  # type: ignore[no-untyped-def]
        r = client.post("/api/jobs", json={"key": "nope", "params": {}}, headers=AUTH)
        assert r.status_code == 400

    def test_a_string_parameter_is_rejected_by_the_schema(self, client) -> None:  # type: ignore[no-untyped-def]
        r = client.post(
            "/api/jobs",
            json={"key": "validate", "params": {"synthetic": "; rm -rf /"}},
            headers=AUTH,
        )
        assert r.status_code == 422, "params are typed as int; a string must not parse"

    def test_the_catalogue_is_served_with_the_token(self, client) -> None:  # type: ignore[no-untyped-def]
        r = client.get("/api/jobs/catalogue", headers=AUTH)
        assert r.status_code == 200
        assert {j["key"] for j in r.json()} == {
            "sample_data",
            "doctor",
            "validate",
            "backtest",
            "harvest",
            "scalp_sweep",
        }
