from __future__ import annotations

import sys
import unittest
from pathlib import Path

PRIMARY_ROOT = Path(__file__).resolve().parents[1]
ECU_ROOT = PRIMARY_ROOT / "ecu"
for path in (PRIMARY_ROOT, ECU_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from experiments.scenario_runner import build_attempts
from experiments.validate_dataset import (
    DatasetValidationError,
    validate_rows,
)
from secondary_serial import (
    FAULT_SCENARIO_DEFAULTS,
    FaultProtocolError,
    SecondarySerial,
)


class FakeSerial:
    def __init__(self, responses):
        self.responses = [line.encode() + b"\n" for line in responses]
        self.writes = []
        self.timeout = 1.0

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def flush(self):
        pass

    def readline(self):
        return self.responses.pop(0) if self.responses else b""


class FaultProtocolTests(unittest.TestCase):
    def make_secondary(self, responses):
        secondary = object.__new__(SecondarySerial)
        secondary.ser = FakeSerial(responses)
        return secondary

    def test_extended_status_fields_are_backward_compatible(self):
        response = (
            "STATUS,stm32-led-001,UID=066CFF373956513043062025,"
            "VER=2.2.0,ACTIVE=A,TARGET=B,READY=1,MAX=49152,"
            "VDD_MV=3200,TEMP_MC=60000,APP_USED=12000,APP_FREE=37152,"
            "POWER_GOOD=1,TELEMETRY_VALID=1,UPTIME_MS=1234,"
            "RESET=SOFTWARE,RESET_CONTEXT=OTA_ACTIVATION,BOOT_ID=42,"
            "SCENARIO=POST_REBOOT_HEALTH_FAIL,ATTEMPT_ID=12ab34cd,"
            "UART_ERR=0,HEALTH=ERROR"
        )
        status = SecondarySerial.parse_status_response(response)
        self.assertEqual(status["scenario"], "POST_REBOOT_HEALTH_FAIL")
        self.assertEqual(status["boot_id"], 42)
        self.assertEqual(status["reset_context"], "OTA_ACTIVATION")
        self.assertEqual(status["attempt_id"], "12AB34CD")
        self.assertEqual(status["health"], "ERROR")

    def test_old_status_without_fault_fields_still_parses(self):
        status = SecondarySerial.parse_status_response(
            "STATUS,stm32-led-001,ACTIVE=A,TARGET=B,READY=1,MAX=49152"
        )
        self.assertIsNone(status["scenario"])
        self.assertIsNone(status["boot_id"])
        self.assertIsNone(status["reset_context"])
        self.assertIsNone(status["attempt_id"])

    def test_fault_set_uses_canonical_parameter_and_ack(self):
        secondary = self.make_secondary(["FAULT_ACK,TRANSFER_CORRUPTION"])
        result = secondary.set_fault(
            "transfer_corruption",
            attempt_id="12ab34cd",
        )
        self.assertEqual(result["parameter"], "BYTE_OFFSET")
        self.assertEqual(result["value"], 1024)
        self.assertEqual(
            secondary.ser.writes,
            [
                b"FAULT_SET,TRANSFER_CORRUPTION,BYTE_OFFSET,1024,"
                b"12AB34CD\n"
            ],
        )

    def test_fault_set_rejects_unknown_scenario_and_bad_attempt(self):
        secondary = self.make_secondary([])
        with self.assertRaises(FaultProtocolError):
            secondary.set_fault("NOT_REAL")
        with self.assertRaises(FaultProtocolError):
            secondary.set_fault("NORMAL", attempt_id="not-hex")
        with self.assertRaises(FaultProtocolError):
            secondary.set_fault("VOLTAGE_BLOCK", value=6000)

    def test_default_plan_is_20_campaigns_and_60_attempts(self):
        attempts = build_attempts(sorted(SecondarySerial.EXPECTED_SECONDARIES))
        self.assertEqual(len(attempts), 60)
        self.assertEqual(len({row["campaign_id"] for row in attempts}), 20)
        self.assertEqual(
            {row["scenario"] for row in attempts},
            set(FAULT_SCENARIO_DEFAULTS) - {"VOLTAGE_BLOCK", "TEMP_BLOCK"},
        )

    def test_dataset_validator_requires_allow_success_and_failure(self):
        rows = []
        for index in range(60):
            rows.append({
                "campaign_id": f"campaign-{index // 3}",
                "attempt_id": f"attempt-{index}",
                "secondary_id": f"stm32-led-{index % 3 + 1:03d}",
                "scenario_id": "NORMAL" if index < 30 else "TRANSFER_TIMEOUT",
                "policy_decision": "ALLOW",
                "update_attempted": True,
                "success": index < 30,
            })
        summary = validate_rows(rows)
        self.assertEqual(summary["allow_attempts"], 60)
        self.assertEqual(summary["successes"], 30)
        self.assertEqual(summary["failures"], 30)

        rows[0]["attempt_id"] = rows[1]["attempt_id"]
        with self.assertRaises(DatasetValidationError):
            validate_rows(rows)


if __name__ == "__main__":
    unittest.main()
