"""Build a robust per-board normal profile from baseline JSONL data."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
from datetime import datetime, timezone
from pathlib import Path


PROFILE_SCHEMA_VERSION = 1
PROFILE_VERSION = "normal-profile-v1"
PROFILE_FEATURES = (
    "link_response_ms",
    "supply_voltage_mv",
    "temperature_c",
    "app_flash_free_ratio",
)
MINIMUM_SCALES = {
    "link_response_ms": 0.25,
    "supply_voltage_mv": 2.0,
    "temperature_c": 0.25,
    "app_flash_free_ratio": 0.005,
}

PRIMARY_ECU_DIR = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = (
    PRIMARY_ECU_DIR
    / "data"
    / "ai_baseline"
    / "run30"
    / "normal_status_aggregated.jsonl"
)
DEFAULT_OUTPUT = PRIMARY_ECU_DIR / "models" / "normal-profile-v1.json"
DEFAULT_REGISTRY = PRIMARY_ECU_DIR / "config" / "secondary_registry.json"


class NormalProfileError(ValueError):
    """Raised when baseline data cannot form a trustworthy profile."""


def _is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def read_jsonl(path: Path) -> list[dict]:
    rows = []

    with path.open("r", encoding="utf-8") as baseline_file:
        for line_number, line in enumerate(baseline_file, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise NormalProfileError(
                    f"invalid JSON at {path}:{line_number}: {exc}"
                ) from exc
            if not isinstance(row, dict):
                raise NormalProfileError(
                    f"baseline row must be an object: {path}:{line_number}"
                )
            rows.append(row)

    if not rows:
        raise NormalProfileError("baseline dataset is empty")

    return rows


def _source_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_profile(
    rows: list[dict],
    registry: dict,
    *,
    minimum_samples: int = 20,
    source_sha256: str | None = None,
) -> dict:
    if minimum_samples <= 0:
        raise NormalProfileError("minimum_samples must be positive")

    grouped = {secondary_id: [] for secondary_id in registry}

    for row_index, row in enumerate(rows, start=1):
        secondary_id = row.get("secondary_id")
        if secondary_id not in grouped:
            raise NormalProfileError(
                f"unknown secondary in baseline row {row_index}: "
                f"{secondary_id!r}"
            )
        grouped[secondary_id].append(row)

    boards = {}

    for secondary_id, board_rows in grouped.items():
        if len(board_rows) < minimum_samples:
            raise NormalProfileError(
                f"not enough baseline rows for {secondary_id}: "
                f"required={minimum_samples}, actual={len(board_rows)}"
            )

        expected_uid = str(registry[secondary_id].get("uid") or "").upper()
        observed_uids = {
            str(row.get("uid") or "").upper()
            for row in board_rows
        }
        if observed_uids != {expected_uid}:
            raise NormalProfileError(
                f"UID mismatch in baseline for {secondary_id}: "
                f"expected={expected_uid}, observed={sorted(observed_uids)}"
            )

        feature_profile = {}
        for feature_name in PROFILE_FEATURES:
            values = []
            for row in board_rows:
                features = row.get("ai_features")
                value = (
                    features.get(feature_name)
                    if isinstance(features, dict)
                    else None
                )
                if not _is_number(value):
                    raise NormalProfileError(
                        f"missing/invalid {feature_name} for {secondary_id}"
                    )
                values.append(float(value))

            center = float(statistics.median(values))
            mad = float(
                statistics.median(abs(value - center) for value in values)
            )
            robust_scale = 1.4826 * mad
            scale = max(robust_scale, MINIMUM_SCALES[feature_name])

            feature_profile[feature_name] = {
                "center": round(center, 9),
                "mad": round(mad, 9),
                "scale": round(scale, 9),
                "minimum_scale": MINIMUM_SCALES[feature_name],
                "minimum": round(min(values), 9),
                "maximum": round(max(values), 9),
            }

        boards[secondary_id] = {
            "uid": expected_uid,
            "sample_count": len(board_rows),
            "sessions": sorted({row.get("session_id") for row in board_rows}),
            "versions": sorted({row.get("version") for row in board_rows}),
            "features": feature_profile,
        }

    return {
        "profile_schema_version": PROFILE_SCHEMA_VERSION,
        "profile_version": PROFILE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_sha256": source_sha256,
        "feature_names": list(PROFILE_FEATURES),
        "minimum_samples_per_board": minimum_samples,
        "boards": boards,
    }


def write_profile(path: Path, profile: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(profile, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_path, path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a robust normal profile from baseline JSONL."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--minimum-samples", type=int, default=20)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    registry = json.loads(args.registry.read_text(encoding="utf-8"))
    rows = read_jsonl(args.input)
    profile = build_profile(
        rows,
        registry,
        minimum_samples=args.minimum_samples,
        source_sha256=_source_sha256(args.input),
    )
    write_profile(args.output, profile)
    print(
        f"[AI Profile] created={args.output}, "
        f"boards={len(profile['boards'])}, rows={len(rows)}"
    )
    for secondary_id, board in profile["boards"].items():
        centers = {
            name: values["center"]
            for name, values in board["features"].items()
        }
        print(f"[AI Profile] {secondary_id}: {centers}")


if __name__ == "__main__":
    main()
