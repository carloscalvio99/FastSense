"""Quality Gate: el modelo solo se publica si RMSE <= 0.85."""

from __future__ import annotations

from quality_gate import evaluate_quality_gate, is_rmse_acceptable


def test_gate_accepts_rmse_at_threshold():
    verdict = evaluate_quality_gate(0.85)
    assert verdict["passed"] is True
    assert verdict["threshold"] == 0.85
    assert "promovido" in verdict["reason"]


def test_gate_accepts_low_rmse_with_precision():
    verdict = evaluate_quality_gate(0.61, 0.85, precision_at_10=0.72)
    assert verdict["passed"] is True
    assert verdict["precision_at_10"] == 0.72


def test_gate_rejects_rmse_above_threshold():
    assert is_rmse_acceptable(0.8501) is False
    verdict = evaluate_quality_gate(1.42)
    assert verdict["passed"] is False
    assert "rechazado" in verdict["reason"]


def test_gate_supports_custom_threshold():
    assert is_rmse_acceptable(0.90, threshold=0.95) is True
    assert is_rmse_acceptable(0.90, threshold=0.80) is False
