"""Purged, embargoed cross-validation.

Trade outcomes OVERLAP in time: a setup at 10:00 may not resolve until 14:00, so a
training sample and a test sample that overlap share information. Naive k-fold
therefore leaks, and the leak is generous — it routinely turns a worthless model into
one with an impressive AUC.

Two corrections, both required (López de Prado):

  PURGING  — drop training samples whose outcome window overlaps the test window.
  EMBARGO  — additionally drop training samples immediately AFTER the test window,
             because serial correlation makes them near-duplicates of test samples.

Implemented here rather than taken as a dependency: it is about 80 lines and the
alternative pulls in a large, loosely maintained package for one algorithm.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np


@dataclass(slots=True)
class PurgedKFold:
    n_splits: int = 5
    embargo_pct: float = 0.01

    def split(self, t0: np.ndarray, t1: np.ndarray) -> Iterator[tuple[np.ndarray, np.ndarray]]:
        """Yield (train_idx, test_idx). Samples must be sorted by t0."""
        n = len(t0)
        if n < self.n_splits * 2:
            raise ValueError(f"need at least {self.n_splits * 2} samples, got {n}")
        order = np.argsort(t0)
        if not np.array_equal(order, np.arange(n)):
            raise ValueError("samples must be sorted by event time t0")

        embargo = int(n * self.embargo_pct)
        bounds = np.linspace(0, n, self.n_splits + 1).astype(int)

        for k in range(self.n_splits):
            test_start, test_end = bounds[k], bounds[k + 1]
            test_idx = np.arange(test_start, test_end)
            if test_idx.size == 0:
                continue

            test_t0 = t0[test_start]
            test_t1 = t1[test_start:test_end].max()

            train_mask = np.ones(n, dtype=bool)
            train_mask[test_start:test_end] = False

            # PURGE: any training sample whose outcome window overlaps the test window.
            overlaps = (t1 >= test_t0) & (t0 <= test_t1)
            train_mask &= ~overlaps

            # EMBARGO: samples immediately after the test window.
            if embargo > 0:
                lo = test_end
                hi = min(n, test_end + embargo)
                train_mask[lo:hi] = False

            train_idx = np.flatnonzero(train_mask)
            if train_idx.size and test_idx.size:
                yield train_idx, test_idx


def leakage_report(
    t0: np.ndarray, t1: np.ndarray, n_splits: int = 5, embargo_pct: float = 0.01
) -> dict[str, float]:
    """Quantify how much purging removes — useful evidence for a validation report."""
    cv = PurgedKFold(n_splits, embargo_pct)
    naive_train = 0
    purged_train = 0
    n = len(t0)
    for train_idx, test_idx in cv.split(t0, t1):
        purged_train += len(train_idx)
        naive_train += n - len(test_idx)
    return {
        "naive_train_samples": naive_train,
        "purged_train_samples": purged_train,
        "removed_fraction": ((naive_train - purged_train) / naive_train if naive_train else 0.0),
    }
