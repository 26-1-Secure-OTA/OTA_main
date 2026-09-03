"""Score and order Safety-Policy-allowed STM32 boards by normality."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from .normal_profile import PROFILE_FEATURES, PROFILE_SCHEMA_VERSION


FIXED_ORDER = ("stm32-led-001", "stm32-led-002", "stm32-led-003")
CONTINUOUS_WEIGHTS = {
    "link_response_ms": 0.30,
    "supply_voltage_mv": 0.15,
    "temperature_c": 0.20,
    "app_flash_free_ratio": 0.05,
}
HISTORY_WEIGHTS = {
    "previous_failures": 0.20,
    "recent_reset_count": 0.10,
}
MAX_CONTINUOUS_DEVIATION = 5.0
MAX_HISTORY_COUNT = 3.0
VALID_MODES = frozenset({"OFF", "SHADOW", "ACTIVE"})


class AnomalyScoringError(ValueError):
    """Raised when the profile or pre-update features cannot be trusted."""


def _is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def load_profile(path: str | Path) -> tuple[dict, str]:
    profile_path = Path(path)
    try:
        raw = profile_path.read_bytes()
    except FileNotFoundError as exc:
        raise AnomalyScoringError("PROFILE_NOT_FOUND") from exc

    try:
        profile = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnomalyScoringError("PROFILE_INVALID") from exc

    if profile.get("profile_schema_version") != PROFILE_SCHEMA_VERSION:
        raise AnomalyScoringError("PROFILE_SCHEMA_MISMATCH")
    if not isinstance(profile.get("boards"), dict):
        raise AnomalyScoringError("PROFILE_INVALID")

    return profile, hashlib.sha256(raw).hexdigest()


def score_secondary(
    *,
    secondary_id: str,
    uid: str | None,
    features: dict,
    profile: dict,
) -> dict:
    board_profile = profile["boards"].get(secondary_id)
    if not isinstance(board_profile, dict):
        raise AnomalyScoringError("SECONDARY_NOT_IN_PROFILE")

    expected_uid = str(board_profile.get("uid") or "").upper()
    observed_uid = str(uid or "").upper()
    if observed_uid != expected_uid:
        raise AnomalyScoringError("PROFILE_UID_MISMATCH")

    deviations = {}
    contributions = {}
    risk_score = 0.0

    for feature_name in PROFILE_FEATURES:
        value = features.get(feature_name)
        feature_profile = board_profile.get("features", {}).get(feature_name)
        if not _is_number(value) or not isinstance(feature_profile, dict):
            raise AnomalyScoringError("FEATURE_MISSING")

        center = feature_profile.get("center")
        scale = feature_profile.get("scale")
        if not _is_number(center) or not _is_number(scale) or scale <= 0:
            raise AnomalyScoringError("PROFILE_INVALID")

        deviation = abs(float(value) - float(center)) / float(scale)
        normalized = min(deviation / MAX_CONTINUOUS_DEVIATION, 1.0)
        contribution = CONTINUOUS_WEIGHTS[feature_name] * normalized
        deviations[feature_name] = round(deviation, 6)
        contributions[feature_name] = round(contribution, 6)
        risk_score += contribution

    for feature_name, weight in HISTORY_WEIGHTS.items():
        value = features.get(feature_name)
        if not _is_number(value) or value < 0:
            raise AnomalyScoringError("FEATURE_MISSING")
        normalized = min(float(value) / MAX_HISTORY_COUNT, 1.0)
        contribution = weight * normalized
        deviations[feature_name] = round(float(value), 6)
        contributions[feature_name] = round(contribution, 6)
        risk_score += contribution

    return {
        "risk_score": round(min(max(risk_score, 0.0), 1.0), 6),
        "deviations": deviations,
        "contributions": contributions,
    }


def schedule_allow_ecus(
    *,
    allow_context: dict[str, dict],
    profile_path: str | Path,
    model_path: str | Path | None = None,
    metadata_path: str | Path | None = None,
    requested_mode: str = "ACTIVE",
    apply_mode: str | None = None,
    fixed_order=FIXED_ORDER,
) -> dict:
    # Compatibility wrapper: legacy callers keep OFF/SHADOW/ACTIVE while the
    # implementation uses separate model/apply modes internally.
    from .risk_engine import RiskEngine

    requested_mode = str(requested_mode).upper()
    result = RiskEngine(
        profile_path=profile_path,
        model_path=model_path,
        metadata_path=metadata_path,
        fixed_order=fixed_order,
    ).rank(
        allow_context,
        model_mode=requested_mode,
        apply_mode=apply_mode,
    ).to_dict()
    if (
        requested_mode in {"ACTIVE", "SHADOW"}
        and result["used_mode"] == "STATISTICAL"
    ):
        result["used_mode"] = requested_mode
    return result
