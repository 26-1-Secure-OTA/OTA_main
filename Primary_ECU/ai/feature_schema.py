import math


FEATURE_SCHEMA_VERSION = 1
FEATURE_NAMES = (
    "link_response_ms",
    "previous_failures",
    "supply_voltage_mv",
    "temperature_c",
    "app_flash_free_ratio",
    "recent_reset_count",
)
FEATURE_UNITS = {
    "link_response_ms": "milliseconds",
    "previous_failures": "count",
    "supply_voltage_mv": "millivolts",
    "temperature_c": "Celsius",
    "app_flash_free_ratio": "ratio",
    "recent_reset_count": "count",
}

LEAKAGE_FIELDS = frozenset({
    "success", "failure_label", "failure_stage", "failure_reason_code",
    "retry_count", "update_duration_ms", "transfer_duration_ms",
    "throughput_kbps", "fw_ok_received", "slot_switched",
    "version_verified", "active_slot_after", "rollback",
    "rollback_result", "rollback_performed",
})


class FeatureSchemaError(ValueError):
    pass


def validate_feature_definition(names) -> None:
    names = tuple(names)
    leaked = sorted(set(names) & LEAKAGE_FIELDS)
    if leaked:
        raise FeatureSchemaError(f"leakage fields are forbidden: {leaked}")
    if names != FEATURE_NAMES:
        raise FeatureSchemaError(
            f"feature order mismatch: expected={FEATURE_NAMES}, actual={names}"
        )


def validate_feature_values(values: dict) -> list[float]:
    if not isinstance(values, dict):
        raise FeatureSchemaError("features must be an object")
    missing = [name for name in FEATURE_NAMES if name not in values]
    if missing:
        raise FeatureSchemaError(f"required features missing: {missing}")
    output = []
    for name in FEATURE_NAMES:
        value = values[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise FeatureSchemaError(f"{name} must be numeric and non-null")
        if not math.isfinite(value):
            raise FeatureSchemaError(f"{name} must be finite")
        if name in {"link_response_ms", "previous_failures", "supply_voltage_mv", "recent_reset_count"} and value < 0:
            raise FeatureSchemaError(f"{name} must be non-negative")
        if name == "app_flash_free_ratio" and not 0.0 <= value <= 1.0:
            raise FeatureSchemaError("app_flash_free_ratio must be between 0 and 1")
        output.append(float(value))
    return output


def schema_document() -> dict:
    return {
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "feature_order": list(FEATURE_NAMES),
        "feature_units": dict(FEATURE_UNITS),
    }
