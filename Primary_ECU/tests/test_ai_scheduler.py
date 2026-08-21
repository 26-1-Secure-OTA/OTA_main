import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai.anomaly_scorer import schedule_allow_ecus, score_secondary
from ai.normal_profile import build_profile, write_profile
from ecu.installer import Installer
from ecu.secondary_serial import SecondarySerial


class AiSchedulerTests(unittest.TestCase):
    registry = {
        "stm32-led-001": {"uid": "00112233445566778899AABB"},
        "stm32-led-002": {"uid": "112233445566778899AABBCC"},
        "stm32-led-003": {"uid": "2233445566778899AABBCCDD"},
    }

    def baseline_rows(self):
        rows = []
        for secondary_id, entry in self.registry.items():
            for index in range(20):
                rows.append({
                    "secondary_id": secondary_id,
                    "uid": entry["uid"],
                    "session_id": "NORMAL-001",
                    "version": "2.1.1",
                    "ai_features": {
                        "link_response_ms": 25.0 + (index % 3) * 0.1,
                        "supply_voltage_mv": 3300 + index % 2,
                        "temperature_c": 39.0 + (index % 4) * 0.1,
                        "app_flash_free_ratio": 0.76,
                    },
                })
        return rows

    def context(self, response_by_board=None):
        response_by_board = response_by_board or {}
        return {
            secondary_id: {
                "status": {
                    "uid": entry["uid"],
                    "telemetry_valid": True,
                    "telemetry_valid_all": True,
                },
                "features": {
                    "link_response_ms": response_by_board.get(
                        secondary_id, 25.1
                    ),
                    "supply_voltage_mv": 3300,
                    "temperature_c": 39.1,
                    "app_flash_free_ratio": 0.76,
                    "previous_failures": 0,
                    "recent_reset_count": 0,
                },
            }
            for secondary_id, entry in self.registry.items()
        }

    def make_profile(self, directory):
        profile = build_profile(self.baseline_rows(), self.registry)
        path = Path(directory) / "normal-profile.json"
        write_profile(path, profile)
        return profile, path

    def test_high_deviation_has_higher_risk(self):
        profile = build_profile(self.baseline_rows(), self.registry)
        normal = score_secondary(
            secondary_id="stm32-led-001",
            uid=self.registry["stm32-led-001"]["uid"],
            features=self.context()["stm32-led-001"]["features"],
            profile=profile,
        )
        delayed_features = dict(
            self.context()["stm32-led-001"]["features"]
        )
        delayed_features["link_response_ms"] = 100.0
        delayed = score_secondary(
            secondary_id="stm32-led-001",
            uid=self.registry["stm32-led-001"]["uid"],
            features=delayed_features,
            profile=profile,
        )
        self.assertGreater(delayed["risk_score"], normal["risk_score"])

    def test_active_mode_orders_low_risk_first(self):
        with tempfile.TemporaryDirectory() as directory:
            _, path = self.make_profile(directory)
            result = schedule_allow_ecus(
                allow_context=self.context({
                    "stm32-led-001": 100.0,
                    "stm32-led-002": 25.2,
                    "stm32-led-003": 26.0,
                }),
                profile_path=path,
                requested_mode="ACTIVE",
            )

        self.assertEqual(result["used_mode"], "ACTIVE")
        self.assertEqual(
            result["execution_order"],
            ["stm32-led-002", "stm32-led-003", "stm32-led-001"],
        )

    def test_shadow_recommends_but_keeps_fixed_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            _, path = self.make_profile(directory)
            result = schedule_allow_ecus(
                allow_context=self.context({"stm32-led-001": 100.0}),
                profile_path=path,
                requested_mode="SHADOW",
            )

        self.assertNotEqual(
            result["recommended_order"], result["execution_order"]
        )
        self.assertEqual(
            result["execution_order"],
            ["stm32-led-001", "stm32-led-002", "stm32-led-003"],
        )

    def test_missing_profile_falls_back_to_fixed_order(self):
        result = schedule_allow_ecus(
            allow_context=self.context(),
            profile_path="/definitely/missing/profile.json",
            requested_mode="ACTIVE",
        )
        self.assertEqual(result["used_mode"], "OFF")
        self.assertEqual(result["fallback_reason"], "PROFILE_NOT_FOUND")
        self.assertEqual(
            result["execution_order"],
            ["stm32-led-001", "stm32-led-002", "stm32-led-003"],
        )

    def test_only_allow_context_is_ranked(self):
        with tempfile.TemporaryDirectory() as directory:
            _, path = self.make_profile(directory)
            allow_context = self.context()
            del allow_context["stm32-led-002"]
            result = schedule_allow_ecus(
                allow_context=allow_context,
                profile_path=path,
                requested_mode="ACTIVE",
            )

        self.assertNotIn("stm32-led-002", result["execution_order"])
        self.assertNotIn("stm32-led-002", result["scores"])

    def test_installer_ai_symbols_are_available_at_runtime(self):
        installer = object.__new__(Installer)
        with patch.object(
            SecondarySerial,
            "discover_secondaries",
            return_value={},
        ):
            result = installer.install_serial_firmware([])

        self.assertTrue(result["skipped"])
        self.assertEqual(result["results"], [])


if __name__ == "__main__":
    unittest.main()
