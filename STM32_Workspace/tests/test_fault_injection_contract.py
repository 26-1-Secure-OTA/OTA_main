from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = ROOT / "STM32_Workspace"


class FaultInjectionFirmwareContractTests(unittest.TestCase):
    def test_slot_firmwares_share_the_same_fault_contract(self):
        required = (
            "FAULT_SET,",
            "FAULT_ACK,%s",
            "RESET_CONTEXT=%s",
            "BOOT_ID=%u",
            "SCENARIO=%s",
            "ATTEMPT_ID=%08lX",
            "FW_RECEIVE_TIMEOUT_MS",
            "FW_INTERRUPTED",
            "FW_HASH_FAIL",
            "OTA_FAULT_RESET_DURING_TRANSFER",
            "OTA_FAULT_POST_REBOOT_HEALTH_FAIL",
        )
        for project in ("OTA_LED_A_TEST", "OTA_LED_B_TEST"):
            source = (WORKSPACE / project / "Src" / "main.c").read_text()
            with self.subTest(project=project):
                for token in required:
                    self.assertIn(token, source)
                receive_body = source.split(
                    "static void Receive_Firmware_Data(void)", 2
                )[-1]
                self.assertNotIn("HAL_MAX_DELAY", receive_body)

    def test_boot_failure_hook_falls_back_to_previous_valid_slot(self):
        source = (
            WORKSPACE / "OTA_BOOTLOADER" / "Src" / "main.c"
        ).read_text()
        self.assertIn("OTA_FAULT_BOOT_FAILED", source)
        self.assertIn("Jump_To_Application(SLOT_B_ADDRESS)", source)
        self.assertIn("Jump_To_Application(SLOT_A_ADDRESS)", source)


if __name__ == "__main__":
    unittest.main()
