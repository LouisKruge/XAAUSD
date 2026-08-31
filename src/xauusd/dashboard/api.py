"""Dashboard backend.

Runs as a SEPARATE PROCESS from the engine, with a read-only database role. The
dashboard is the component most likely to be edited, restarted and experimented with,
and it must never be able to deadlock, block or crash the trading loop.

Its only write paths are two explicitly audited safety commands — trip the kill switch
and flatten all positions — which are published as messages for the engine to execute.
The API never places or modifies an order itself.
"""

from __future__ import annotations

import json
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from xauusd.config.settings import Settings, load_settings
from xauusd.database.repositories import Repositories
from xauusd.database.session import Database
from xauusd.monitoring.logging import get_logger

log = get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class CommandRequest(BaseModel):
    """One of the two audited write paths."""

    reason: str
    operator: str = "dashboard"


class Hub:
    """In-process pub/sub for WebSocket clients.

    In production the engine publishes to Redis and this subscribes; the interface is
    the same so the dashboard code does not change between the two.
    """

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.latest: dict[str, Any] = {}
        self.commands: list[dict[str, Any]] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)
        if self.latest:
            await ws.send_text(json.dumps({"type": "snapshot", "data": self.latest}))

    def disconnect(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def broadcast(self, kind: str, data: dict[str, Any]) -> None:
        if kind == "state":
            self.latest = data
        message = json.dumps({"type": kind, "data": data}, default=str)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    def queue_command(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        cmd = {
            "command": name,
            "payload": payload,
            "queued_at": datetime.now(UTC).isoformat(),
        }
        self.commands.append(cmd)
        log.warning("dashboard_command_queued", command=name, **payload)
        return cmd


hub = Hub()
_settings: Settings | None = None
_db: Database | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def _database() -> Database:
    global _db
    if _db is None:
        _db = Database(get_settings().database.url)
        _db.create_all()
    return _db


def get_repos():  # type: ignore[no-untyped-def]
    with _database().session() as s:
        yield Repositories(s)


def require_token(authorization: str | None = Header(default=None)) -> None:
    """Bearer-token gate on every route.

    When no token is configured the dashboard is loopback-only (enforced in
    DashboardConfig), so the OS is the boundary and this is a no-op. Once a token is
    set it is required everywhere — including the read endpoints, because the decision
    journal is the record of a live trading account, and including the engine-facing
    endpoints, because GET /api/commands/pending consumes the queue.
    """
    expected = get_settings().dashboard.auth_token
    if not expected:
        return
    supplied = ""
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:]
    # compare_digest so a wrong token cannot be recovered a byte at a time.
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    log.info("dashboard_starting", static=str(STATIC_DIR))
    yield
    log.info("dashboard_stopping")


app = FastAPI(title="XAUUSD Trading Terminal", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def authenticate(request: Request, call_next: Any) -> Any:
    """Guard every /api path.

    Deliberately a middleware and not a per-route dependency: a route added later is
    protected because it is under /api, not because whoever wrote it remembered to ask.
    The page shell and its assets are served unauthenticated — they contain no data, and
    the browser cannot attach a bearer header to a top-level navigation.
    """
    if request.url.path.startswith("/api"):
        try:
            require_token(request.headers.get("authorization"))
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------------------
# Command centre
# --------------------------------------------------------------------------------------


@app.get("/api/state")
async def state() -> dict[str, Any]:
    """The live engine state, as last published. Never queries the engine directly."""
    if not hub.latest:
        s = get_settings()
        return {
            "connected": False,
            "mode": str(s.mode),
            "live_trading": s.live_trading,
            "message": "engine has not published state yet",
        }
    return hub.latest


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "ts": datetime.now(UTC).isoformat(),
        "engine_connected": bool(hub.latest),
        "websocket_clients": len(hub.clients),
    }


@app.get("/api/config")
async def config() -> dict[str, Any]:
    s = get_settings()
    return {
        "mode": str(s.mode),
        "live_trading": s.live_trading,
        "symbol": s.symbol,
        "config_hash": s.config_hash(),
        "risk": s.risk.model_dump(),
        "thresholds": s.thresholds.model_dump(),
        "scoring": s.scoring.model_dump(),
        "enabled_strategies": s.enabled_strategies,
    }


# --------------------------------------------------------------------------------------
# Decisions — the explainability surface
# --------------------------------------------------------------------------------------


@app.get("/api/decisions")
async def decisions(
    limit: int = Query(100, le=500),
    classification: str | None = None,
    repos=Depends(get_repos),  # type: ignore[no-untyped-def]
) -> list[dict[str, Any]]:
    rows = repos.decisions.recent(limit=limit, classification=classification)
    return [
        {
            "id": r.id,
            "ts": r.ts,
            "classification": r.classification,
            "strategy": r.strategy,
            "direction": r.direction,
            "score": r.setup_score,
            "probability": r.probability,
            "rr": r.planned_rr,
            "entry": float(r.planned_entry) if r.planned_entry else None,
            "sl": float(r.planned_sl) if r.planned_sl else None,
            "tp": float(r.planned_tp2 or r.planned_tp1)
            if (r.planned_tp1 or r.planned_tp2)
            else None,
            "lots": r.planned_lots,
            "risk_pct": r.planned_risk_pct,
            "blocking_gate": r.blocking_gate,
            "mode": r.mode,
        }
        for r in rows
    ]


@app.get("/api/decisions/{decision_id}")
async def decision_detail(decision_id: int, repos=Depends(get_repos)) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Everything about one decision. This answers both required questions."""
    r = repos.decisions.get(decision_id)
    if r is None:
        raise HTTPException(404, "decision not found")
    return {
        "id": r.id,
        "ts": r.ts,
        "symbol": r.symbol,
        "mode": r.mode,
        "classification": r.classification,
        "strategy": r.strategy,
        "strategy_version": r.strategy_version,
        "direction": r.direction,
        "score": r.setup_score,
        "score_breakdown": r.score_breakdown,
        "probability": r.probability,
        "model_id": r.model_id,
        "model_health": r.model_health,
        "features": r.features,
        "gate_trace": r.gate_trace,
        "blocking_gate": r.blocking_gate,
        "all_blocking": r.all_blocking,
        "reasons_for": r.reasons_for,
        "reasons_against": r.reasons_against,
        "sizing": r.sizing,
        "entry": float(r.planned_entry) if r.planned_entry else None,
        "sl": float(r.planned_sl) if r.planned_sl else None,
        "tp1": float(r.planned_tp1) if r.planned_tp1 else None,
        "tp2": float(r.planned_tp2) if r.planned_tp2 else None,
        "rr": r.planned_rr,
        "invalidation": r.invalidation,
        "config_hash": r.config_hash,
        "git_sha": r.git_sha,
        "latency_ms": r.latency_ms,
    }


@app.get("/api/rejections")
async def rejections(
    hours: int = Query(24, le=24 * 90),
    repos=Depends(get_repos),  # type: ignore[no-untyped-def]
) -> dict[str, Any]:
    """The rejection ledger: why the bot did not trade.

    During paper trading this is the most useful screen in the system — it is how you
    find out the bot is idle because of a bug rather than because of discipline.
    """
    end = datetime.now(UTC)
    start = end - timedelta(hours=hours)
    ledger = repos.decisions.rejection_ledger(start, end)
    counts = repos.decisions.counts_by_classification(start, end)
    total = sum(counts.values())
    return {
        "window_hours": hours,
        "total_evaluations": total,
        "classifications": counts,
        "selectivity": (round(1 - counts.get("NO_TRADE", 0) / total, 6) if total else 0.0),
        "ledger": [
            {"gate": gate, "count": n, "share": round(n / total, 4) if total else 0}
            for gate, n in ledger
        ],
    }


# --------------------------------------------------------------------------------------
# Performance
# --------------------------------------------------------------------------------------


@app.get("/api/performance")
async def performance(
    days: int = Query(90, le=3650),
    repos=Depends(get_repos),  # type: ignore[no-untyped-def]
) -> dict[str, Any]:
    from xauusd.backtesting.metrics import compute
    from xauusd.domain.enums import Classification as C
    from xauusd.domain.enums import Direction, ExitReason, Regime, Session
    from xauusd.domain.types import ClosedTrade

    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    rows = repos.positions.closed_between(start, end)
    trades = [
        ClosedTrade(
            opened_at=r.opened_at,
            closed_at=r.closed_at,
            symbol=r.symbol,
            direction=Direction(r.side),
            strategy=r.strategy or "unknown",
            classification=C(r.classification or "A"),
            entry=float(r.entry_price),
            initial_sl=float(r.initial_sl),
            exit_price=float(r.exit_price or r.entry_price),
            volume=float(r.volume),
            risk_money=float(r.risk_money),
            gross_pnl=float(r.gross_pnl or 0),
            commission=float(r.commission or 0),
            swap=float(r.swap or 0),
            exit_reason=ExitReason(r.exit_reason or "MANUAL"),
            mae_r=float(r.mae_r or 0),
            mfe_r=float(r.mfe_r or 0),
            bars_held=r.bars_held or 0,
            session=Session(r.session or "OFF"),
            regime=Regime(r.regime or "RANGE"),
        )
        for r in rows
        if r.closed_at
    ]
    curve = repos.risk.equity_curve(start, end)
    metrics = compute(trades, equity_curve=[e for _, e in curve] or None, period_days=days)
    return {
        "metrics": metrics.as_dict(),
        "equity_curve": [{"ts": ts, "equity": eq} for ts, eq in curve],
        "trades": [
            {
                "opened_at": t.opened_at,
                "closed_at": t.closed_at,
                "direction": str(t.direction),
                "strategy": t.strategy,
                "classification": str(t.classification),
                "entry": t.entry,
                "exit": t.exit_price,
                "r": round(t.r_multiple, 4),
                "pnl": round(t.net_pnl, 2),
                "reason": str(t.exit_reason),
                "session": str(t.session),
            }
            for t in trades[-200:]
        ],
    }


@app.get("/api/strategies")
async def strategies(repos=Depends(get_repos)) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    return [
        {
            "strategy": r.strategy,
            "version": r.strategy_version,
            "status": r.status,
            "max_class": r.max_class,
            "approved_regimes": r.approved_regimes,
            "approved_sessions": r.approved_sessions,
            "updated_at": r.updated_at,
        }
        for r in repos.strategy_status.all()
    ]


@app.get("/api/kill-switch")
async def kill_switch_history(repos=Depends(get_repos)) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    return [
        {
            "ts": r.ts,
            "action": r.action,
            "reason": r.reason_code,
            "detail": r.detail,
            "cleared_by": r.cleared_by,
            "auto_clearable": r.auto_clearable,
        }
        for r in repos.risk.kill_switch_history()
    ]


# --------------------------------------------------------------------------------------
# The two audited write paths
# --------------------------------------------------------------------------------------


def _queue(command: str, req: CommandRequest) -> dict[str, Any]:
    """Persist the request, so it survives a restart of either process and leaves a
    record of who asked. The API never touches the broker."""
    db = _database()
    with db.session() as s:
        command_id = Repositories(s).commands.queue(command, req.reason, req.operator)
    log.warning(
        "operator_command_queued",
        command=command,
        command_id=command_id,
        reason=req.reason,
        operator=req.operator,
    )
    return {
        "queued": True,
        "id": command_id,
        "command": command,
        "queued_at": datetime.now(UTC).isoformat(),
    }


@app.post("/api/commands/halt")
async def halt(req: CommandRequest) -> dict[str, Any]:
    """Trip the kill switch. Picked up by the engine on its next poll."""
    cmd = _queue("HALT", req)
    await hub.broadcast("command", cmd)
    return cmd


@app.post("/api/commands/flatten")
async def flatten(req: CommandRequest) -> dict[str, Any]:
    """Close all positions. Picked up by the engine on its next poll."""
    cmd = _queue("FLATTEN", req)
    await hub.broadcast("command", cmd)
    return cmd


@app.get("/api/commands")
async def command_history(repos=Depends(get_repos)) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    """The audit trail: what was asked, by whom, and what the engine did about it."""
    return [
        {
            "id": r.id,
            "command": r.command,
            "reason": r.reason,
            "operator": r.operator,
            "status": r.status,
            "queued_at": r.queued_at,
            "completed_at": r.completed_at,
            "result": r.result,
        }
        for r in repos.commands.recent(50)
    ]


# --------------------------------------------------------------------------------------
# Streaming and static
# --------------------------------------------------------------------------------------


@app.websocket("/ws")
async def websocket(ws: WebSocket, token: str | None = Query(default=None)) -> None:
    # The HTTP middleware does not see WebSocket scopes, and a browser cannot set a
    # header on a WebSocket handshake, so the token comes as a query parameter.
    expected = get_settings().dashboard.auth_token
    if expected and (not token or not secrets.compare_digest(token, expected)):
        await ws.close(code=1008)  # policy violation
        return
    await hub.connect(ws)
    try:
        while True:
            await ws.receive_text()  # keepalive; the client sends pings
    except WebSocketDisconnect:
        hub.disconnect(ws)
    except Exception:
        hub.disconnect(ws)


@app.post("/api/publish")
async def publish(payload: dict[str, Any]) -> dict[str, bool]:
    """Engine -> dashboard state push. Bound to localhost in deployment."""
    await hub.broadcast(payload.get("type", "state"), payload.get("data", {}))
    return {"ok": True}


if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")


@app.get("/", response_class=HTMLResponse)
async def index() -> Any:
    path = STATIC_DIR / "index.html"
    if path.exists():
        return FileResponse(path)
    return HTMLResponse(
        "<h1>XAUUSD Terminal</h1><p>Dashboard assets not found. "
        "Run <code>python -m xauusd.cli dashboard</code> from the repo root.</p>"
    )


def run(host: str | None = None, port: int | None = None, reload: bool = False) -> None:
    import uvicorn

    cfg = get_settings().dashboard
    host = host or cfg.host
    port = port or cfg.port

    # DashboardConfig enforces this for configured values; re-check here because a
    # --host flag reaches uvicorn without passing through the config at all.
    loopback = host in {"127.0.0.1", "localhost", "::1"}
    if not loopback and not cfg.auth_token:
        raise SystemExit(
            f"refusing to bind the dashboard to {host}: it can halt the engine and "
            "flatten positions, and no auth_token is set.\n"
            "Set XAUUSD_DASHBOARD__AUTH_TOKEN, or keep the default 127.0.0.1 bind and "
            "reach it over WireGuard or an SSH tunnel."
        )
    if cfg.auth_token:
        log.info("dashboard_auth_enabled", host=host, port=port)
    else:
        log.warning("dashboard_auth_disabled", host=host, port=port, detail="loopback only")

    uvicorn.run("xauusd.dashboard.api:app", host=host, port=port, reload=reload, log_level="info")
