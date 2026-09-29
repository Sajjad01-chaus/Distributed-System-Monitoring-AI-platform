"""Fleet-level outlier model: which hosts behave unlike the rest of the fleet.

The Phase 1 engine refit an IsolationForest on every message (~133 ms each) with
contamination=0.1, i.e. it declared 10% of all samples anomalous by construction. Here:
- the model is refit *periodically* (default every 5 min) on a bounded reservoir sample, so
  the per-message cost is a vectorised score of the whole batch;
- the cut-off is an extreme tail of the training scores (0.5%), and the per-agent detector
  additionally requires the flag to persist (see detectors.fleet_outlier);
- until enough data has been seen, nothing is flagged.
Each detect worker trains on the traffic it sees; that is a representative sample because
the consumer group spreads messages evenly over workers.
"""
from __future__ import annotations

import logging
import math
import random
import time
from typing import List, Optional, Sequence

import numpy as np
from sklearn.ensemble import IsolationForest

log = logging.getLogger(__name__)

FEATURES = ("cpu", "mem", "disk", "lat", "loss")


def features(sample: dict) -> List[float]:
    # Latency is heavy-tailed; log keeps a few slow links from dominating the geometry.
    return [sample.get("cpu") or 0.0, sample.get("mem") or 0.0, sample.get("disk") or 0.0,
            math.log1p(max(sample.get("lat") or 0.0, 0.0)), sample.get("loss") or 0.0]


class FleetOutlierModel:
    def __init__(self, reservoir_size: int = 5000, min_train: int = 500, refit_every_s: float = 300,
                 tail_quantile: float = 0.005, seed: int = 42):
        self.reservoir: List[List[float]] = []
        self.reservoir_size = reservoir_size
        self.min_train = min_train
        self.refit_every_s = refit_every_s
        self.tail_quantile = tail_quantile
        self.seen = 0
        self.rng = random.Random(seed)
        self.seed = seed
        self.model: Optional[IsolationForest] = None
        self.cutoff = -math.inf
        self.fitted_at = 0.0

    def observe(self, rows: Sequence[List[float]]) -> None:
        """Reservoir sampling: a uniform sample of everything seen, in bounded memory."""
        for row in rows:
            self.seen += 1
            if len(self.reservoir) < self.reservoir_size:
                self.reservoir.append(row)
            else:
                j = self.rng.randrange(self.seen)
                if j < self.reservoir_size:
                    self.reservoir[j] = row

    def maybe_refit(self, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        if len(self.reservoir) < self.min_train:
            return False
        if self.model is not None and now - self.fitted_at < self.refit_every_s:
            return False
        X = np.asarray(self.reservoir)
        model = IsolationForest(n_estimators=100, contamination="auto", random_state=self.seed).fit(X)
        self.cutoff = float(np.quantile(model.score_samples(X), self.tail_quantile))
        self.model, self.fitted_at = model, now
        log.info("fleet outlier model refit on %d samples (cut-off %.3f)", len(X), self.cutoff)
        return True

    def flags(self, rows: Sequence[List[float]]) -> List[int]:
        """1 for rows in the extreme low-score tail, else 0. All zeros before the first fit."""
        if self.model is None or not rows:
            return [0] * len(rows)
        scores = self.model.score_samples(np.asarray(rows))
        return [int(s < self.cutoff) for s in scores]
