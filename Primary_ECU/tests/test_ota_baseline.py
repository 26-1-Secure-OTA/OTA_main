import json
import tempfile
import unittest
from pathlib import Path

from ecu.feature_collector import (
    FEATURE_NAMES,
    FeatureCollectionError,
    collect_features,
)
from ecu.installer import Installer
from ecu.safety_policy import evaluate_policy
from ecu.secondary_serial import (
    FirmwareTransferError,
    SecondarySerial,
)


class OtaBaselineTests(unittest.TestCase):
    def setUp(self):
        self.status = {
            "ecu_serial": "stm32-led-001",
            "uid": "00112233445566778899AABB",
            "version": "1.0.1",
            "active_slot": "A",
            "target_slot": "B",
            "ready": True,
            "max_size": 49152,
            "uptime_ms": 300,
            "reset_cause": "POWER_ON",
            "uart_error_count": 0,
            "health": "OK",
            "link_response_ms": 12.5,
        }
        self.artifact = {
            "status": "OK",
            "target_slot": "B",
            "target_version": "1.9.0",
            "length": 12288,
        }

    def evaluate(self, **changes):
        status = dict(self.status)
        artifact = dict(self.artifact)
        status.update(changes.pop("status", {}))
        artifact.update(changes.pop("artifact", {}))
        return evaluate_policy(
            expected_ecu="stm32-led-001",
            status=status,
            artifact_info=artifact,
            expected_uid=self.status["uid"],
            **changes,
        )

    def test_target_slot_accepts_current_filename_format(self):
        for slot in ("a", "b"):
            item = {
                "images": {
                    "image_name": f"stm32-led-001_1.9.0_slot_{slot}",
                    "image_info": {
                        "custom": {"target_slot": slot.upper()}
                    },
                }
            }
            self.assertEqual(
                Installer._target_slot_from_update(item),
                slot.upper(),
            )

    def test_parses_extended_stm32_status(self):
        response = (
            "STATUS,stm32-led-001,"
            "UID=12345678ABCDEF0011223344,"
            "VER=1.0.1,ACTIVE=A,TARGET=B,"
            "READY=1,MAX=49152,UPTIME_MS=125340,"
            "RESET=POWER_ON,UART_ERR=0,HEALTH=OK\r\n"
        )

        status = SecondarySerial.parse_status_response(response)

        self.assertEqual(status["ecu_serial"], "stm32-led-001")
        self.assertEqual(status["uid"], "12345678ABCDEF0011223344")
        self.assertEqual(status["version"], "1.0.1")
        self.assertEqual(status["active_slot"], "A")
        self.assertEqual(status["target_slot"], "B")
        self.assertTrue(status["ready"])
        self.assertEqual(status["max_size"], 49152)
        self.assertEqual(status["uptime_ms"], 125340)
        self.assertEqual(status["reset_cause"], "POWER_ON")
        self.assertEqual(status["uart_error_count"], 0)
        self.assertEqual(status["health"], "OK")

    def test_rejects_duplicate_or_empty_status_fields(self):
        invalid_responses = (
            (
                "STATUS,stm32-led-001,ACTIVE=A,ACTIVE=B,"
                "TARGET=B,READY=1,MAX=49152"
            ),
            (
                "STATUS,stm32-led-001,UID=,ACTIVE=A,"
                "TARGET=B,READY=1,MAX=49152"
            ),
        )

        for response in invalid_responses:
            with self.subTest(response=response), self.assertRaises(
                FirmwareTransferError
            ):
                SecondarySerial.parse_status_response(response)

    def test_target_slot_rejects_metadata_name_mismatch(self):
        item = {
            "images": {
                "image_name": "stm32-led-001_1.9.0_slot_a",
                "image_info": {"custom": {"target_slot": "B"}},
            }
        }
        with self.assertRaises(RuntimeError):
            Installer._target_slot_from_update(item)

    def test_policy_blocks_invalid_target_version(self):
        for version in ("v1.9.0", "1.9", "abc", "1.2.x"):
            decision = self.evaluate(
                artifact={"target_version": version}
            )
            self.assertEqual(
                decision["reason_code"],
                "INVALID_TARGET_VERSION",
            )

    def test_policy_blocks_invalid_current_version(self):
        decision = self.evaluate(status={"version": "1.2"})
        self.assertEqual(
            decision["reason_code"],
            "INVALID_CURRENT_VERSION",
        )

    def test_policy_preserves_upgrade_and_downgrade_rules(self):
        self.assertEqual(self.evaluate()["decision"], "ALLOW")
        for version in ("1.9.0", "2.0.3"):
            decision = self.evaluate(status={"version": version})
            self.assertEqual(decision["reason_code"], "DOWNGRADE_ATTEMPT")

    def test_collects_exact_pre_update_features_and_history(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "experiments.jsonl"
            rows = [
                {
                    "secondary_id": "stm32-led-001",
                    "update_attempted": True,
                    "success": False,
                    "retry_count": 1,
                    "uptime_ms": 1000,
                },
                {
                    "secondary_id": "stm32-led-001",
                    "update_attempted": True,
                    "success": True,
                    "retry_count": 0,
                    "uptime_ms": 200,
                },
            ]
            with log_path.open("w", encoding="utf-8") as log:
                for row in rows:
                    log.write(json.dumps(row) + "\n")
                log.write("{broken json\n")

            features = collect_features(
                secondary_id="stm32-led-001",
                status=self.status,
                artifact_info=self.artifact,
                power_percent=85.0,
                temperature_c=31.5,
                log_path=str(log_path),
            )

        self.assertEqual(set(features), set(FEATURE_NAMES))
        self.assertEqual(features["image_size"], 12288)
        self.assertEqual(features["free_flash_ratio"], 0.75)
        self.assertEqual(features["recent_retry_rate"], 0.5)
        self.assertEqual(features["previous_failures"], 1)
        self.assertEqual(features["recent_reset_count"], 1)
        self.assertEqual(features["link_response_ms"], 12.5)

    def test_feature_validation(self):
        invalid_cases = (
            {"power_percent": 101},
            {"temperature_c": "hot"},
            {"status": {"link_response_ms": -1}},
            {"artifact": {"length": 0}},
            {"artifact": {"length": 49153}},
        )

        for case in invalid_cases:
            status = dict(self.status)
            artifact = dict(self.artifact)
            status.update(case.get("status", {}))
            artifact.update(case.get("artifact", {}))
            with self.subTest(case=case), self.assertRaises(
                FeatureCollectionError
            ):
                collect_features(
                    secondary_id="stm32-led-001",
                    status=status,
                    artifact_info=artifact,
                    power_percent=case.get("power_percent", 85.0),
                    temperature_c=case.get("temperature_c", 31.5),
                )


if __name__ == "__main__":
    unittest.main()
