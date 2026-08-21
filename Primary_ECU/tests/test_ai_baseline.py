import tempfile
import unittest
from pathlib import Path

from ai.baseline_collector import (
    BaselineCollectionError,
    build_cycle_rows,
    validate_discovered,
)
from ecu.feature_collector import collect_features
from ecu.secondary_serial import FirmwareTransferError, SecondarySerial


class AiBaselineTests(unittest.TestCase):
    def make_status(self, response, voltage, temperature):
        return {
            "ecu_serial": "stm32-led-001",
            "uid": "00112233445566778899AABB",
            "version": "2.2.0",
            "active_slot": "A",
            "target_slot": "B",
            "ready": True,
            "max_size": 49152,
            "uptime_ms": 1000,
            "reset_cause": "POWER_ON",
            "uart_error_count": 0,
            "health": "OK",
            "link_response_ms": response,
            "supply_voltage_mv": voltage,
            "temperature_c": temperature,
            "app_flash_free_ratio": 0.75,
            "power_good": True,
            "telemetry_valid": True,
        }

    def sample(self, statuses):
        secondary = object.__new__(SecondarySerial)
        pending = iter(statuses)
        secondary.get_status = lambda timeout_seconds: next(pending)
        return secondary.get_status_samples(
            sample_count=len(statuses),
            interval_seconds=0,
        )

    def test_five_status_samples_keep_latest_and_add_medians(self):
        statuses = [
            self.make_status(response, voltage, temperature)
            for response, voltage, temperature in (
                (30.0, 3290, 39.0),
                (10.0, 3300, 37.0),
                (20.0, 3298, 38.0),
                (50.0, 3296, 36.0),
                (40.0, 3294, 35.0),
            )
        ]

        result = self.sample(statuses)
        status = result["status"]

        self.assertEqual(status["link_response_ms"], 40.0)
        self.assertEqual(status["link_response_median_ms"], 30.0)
        self.assertEqual(status["supply_voltage_median_mv"], 3296)
        self.assertEqual(status["temperature_median_c"], 37.0)
        self.assertEqual(status["status_sample_count"], 5)
        self.assertTrue(status["telemetry_valid_all"])

        features = collect_features(
            secondary_id="stm32-led-001",
            status=status,
            artifact_info={},
        )
        self.assertEqual(features["link_response_ms"], 30.0)
        self.assertEqual(features["supply_voltage_mv"], 3296)
        self.assertEqual(features["temperature_c"], 37.0)

    def test_sampling_rejects_board_identity_change(self):
        first = self.make_status(10.0, 3300, 37.0)
        second = self.make_status(11.0, 3301, 37.1)
        second["uid"] = "FFEEDDCCBBAA998877665544"

        with self.assertRaises(FirmwareTransferError):
            self.sample([first, second])

    def test_registry_requires_all_boards_and_exact_uid(self):
        registry = {
            "stm32-led-001": {"uid": "00112233445566778899AABB"},
            "stm32-led-002": {"uid": "112233445566778899AABBCC"},
        }
        incomplete = {
            "stm32-led-001": {"status": self.make_status(10, 3300, 37)},
        }

        with self.assertRaises(BaselineCollectionError):
            validate_discovered(incomplete, registry)

    def test_cycle_writes_five_raw_rows_and_one_aggregate_row(self):
        statuses = [
            self.make_status(10 + index, 3295 + index, 36 + index / 10)
            for index in range(5)
        ]
        sample_result = self.sample(statuses)
        registry = {
            "stm32-led-001": {"uid": "00112233445566778899AABB"},
        }
        discovered = {
            "stm32-led-001": {
                "port": "/dev/ttyACM0",
                "status": sample_result["status"],
                "status_samples": sample_result["samples"],
            }
        }

        with tempfile.TemporaryDirectory() as directory:
            raw_rows, aggregate_rows = build_cycle_rows(
                discovered=discovered,
                registry=registry,
                session_id="NORMAL_BASELINE-TEST-001",
                cycle_index=1,
                history_log=str(Path(directory) / "missing.jsonl"),
            )

        self.assertEqual(len(raw_rows), 5)
        self.assertEqual(len(aggregate_rows), 1)
        self.assertEqual(
            set(aggregate_rows[0]["ai_features"]),
            {
                "link_response_ms",
                "previous_failures",
                "supply_voltage_mv",
                "temperature_c",
                "app_flash_free_ratio",
                "recent_reset_count",
            },
        )


if __name__ == "__main__":
    unittest.main()
