import json
import math
from pathlib import Path
from typing import Optional

from .failure_taxonomy import legacy_row_label
from .reset_history import count_recent_unexpected_resets


FEATURE_NAMES = (
    "power_percent",
    "supply_voltage_mv",
    "power_good",
    "temperature_c",
    "telemetry_valid",
    "app_flash_free_ratio",
    "free_flash_ratio",
    "image_size_ratio",
    "link_response_ms",
    "recent_retry_rate",
    "previous_failures",
    "previous_failure_rate",
    "history_attempt_count",
    "image_size",
    "recent_reset_count",
)


class FeatureCollectionError(ValueError):
    """Raised when an observed feature value is invalid."""


def _is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _read_secondary_history(
    log_path: str,
    secondary_id: str,
) -> list[dict]:
    path = Path(log_path)

    if not path.is_file():
        return []

    rows = []

    with path.open("r", encoding="utf-8") as log:
        for line_number, line in enumerate(log, start=1):
            if not line.strip():
                continue

            try:
                row = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                print(
                    "[Feature Collector] WARNING: "
                    f"skipping invalid JSON line {line_number} "
                    f"in {path}: {exc}"
                )
                continue

            if (
                isinstance(row, dict)
                and row.get("secondary_id") == secondary_id
            ):
                rows.append(row)

    return rows


def _history_features(
    rows: list[dict],
    current_status: Optional[dict],
    recent_window: int,
) -> tuple[float, int, float, int, int]:
    attempts = [
        row
        for row in rows
        if row.get("update_attempted") is True
    ][-recent_window:]

    retry_attempts = sum(
        1
        for row in attempts
        if (
            _is_number(row.get("retry_count"))
            and row["retry_count"] > 0
        )
    )

    recent_retry_rate = (
        retry_attempts / len(attempts)
        if attempts
        else 0.0
    )

    eligible_history = []
    for row in rows:
        eligible, label = legacy_row_label(row)
        if eligible:
            eligible_history.append((row, label))

    eligible_history = eligible_history[-recent_window:]
    history_attempt_count = len(eligible_history)
    previous_failures = sum(
        1 for _, label in eligible_history if label == 1
    )
    previous_failure_rate = (
        previous_failures / history_attempt_count
        if history_attempt_count
        else 0.0
    )

    recent_reset_count = count_recent_unexpected_resets(
        rows,
        current_status=current_status,
        recent_attempt_window=recent_window,
    )

    return (
        recent_retry_rate,
        previous_failures,
        previous_failure_rate,
        history_attempt_count,
        recent_reset_count,
    )


def collect_features(
    *,
    secondary_id: str,
    status: dict,
    artifact_info: dict,
    power_percent: float | None = None,
    temperature_c: float | None = None,
    log_path: str = "./logs/ota_experiments.jsonl",
    recent_window: int = 10,
) -> dict:
    """Collect only values available before an update attempt."""

    if not isinstance(secondary_id, str) or not secondary_id:
        raise FeatureCollectionError("secondary_id must be non-empty")
    if not isinstance(status, dict):
        raise FeatureCollectionError("status must be a dict")
    if not isinstance(artifact_info, dict):
        raise FeatureCollectionError("artifact_info must be a dict")
    if not isinstance(recent_window, int) or recent_window <= 0:
        raise FeatureCollectionError("recent_window must be positive")

    if power_percent is not None:
        if not _is_number(power_percent):
            raise FeatureCollectionError("power_percent must be numeric")
        if not 0 <= power_percent <= 100:
            raise FeatureCollectionError(
                "power_percent must be between 0 and 100"
            )

    if temperature_c is None:
        temperature_c = status.get(
            "temperature_median_c",
            status.get("temperature_c"),
        )

    if temperature_c is not None and not _is_number(temperature_c):
        raise FeatureCollectionError("temperature_c must be numeric")

    supply_voltage_mv = status.get(
        "supply_voltage_median_mv",
        status.get("supply_voltage_mv"),
    )
    if supply_voltage_mv is not None:
        if (
            not _is_number(supply_voltage_mv)
            or not 0 <= supply_voltage_mv <= 5000
        ):
            raise FeatureCollectionError(
                "supply_voltage_mv must be numeric from 0 to 5000"
            )

    power_good = status.get("power_good")
    if power_good is not None and not isinstance(power_good, bool):
        raise FeatureCollectionError("power_good must be boolean")

    telemetry_valid = status.get("telemetry_valid")
    if (
        telemetry_valid is not None
        and not isinstance(telemetry_valid, bool)
    ):
        raise FeatureCollectionError("telemetry_valid must be boolean")

    app_flash_free_ratio = status.get("app_flash_free_ratio")
    if app_flash_free_ratio is not None:
        if (
            not _is_number(app_flash_free_ratio)
            or not 0.0 <= app_flash_free_ratio <= 1.0
        ):
            raise FeatureCollectionError(
                "app_flash_free_ratio must be between 0 and 1"
            )

    image_size = artifact_info.get("length")
    if image_size is not None:
        if (
            not isinstance(image_size, int)
            or isinstance(image_size, bool)
            or image_size <= 0
        ):
            raise FeatureCollectionError(
                "artifact length must be a positive byte count"
            )

    max_firmware_size = status.get("max_size")
    free_flash_ratio = None
    image_size_ratio = None

    if max_firmware_size is not None:
        if (
            not isinstance(max_firmware_size, int)
            or isinstance(max_firmware_size, bool)
            or max_firmware_size <= 0
        ):
            raise FeatureCollectionError("max_firmware_size must be positive")

    if image_size is not None and max_firmware_size is not None:
        if image_size > max_firmware_size:
            raise FeatureCollectionError(
                "image_size exceeds max_firmware_size"
            )
        free_flash_ratio = (
            max_firmware_size - image_size
        ) / max_firmware_size
        image_size_ratio = image_size / max_firmware_size
        if not 0.0 <= free_flash_ratio <= 1.0:
            raise FeatureCollectionError("free_flash_ratio is out of range")
        if not 0.0 <= image_size_ratio <= 1.0:
            raise FeatureCollectionError("image_size_ratio is out of range")

    link_response_ms = status.get(
        "link_response_median_ms",
        status.get("link_response_ms"),
    )
    if link_response_ms is not None:
        if not _is_number(link_response_ms) or link_response_ms < 0:
            raise FeatureCollectionError(
                "link_response_ms must be non-negative"
            )

    current_uptime_ms = status.get("uptime_ms")
    if current_uptime_ms is not None:
        if (
            not isinstance(current_uptime_ms, int)
            or isinstance(current_uptime_ms, bool)
            or current_uptime_ms < 0
        ):
            raise FeatureCollectionError("uptime_ms must be non-negative")

    rows = _read_secondary_history(log_path, secondary_id)
    (
        recent_retry_rate,
        previous_failures,
        previous_failure_rate,
        history_attempt_count,
        recent_reset_count,
    ) = _history_features(rows, status, recent_window)

    return {
        "power_percent": power_percent,
        "supply_voltage_mv": supply_voltage_mv,
        "power_good": power_good,
        "temperature_c": temperature_c,
        "telemetry_valid": telemetry_valid,
        "app_flash_free_ratio": app_flash_free_ratio,
        "free_flash_ratio": free_flash_ratio,
        "image_size_ratio": image_size_ratio,
        "link_response_ms": link_response_ms,
        "recent_retry_rate": recent_retry_rate,
        "previous_failures": previous_failures,
        "previous_failure_rate": previous_failure_rate,
        "history_attempt_count": history_attempt_count,
        "image_size": image_size,
        "recent_reset_count": recent_reset_count,
    }
