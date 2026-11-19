"""Quality Gate puro (sin dependencias externas) para test unitario.

Regla del informe: un modelo solo se publica si RMSE <= 0.85.
"""

from __future__ import annotations

from typing import Any


DEFAULT_RMSE_THRESHOLD = 0.85


def normalize_rmse(value: Any) -> float:
    return float(value)


def evaluate_quality_gate(
    rmse: Any,
    threshold: float = DEFAULT_RMSE_THRESHOLD,
    precision_at_10: Any | None = None,
) -> dict[str, Any]:
    """Devuelve el veredicto del gate con una razón legible.

    Se usa en el pipeline y se testea de forma aislada (sin Surprise/MySQL).
    """
    value = normalize_rmse(rmse)
    passed = value <= float(threshold)
    return {
        "passed": passed,
        "rmse": round(value, 4),
        "threshold": float(threshold),
        "precision_at_10": (
            round(float(precision_at_10), 4) if precision_at_10 is not None else None
        ),
        "reason": (
            f"RMSE {value:.4f} <= {float(threshold):.2f}: modelo promovido"
            if passed
            else f"RMSE {value:.4f} > {float(threshold):.2f}: modelo rechazado"
        ),
    }


def is_rmse_acceptable(rmse: Any, threshold: float = DEFAULT_RMSE_THRESHOLD) -> bool:
    return evaluate_quality_gate(rmse, threshold)["passed"]
