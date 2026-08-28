"""Isolation Forest anomaly-based risk ordering."""

from __future__ import annotations

import math
import numpy as np

from .feature_schema import DEFAULT_SCHEMA
from .model_loader import load_model
from .risk_engine import RiskResult


class IsolationForestEngine:
    def __init__(self, model_path, metadata_path, fixed_order) -> None:
        self.model_path = model_path
        self.metadata_path = metadata_path
        self.fixed_order = tuple(fixed_order)

    def rank(self, allow_context: dict[str, dict]) -> RiskResult:
        model, metadata = load_model(
            self.model_path, self.metadata_path,
            expected_type="ISOLATION_FOREST",
            expected_schema_version=DEFAULT_SCHEMA.version,
        )
        fixed = [ecu for ecu in self.fixed_order if ecu in allow_context]
        fixed += [ecu for ecu in allow_context if ecu not in self.fixed_order]
        X = np.asarray([
            DEFAULT_SCHEMA.to_vector((allow_context[ecu].get("features") or {}))
            for ecu in fixed
        ])
        try:
            anomaly_scores = -np.asarray(model.score_samples(X), dtype=float)
        except Exception as exc:
            raise RuntimeError("PREDICTION_FAILED") from exc
        normalization = metadata.get("risk_normalization") or {}
        quantiles = np.asarray(normalization.get("anomaly_score_quantiles"), dtype=float)
        probabilities = np.asarray(normalization.get("quantile_probabilities"), dtype=float)
        if quantiles.shape != (101,) or probabilities.shape != (101,) or not np.all(np.isfinite(quantiles)):
            raise ValueError("NORMALIZATION_METADATA_INVALID")
        risks = np.interp(anomaly_scores, quantiles, probabilities, left=0.0, right=1.0)
        if not np.all(np.isfinite(risks)):
            raise ValueError("PREDICTION_NONFINITE")
        scores = {
            ecu: {
                "risk_score": round(float(risk), 6),
                "anomaly_score": round(float(anomaly), 9),
                "score_semantics": "anomaly_based_risk_not_failure_probability",
            }
            for ecu, risk, anomaly in zip(fixed, risks, anomaly_scores)
        }
        tie_break = {ecu: i for i, ecu in enumerate(fixed)}
        recommended = sorted(fixed, key=lambda ecu: (scores[ecu]["risk_score"], tie_break[ecu]))
        return RiskResult(
            requested_mode="ISOLATION_FOREST",
            used_mode="ISOLATION_FOREST",
            recommended_order=recommended,
            execution_order=recommended,
            scores=scores,
            model_type="ISOLATION_FOREST",
            model_version=metadata["model_version"],
            model_hash=metadata["model_sha256"],
        )

