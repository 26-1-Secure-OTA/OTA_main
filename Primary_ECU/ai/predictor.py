import math

from .feature_schema import FEATURE_NAMES, FEATURE_SCHEMA_VERSION, validate_feature_values
from .model_loader import load_model


class PredictionError(ValueError):
    pass


class FailureRiskPredictor:
    def __init__(self, model_path, metadata_path):
        self.model, self.metadata = load_model(model_path, metadata_path)

    def predict(self, allow_features: dict[str, dict]) -> dict:
        ids = list(allow_features)
        if any(allow_features[secondary_id].get("telemetry_valid") is not True for secondary_id in ids):
            raise PredictionError("INVALID_TELEMETRY")
        try:
            matrix = [validate_feature_values(allow_features[secondary_id]) for secondary_id in ids]
        except ValueError as exc:
            raise PredictionError("FEATURE_MISSING") from exc
        probabilities = self.model.predict_proba(matrix)
        classes = list(self.model.classes_)
        if 1 not in classes:
            raise PredictionError("model has no failure class")
        failure_index = classes.index(1)
        predictions = []
        for secondary_id, values, probabilities_for_row in zip(ids, matrix, probabilities):
            risk = float(probabilities_for_row[failure_index])
            if not math.isfinite(risk) or not 0.0 <= risk <= 1.0:
                raise PredictionError("INVALID_RISK")
            reasons = []
            try:
                node_indicator = self.model.decision_path([values])
                leaf_id = self.model.apply([values])[0]
                for node_id in node_indicator.indices:
                    if node_id == leaf_id:
                        continue
                    feature_index = self.model.tree_.feature[node_id]
                    threshold = self.model.tree_.threshold[node_id]
                    operator = "<=" if values[feature_index] <= threshold else ">"
                    reasons.append(f"{FEATURE_NAMES[feature_index]} {operator} {threshold:.3f}")
            except Exception:
                reasons = []
            predictions.append({"secondary_id": secondary_id, "failure_risk": risk, "reasons": reasons})
        return {
            "model_version": self.metadata["model_version"],
            "model_hash": self.metadata["model_hash"],
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "predictions": predictions,
        }
