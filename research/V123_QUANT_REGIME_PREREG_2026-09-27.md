# V12.3 Quant Regime Shadow — preregistration (27 Sep 2026)

**Status:** RESEARCH ONLY / SHADOW ONLY / NO PRODUCTION CONTROL

## Objective

Replace the idea that "Actionable" is a short-lived event with a causal
mathematical estimate of the underlying market regime.

The research question is:

> Can DBIndicator identify a persistent directional regime in the underlying
> stock early enough that the stock naturally remains ACTIONABLE while the
> regime survives, without any fixed 15/30 minute dwell rule?

Time in ACTIONABLE is an **output**, never an input.

A stock may remain actionable for seconds, minutes, hours, or the entire session
if the inferred underlying regime remains directionally persistent.

## Isolation rules

1. This model cannot change V12.3 Focus Desk, tactical states, execution routes,
   options, alerts, settings, or scans.
2. It is forbidden to use option prices, fills, P&L, future bars, post-entry
   path efficiency, later pullback ratio, PROFIT_PROTECT, or any outcome label
   in the state inference.
3. Training parameters for a session must be fitted only from completed prior
   sessions.  They are frozen for the session being evaluated.
4. Primary evaluation is stock-price / underlying only.
5. No production promotion until forward evidence is sufficient and separately
   approved.

## Mathematical pipeline

### A. Causal latent trend

For log-price y_t = log(P_t), estimate a local-linear state:

    y_t = level_t + observation_noise
    level_t = level_(t-1) + drift_(t-1) + process_noise
    drift_t = drift_(t-1) + drift_noise

A Kalman filter produces a continuously updated estimate of latent drift and
its uncertainty.

The research feature is not raw drift but the uncertainty-scaled drift:

    z_drift,t = drift_t / sd(drift_t)

This allows stocks with different prices and volatility to be compared without
a hand-built score.

### B. Market/sector residual

Fit the stock return causally with recursive least squares:

    r_stock,t = alpha_t
                + beta_market,t * r_NIFTY,t
                + beta_sector,t * r_sector,t
                + u_t

The residual u_t measures stock-specific movement after removing broad market
and sector effects.  It is standardized online from its own innovation process.

Sector return is optional until a reliable live sector stream is available; a
missing sector input must be explicit and cannot be silently substituted by a
future value.

### C. Acceleration and participation

The observation vector contains four independent coordinates:

    X_t = [
        uncertainty-scaled latent drift,
        standardized idiosyncratic residual,
        standardized change in latent drift,
        standardized contemporaneous participation
    ]

Participation may be a causal volume-rate/participation measure.  It is not a
vote and is not combined into a weighted score.

### D. Latent regime

Fit a three-state diagonal-Gaussian Hidden Markov Model from prior-session
feature vectors only.

The learned latent states are relabelled by their fitted directional centres:

    DOWN, FLAT, UP

The transition matrix is learned from data.  Therefore persistence comes from
the empirical transition process, not a hard-coded dwell time.

During the live/replay session use **causal filtering only**:

    P(S_t | X_1, ..., X_t)

Never use backward smoothing for a live-equivalent result because smoothing
would use future data.

### E. Human-readable lifecycle

The latent state is translated into a desk lifecycle:

    CLOSED
      -> BUILDING_UP
      -> ACTIONABLE_UP
      -> DECAYING_UP
      -> CLOSED

and symmetrically for DOWN.

No timer appears in this lifecycle.

If the HMM remains in UP all day, ACTIONABLE_UP may remain active all day.
If the state becomes FLAT, the lifecycle records DECAYING.  If a directly
opposing state becomes dominant, the direction changes structurally rather than
waiting for a timer.

The underlying thesis and execution episode remain separate.  Multiple entry,
exit and re-entry episodes may occur inside one persistent underlying regime.

## Walk-forward training

For session D:

1. Build feature vectors from completed sessions strictly before D.
2. Fit the HMM on those vectors.
3. Freeze means, variances, transition matrix and initial distribution.
4. Replay or observe D causally.
5. Save every latent state, posterior vector and lifecycle transition.
6. Do not refit using D until D is complete and is eligible to become part of
   a later session's training set.

## Primary evaluation

The model is not optimized for hit rate and is not optimized to maximize time
in ACTIONABLE.

Measure:

- capture rate of eventual large underlying movers,
- fraction of the directional move remaining at first ACTIONABLE,
- ACTIONABLE dwell distribution (entire distribution, not a target duration),
- 15m / 45m / end-of-session follow-through in ATR units,
- MFE and MAE while regime remains actionable,
- number of state switches per stock/session,
- wrong-direction actionable occupancy,
- counter-trend repetitions,
- actionable breadth versus Focus Desk capacity,
- same-stock / same-direction random-time benchmark,
- survival of the underlying regime through separate re-entry episodes.

The model is useful only if it finds directional movement earlier and preserves
valid regimes without increasing wrong-direction persistence.

## Explicit non-goals

- no weighted "actionability score",
- no 70/55-style entry/maintenance thresholds,
- no forced 15-minute hold,
- no forced 30-minute expiry,
- no option-cost optimization in the primary study,
- no use of hindsight-defined post-entry variables as live filters,
- no parameter tuning on the forward evaluation session.

## Phase 1 deliverable

The first implementation is a pure Python research module:

    app/v123_quant_regime.py

It provides:
- causal local-linear trend estimation,
- causal recursive market/sector residuals,
- causal participation/acceleration features,
- deterministic unsupervised three-state Gaussian HMM fitting,
- causal posterior filtering,
- timer-free BUILDING/ACTIONABLE/DECAYING lifecycle,
- serializable model diagnostics.

This phase is intentionally not wired into background.py or the live dashboard.
A later separately approved shadow-recorder patch may feed it live data while
still leaving production decisions untouched.
