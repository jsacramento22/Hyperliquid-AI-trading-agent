"""Tests for the pure verdict logic in scripts/drift_check.py.

These cover the boundary cases that distinguish the four verdict codes
without exercising the storage / settings / I/O glue around them — that
part is just plumbing and gets covered by the smoke run in CI/manual.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# scripts/ isn't a package; add it to sys.path so we can import the module
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from drift_check import compute_verdict  # noqa: E402

# Phase 1.5 R3.1 backtest — what the live numbers will be compared against
_BACKTEST_MEAN = 0.5221
_BACKTEST_STD = 0.0068


def _verdict(*, n: int, correct: int, **kwargs):
    return compute_verdict(
        live_n=n,
        live_correct=correct,
        backtest_mean=_BACKTEST_MEAN,
        backtest_std=_BACKTEST_STD,
        **kwargs,
    )


def test_insufficient_below_min_samples():
    v = _verdict(n=50, correct=25, min_samples=100)
    assert v.code == "INSUFFICIENT"
    assert v.exit_code == 0
    assert v.z_score is None
    assert "50/100" in v.reason


def test_insufficient_at_zero():
    v = _verdict(n=0, correct=0, min_samples=100)
    assert v.code == "INSUFFICIENT"
    assert v.live_accuracy == 0.0


def test_ok_matching_backtest():
    # 500 samples at exactly the backtest mean → z ≈ 0 → OK
    n = 500
    correct = round(n * _BACKTEST_MEAN)
    v = _verdict(n=n, correct=correct, min_samples=100)
    assert v.code == "OK"
    assert v.exit_code == 0
    assert v.z_score is not None
    assert abs(v.z_score) < 0.5


def test_ok_above_backtest():
    # Live is BETTER than backtest — never a problem
    v = _verdict(n=500, correct=300, min_samples=100)
    assert v.code == "OK"
    assert v.exit_code == 0
    assert v.z_score > 0


def test_warn_band():
    # Pick a live accuracy ~2σ below — should be WARN (between -1.5σ and -2.5σ)
    # With n=500, binomial SE = sqrt(0.5*0.5/500) ≈ 0.0224. Combined with
    # backtest std 0.0068, combined SE ≈ sqrt(0.0224² + 0.0068²) ≈ 0.0234.
    # 2σ below backtest mean 0.5221 ≈ 0.5221 - 2*0.0234 = 0.4753.
    n = 500
    live_acc = _BACKTEST_MEAN - 2.0 * (
        (0.5 * 0.5 / n + _BACKTEST_STD ** 2) ** 0.5
    )
    correct = round(n * live_acc)
    v = _verdict(n=n, correct=correct, min_samples=100)
    assert v.code == "WARN"
    assert v.exit_code == 1
    assert v.z_score < -1.5
    assert v.z_score >= -2.5


def test_alert_band():
    # 3σ below the backtest mean → ALERT
    n = 1000
    se_live = (0.5 * 0.5 / n) ** 0.5
    combined_se = (se_live ** 2 + _BACKTEST_STD ** 2) ** 0.5
    live_acc = _BACKTEST_MEAN - 3.0 * combined_se
    correct = round(n * live_acc)
    v = _verdict(n=n, correct=correct, min_samples=100)
    assert v.code == "ALERT"
    assert v.exit_code == 2
    assert v.z_score < -2.5
    assert "retrain" in v.reason.lower()


def test_alert_extreme():
    # Live at 30% accuracy on 500 samples — way below any reasonable band
    v = _verdict(n=500, correct=150, min_samples=100)
    assert v.code == "ALERT"
    assert v.z_score < -5


def test_zero_backtest_std_doesnt_crash():
    # Pathological backtest std = 0 (single-fold backtest, hypothetically).
    # With n=500 the binomial SE remains > 0 so we still get a clean verdict.
    v = compute_verdict(
        live_n=500, live_correct=250,
        backtest_mean=0.52, backtest_std=0.0,
        min_samples=100,
    )
    assert v.code in {"OK", "WARN", "ALERT"}
    assert v.z_score is not None


def test_perfect_accuracy_degenerate_se():
    # 100/100 correct → binomial SE = 0, and backtest std hypothetically 0
    # → combined SE = 0. The function returns OK with z=0 rather than divide-
    # by-zero. (Real backtest std is never 0, so this only fires on synthetic
    # inputs, but the guard keeps drift_check from crashing.)
    v = compute_verdict(
        live_n=100, live_correct=100,
        backtest_mean=0.52, backtest_std=0.0,
        min_samples=100,
    )
    assert v.code == "OK"
    assert v.z_score == 0.0


def test_exit_codes_distinct():
    # Sanity: the four verdict codes map to three distinct exit codes
    # (OK and INSUFFICIENT both 0 — that's intentional, neither pages).
    assert _verdict(n=10, correct=5, min_samples=100).exit_code == 0  # INSUFF
    assert _verdict(n=500, correct=261, min_samples=100).exit_code == 0  # OK
    n = 1000
    se = ((0.5 * 0.5 / n) + _BACKTEST_STD ** 2) ** 0.5
    warn_acc = _BACKTEST_MEAN - 2.0 * se
    alert_acc = _BACKTEST_MEAN - 3.0 * se
    assert _verdict(n=n, correct=round(n * warn_acc), min_samples=100).exit_code == 1
    assert _verdict(n=n, correct=round(n * alert_acc), min_samples=100).exit_code == 2
