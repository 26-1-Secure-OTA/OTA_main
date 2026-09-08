"""Versioned, order-independent feature validation for AI models."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping


class FeatureValidationError(ValueError):
    """Raised when features cannot be represented by a schema."""


SCHEMA_FEATURES = {
    1: (
        "link_response_ms",
        "supply_voltage_mv",
        "temperature_c",
        "app_flash_free_ratio",
        "previous_failures",
        "recent_reset_count",
    ),
    2: (
        "link_response_ms",
        "supply_voltage_mv",
        "temperature_c",
        "image_size_ratio",
        "previous_failures",
        "recent_reset_count",
    ),
    # Hybrid OTA scheduler v3: Isolation Forest learns only the physical
    # measurements for which deviations in either direction are meaningful.
    3: (
        "supply_voltage_mv",
        "temperature_c",
    ),
}

SELECTED_RISK_FEATURES = (
    "link_response_ms",
    "supply_voltage_mv",
    "temperature_c",
    "image_size_ratio",
    "previous_failures",
)


@dataclass(frozen=True)
class FeatureSchema:
    version: int = 1

    def __post_init__(self) -> None:
        if self.version not in SCHEMA_FEATURES:
            raise FeatureValidationError("UNSUPPORTED_SCHEMA_VERSION")

    @property
    def feature_order(self) -> tuple[str, ...]:
        return SCHEMA_FEATURES[self.version]

    def validate_feature_order(self, feature_order) -> None:
        if tuple(feature_order) != self.feature_order:
            raise FeatureValidationError("FEATURE_ORDER_MISMATCH")

    def to_vector(self, features: Mapping[str, object]) -> list[float]:
        if not isinstance(features, Mapping):
            raise FeatureValidationError("FEATURES_NOT_MAPPING")
        missing = [name for name in self.feature_order if name not in features]
        if missing:
            raise FeatureValidationError("FEATURE_MISSING:" + ",".join(missing))

        vector = []
        for name in self.feature_order:
            value = features[name]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise FeatureValidationError(f"FEATURE_INVALID:{name}")
            vector.append(float(value))
        return vector


DEFAULT_SCHEMA = FeatureSchema(3)
