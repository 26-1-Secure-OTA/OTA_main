import json
import tempfile
import unittest
from pathlib import Path

from ai.adaptive_model import (
    is_adaptive_training_eligible,
    maybe_retrain,
)
from ai.model_loader import load_model
from ai.train_isolation_forest import save_artifacts, train


class AdaptiveModelTests(unittest.TestCase):
    def feature_values(self, index=0):
        return {
            "link_response_ms": 30.0 + index / 10,
            "supply_voltage_mv": 3295 + index % 3,
            "temperature_c": 38.0 + index / 20,
            "image_size_ratio": 0.55,
            "previous_failures": 0,
        }

    def baseline_row(self, board, index):
        return {
            "secondary_id": board,
            "ai_features": self.feature_values(index),
        }

    def experiment_row(self, board, index, **overrides):
        row = {
            "secondary_id": board,
            "attempt_id": f"campaign-{index}:{board}",
            "data_source": "BOARD",
            "scenario_id": "UNSPECIFIED",
            "board_scenario": "NORMAL",
            "policy_preflight_decision": "ALLOW",
            "update_attempted": True,
            "success": True,
            "telemetry_valid": True,
            "power_good": True,
            "health": "OK",
            "failure_reason_code": None,
            "ai_mode_used": "ISOLATION_FOREST",
            "ai_physical_anomaly_risk": 0.5,
            "features": self.feature_values(index),
        }
        row.update(overrides)
        return row

    @staticmethod
    def write_jsonl(path, rows):
        path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )

    def test_eligibility_rejects_fault_failure_and_simulator(self):
        normal = self.experiment_row("stm32-led-001", 1)
        self.assertTrue(is_adaptive_training_eligible(normal))
        self.assertFalse(is_adaptive_training_eligible(dict(normal, success=False)))
        self.assertFalse(is_adaptive_training_eligible(dict(normal, data_source="SIMULATOR")))
        self.assertFalse(is_adaptive_training_eligible(dict(normal, board_scenario="TEMP_HIGH_ALLOW")))

    def test_retrains_only_after_new_batch_and_updates_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "baseline.jsonl"
            experiment = root / "experiments.jsonl"
            model = root / "model.joblib"
            metadata = root / "metadata.json"
            baseline_rows = [
                self.baseline_row(f"stm32-led-00{board}", index)
                for board in (1, 2, 3)
                for index in range(4)
            ]
            self.write_jsonl(baseline, baseline_rows)
            pipeline, initial_metadata = train(baseline)
            save_artifacts(pipeline, initial_metadata, model, metadata)

            rows = [
                self.experiment_row(f"stm32-led-00{board}", board)
                for board in (1, 2)
            ]
            self.write_jsonl(experiment, rows)
            waiting = maybe_retrain(
                experiment_log_path=experiment,
                bootstrap_dataset_path=baseline,
                model_path=model,
                metadata_path=metadata,
                retrain_batch_rows=3,
                rows_per_board=10,
            )
            self.assertEqual(waiting["status"], "WAITING_FOR_BATCH")

            rows.append(self.experiment_row("stm32-led-003", 3))
            self.write_jsonl(experiment, rows)
            updated = maybe_retrain(
                experiment_log_path=experiment,
                bootstrap_dataset_path=baseline,
                model_path=model,
                metadata_path=metadata,
                retrain_batch_rows=3,
                rows_per_board=10,
            )
            self.assertEqual(updated["status"], "RETRAINED_FOR_NEXT_CAMPAIGN")
            _, loaded_metadata = load_model(
                model,
                metadata,
                expected_type="ISOLATION_FOREST",
                expected_schema_version=3,
            )
            self.assertEqual(
                loaded_metadata["adaptive_training"]["eligible_rows_seen"],
                3,
            )
            self.assertTrue((root / "model.previous.joblib").is_file())
            self.assertTrue((root / "metadata.previous.json").is_file())

            waiting_again = maybe_retrain(
                experiment_log_path=experiment,
                bootstrap_dataset_path=baseline,
                model_path=model,
                metadata_path=metadata,
                retrain_batch_rows=3,
                rows_per_board=10,
            )
            self.assertEqual(waiting_again["new_eligible_rows"], 0)


if __name__ == "__main__":
    unittest.main()
