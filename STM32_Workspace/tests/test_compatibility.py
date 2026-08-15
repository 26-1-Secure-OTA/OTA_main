from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Primary_ECU"))

from ecu.secondary_serial import SecondarySerial

FIRMWARE = ROOT / "STM32_Workspace" / "firmware" / "1.8.0"


class Stm32CompatibilityTests(unittest.TestCase):
    def test_all_ecu_slot_images_match_primary_contract(self):
        for suffix in ("001", "002", "003"):
            for slot in ("a", "b"):
                path = FIRMWARE / f"stm32-led-{suffix}_1.8.0_slot_{slot}.bin"
                with self.subTest(ecu=suffix, slot=slot):
                    self.assertTrue(path.is_file(), path)
                    image = path.read_bytes()
                    self.assertLessEqual(len(image), 49152)
                    self.assertEqual(
                        SecondarySerial.detect_firmware_slot(str(path)),
                        slot.upper(),
                    )
                    self.assertIn(f"stm32-led-{suffix}".encode(), image)
                    self.assertIn(b"1.8.0", image)

    def test_extended_board_status_is_accepted_by_primary(self):
        response = (
            "STATUS,stm32-led-001,UID=066CFF373956513043062025,"
            "VER=1.8.0,ACTIVE=B,TARGET=A,READY=1,MAX=49152,"
            "UPTIME_MS=1234,RESET=SOFTWARE,UART_ERR=0,HEALTH=OK"
        )
        status = SecondarySerial.parse_status_response(response)
        self.assertEqual(status["version"], "1.8.0")
        self.assertEqual(status["active_slot"], "B")
        self.assertEqual(status["target_slot"], "A")
        self.assertEqual(status["health"], "OK")


if __name__ == "__main__":
    unittest.main()
