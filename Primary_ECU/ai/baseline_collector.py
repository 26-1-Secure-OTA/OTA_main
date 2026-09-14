"""Collect normal STM32 STATUS data without performing an OTA update."""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ecu.feature_collector import collect_features
from ecu.secondary_serial import SecondarySerial


PRIMARY_ECU_DIR = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = PRIMARY_ECU_DIR / "config" / "secondary_registry.json"
DEFAULT_HISTORY_LOG = PRIMARY_ECU_DIR / "logs" / "ota_experiments.jsonl"
DEFAULT_OUTPUT_DIR = PRIMARY_ECU_DIR / "data" / "ai_baseline"

AI_FEATURE_NAMES = (
    "link_response_ms",
    "previous_failures",
    "supply_voltage_mv",
    "temperature_c",
    "app_flash_free_ratio",
    "recent_reset_count",
)


class BaselineCollectionError(RuntimeError):
    """Raised when a complete and trustworthy baseline cycle cannot be made."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_registry(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as registry_file:
        registry = json.load(registry_file)

    if not isinstance(registry, dict) or not registry:
        raise BaselineCollectionError("secondary registry must be non-empty")

    for secondary_id, entry in registry.items():
        if (
            not isinstance(secondary_id, str)
            or not isinstance(entry, dict)
            or not isinstance(entry.get("uid"), str)
        ):
            raise BaselineCollectionError(
                f"invalid registry entry: {secondary_id!r}"
            )

    return registry


def validate_discovered(discovered: dict, registry: dict) -> None:
    expected_ids = set(registry)
    discovered_ids = set(discovered)

    if discovered_ids != expected_ids:
        raise BaselineCollectionError(
            "all registered boards must be connected: "
            f"missing={sorted(expected_ids - discovered_ids)}, "
            f"unexpected={sorted(discovered_ids - expected_ids)}"
        )

    for secondary_id in sorted(expected_ids):
        status = discovered[secondary_id].get("status", {})
        expected_uid = registry[secondary_id]["uid"].upper()
        observed_uid = str(status.get("uid") or "").upper()

        if observed_uid != expected_uid:
            raise BaselineCollectionError(
                f"UID mismatch for {secondary_id}: "
                f"expected={expected_uid}, observed={observed_uid or None}"
            )


def build_cycle_rows(
    *,
    discovered: dict,
    registry: dict,
    session_id: str,
    cycle_index: int,
    history_log: str,
) -> tuple[list[dict], list[dict]]:
    validate_discovered(discovered, registry)
    collected_at = _utc_now()
    raw_rows = []
    aggregate_rows = []

    for secondary_id in sorted(registry):
        secondary_info = discovered[secondary_id]
        status = secondary_info["status"]
        samples = secondary_info.get("status_samples") or []

        if len(samples) != status.get("status_sample_count"):
            raise BaselineCollectionError(
                f"incomplete STATUS samples for {secondary_id}: "
                f"samples={len(samples)}, "
                f"reported={status.get('status_sample_count')}"
            )

        features = collect_features(
            secondary_id=secondary_id,
            status=status,
            artifact_info={},
            log_path=history_log,
        )

        ai_features = {
            name: features[name]
            for name in AI_FEATURE_NAMES
        }

        common = {
            "baseline_schema_version": 1,
            "session_id": session_id,
            "cycle_index": cycle_index,
            "collected_at": collected_at,
            "secondary_id": secondary_id,
            "uid": status.get("uid"),
            "port": secondary_info.get("port"),
            "version": status.get("version"),
            "active_slot": status.get("active_slot"),
        }

        for sample_index, sample in enumerate(samples, start=1):
            raw_rows.append({
                **common,
                "sample_index": sample_index,
                "link_response_ms": sample.get("link_response_ms"),
                "supply_voltage_mv": sample.get("supply_voltage_mv"),
                "temperature_c": sample.get("temperature_c"),
                "app_flash_free_ratio": sample.get(
                    "app_flash_free_ratio"
                ),
                "uptime_ms": sample.get("uptime_ms"),
                "reset_cause": sample.get("reset_cause"),
                "power_good": sample.get("power_good"),
                "telemetry_valid": sample.get("telemetry_valid"),
            })

        aggregate_rows.append({
            **common,
            "status_sample_count": len(samples),
            "ai_features": ai_features,
            "power_good": status.get("power_good"),
            "telemetry_valid": status.get("telemetry_valid"),
            "telemetry_valid_all": status.get("telemetry_valid_all"),
            "status_sample_values": status.get("status_sample_values"),
        })

    return raw_rows, aggregate_rows


def append_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("a", encoding="utf-8") as output:
        for row in rows:
            output.write(
                json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            )
        output.flush()
        os.fsync(output.fileno())


def collect_baseline(
    *,
    cycles: int,
    cycle_interval_seconds: float,
    status_sample_count: int,
    status_sample_interval: float,
    registry_path: Path,
    history_log: str,
    output_dir: Path,
) -> tuple[Path, Path, str]:
    if cycles <= 0:
        raise ValueError("cycles must be positive")
    if cycle_interval_seconds < 0:
        raise ValueError("cycle_interval_seconds must be non-negative")

    registry = load_registry(registry_path)
    session_id = (
        datetime.now(timezone.utc).strftime("NORMAL_BASELINE-%Y%m%dT%H%M%SZ-")
        + uuid.uuid4().hex[:8].upper()
    )
    raw_path = output_dir / "normal_status_raw.jsonl"
    aggregate_path = output_dir / "normal_status_aggregated.jsonl"

    for cycle_index in range(1, cycles + 1):
        print(f"[AI Baseline] cycle {cycle_index}/{cycles}")
        discovered = SecondarySerial.discover_secondaries(
            status_sample_count=status_sample_count,
            status_sample_interval=status_sample_interval,
        )
        raw_rows, aggregate_rows = build_cycle_rows(
            discovered=discovered,
            registry=registry,
            session_id=session_id,
            cycle_index=cycle_index,
            history_log=history_log,
        )
        append_jsonl(raw_path, raw_rows)
        append_jsonl(aggregate_path, aggregate_rows)

        for row in aggregate_rows:
            features = row["ai_features"]
            print(
                "[AI Baseline] "
                f"{row['secondary_id']} "
                f"response={features['link_response_ms']}ms "
                f"vdd={features['supply_voltage_mv']}mV "
                f"temp={features['temperature_c']}C "
                f"flash_free={features['app_flash_free_ratio']}"
            )

        if cycle_index < cycles and cycle_interval_seconds > 0:
            time.sleep(cycle_interval_seconds)

    return raw_path, aggregate_path, session_id


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Collect repeated normal STATUS measurements from all three "
            "registered STM32 boards. No firmware update is performed."
        )
    )
    parser.add_argument("--cycles", type=int, default=30)
    parser.add_argument("--cycle-interval", type=float, default=10.0)
    parser.add_argument("--status-samples", type=int, default=5)
    parser.add_argument("--status-interval", type=float, default=0.05)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--history-log", default=str(DEFAULT_HISTORY_LOG))
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    raw_path, aggregate_path, session_id = collect_baseline(
        cycles=args.cycles,
        cycle_interval_seconds=args.cycle_interval,
        status_sample_count=args.status_samples,
        status_sample_interval=args.status_interval,
        registry_path=args.registry,
        history_log=args.history_log,
        output_dir=args.output_dir,
    )
    print(f"[AI Baseline] complete: session={session_id}")
    print(f"[AI Baseline] raw={raw_path}")
    print(f"[AI Baseline] aggregated={aggregate_path}")


if __name__ == "__main__":
    main()
