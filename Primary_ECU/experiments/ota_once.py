"""Run one local, verified same-version OTA attempt for hardware experiments."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys

PRIMARY_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PRIMARY_ROOT.parent
DEFAULT_FIRMWARE_ROOT = REPOSITORY_ROOT / "STM32_Workspace" / "firmware"
if str(PRIMARY_ROOT) not in sys.path:
    sys.path.insert(0, str(PRIMARY_ROOT))

from ecu.installer import Installer  # noqa: E402
from ecu.secondary_serial import SecondarySerial  # noqa: E402
from ecu.storage import Storage  # noqa: E402


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    secondary_id = os.environ.get("OTA_SECONDARY_ID", "").strip()
    if secondary_id not in SecondarySerial.EXPECTED_SECONDARIES:
        raise SystemExit("OTA_SECONDARY_ID must identify one expected STM32")

    discovered = SecondarySerial.discover_secondaries(status_sample_count=5)
    if secondary_id not in discovered:
        raise SystemExit(f"board not discovered: {secondary_id}")

    status = discovered[secondary_id]["status"]
    version = status.get("version")
    target_slot = str(status.get("target_slot") or "").lower()
    firmware_root = Path(
        os.environ.get("OTA_EXPERIMENT_FIRMWARE_ROOT", DEFAULT_FIRMWARE_ROOT)
    )
    firmware = (
        firmware_root
        / str(version)
        / f"{secondary_id}_{version}_slot_{target_slot}.bin"
    )
    if not firmware.is_file():
        raise SystemExit(f"experiment firmware not found: {firmware}")
    if SecondarySerial.detect_firmware_slot(firmware) != target_slot.upper():
        raise SystemExit(f"firmware slot mismatch: {firmware}")

    artifact = {
        "ecu_serial": secondary_id,
        "artifact": firmware.name,
        "path": str(firmware),
        "file_type": "bin",
        "length": firmware.stat().st_size,
        "sha256": sha256_file(firmware),
        "target_slot": target_slot.upper(),
        "target_version": version,
        "status": "OK",
    }

    os.environ["OTA_EXPERIMENT_FORCE_REINSTALL"] = "1"
    os.environ.setdefault("OTA_AI_MODE", "OFF")
    installer = Installer(Storage(base="/tmp/ota_experiment_state"))
    result = installer.install_serial_firmware(
        [artifact],
        expected_secondary_statuses={secondary_id: status},
    )
    board_result = next(
        (
            row
            for row in result.get("results", [])
            if row.get("ecu_serial") == secondary_id
        ),
        {},
    )
    output = {
        "preflight_decision": board_result.get("decision"),
        "preflight_reason_code": board_result.get("reason_code"),
        "ok": board_result.get("ok", False),
        "success": board_result.get("success"),
        "update_attempted": board_result.get("update_attempted", False),
        "failure_stage": board_result.get("failure_stage"),
        "failure_reason_code": board_result.get("failure_reason_code"),
        "active_slot_before": status.get("active_slot"),
        "active_slot_after": (
            (board_result.get("secondary_after") or {}).get("active_slot")
        ),
    }
    print(json.dumps(output, sort_keys=True))
    return 0 if output["preflight_decision"] == "ALLOW" else 2


if __name__ == "__main__":
    raise SystemExit(main())
