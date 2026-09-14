import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ecu.experiment_logger import ExperimentLogger
from ecu.failure_taxonomy import (
    classify_failure,
    label_for_outcome,
)
from ecu.feature_collector import collect_features
from ecu.installer import Installer
from ecu.reset_history import count_recent_unexpected_resets
from ecu.secondary_serial import SecondarySerial


class _StateStore:
    def __init__(self):
        self.events = []

    def transition(self, ecu_serial, state, **kwargs):
        self.events.append((ecu_serial, state, kwargs))


class TeamOneDataSafetyTests(unittest.TestCase):
    def setUp(self):
        self.uid = "00112233445566778899AABB"
        self.status = {
            "ecu_serial": "stm32-led-001",
            "uid": self.uid,
            "version": "1.0.0",
            "active_slot": "A",
            "target_slot": "B",
            "ready": True,
            "max_size": 49152,
            "uptime_ms": 1000,
            "reset_cause": "POWER_ON",
            "uart_error_count": 0,
            "health": "OK",
            "link_response_ms": 25.0,
            "supply_voltage_mv": 3300,
            "temperature_c": 39.0,
            "app_flash_free_ratio": 0.75,
            "power_good": True,
            "telemetry_valid": True,
            "raw": "STATUS,...",
        }
        self.artifact = {
            "ecu_serial": "stm32-led-001",
            "artifact": "stm32-led-001_2.0.0_slot_b.bin",
            "path": "/tmp/unused.bin",
            "file_type": "bin",
            "length": 12288,
            "sha256": "a" * 64,
            "target_slot": "B",
            "target_version": "2.0.0",
            "status": "OK",
        }

    def test_failure_taxonomy_and_labels(self):
        failure = classify_failure(
            "STM32 firmware verification failed: FW_HASH_FAIL",
            stage_hint="TRANSFER",
        )
        self.assertEqual(failure.stage, "VERIFY")
        self.assertEqual(failure.reason_code, "TRANSFER_CORRUPTION")
        self.assertTrue(failure.board_related)
        self.assertEqual(
            label_for_outcome(
                update_attempted=True,
                success=False,
                reason_code=failure.reason_code,
            ),
            (True, 1),
        )
        self.assertEqual(
            label_for_outcome(
                update_attempted=True,
                success=False,
                reason_code="PACKAGE_INVALID",
            ),
            (False, None),
        )

    def test_recent_ten_eligible_failures_and_image_ratio(self):
        rows = []
        for index in range(12):
            failed = index in {2, 8, 11}
            rows.append({
                "secondary_id": "stm32-led-001",
                "update_attempted": True,
                "success": not failed,
                "failure_reason_code": (
                    "TRANSFER_TIMEOUT" if failed else None
                ),
                "retry_count": 0,
                "uptime_ms": 1000 + index * 100,
            })
        rows.append({
            "secondary_id": "stm32-led-001",
            "update_attempted": True,
            "success": False,
            "failure_reason_code": "PACKAGE_INVALID",
            "retry_count": 0,
            "uptime_ms": 3000,
        })

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.jsonl"
            path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            features = collect_features(
                secondary_id="stm32-led-001",
                status=self.status,
                artifact_info=self.artifact,
                log_path=str(path),
            )

        self.assertEqual(features["history_attempt_count"], 10)
        self.assertEqual(features["previous_failures"], 3)
        self.assertEqual(features["previous_failure_rate"], 0.3)
        self.assertEqual(features["image_size_ratio"], 0.25)
        self.assertEqual(features["free_flash_ratio"], 0.75)

    def test_expected_ota_reset_is_excluded(self):
        rows = [{
            "update_attempted": True,
            "success": True,
            "slot_switched": True,
            "uptime_ms": 10000,
            "reset_cause": "SOFTWARE",
        }]
        current = {
            "uptime_ms": 100,
            "reset_cause": "SOFTWARE",
        }
        self.assertEqual(
            count_recent_unexpected_resets(
                rows,
                current_status=current,
            ),
            0,
        )

    def test_watchdog_reset_is_counted_and_boot_id_deduplicates(self):
        rows = [
            {
                "update_attempted": True,
                "success": False,
                "slot_switched": False,
                "boot_id": 1,
                "uptime_ms": 10000,
                "reset_cause": "POWER_ON",
            },
            {
                "boot_id": 2,
                "uptime_ms": 100,
                "reset_cause": "WATCHDOG",
            },
            {
                "boot_id": 2,
                "uptime_ms": 200,
                "reset_cause": "WATCHDOG",
            },
        ]
        self.assertEqual(
            count_recent_unexpected_resets(
                rows,
                current_status=None,
            ),
            1,
        )

    def test_manual_reset_is_distinguished_from_anomaly_count(self):
        rows = [{
            "boot_id": 10,
            "uptime_ms": 10000,
            "reset_cause": "POWER_ON",
        }]
        current = {
            "boot_id": 11,
            "uptime_ms": 100,
            "reset_cause": "PIN_RESET",
            "reset_context": "MANUAL_RESET",
        }
        self.assertEqual(
            count_recent_unexpected_resets(
                rows,
                current_status=current,
            ),
            0,
        )

    def test_experiment_logger_adds_schema_v2(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "experiments.jsonl"
            ExperimentLogger(str(path)).append({"secondary_id": "ecu"})
            row = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(row["log_schema_version"], 2)
        self.assertIn("collected_at", row)

    def test_status_parser_accepts_optional_reset_identity(self):
        response = (
            "STATUS,stm32-led-001,ACTIVE=A,TARGET=B,READY=1,MAX=49152,"
            "UID=00112233445566778899AABB,VER=1.0.0,UPTIME_MS=100,"
            "RESET=WATCHDOG,BOOT_ID=7,RESET_CONTEXT=UNEXPECTED,UART_ERR=0,"
            "HEALTH=OK\r\n"
        )
        status = SecondarySerial.parse_status_response(response)

        self.assertEqual(status["boot_id"], 7)
        self.assertEqual(status["reset_context"], "UNEXPECTED")

    def test_fresh_status_policy_hold_prevents_transfer(self):
        fresh_status = dict(self.status, ready=False)
        send_calls = []

        class FakeSecondary:
            def __init__(self, port):
                self.port = port

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def get_status(self, timeout_seconds=5.0):
                return fresh_status

            def send_firmware(self, **kwargs):
                send_calls.append(kwargs)
                raise AssertionError("transfer must not start")

        installer = object.__new__(Installer)
        installer.secondary_states = _StateStore()

        with patch(
            "ecu.secondary_serial.SecondarySerial",
            FakeSecondary,
        ):
            result = installer._install_one_serial_firmware(
                downloaded_results=[self.artifact],
                port="/dev/fake",
                expected_ecu="stm32-led-001",
                expected_uid=self.uid,
                policy_artifact_info=self.artifact,
            )

        self.assertTrue(result["skipped"])
        self.assertFalse(result["update_attempted"])
        self.assertEqual(result["decision"], "HOLD")
        self.assertEqual(result["reason_code"], "SECONDARY_NOT_READY")
        self.assertEqual(send_calls, [])


if __name__ == "__main__":
    unittest.main()
