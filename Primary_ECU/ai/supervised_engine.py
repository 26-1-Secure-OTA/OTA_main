"""Future predict_proba adapter; no production supervised model is bundled."""

from __future__ import annotations

import numpy as np

from .feature_schema import DEFAULT_SCHEMA
from .risk_engine import FIXED_ORDER, RiskResult


class SupervisedEngine:
    def __init__(self, model=None, failure_class=None, fixed_order=FIXED_ORDER) -> None:
        self.model = model
        self.failure_class = failure_class
        self.fixed_order = tuple(fixed_order)

    def rank(self, allow_context):
        if self.model is None:
            raise RuntimeError("SUPERVISED_MODEL_NOT_CONFIGURED")
        if not hasattr(self.model, "predict_proba") or not hasattr(self.model, "classes_"):
            raise RuntimeError("SUPERVISED_MODEL_INTERFACE_INVALID")
        fixed = [ecu for ecu in self.fixed_order if ecu in allow_context]
        fixed += [ecu for ecu in allow_context if ecu not in self.fixed_order]
        X = np.asarray([
            DEFAULT_SCHEMA.to_vector((allow_context[ecu].get("features") or {}))
            for ecu in fixed
        ])
        classes = list(self.model.classes_)
        if self.failure_class not in classes:
            raise RuntimeError("FAILURE_CLASS_NOT_FOUND")
        try:
            probabilities = np.asarray(self.model.predict_proba(X), dtype=float)
            risks = probabilities[:, classes.index(self.failure_class)]
        except Exception as exc:
            raise RuntimeError("PREDICTION_FAILED") from exc
        if risks.shape != (len(fixed),) or not np.all(np.isfinite(risks)) or np.any((risks < 0) | (risks > 1)):
            raise RuntimeError("PREDICTION_INVALID")
        scores = {ecu: {"risk_score": round(float(risk), 6)} for ecu, risk in zip(fixed, risks)}
        tie_break = {ecu: i for i, ecu in enumerate(fixed)}
        recommended = sorted(fixed, key=lambda ecu: (scores[ecu]["risk_score"], tie_break[ecu]))
        return RiskResult(
            requested_mode="SUPERVISED", used_mode="SUPERVISED",
            recommended_order=recommended, execution_order=recommended,
            scores=scores, model_type="SUPERVISED",
        )
