"""Long-running operations, started from the dashboard instead of a terminal.

Three things previously required the command line: the pre-flight check, the deployment
gate, and a backtest. None of them belong in a request handler — validation runs for
minutes — so each becomes a background job with captured output the browser can poll.

Two rules govern everything here, because this module starts processes on behalf of a
network request:

1. **The job list is a fixed allowlist.** A caller picks a job by name from a closed set.
   There is no path by which a request supplies a command, an argument name, or a file
   path.
2. **Parameters are typed and bounded, never interpolated.** The only free values are
   integers, each clamped to a stated range, passed as separate argv entries. Nothing
   reaches a shell: every spawn is `shell=False` with a list.

Jobs are also serialised — one at a time. A backtest and a validation run competing for
the same cores would make both slower and the machine unresponsive, and the operator
almost never wants two at once.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from xauusd.monitoring.logging import get_logger

log = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
MAX_OUTPUT_LINES = 2000
MAX_HISTORY = 20


@dataclass
class JobSpec:
    """One allowlisted operation.

    `build` turns validated integer parameters into an argv list. It never sees a string
    from the caller.
    """

    key: str
    title: str
    description: str
    params: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    # name -> (default, minimum, maximum)

    def argv(self, values: dict[str, int]) -> list[str]:
        raise NotImplementedError


@dataclass
class DoctorJob(JobSpec):
    def argv(self, values: dict[str, int]) -> list[str]:
        return [sys.executable, "-m", "xauusd.cli", "doctor"]


@dataclass
class ValidateJob(JobSpec):
    def argv(self, values: dict[str, int]) -> list[str]:
        return [
            sys.executable,
            "-m",
            "xauusd.cli",
            "validate",
            "--synthetic",
            str(values["synthetic"]),
            "--step",
            str(values["step"]),
        ]


@dataclass
class SampleDataJob(JobSpec):
    """Populate the database so the dashboard can be explored before a broker exists.

    Without this the first thing a new operator sees is five empty panels, which is
    indistinguishable from a broken install.
    """

    def argv(self, values: dict[str, int]) -> list[str]:
        return [
            sys.executable,
            str(REPO_ROOT / "scripts" / "seed_demo_data.py"),
            str(values["days"]),
        ]


@dataclass
class BacktestJob(JobSpec):
    def argv(self, values: dict[str, int]) -> list[str]:
        return [
            sys.executable,
            "-m",
            "xauusd.cli",
            "backtest",
            "--synthetic",
            str(values["synthetic"]),
            "--step",
            str(values["step"]),
        ]


JOBS: dict[str, JobSpec] = {
    "sample_data": SampleDataJob(
        key="sample_data",
        title="Load sample data",
        description=(
            "Fills the dashboard with realistic decisions, trades and equity history so "
            "you can see how every screen reads before connecting a broker. Sample data "
            "only — it is not a backtest and proves nothing about the strategy."
        ),
        params={"days": (45, 5, 365)},
    ),
    "doctor": DoctorJob(
        key="doctor",
        title="Pre-flight check",
        description=(
            "Config, database and broker connectivity, and the ACTUAL symbol "
            "specification the broker returns. Read those numbers before anything else."
        ),
    ),
    "validate": ValidateJob(
        key="validate",
        title="Run the deployment gate",
        description=(
            "The full validation suite: in-sample, out-of-sample, walk-forward, Monte "
            "Carlo and regime splits with realistic costs. Nothing reaches a live "
            "account until this passes. Expect it to fail — that is the gate working. "
            "Takes several minutes."
        ),
        params={"synthetic": (60000, 5000, 400000), "step": (3, 1, 60)},
    ),
    "backtest": BacktestJob(
        key="backtest",
        title="Run a backtest",
        description="A single backtest over generated data, with the metrics report.",
        params={"synthetic": (30000, 5000, 400000), "step": (6, 1, 60)},
    ),
}


@dataclass
class Job:
    id: int
    key: str
    title: str
    started_at: datetime
    status: str = "RUNNING"  # RUNNING | PASSED | FAILED | ERROR
    exit_code: int | None = None
    finished_at: datetime | None = None
    output: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_OUTPUT_LINES))
    params: dict[str, int] = field(default_factory=dict)

    def as_dict(self, include_output: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "key": self.key,
            "title": self.title,
            "status": self.status,
            "exit_code": self.exit_code,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "params": self.params,
            "running": self.status == "RUNNING",
        }
        if include_output:
            d["output"] = list(self.output)
        return d


class JobAlreadyRunning(RuntimeError):
    pass


class UnknownJob(KeyError):
    pass


class JobRunner:
    """Runs allowlisted operations one at a time and keeps their output."""

    def __init__(self, max_history: int = MAX_HISTORY) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[int, Job] = {}
        self._order: deque[int] = deque(maxlen=max_history)
        self._next_id = 1
        self._current: int | None = None
        self._process: subprocess.Popen[str] | None = None

    # -- inspection ---------------------------------------------------------------

    def catalogue(self) -> list[dict[str, Any]]:
        return [
            {
                "key": s.key,
                "title": s.title,
                "description": s.description,
                "params": {
                    n: {"default": d, "min": lo, "max": hi} for n, (d, lo, hi) in s.params.items()
                },
            }
            for s in JOBS.values()
        ]

    def get(self, job_id: int) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._jobs[i].as_dict(include_output=False) for i in reversed(self._order)]

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._current is not None

    # -- running ------------------------------------------------------------------

    def _validate_params(self, spec: JobSpec, supplied: dict[str, Any]) -> dict[str, int]:
        """Coerce to int and clamp. Anything unparseable falls back to the default
        rather than reaching argv, and unknown keys are dropped entirely."""
        out: dict[str, int] = {}
        for name, (default, lo, hi) in spec.params.items():
            raw = supplied.get(name, default)
            try:
                value = int(raw)
            except (TypeError, ValueError):
                value = default
            out[name] = max(lo, min(hi, value))
        return out

    def start(self, key: str, params: dict[str, Any] | None = None) -> Job:
        spec = JOBS.get(key)
        if spec is None:
            raise UnknownJob(key)

        values = self._validate_params(spec, params or {})
        with self._lock:
            if self._current is not None:
                running = self._jobs[self._current]
                raise JobAlreadyRunning(f"{running.title} is still running")
            job = Job(
                id=self._next_id,
                key=key,
                title=spec.title,
                started_at=datetime.now(UTC),
                params=values,
            )
            self._jobs[job.id] = job
            if len(self._order) == self._order.maxlen:
                self._jobs.pop(self._order[0], None)
            self._order.append(job.id)
            self._next_id += 1
            self._current = job.id

        argv = spec.argv(values)
        log.info("job_started", job=key, id=job.id, params=values)
        threading.Thread(target=self._run, args=(job, argv), daemon=True).start()
        return job

    def _run(self, job: Job, argv: list[str]) -> None:
        try:
            # shell=False with a list argv: nothing here is parsed as a command line.
            proc = subprocess.Popen(
                argv,
                cwd=REPO_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            with self._lock:
                self._process = proc
            assert proc.stdout is not None
            for line in proc.stdout:
                job.output.append(line.rstrip("\n"))
            code = proc.wait()
            job.exit_code = code
            # A non-zero exit from `validate` means the gate REFUSED the strategy, which
            # is a result, not a malfunction. Naming it FAILED rather than ERROR keeps
            # that distinction visible in the UI.
            job.status = "PASSED" if code == 0 else "FAILED"
        except Exception as exc:
            job.output.append(f"could not run the job: {type(exc).__name__}: {exc}")
            job.status = "ERROR"
            job.exit_code = -1
            log.error("job_error", job=job.key, id=job.id, error=str(exc))
        finally:
            job.finished_at = datetime.now(UTC)
            with self._lock:
                self._current = None
                self._process = None
            log.info("job_finished", job=job.key, id=job.id, status=job.status)

    def cancel(self) -> bool:
        """Stop the running job. Returns False when nothing was running."""
        with self._lock:
            proc = self._process
        if proc is None:
            return False
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        return True


runner = JobRunner()
