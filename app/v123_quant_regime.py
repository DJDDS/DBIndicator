"""V12.3 research-only quantitative underlying regime model.

This module is intentionally isolated from the production Focus Desk.  It does
not emit trading actions and it does not contain a timer that forces a stock to
remain actionable.  The objective is to infer whether the underlying is in a
persistent directional regime, with duration emerging from the fitted state
process itself.

The intended research pipeline is:

    live samples
      -> causal latent-trend / factor-residual features
      -> three-state Gaussian HMM fitted only on PRIOR completed sessions
      -> causal posterior filter (DOWN / FLAT / UP)
      -> semantic lifecycle (BUILDING / ACTIONABLE / DECAYING / CLOSED)

The HMM transition matrix is learned from historical observations.  Therefore
state persistence is data-derived rather than imposed as a 15/30 minute rule.

This is a research instrument only.  No option features, execution outcomes,
post-entry path features, or future labels are accepted by the model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Iterable, Sequence

import numpy as np

EPS = 1e-12
STATE_DOWN = 0
STATE_FLAT = 1
STATE_UP = 2
STATE_NAMES = ("DOWN", "FLAT", "UP")


def _as_float(value, default=0.0):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _logsumexp(values, axis=None):
    arr = np.asarray(values, dtype=float)
    m = np.max(arr, axis=axis, keepdims=True)
    out = m + np.log(np.maximum(np.sum(np.exp(arr - m), axis=axis, keepdims=True), EPS))
    if axis is None:
        return float(out.ravel()[0])
    return np.squeeze(out, axis=axis)


@dataclass
class RunningScale:
    """Causal exponentially weighted location/scale estimator."""

    mean: float = 0.0
    var: float = 1.0
    initialized: bool = False
    alpha: float = 0.03

    def update(self, value: float) -> float:
        x = _as_float(value)
        if not self.initialized:
            self.mean = x
            self.var = 1.0
            self.initialized = True
            return 0.0
        prior_mean = self.mean
        self.mean = (1.0 - self.alpha) * self.mean + self.alpha * x
        innovation = x - prior_mean
        self.var = max(
            EPS,
            (1.0 - self.alpha) * self.var + self.alpha * innovation * innovation,
        )
        return (x - self.mean) / math.sqrt(self.var)


@dataclass
class LocalLinearTrend:
    """Causal local-linear state-space filter on log price.

    State is [latent level, latent drift].  Observation noise adapts from the
    innovation process, so the drift is automatically scaled to each stock's
    realised noise level.
    """

    level: float | None = None
    drift: float = 0.0
    covariance: np.ndarray = field(default_factory=lambda: np.eye(2, dtype=float))
    obs_var: float = 1e-6
    innovation_alpha: float = 0.03

    def update(self, price: float) -> dict:
        p = _as_float(price, default=float("nan"))
        if not math.isfinite(p) or p <= 0:
            raise ValueError("price must be positive and finite")
        y = math.log(p)
        if self.level is None:
            self.level = y
            self.covariance = np.eye(2, dtype=float) * 1e-3
            return {
                "level": y,
                "drift": 0.0,
                "drift_signal": 0.0,
                "innovation": 0.0,
            }

        # Constant-velocity local-linear trend.
        F = np.array([[1.0, 1.0], [0.0, 1.0]], dtype=float)
        H = np.array([[1.0, 0.0]], dtype=float)
        x = np.array([self.level, self.drift], dtype=float)

        # Process uncertainty is tied to the empirically observed noise, not
        # price level.  This keeps the filter dimensionless across symbols.
        q = max(self.obs_var, 1e-10)
        Q = np.array([[q * 0.05, 0.0], [0.0, q * 0.005]], dtype=float)

        x_pred = F @ x
        P_pred = F @ self.covariance @ F.T + Q
        innovation = y - float((H @ x_pred).item())
        S = float((H @ P_pred @ H.T).item()) + max(self.obs_var, 1e-10)
        K = (P_pred @ H.T / S).reshape(2)
        x_new = x_pred + K * innovation
        P_new = (np.eye(2) - np.outer(K, H.reshape(2))) @ P_pred

        self.obs_var = max(
            1e-10,
            (1.0 - self.innovation_alpha) * self.obs_var
            + self.innovation_alpha * innovation * innovation,
        )
        self.level = float(x_new[0])
        self.drift = float(x_new[1])
        self.covariance = P_new
        drift_sd = math.sqrt(max(float(P_new[1, 1]), EPS))
        return {
            "level": self.level,
            "drift": self.drift,
            "drift_signal": self.drift / drift_sd,
            "innovation": innovation / math.sqrt(max(S, EPS)),
        }


@dataclass
class RecursiveFactorResidual:
    """Causal recursive least-squares market/sector residual model."""

    beta: np.ndarray = field(default_factory=lambda: np.zeros(3, dtype=float))
    covariance: np.ndarray = field(default_factory=lambda: np.eye(3, dtype=float) * 100.0)
    forgetting: float = 0.995
    residual_scale: RunningScale = field(default_factory=lambda: RunningScale(alpha=0.03))

    def update(
        self,
        stock_return: float,
        market_return: float | None,
        sector_return: float | None,
    ) -> dict:
        y = _as_float(stock_return)
        x = np.array(
            [
                1.0,
                _as_float(market_return),
                _as_float(sector_return),
            ],
            dtype=float,
        )
        Px = self.covariance @ x
        denom = self.forgetting + float(x.T @ Px)
        gain = Px / max(denom, EPS)
        prediction = float(x.T @ self.beta)
        residual = y - prediction
        self.beta = self.beta + gain * residual
        self.covariance = (
            self.covariance - np.outer(gain, x) @ self.covariance
        ) / self.forgetting
        residual_signal = self.residual_scale.update(residual)
        return {
            "prediction": prediction,
            "residual": residual,
            "residual_signal": residual_signal,
            "beta_market": float(self.beta[1]),
            "beta_sector": float(self.beta[2]),
        }


@dataclass
class QuantFeatureState:
    """Build the causal, underlying-only observation vector."""

    trend: LocalLinearTrend = field(default_factory=LocalLinearTrend)
    factor: RecursiveFactorResidual = field(default_factory=RecursiveFactorResidual)
    participation_scale: RunningScale = field(default_factory=lambda: RunningScale(alpha=0.03))
    last_price: float | None = None
    last_drift: float = 0.0
    drift_change_scale: RunningScale = field(default_factory=lambda: RunningScale(alpha=0.03))

    def update(
        self,
        *,
        price: float,
        market_return: float | None = None,
        sector_return: float | None = None,
        participation: float | None = None,
    ) -> np.ndarray:
        p = _as_float(price, default=float("nan"))
        if not math.isfinite(p) or p <= 0:
            raise ValueError("price must be positive and finite")

        if self.last_price is None:
            stock_ret = 0.0
        else:
            stock_ret = math.log(p / self.last_price)
        self.last_price = p

        trend_now = self.trend.update(p)
        drift = float(trend_now["drift"])
        drift_change = drift - self.last_drift
        self.last_drift = drift

        factor_now = self.factor.update(stock_ret, market_return, sector_return)
        acceleration_signal = self.drift_change_scale.update(drift_change)

        if participation is None:
            participation_signal = 0.0
        else:
            # Participation may be a volume-rate ratio or another positive
            # contemporaneous activity measure.  log1p makes it scale-stable.
            part = max(0.0, _as_float(participation))
            participation_signal = self.participation_scale.update(math.log1p(part))

        # No composite score: these remain separate coordinates in the
        # probabilistic state model.
        return np.array(
            [
                float(trend_now["drift_signal"]),
                float(factor_now["residual_signal"]),
                float(acceleration_signal),
                float(participation_signal),
            ],
            dtype=float,
        )


@dataclass
class GaussianRegimeHMM:
    """Three-state diagonal-Gaussian HMM learned from prior-session features.

    States are relabelled after fitting so that the state with the most negative
    joint drift/residual centre is DOWN, the middle state is FLAT, and the most
    positive is UP.  Runtime classification uses causal filtering only.
    """

    means: np.ndarray | None = None
    variances: np.ndarray | None = None
    transition: np.ndarray | None = None
    initial: np.ndarray | None = None
    max_iter: int = 80
    tol: float = 1e-5

    def _check_matrix(self, x: Sequence[Sequence[float]]) -> np.ndarray:
        arr = np.asarray(x, dtype=float)
        if arr.ndim != 2 or arr.shape[0] < 12:
            raise ValueError("need at least 12 observations in a 2-D feature matrix")
        if not np.all(np.isfinite(arr)):
            raise ValueError("feature matrix contains non-finite values")
        return arr

    @staticmethod
    def _log_emission(x, means, variances):
        diff = x[:, None, :] - means[None, :, :]
        return -0.5 * np.sum(
            np.log(2.0 * math.pi * variances[None, :, :])
            + diff * diff / variances[None, :, :],
            axis=2,
        )

    def fit(self, x: Sequence[Sequence[float]]) -> "GaussianRegimeHMM":
        arr = self._check_matrix(x)
        n, d = arr.shape

        # Deterministic unsupervised initialisation from the two directional
        # coordinates only.  This is not a runtime score or threshold.
        axis = arr[:, 0] + arr[:, 1]
        order = np.argsort(axis)
        groups = np.array_split(order, 3)
        means = np.vstack([np.mean(arr[g], axis=0) for g in groups])
        global_var = np.maximum(np.var(arr, axis=0), 1e-3)
        variances = np.vstack([
            np.maximum(np.var(arr[g], axis=0), global_var * 0.10)
            if len(g) > 1 else global_var.copy()
            for g in groups
        ])
        transition = np.full((3, 3), 1.0 / 3.0, dtype=float)
        initial = np.full(3, 1.0 / 3.0, dtype=float)

        last_ll = None
        for _ in range(self.max_iter):
            log_b = self._log_emission(arr, means, variances)
            log_a = np.log(np.maximum(transition, EPS))
            log_pi = np.log(np.maximum(initial, EPS))

            alpha = np.zeros((n, 3), dtype=float)
            alpha[0] = log_pi + log_b[0]
            for t in range(1, n):
                alpha[t] = log_b[t] + np.array([
                    _logsumexp(alpha[t - 1] + log_a[:, j]) for j in range(3)
                ])
            ll = _logsumexp(alpha[-1])

            beta = np.zeros((n, 3), dtype=float)
            for t in range(n - 2, -1, -1):
                beta[t] = np.array([
                    _logsumexp(log_a[i] + log_b[t + 1] + beta[t + 1])
                    for i in range(3)
                ])

            log_gamma = alpha + beta - ll
            gamma = np.exp(log_gamma)
            gamma /= np.maximum(np.sum(gamma, axis=1, keepdims=True), EPS)

            xi_sum = np.zeros((3, 3), dtype=float)
            for t in range(n - 1):
                lx = (
                    alpha[t][:, None]
                    + log_a
                    + log_b[t + 1][None, :]
                    + beta[t + 1][None, :]
                    - ll
                )
                xit = np.exp(lx)
                xit /= max(float(np.sum(xit)), EPS)
                xi_sum += xit

            weights = np.maximum(np.sum(gamma, axis=0), EPS)
            means = (gamma.T @ arr) / weights[:, None]
            for k in range(3):
                diff = arr - means[k]
                variances[k] = np.maximum(
                    np.sum(gamma[:, k][:, None] * diff * diff, axis=0) / weights[k],
                    1e-4,
                )
            transition = xi_sum + 1e-6
            transition /= np.maximum(np.sum(transition, axis=1, keepdims=True), EPS)
            initial = np.maximum(gamma[0], EPS)
            initial /= np.sum(initial)

            if last_ll is not None and abs(ll - last_ll) <= self.tol * (1.0 + abs(last_ll)):
                break
            last_ll = ll

        # Relabel states by learned directional centre: negative, flat, positive.
        directional_centres = means[:, 0] + means[:, 1]
        relabel = np.argsort(directional_centres)
        means = means[relabel]
        variances = variances[relabel]
        transition = transition[np.ix_(relabel, relabel)]
        initial = initial[relabel]
        initial /= np.sum(initial)

        self.means = means
        self.variances = variances
        self.transition = transition
        self.initial = initial
        return self

    def _require_fit(self):
        if any(x is None for x in (self.means, self.variances, self.transition, self.initial)):
            raise RuntimeError("regime model is not fitted")

    def filter(self, x: Sequence[Sequence[float]]) -> list[dict]:
        """Causal posterior filter; does not use future observations."""
        self._require_fit()
        arr = np.asarray(x, dtype=float)
        if arr.ndim == 1:
            arr = arr.reshape(1, -1)
        if arr.ndim != 2 or arr.shape[1] != self.means.shape[1]:
            raise ValueError("feature dimension mismatch")

        out = []
        posterior = np.asarray(self.initial, dtype=float)
        for row in arr:
            log_b = self._log_emission(row.reshape(1, -1), self.means, self.variances)[0]
            prior = posterior @ self.transition
            log_post = np.log(np.maximum(prior, EPS)) + log_b
            log_post -= _logsumexp(log_post)
            posterior = np.exp(log_post)
            state = int(np.argmax(posterior))
            out.append({
                "latent_state": STATE_NAMES[state],
                "posterior": {
                    "DOWN": float(posterior[STATE_DOWN]),
                    "FLAT": float(posterior[STATE_FLAT]),
                    "UP": float(posterior[STATE_UP]),
                },
            })
        return out

    def expected_dwell_observations(self) -> dict:
        """Implied geometric dwell from the learned transition matrix."""
        self._require_fit()
        out = {}
        for i, name in enumerate(STATE_NAMES):
            stay = min(max(float(self.transition[i, i]), 0.0), 1.0 - 1e-9)
            out[name] = 1.0 / max(1.0 - stay, EPS)
        return out


def lifecycle_step(previous: str | None, latent_state: str) -> str:
    """Map causal latent-state changes into a human-readable thesis lifecycle.

    No clock or minimum/maximum duration is used.  If the HMM remains in UP all
    session, ACTIONABLE_UP can remain active all session.  A direct opposing
    latent state is treated as a structural reversal; a FLAT transition first
    enters DECAYING so that loss of drift is distinguished from reversal.
    """

    latent = str(latent_state or "").upper()
    prev = str(previous or "CLOSED").upper()
    if latent not in STATE_NAMES:
        raise ValueError(f"unknown latent state: {latent_state!r}")

    bullish = {"BUILDING_UP", "ACTIONABLE_UP", "DECAYING_UP"}
    bearish = {"BUILDING_DOWN", "ACTIONABLE_DOWN", "DECAYING_DOWN"}

    if latent == "UP":
        if prev in bullish:
            return "ACTIONABLE_UP"
        if prev in bearish:
            return "BUILDING_UP"
        return "BUILDING_UP"

    if latent == "DOWN":
        if prev in bearish:
            return "ACTIONABLE_DOWN"
        if prev in bullish:
            return "BUILDING_DOWN"
        return "BUILDING_DOWN"

    # FLAT
    if prev in {"BUILDING_UP", "ACTIONABLE_UP"}:
        return "DECAYING_UP"
    if prev in {"BUILDING_DOWN", "ACTIONABLE_DOWN"}:
        return "DECAYING_DOWN"
    if prev in {"DECAYING_UP", "DECAYING_DOWN"}:
        return "CLOSED"
    return "CLOSED"


def lifecycle_sequence(filtered_rows: Iterable[dict]) -> list[dict]:
    out = []
    lifecycle = "CLOSED"
    for row in filtered_rows:
        lifecycle = lifecycle_step(lifecycle, row["latent_state"])
        item = dict(row)
        item["lifecycle"] = lifecycle
        item["actionable"] = lifecycle in {"ACTIONABLE_UP", "ACTIONABLE_DOWN"}
        out.append(item)
    return out


def model_snapshot(model: GaussianRegimeHMM) -> dict:
    """Serializable model diagnostics for a research ledger."""
    model._require_fit()
    return {
        "state_names": list(STATE_NAMES),
        "means": model.means.tolist(),
        "variances": model.variances.tolist(),
        "transition": model.transition.tolist(),
        "initial": model.initial.tolist(),
        "expected_dwell_observations": model.expected_dwell_observations(),
        "model_kind": "causal_three_state_diagonal_gaussian_hmm",
        "production_controls": False,
    }
