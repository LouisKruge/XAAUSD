"""Broker/database reconciliation.

The broker is ALWAYS the source of truth. The database is corrected to match it, and
any divergence that cannot be explained trips the kill switch — an engine that thinks
it has one position while the account has two is more dangerous than one that has
stopped.

Runs every 60 seconds, on every startup, on every reconnect, and before every send.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from xauusd.domain.enums import KillSwitchReason
from xauusd.domain.types import BrokerPosition
from xauusd.monitoring.alerts import Notifier
from xauusd.monitoring.logging import get_logger
from xauusd.risk.kill_switch import KillSwitch

log = get_logger(__name__)


@dataclass(slots=True)
class Divergence:
    kind: str  # ORPHAN_AT_BROKER | MISSING_AT_BROKER | SL_MISMATCH |
    # VOLUME_MISMATCH | UNTAGGED_POSITION
    ticket: int
    detail: str
    severity: str = "WARNING"  # WARNING | CRITICAL
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ReconcileResult:
    checked_at: datetime
    broker_positions: int
    db_positions: int
    divergences: list[Divergence] = field(default_factory=list)
    adopted: list[int] = field(default_factory=list)
    closed_out: list[int] = field(default_factory=list)
    restored_stops: list[int] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.divergences

    @property
    def critical(self) -> list[Divergence]:
        return [d for d in self.divergences if d.severity == "CRITICAL"]

    def summary(self) -> str:
        if self.clean:
            return f"reconciled clean: {self.broker_positions} positions"
        return f"{len(self.divergences)} divergence(s): " + "; ".join(
            f"{d.kind}#{d.ticket}" for d in self.divergences[:5]
        )


class Reconciler:
    def __init__(
        self,
        broker: Any,
        kill_switch: KillSwitch,
        notifier: Notifier | None = None,
        magic: int = 0,
    ) -> None:
        self.broker = broker
        self.kill_switch = kill_switch
        self.notifier = notifier or Notifier()
        self.magic = magic

    def reconcile(
        self,
        db_positions: list[dict[str, Any]],
        now: datetime | None = None,
        adopt_orphans: bool = True,
        restore_stops: bool = True,
    ) -> ReconcileResult:
        """Compare broker truth with our record and classify every difference."""
        now = now or datetime.now(UTC)
        try:
            broker_positions = self.broker.positions(magic=None)
        except Exception as exc:
            self.kill_switch.trip(
                KillSwitchReason.BROKER_UNREACHABLE,
                f"cannot read positions for reconciliation: {exc}",
                now=now,
            )
            return ReconcileResult(
                now,
                0,
                len(db_positions),
                [Divergence("BROKER_UNREACHABLE", 0, str(exc), "CRITICAL")],
            )

        by_ticket: dict[int, BrokerPosition] = {p.ticket: p for p in broker_positions}
        db_by_ticket = {int(p["mt5_position"]): p for p in db_positions if p.get("mt5_position")}
        result = ReconcileResult(now, len(broker_positions), len(db_positions))

        # --- positions the broker has that we do not ---------------------------------
        for ticket, pos in by_ticket.items():
            if ticket in db_by_ticket:
                continue
            ours = pos.magic == self.magic
            if ours and adopt_orphans:
                result.adopted.append(ticket)
                result.divergences.append(
                    Divergence(
                        "ORPHAN_AT_BROKER",
                        ticket,
                        f"position {ticket} carries our magic but is absent from the database "
                        f"(crash recovery); adopting",
                        "WARNING",
                        {"symbol": pos.symbol, "volume": pos.volume, "tag": pos.client_tag},
                    )
                )
            else:
                # A position on our symbol that is not ours means a human is trading the
                # same account. That invalidates every exposure calculation.
                result.divergences.append(
                    Divergence(
                        "UNTAGGED_POSITION",
                        ticket,
                        f"position {ticket} on {pos.symbol} is not ours (magic {pos.magic}); "
                        f"exposure and risk calculations cannot be trusted",
                        "CRITICAL",
                        {"symbol": pos.symbol, "volume": pos.volume, "magic": pos.magic},
                    )
                )

        # --- positions we have that the broker does not ------------------------------
        for ticket, row in db_by_ticket.items():
            if ticket in by_ticket:
                continue
            result.closed_out.append(ticket)
            result.divergences.append(
                Divergence(
                    "MISSING_AT_BROKER",
                    ticket,
                    f"position {ticket} closed externally (stop, target, manual or stop-out); "
                    f"closing our record",
                    "WARNING",
                    {"strategy": row.get("strategy")},
                )
            )

        # --- field-level mismatches ---------------------------------------------------
        for ticket, pos in by_ticket.items():
            db_row = db_by_ticket.get(ticket)
            if db_row is None:
                continue
            db_sl = float(db_row.get("current_sl") or 0)
            if db_sl and pos.stop_loss and abs(db_sl - pos.stop_loss) > 1e-6:
                # The broker wins. Our intent is restored only if it is TIGHTER.
                tighter = db_sl > pos.stop_loss if pos.direction.sign > 0 else db_sl < pos.stop_loss
                if tighter and restore_stops:
                    result.restored_stops.append(ticket)
                result.divergences.append(
                    Divergence(
                        "SL_MISMATCH",
                        ticket,
                        f"broker stop {pos.stop_loss} differs from our record {db_sl}"
                        + (" (ours is tighter; restoring)" if tighter else ""),
                        "WARNING",
                        {"broker_sl": pos.stop_loss, "db_sl": db_sl},
                    )
                )
            if not pos.stop_loss:
                result.divergences.append(
                    Divergence(
                        "NO_SERVER_STOP",
                        ticket,
                        f"position {ticket} has NO server-side stop at the broker",
                        "CRITICAL",
                        {"symbol": pos.symbol},
                    )
                )
            db_vol = float(db_row.get("remaining_volume") or db_row.get("volume") or 0)
            if db_vol and abs(db_vol - pos.volume) > 1e-8:
                result.divergences.append(
                    Divergence(
                        "VOLUME_MISMATCH",
                        ticket,
                        f"broker volume {pos.volume} differs from our record {db_vol} "
                        f"(a partial close we did not record?)",
                        "WARNING",
                        {"broker": pos.volume, "db": db_vol},
                    )
                )

        if result.critical:
            details = "; ".join(d.detail for d in result.critical)
            self.kill_switch.trip(
                KillSwitchReason.STATE_DIVERGENCE,
                details,
                {"divergences": [d.kind for d in result.critical]},
                now,
            )
            self.notifier.critical(
                "RECONCILE",
                "Engine and broker state diverged",
                details,
                count=len(result.critical),
            )
        elif result.divergences:
            log.warning("reconcile_divergences", summary=result.summary())
        return result
