"""Component health tracking and the process heartbeat."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any


@dataclass(slots=True)
class ComponentHealth:
    name: str
    status: str = "UNKNOWN"  # OK | DEGRADED | DOWN | UNKNOWN
    last_ok: datetime | None = None
    last_check: datetime | None = None
    consecutive_failures: int = 0
    latency_ms: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def is_ok(self) -> bool:
        return self.status == "OK"


class HealthRegistry:
    """Central view of every subsystem's health, consumed by the dashboard and watchdog."""

    def __init__(self, stale_after_seconds: float = 120.0) -> None:
        self._components: dict[str, ComponentHealth] = {}
        self._stale_after = timedelta(seconds=stale_after_seconds)

    def report(
        self,
        name: str,
        ok: bool,
        latency_ms: int = 0,
        detail: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> ComponentHealth:
        now = now or datetime.now(UTC)
        c = self._components.setdefault(name, ComponentHealth(name=name))
        c.last_check = now
        c.latency_ms = latency_ms
        c.detail = detail or {}
        if ok:
            c.status = "OK"
            c.last_ok = now
            c.consecutive_failures = 0
        else:
            c.consecutive_failures += 1
            c.status = "DOWN" if c.consecutive_failures >= 3 else "DEGRADED"
        return c

    def get(self, name: str) -> ComponentHealth | None:
        return self._components.get(name)

    def all(self) -> dict[str, ComponentHealth]:
        return dict(self._components)

    def is_healthy(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        if not self._components:
            return False
        for c in self._components.values():
            if c.status == "DOWN":
                return False
            if c.last_check is None or (now - c.last_check) > self._stale_after:
                return False
        return True

    def summary(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        return {
            "healthy": self.is_healthy(now),
            "components": {
                n: {
                    "status": c.status,
                    "last_check": c.last_check.isoformat() if c.last_check else None,
                    "consecutive_failures": c.consecutive_failures,
                    "latency_ms": c.latency_ms,
                    "detail": c.detail,
                }
                for n, c in self._components.items()
            },
        }
