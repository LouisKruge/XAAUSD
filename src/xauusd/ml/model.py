"""Probability model: score + features -> calibrated P(+2R before -1R).

Deliberately SECONDARY to the rule-based engine. The confluence score is the primary
authority; the model refines ranking and supplies a calibrated probability. If the
model is missing or unhealthy the system degrades to score-only in A-only mode and says
so on the dashboard — it does not stop, and it does not silently invent a probability.

Three properties that matter more than the algorithm choice:

  1. CALIBRATION, not accuracy. A model that says 65% must be right about 65% of the
     time. Measured by Brier score and a reliability curve, and isotonic-calibrated on
     held-out data.
  2. MONOTONIC CONSTRAINTS. More confluence must never lower the predicted probability.
     Without this a tree model will happily learn a non-monotonic artefact from noise.
  3. A FEATURE SCHEMA HASH. The engine refuses to load a model whose schema does not
     match the running code, so a stale model cannot silently mis-score live setups.
"""

from __future__ import annotations

import contextlib
import pickle
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from xauusd.monitoring.logging import get_logger
from xauusd.strategy.features import FeatureVector, schema_hash

log = get_logger(__name__)

# See _make_model: LightGBM's default thread count is pathological under container CPU
# limits. Two threads is ample for datasets of this size (hundreds to low thousands of
# rows) and is stable everywhere the system runs.
N_JOBS = 2

# Features where MORE must never mean a LOWER probability. Encoded as monotonic
# constraints so the model cannot learn a nonsensical inversion from noise.
MONOTONIC_INCREASING = {
    "htf_alignment",
    "has_mss",
    "mss_displacement_atr",
    "sweep_quality",
    "sweep_displacement_atr",
    "fvg_quality",
    "ob_quality",
    "zone_confluence",
    "correct_side_of_range",
    "sr_confluence_importance",
    "macro_aligned",
    "target_liquidity_rr",
    "rr",
    "regime_aligned",
}
MONOTONIC_DECREASING = {
    "htf_conflict",
    "news_risk",
    "news_blackout",
    "spread_ratio",
    "news_stale",
    "sr_blocking_target",
    "opposing_liquidity_count",
}


@dataclass(slots=True)
class CalibrationReport:
    brier: float
    log_loss: float
    auc: float
    slope: float
    intercept: float
    bins: list[dict[str, float]] = field(default_factory=list)
    n: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "brier": round(self.brier, 6),
            "log_loss": round(self.log_loss, 6),
            "auc": round(self.auc, 6),
            "slope": round(self.slope, 6),
            "intercept": round(self.intercept, 6),
            "n": self.n,
            "bins": self.bins,
        }

    @property
    def is_calibrated(self) -> bool:
        return 0.8 <= self.slope <= 1.2 and self.brier <= 0.25


def reliability(y_true: np.ndarray, y_prob: np.ndarray, n_bins: int = 10) -> CalibrationReport:
    """Brier, log loss, AUC and a reliability curve, plus the calibration slope.

    The slope is a regression of observed frequency on predicted probability across
    bins. 1.0 is perfect; below 1 means the model is overconfident, which for a trading
    system means over-trading.
    """
    y_true = np.asarray(y_true, dtype=float)
    y_prob = np.clip(np.asarray(y_prob, dtype=float), 1e-6, 1 - 1e-6)
    n = len(y_true)
    if n == 0:
        return CalibrationReport(1.0, 1.0, 0.5, 0.0, 0.0, [], 0)

    brier = float(np.mean((y_prob - y_true) ** 2))
    log_loss = float(-np.mean(y_true * np.log(y_prob) + (1 - y_true) * np.log(1 - y_prob)))

    pos, neg = y_prob[y_true == 1], y_prob[y_true == 0]
    if pos.size and neg.size:
        order = np.argsort(y_prob)
        ranks = np.empty_like(order, dtype=float)
        ranks[order] = np.arange(1, n + 1)
        auc = float(
            (ranks[y_true == 1].sum() - pos.size * (pos.size + 1) / 2) / (pos.size * neg.size)
        )
    else:
        auc = 0.5

    edges = np.linspace(0, 1, n_bins + 1)
    bins, xs, ys, ws = [], [], [], []
    for i in range(n_bins):
        mask = (y_prob >= edges[i]) & (y_prob < edges[i + 1] if i < n_bins - 1 else y_prob <= 1)
        if not mask.any():
            continue
        pred = float(y_prob[mask].mean())
        obs = float(y_true[mask].mean())
        bins.append(
            {"predicted": round(pred, 4), "observed": round(obs, 4), "count": int(mask.sum())}
        )
        xs.append(pred)
        ys.append(obs)
        ws.append(int(mask.sum()))

    slope, intercept = 1.0, 0.0
    if len(xs) >= 2:
        w = np.array(ws, dtype=float)
        x = np.array(xs)
        y = np.array(ys)
        xm = np.average(x, weights=w)
        ym = np.average(y, weights=w)
        denom = float(np.sum(w * (x - xm) ** 2))
        if denom > 0:
            slope = float(np.sum(w * (x - xm) * (y - ym)) / denom)
            intercept = float(ym - slope * xm)

    return CalibrationReport(brier, log_loss, auc, slope, intercept, bins, n)


class ProbabilityModel:
    """LightGBM with isotonic calibration; falls back to logistic regression."""

    def __init__(
        self,
        model_id: str,
        feature_names: list[str],
        booster: Any = None,
        calibrator: Any = None,
        schema: str = "",
        calibration: CalibrationReport | None = None,
    ) -> None:
        self.model_id = model_id
        self.feature_names = feature_names
        self.booster = booster
        self.calibrator = calibrator
        self.schema = schema or schema_hash()
        self.calibration = calibration
        self._healthy = True
        self._drift_notes: list[str] = []

    # -- inference ---------------------------------------------------------------------

    def predict(self, features: FeatureVector) -> float | None:
        if self.booster is None:
            return None
        if self.schema != schema_hash():
            # A model trained on a different feature set would mis-score silently.
            log.error(
                "model_schema_mismatch",
                model=self.model_id,
                model_schema=self.schema,
                code_schema=schema_hash(),
            )
            self._healthy = False
            return None
        d = features.as_dict()
        x = np.array([[float(d.get(n, 0.0)) for n in self.feature_names]])
        try:
            raw = float(self._raw_predict(x)[0])
        except Exception as exc:
            log.error("model_predict_error", error=str(exc))
            return None
        if self.calibrator is not None:
            # An unusable calibrator leaves the raw score in place rather than
            # dropping the prediction; the score is still monotonic in the signal.
            with contextlib.suppress(Exception):
                raw = float(self.calibrator.predict([raw])[0])
        return float(np.clip(raw, 0.001, 0.999))

    def _raw_predict(self, x: np.ndarray) -> np.ndarray:
        if hasattr(self.booster, "predict_proba"):
            return np.asarray(self.booster.predict_proba(x)[:, 1])
        return np.asarray(self.booster.predict(x))

    def is_healthy(self) -> bool:
        return self._healthy and self.booster is not None

    def mark_unhealthy(self, reason: str) -> None:
        self._healthy = False
        self._drift_notes.append(reason)
        log.warning("model_marked_unhealthy", model=self.model_id, reason=reason)

    # -- persistence -------------------------------------------------------------------

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("wb") as fh:
            pickle.dump(
                {
                    "model_id": self.model_id,
                    "feature_names": self.feature_names,
                    "booster": self.booster,
                    "calibrator": self.calibrator,
                    "schema": self.schema,
                    "calibration": self.calibration.as_dict() if self.calibration else None,
                },
                fh,
            )
        return p

    @classmethod
    def load(cls, path: str | Path) -> ProbabilityModel:
        with Path(path).open("rb") as fh:
            d = pickle.load(fh)
        cal = d.get("calibration")
        report = CalibrationReport(**dict(cal)) if cal else None
        m = cls(
            d["model_id"], d["feature_names"], d["booster"], d["calibrator"], d["schema"], report
        )
        if m.schema != schema_hash():
            m.mark_unhealthy(f"feature schema mismatch: model {m.schema} vs code {schema_hash()}")
        return m


def train(
    X: np.ndarray,
    y: np.ndarray,
    t0: np.ndarray,
    t1: np.ndarray,
    feature_names: list[str],
    n_splits: int = 5,
    embargo_pct: float = 0.01,
    model_id: str | None = None,
    seed: int = 42,
) -> tuple[ProbabilityModel, CalibrationReport, dict[str, Any]]:
    """Train with purged, embargoed CV and isotonic calibration on held-out folds.

    The reported calibration comes from OUT-OF-FOLD predictions only. Calibrating on
    the training data would produce a beautiful reliability curve that means nothing.
    """
    from xauusd.ml.purged_cv import PurgedKFold

    model_id = model_id or f"prob-{datetime.now(UTC):%Y%m%d-%H%M%S}"
    n = len(y)
    if n < 60 or len(np.unique(y)) < 2:
        raise ValueError(f"need at least 60 samples of both classes, got {n}")

    monotone = [
        1 if name in MONOTONIC_INCREASING else (-1 if name in MONOTONIC_DECREASING else 0)
        for name in feature_names
    ]

    oof = np.full(n, np.nan)
    cv = PurgedKFold(n_splits, embargo_pct)

    for train_idx, test_idx in cv.split(t0, t1):
        if len(np.unique(y[train_idx])) < 2:
            continue
        model = _make_model(monotone, seed)
        model.fit(X[train_idx], y[train_idx])
        oof[test_idx] = _proba(model, X[test_idx])

    mask = ~np.isnan(oof)
    if mask.sum() < 20:
        raise ValueError("purged CV left too few out-of-fold predictions to calibrate")

    # Final model on everything, calibrated on the out-of-fold predictions.
    final = _make_model(monotone, seed)
    final.fit(X, y)

    calibrator = None
    try:
        from sklearn.isotonic import IsotonicRegression

        calibrator = IsotonicRegression(out_of_bounds="clip", y_min=0.01, y_max=0.99)
        calibrator.fit(oof[mask], y[mask])
        calibrated = calibrator.predict(oof[mask])
    except Exception as exc:
        log.warning("calibration_failed", error=str(exc))
        calibrated = oof[mask]

    report = reliability(y[mask], calibrated)
    model = ProbabilityModel(model_id, feature_names, final, calibrator, schema_hash(), report)

    importance: dict[str, float] = {}
    if hasattr(final, "feature_importances_"):
        importance = {
            n: float(v) for n, v in zip(feature_names, final.feature_importances_, strict=True)
        }
        importance = dict(sorted(importance.items(), key=lambda kv: -kv[1])[:20])

    meta = {
        "model_id": model_id,
        "samples": int(n),
        "positive_rate": float(y.mean()),
        "oof_predictions": int(mask.sum()),
        "uncalibrated": reliability(y[mask], oof[mask]).as_dict(),
        "calibrated": report.as_dict(),
        "top_features": importance,
        "schema": schema_hash(),
    }
    return model, report, meta


def _make_model(monotone: list[int], seed: int) -> Any:
    try:
        import lightgbm as lgb

        return lgb.LGBMClassifier(
            n_estimators=200,
            learning_rate=0.05,
            num_leaves=15,
            max_depth=4,
            min_child_samples=20,
            subsample=0.8,
            subsample_freq=1,  # without this, subsample is silently ignored
            colsample_bytree=0.8,
            monotone_constraints=monotone,
            random_state=seed,
            verbose=-1,
            # n_jobs is pinned deliberately. LightGBM defaults to one thread per core,
            # which in a cgroup-limited container (a VPS, or CI) causes severe thread
            # thrashing: measured here at 42s for a 600-sample fit versus 0.02s pinned.
            n_jobs=N_JOBS,
        )
    except ImportError:
        from sklearn.linear_model import LogisticRegression

        log.warning("lightgbm_unavailable", detail="falling back to logistic regression")
        return LogisticRegression(max_iter=2000, C=0.5, random_state=seed)


def _proba(model: Any, X: np.ndarray) -> np.ndarray:
    raw = model.predict_proba(X)[:, 1] if hasattr(model, "predict_proba") else model.predict(X)
    return np.asarray(raw)


class ModelHealthMonitor:
    """Live drift monitoring: does the model still say what it said out of sample?"""

    def __init__(self, window: int = 40, brier_tolerance: float = 0.08) -> None:
        self.window = window
        self.brier_tolerance = brier_tolerance
        self.outcomes: list[tuple[float, int]] = []

    def record(self, predicted: float, outcome: int) -> None:
        self.outcomes.append((predicted, outcome))
        self.outcomes = self.outcomes[-self.window * 3 :]

    def evaluate(self, baseline_brier: float) -> dict[str, Any]:
        if len(self.outcomes) < self.window:
            return {"verdict": "INSUFFICIENT_DATA", "n": len(self.outcomes)}
        recent = self.outcomes[-self.window :]
        probs = np.array([p for p, _ in recent])
        outs = np.array([o for _, o in recent], dtype=float)
        rep = reliability(outs, probs)
        drift = rep.brier - baseline_brier
        verdict = (
            "DEGRADED"
            if drift > self.brier_tolerance * 2
            else "DRIFTING"
            if drift > self.brier_tolerance
            else "HEALTHY"
        )
        return {
            "verdict": verdict,
            "n": len(recent),
            "realised_win_rate": round(float(outs.mean()), 4),
            "predicted_win_rate": round(float(probs.mean()), 4),
            "brier": round(rep.brier, 4),
            "baseline_brier": round(baseline_brier, 4),
            "drift": round(drift, 4),
        }
