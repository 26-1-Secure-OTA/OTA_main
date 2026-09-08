"""Direction-aware risk components for the hybrid OTA scheduler."""

from __future__ import annotations

import math
from typing import Mapping, Sequence

from .feature_schema import SELECTED_RISK_FEATURES


class HybridRiskError(ValueError):
    """Raised when hybrid risk inputs or model metadata are invalid."""


DEFAULT_WEIGHTS = {
    "physical_anomaly": 0.40,
    "link_delay": 0.30,
    "image_size": 0.15,
    "previous_failures": 0.15,
}
DEFAULT_LINK_LIMIT_MS = 1000.0
DEFAULT_FAILURE_SATURATION_COUNT = 3.0


def _number(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise HybridRiskError(f"FEATURE_INVALID:{name}")
    return float(value)


def build_hybrid_config(rows: Sequence[dict]) -> dict:
    """Build auditable directional-risk configuration from normal rows."""
    link_values = []
    for row in rows:
        features = row.get("ai_features") if isinstance(row, dict) else None
        if not isinstance(features, Mapping):
            raise HybridRiskError("TRAINING_FEATURES_INVALID")
        value = _number(features.get("link_response_ms"), "link_response_ms")
        if value < 0:
            raise HybridRiskError("FEATURE_INVALID:link_response_ms")
        link_values.append(value)

    if not link_values:
        raise HybridRiskError("TRAINING_LINK_VALUES_EMPTY")

    ordered = sorted(link_values)
    middle = len(ordered) // 2
    normal_link_ms = (
        ordered[middle]
        if len(ordered) % 2
        else (ordered[middle - 1] + ordered[middle]) / 2.0
    )
    if normal_link_ms >= DEFAULT_LINK_LIMIT_MS:
        raise HybridRiskError("NORMAL_LINK_THRESHOLD_EXCEEDS_LIMIT")

    return {
        "selected_features": list(SELECTED_RISK_FEATURES),
        "weights": dict(DEFAULT_WEIGHTS),
        "link_response": {
            "method": "normal_median_excess_normalized_to_policy_limit",
            "normal_threshold_ms": normal_link_ms,
            "policy_limit_ms": DEFAULT_LINK_LIMIT_MS,
            "direction": "higher_is_riskier",
        },
        "image_size_ratio": {
            "method": "slot_occupancy_ratio",
            "direction": "higher_is_riskier",
        },
        "previous_failures": {
            "method": "linear_clipped_count",
            "saturation_count": DEFAULT_FAILURE_SATURATION_COUNT,
            "direction": "higher_is_riskier",
        },
        "score_semantics": "relative_scheduling_risk_not_failure_probability",
    }


def combine_hybrid_risk(
    *,
    physical_anomaly_risk: float,
    features: Mapping[str, object],
    config: Mapping[str, object],
) -> dict:
    """Combine two-feature IF anomaly risk with three directional risks."""
    physical = _number(physical_anomaly_risk, "physical_anomaly_risk")
    if not 0.0 <= physical <= 1.0:
        raise HybridRiskError("PHYSICAL_ANOMALY_RISK_OUT_OF_RANGE")

    if not isinstance(features, Mapping):
        raise HybridRiskError("FEATURES_NOT_MAPPING")
    missing = [name for name in SELECTED_RISK_FEATURES if name not in features]
    if missing:
        raise HybridRiskError("FEATURE_MISSING:" + ",".join(missing))

    selected = {
        name: _number(features[name], name)
        for name in SELECTED_RISK_FEATURES
    }
    link_ms = selected["link_response_ms"]
    image_ratio = selected["image_size_ratio"]
    previous_failures = selected["previous_failures"]
    if link_ms < 0:
        raise HybridRiskError("FEATURE_INVALID:link_response_ms")
    if not 0.0 <= image_ratio <= 1.0:
        raise HybridRiskError("FEATURE_INVALID:image_size_ratio")
    if previous_failures < 0:
        raise HybridRiskError("FEATURE_INVALID:previous_failures")

    try:
        weights = config["weights"]
        link_config = config["link_response"]
        failure_config = config["previous_failures"]
        normal_link_ms = _number(
            link_config["normal_threshold_ms"], "normal_threshold_ms"
        )
        link_limit_ms = _number(
            link_config["policy_limit_ms"], "policy_limit_ms"
        )
        failure_saturation = _number(
            failure_config["saturation_count"], "saturation_count"
        )
        parsed_weights = {
            name: _number(weights[name], f"weight:{name}")
            for name in DEFAULT_WEIGHTS
        }
    except (KeyError, TypeError) as exc:
        raise HybridRiskError("HYBRID_CONFIG_INVALID") from exc

    if (
        normal_link_ms < 0
        or link_limit_ms <= normal_link_ms
        or failure_saturation <= 0
        or any(weight < 0 for weight in parsed_weights.values())
        or not math.isclose(sum(parsed_weights.values()), 1.0, abs_tol=1e-9)
    ):
        raise HybridRiskError("HYBRID_CONFIG_INVALID")
    if tuple(config.get("selected_features", ())) != SELECTED_RISK_FEATURES:
        raise HybridRiskError("SELECTED_FEATURES_MISMATCH")

    link_excess_ms = max(0.0, link_ms - normal_link_ms)
    link_risk = min(
        link_excess_ms / (link_limit_ms - normal_link_ms),
        1.0,
    )
    image_risk = image_ratio
    failure_risk = min(previous_failures / failure_saturation, 1.0)

    components = {
        "physical_anomaly": physical,
        "link_delay": link_risk,
        "image_size": image_risk,
        "previous_failures": failure_risk,
    }
    contributions = {
        name: parsed_weights[name] * components[name]
        for name in components
    }
    final_risk = min(max(sum(contributions.values()), 0.0), 1.0)

    return {
        "risk_score": round(final_risk, 6),
        "physical_anomaly_risk": round(physical, 6),
        "directional_risk": round(
            contributions["link_delay"]
            + contributions["image_size"]
            + contributions["previous_failures"],
            6,
        ),
        "component_scores": {
            name: round(value, 6) for name, value in components.items()
        },
        "contributions": {
            name: round(value, 6) for name, value in contributions.items()
        },
        "deviations": {
            "link_delay_excess_ms": round(link_excess_ms, 6),
            "link_delay_excess_ratio": round(link_risk, 6),
            "image_size_ratio": round(image_ratio, 6),
            "previous_failures_normalized": round(failure_risk, 6),
        },
        "selected_features": {
            name: round(value, 6) for name, value in selected.items()
        },
    }
