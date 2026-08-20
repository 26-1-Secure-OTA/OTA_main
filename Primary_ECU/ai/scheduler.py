import math

from .feature_schema import FEATURE_SCHEMA_VERSION


FIXED_ORDER = ("stm32-led-001", "stm32-led-002", "stm32-led-003")
FALLBACK_REASONS = frozenset({
    "MODEL_NOT_FOUND", "MODEL_HASH_MISMATCH", "FEATURE_SCHEMA_MISMATCH",
    "FEATURE_MISSING", "INVALID_TELEMETRY", "MODEL_TIMEOUT", "MODEL_EXCEPTION",
    "INVALID_RISK", "SECONDARY_MISSING", "SECONDARY_DUPLICATED", "UNKNOWN_SECONDARY",
    "MODEL_DEPENDENCY_MISSING",
})


def fixed_schedule(secondary_ids) -> list[str]:
    selected = set(secondary_ids)
    unknown = selected - set(FIXED_ORDER)
    if unknown:
        raise ValueError(f"unknown Secondary: {sorted(unknown)}")
    return [secondary_id for secondary_id in FIXED_ORDER if secondary_id in selected]


def _validate_prediction(document: dict, allow_ids: list[str], registry_ids: set[str]) -> dict[str, float]:
    if document.get("feature_schema_version") != FEATURE_SCHEMA_VERSION:
        raise ValueError("FEATURE_SCHEMA_MISMATCH")
    predictions = document.get("predictions")
    if not isinstance(predictions, list):
        raise ValueError("MODEL_EXCEPTION")
    ids = [item.get("secondary_id") for item in predictions if isinstance(item, dict)]
    if len(ids) != len(set(ids)):
        raise ValueError("SECONDARY_DUPLICATED")
    if set(ids) - registry_ids:
        raise ValueError("UNKNOWN_SECONDARY")
    if set(ids) != set(allow_ids):
        raise ValueError("SECONDARY_MISSING")
    risks = {}
    for item in predictions:
        risk = item.get("failure_risk")
        if isinstance(risk, bool) or not isinstance(risk, (int, float)) or not math.isfinite(risk) or not 0.0 <= risk <= 1.0:
            raise ValueError("INVALID_RISK")
        risks[item["secondary_id"]] = float(risk)
    return risks


def schedule_allow_ecus(allow_features: dict[str, dict], registry_ids, predictor=None, requested="FIXED") -> dict:
    allow_ids = fixed_schedule(allow_features.keys())
    base = {
        "scheduler_requested": requested,
        "scheduler_used": "FIXED",
        "fallback_reason": None,
        "order": allow_ids,
        "predictions": {},
        "model_version": None,
        "model_hash": None,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
    }
    if requested != "AI":
        return base
    if predictor is None:
        base["fallback_reason"] = "MODEL_NOT_FOUND"
        return base
    try:
        document = predictor.predict(allow_features)
        risks = _validate_prediction(document, allow_ids, set(registry_ids))
        tie_break = {secondary_id: index for index, secondary_id in enumerate(FIXED_ORDER)}
        base.update({
            "scheduler_used": "AI",
            "order": sorted(allow_ids, key=lambda secondary_id: (risks[secondary_id], tie_break[secondary_id])),
            "predictions": risks,
            "model_version": document.get("model_version"),
            "model_hash": document.get("model_hash"),
        })
    except Exception as exc:
        reason = str(exc)
        base["fallback_reason"] = reason if reason in FALLBACK_REASONS else "MODEL_EXCEPTION"
    return base
