import hashlib
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from ai.dataset_builder import DatasetValidationError, build_training_rows, group_split, read_jsonl, validate_log_row
from ai.feature_schema import FEATURE_NAMES, FeatureSchemaError, validate_feature_definition, validate_feature_values
from ai.model_loader import ModelValidationError, validate_metadata
from ai.scheduler import FIXED_ORDER, fixed_schedule, schedule_allow_ecus
from ecu.installer import Installer


def valid_row(**changes):
    row = {
        "scenario_id": "NORMAL-001", "secondary_id": "stm32-led-001",
        "policy_decision": "ALLOW", "update_attempted": True, "success": True,
        "telemetry_valid": True, "link_response_ms": 10.0,
        "previous_failures": 0, "supply_voltage_mv": 3300,
        "temperature_c": 30.0, "app_flash_free_ratio": 0.5,
        "recent_reset_count": 0,
    }
    row.update(changes)
    return row


class Predictor:
    def __init__(self, predictions=None, error=None, schema=1):
        self.predictions, self.error, self.schema = predictions, error, schema

    def predict(self, features):
        if self.error:
            raise self.error
        predictions = self.predictions or [
            {"secondary_id": secondary_id, "failure_risk": 0.2, "reasons": []}
            for secondary_id in features
        ]
        return {"model_version": "decision-tree-v1", "model_hash": "sha256:x", "feature_schema_version": self.schema, "predictions": predictions}


class DatasetTests(unittest.TestCase):
    def test_success_and_failure_labels(self):
        rows, rejected = build_training_rows([valid_row(), valid_row(success=False, scenario_id="TRANSFER_DROP-001")])
        self.assertFalse(rejected)
        self.assertEqual([row["failure_label"] for row in rows], [0, 1])

    def test_training_filters(self):
        changes = [
            {"policy_decision": "HOLD"}, {"policy_decision": "BLOCK"},
            {"update_attempted": False}, {"success": None},
            {"scenario_id": "UNSPECIFIED"}, {"telemetry_valid": False},
        ]
        for change in changes:
            with self.subTest(change=change):
                rows, _ = build_training_rows([valid_row(**change)])
                self.assertEqual(rows, [])

    def test_missing_feature_is_rejected(self):
        row = valid_row()
        del row["temperature_c"]
        rows, rejected = build_training_rows([row])
        self.assertEqual(rows, [])
        self.assertTrue(rejected)

    def test_nan_and_infinity_rejected(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_feature_values(valid_row(link_response_ms=value))

    def test_json_parser_rejects_non_finite_constants(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.jsonl"
            path.write_text(json.dumps(valid_row()) + "\n" + '{"x": NaN}\n', encoding="utf-8")
            rows, errors = read_jsonl(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(errors), 1)

    def test_leakage_and_feature_order_rejected(self):
        with self.assertRaises(FeatureSchemaError):
            validate_feature_definition((*FEATURE_NAMES, "success"))
        with self.assertRaises(FeatureSchemaError):
            validate_feature_definition(tuple(reversed(FEATURE_NAMES)))

    def test_group_split_has_no_overlap(self):
        rows = [
            {"scenario_id": f"NORMAL-{group:03d}", "failure_label": group % 2, "features": [0] * 6}
            for group in range(10) for _ in range(3)
        ]
        splits = group_split(rows)
        groups = [{row["scenario_id"] for row in splits[name]} for name in ("train", "validation", "test")]
        self.assertFalse(groups[0] & groups[1] or groups[0] & groups[2] or groups[1] & groups[2])

    def test_group_split_refuses_insufficient_groups(self):
        with self.assertRaises(DatasetValidationError):
            group_split([{"scenario_id": "NORMAL-001"}])

    def test_schema_and_types(self):
        with self.assertRaises(DatasetValidationError):
            validate_log_row(valid_row(update_attempted=1))
        with self.assertRaises(DatasetValidationError):
            validate_log_row(valid_row(app_flash_free_ratio=1.1))


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.features = {secondary_id: valid_row(secondary_id=secondary_id) for secondary_id in FIXED_ORDER}

    def schedule(self, predictor):
        return schedule_allow_ecus(self.features, set(FIXED_ORDER), predictor=predictor, requested="AI")

    def test_fixed_order_regression(self):
        self.assertEqual(fixed_schedule(reversed(FIXED_ORDER)), list(FIXED_ORDER))
        self.assertEqual(schedule_allow_ecus(self.features, FIXED_ORDER)["order"], list(FIXED_ORDER))

    def test_equal_risk_uses_fixed_tie_breaker(self):
        self.assertEqual(self.schedule(Predictor())["order"], list(FIXED_ORDER))

    def test_risk_sorting(self):
        predictions = [
            {"secondary_id": "stm32-led-001", "failure_risk": .8},
            {"secondary_id": "stm32-led-002", "failure_risk": .1},
            {"secondary_id": "stm32-led-003", "failure_risk": .4},
        ]
        self.assertEqual(self.schedule(Predictor(predictions))["order"], ["stm32-led-002", "stm32-led-003", "stm32-led-001"])

    def test_prediction_exception_fallback(self):
        result = self.schedule(Predictor(error=RuntimeError("boom")))
        self.assertEqual((result["scheduler_used"], result["fallback_reason"]), ("FIXED", "MODEL_EXCEPTION"))

    def test_hash_mismatch_fallback(self):
        result = self.schedule(Predictor(error=ValueError("MODEL_HASH_MISMATCH")))
        self.assertEqual(result["fallback_reason"], "MODEL_HASH_MISMATCH")

    def test_schema_mismatch_fallback(self):
        self.assertEqual(self.schedule(Predictor(schema=2))["fallback_reason"], "FEATURE_SCHEMA_MISMATCH")

    def test_invalid_risks(self):
        for risk in (math.nan, math.inf, -0.1, 1.1, "low"):
            predictions = [{"secondary_id": secondary_id, "failure_risk": risk if index == 0 else .2} for index, secondary_id in enumerate(FIXED_ORDER)]
            with self.subTest(risk=risk):
                self.assertEqual(self.schedule(Predictor(predictions))["fallback_reason"], "INVALID_RISK")

    def test_missing_duplicate_unknown_secondary(self):
        cases = [
            ([{"secondary_id": "stm32-led-001", "failure_risk": .1}], "SECONDARY_MISSING"),
            ([{"secondary_id": "stm32-led-001", "failure_risk": .1}] * 3, "SECONDARY_DUPLICATED"),
            ([{"secondary_id": "stm32-led-001", "failure_risk": .1}, {"secondary_id": "stm32-led-002", "failure_risk": .2}, {"secondary_id": "evil", "failure_risk": .3}], "UNKNOWN_SECONDARY"),
        ]
        for predictions, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(self.schedule(Predictor(predictions))["fallback_reason"], reason)

    def test_only_allow_set_is_preserved(self):
        features = {"stm32-led-002": self.features["stm32-led-002"]}
        result = schedule_allow_ecus(features, FIXED_ORDER, predictor=Predictor(), requested="AI")
        self.assertEqual(result["order"], ["stm32-led-002"])


class ModelMetadataTests(unittest.TestCase):
    def test_model_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "model.joblib"
            model.write_bytes(b"model")
            metadata = {
                "model_version": "decision-tree-v1", "model_hash": "sha256:bad",
                "feature_schema_version": 1, "feature_names": list(FEATURE_NAMES),
                "feature_order": list(FEATURE_NAMES),
                "feature_units": {"link_response_ms": "milliseconds", "previous_failures": "count", "supply_voltage_mv": "millivolts", "temperature_c": "Celsius", "app_flash_free_ratio": "ratio", "recent_reset_count": "count"},
                "training_timestamp": "2026-01-01T00:00:00Z",
            }
            with self.assertRaisesRegex(ModelValidationError, "MODEL_HASH_MISMATCH"):
                validate_metadata(metadata, model)


class PolicyRecheckTests(unittest.TestCase):
    def _status(self, **changes):
        status = {
            "ecu_serial": "stm32-led-001", "uid": "00112233445566778899AABB",
            "version": "1.0.0", "active_slot": "A", "target_slot": "B",
            "ready": True, "max_size": 49152, "uptime_ms": 100,
            "reset_cause": "POWER_ON", "uart_error_count": 0, "health": "OK",
            "link_response_ms": 10.0, "raw": "STATUS",
        }
        status.update(changes)
        return status

    def test_recheck_hold_skips_transfer(self):
        status = self._status(ready=False)

        class FakeSerial:
            sent = False
            def __init__(self, port): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def get_status(self): return status
            def send_firmware(self, **kwargs):
                FakeSerial.sent = True

        installer = Installer.__new__(Installer)
        installer.secondary_states = MagicMock()
        artifact = {"status": "OK", "file_type": "bin", "ecu_serial": "stm32-led-001", "target_slot": "B", "target_version": "1.9.0", "path": "/unused", "sha256": "0" * 64}
        with patch("ecu.secondary_serial.SecondarySerial", FakeSerial):
            result = installer._install_one_serial_firmware([artifact], "/dev/fake", "stm32-led-001", artifact_info=artifact, expected_uid=status["uid"])
        self.assertTrue(result["policy_recheck"])
        self.assertEqual(result["policy_recheck_decision"], "HOLD")
        self.assertFalse(FakeSerial.sent)


if __name__ == "__main__":
    unittest.main()
