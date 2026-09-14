from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Primary_ECU"))

from ecu.secondary_serial import SecondarySerial

FIRMWARE_ROOT = ROOT / "STM32_Workspace" / "firmware"
DIRECTOR_FIRMWARE_ROOT = (
    ROOT
    / "OTA_Director_Server"
    / "src_add"
    / "firmware_storage"
)


class Stm32CompatibilityTests(unittest.TestCase):
    def test_all_ecu_slot_images_match_primary_contract(self):
        for version in ("1.8.0", "2.1.0"):
            for suffix in ("001", "002", "003"):
                for slot in ("a", "b"):
                    path = (
                        FIRMWARE_ROOT
                        / version
                        / f"stm32-led-{suffix}_{version}_slot_{slot}.bin"
                    )
                    with self.subTest(version=version, ecu=suffix, slot=slot):
                        self.assertTrue(path.is_file(), path)
                        image = path.read_bytes()
                        self.assertLessEqual(len(image), 49152)
                        self.assertEqual(
                            SecondarySerial.detect_firmware_slot(str(path)),
                            slot.upper(),
                        )
                        self.assertIn(f"stm32-led-{suffix}".encode(), image)
                        self.assertIn(version.encode(), image)

                        if version == "2.1.0":
                            self.assertIn(b"VDD_MV=", image)
                            self.assertIn(b"TEMP_MC=", image)
                            self.assertIn(b"APP_FREE=", image)
                            self.assertIn(b"TELEMETRY_VALID=", image)

    def test_extended_board_status_is_accepted_by_primary(self):
        response = (
            "STATUS,stm32-led-001,UID=066CFF373956513043062025,"
            "VER=1.8.0,ACTIVE=B,TARGET=A,READY=1,MAX=49152,"
            "UPTIME_MS=1234,RESET=SOFTWARE,UART_ERR=0,HEALTH=OK,"
            "VDD_MV=3298,TEMP_MC=31420,APP_USED=11592,APP_FREE=37560,"
            "POWER_GOOD=1,TELEMETRY_VALID=1"
        )
        status = SecondarySerial.parse_status_response(response)
        self.assertEqual(status["version"], "1.8.0")
        self.assertEqual(status["active_slot"], "B")
        self.assertEqual(status["target_slot"], "A")
        self.assertEqual(status["health"], "OK")
        self.assertEqual(status["supply_voltage_mv"], 3298)
        self.assertEqual(status["temperature_c"], 31.42)
        self.assertEqual(status["app_flash_used_bytes"], 11592)
        self.assertEqual(status["app_flash_free_bytes"], 37560)
        self.assertAlmostEqual(status["app_flash_free_ratio"], 37560 / 49152)
        self.assertTrue(status["power_good"])
        self.assertTrue(status["telemetry_valid"])

    def test_fast_and_slow_director_images_match_primary_contract(self):
        profiles = {
            "fast version": "2.2.0",
            "slow version": "2.1.1",
        }

        for profile, version in profiles.items():
            for suffix in ("001", "002", "003"):
                for slot in ("a", "b"):
                    path = (
                        DIRECTOR_FIRMWARE_ROOT
                        / profile
                        / f"stm32-led-{suffix}_{version}_slot_{slot}.bin"
                    )
                    with self.subTest(
                        profile=profile,
                        ecu=suffix,
                        slot=slot,
                    ):
                        self.assertTrue(path.is_file(), path)
                        image = path.read_bytes()
                        self.assertLessEqual(len(image), 49152)
                        self.assertEqual(
                            SecondarySerial.detect_firmware_slot(str(path)),
                            slot.upper(),
                        )
                        self.assertIn(f"stm32-led-{suffix}".encode(), image)
                        self.assertIn(version.encode(), image)
                        self.assertIn(b"VDD_MV=", image)
                        self.assertIn(b"TEMP_MC=", image)
                        self.assertIn(b"APP_FREE=", image)
                        self.assertIn(b"TELEMETRY_VALID=", image)


if __name__ == "__main__":
    unittest.main()
