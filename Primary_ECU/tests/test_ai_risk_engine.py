import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import joblib
import numpy as np

from ai.anomaly_scorer import score_secondary
from ai.dataset_loader import DatasetError, load_jsonl
from ai.isolation_forest_engine import IsolationForestEngine
from ai.model_loader import ModelLoadError, load_model
from ai.normal_profile import build_profile, write_profile
from ai.risk_engine import FIXED_ORDER, RiskEngine
from ai.statistical_engine import StatisticalEngine
from ai.train_isolation_forest import save_artifacts, train


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "data" / "ai_baseline" / "run30" / "normal_status_aggregated.jsonl"
MODEL = ROOT / "models" / "isolation-forest-v1.joblib"
METADATA = ROOT / "models" / "isolation-forest-v1.metadata.json"


class AiRiskEngineTests(unittest.TestCase):
    registry = {
        "stm32-led-001": {"uid": "00112233445566778899AABB"},
        "stm32-led-002": {"uid": "112233445566778899AABBCC"},
        "stm32-led-003": {"uid": "2233445566778899AABBCCDD"},
    }

    def baseline_rows(self):
        return [{
            "secondary_id": ecu, "uid": entry["uid"], "session_id": "s", "version": "1",
            "ai_features": {"link_response_ms": 25 + i % 3 * .1, "supply_voltage_mv": 3300 + i % 2,
                            "temperature_c": 39 + i % 4 * .1, "app_flash_free_ratio": .76},
        } for ecu, entry in self.registry.items() for i in range(20)]

    def context(self):
        return {ecu: {"status": {"uid": entry["uid"], "telemetry_valid": True, "telemetry_valid_all": True},
                      "features": {"link_response_ms": 25.1, "supply_voltage_mv": 3300,
                                   "temperature_c": 39.1, "app_flash_free_ratio": .76,
                                   "previous_failures": 0, "recent_reset_count": 0}}
                for ecu, entry in self.registry.items()}

    def make_profile(self, directory):
        profile = build_profile(self.baseline_rows(), self.registry)
        path = Path(directory) / "profile.json"
        write_profile(path, profile)
        return profile, path

    def test_statistical_adapter_matches_original_scores_and_ranking(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, path = self.make_profile(directory)
            context = self.context()
            context["stm32-led-001"]["features"]["link_response_ms"] = 100
            result = StatisticalEngine(path, FIXED_ORDER).rank(context)
            expected = {ecu: score_secondary(secondary_id=ecu, uid=self.registry[ecu]["uid"],
                        features=context[ecu]["features"], profile=profile) for ecu in FIXED_ORDER}
        self.assertEqual(result.scores, expected)
        self.assertEqual(result.recommended_order,
                         sorted(FIXED_ORDER, key=lambda ecu: (expected[ecu]["risk_score"], FIXED_ORDER.index(ecu))))

    def test_equal_statistical_risk_uses_fixed_order(self):
        with tempfile.TemporaryDirectory() as directory:
            _, path = self.make_profile(directory)
            result = StatisticalEngine(path, FIXED_ORDER).rank(self.context())
        self.assertEqual(result.recommended_order, list(FIXED_ORDER))

    def test_off_shadow_active_and_legacy_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            _, path = self.make_profile(directory)
            context = self.context()
            context["stm32-led-001"]["features"]["link_response_ms"] = 100
            engine = RiskEngine(profile_path=path)
            self.assertEqual(engine.rank(context, model_mode="OFF").execution_order, list(FIXED_ORDER))
            shadow = engine.rank(context, model_mode="STATISTICAL", apply_mode="SHADOW")
            self.assertNotEqual(shadow.recommended_order, shadow.execution_order)
            active = engine.rank(context, model_mode="ACTIVE")
            self.assertEqual(active.used_mode, "STATISTICAL")
            self.assertEqual(active.recommended_order, active.execution_order)

    def test_dataset_loads_90_rows_and_zero_history_features(self):
        dataset = load_jsonl(DATASET)
        self.assertEqual(dataset.X.shape, (90, 6))
        self.assertTrue(np.all(dataset.X[:, 4:] == 0))

    def test_dataset_rejects_invalid_feature(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.jsonl"
            path.write_text(json.dumps({"ai_features": {}}) + "\n")
            with self.assertRaises(DatasetError):
                load_jsonl(path)

    def test_training_is_deterministic_and_artifacts_load(self):
        first, meta1 = train(DATASET)
        second, meta2 = train(DATASET)
        X = load_jsonl(DATASET).X
        np.testing.assert_allclose(first.score_samples(X), second.score_samples(X))
        self.assertEqual(meta1, meta2)
        with tempfile.TemporaryDirectory() as directory:
            model_path, metadata_path = Path(directory) / "m.joblib", Path(directory) / "m.json"
            saved = save_artifacts(first, meta1, model_path, metadata_path)
            loaded, metadata = load_model(model_path, metadata_path, expected_type="ISOLATION_FOREST")
            np.testing.assert_allclose(first.score_samples(X), loaded.score_samples(X))
            self.assertEqual(metadata["model_sha256"], saved["model_sha256"])

    def test_isolation_forest_inference_is_bounded_and_finite(self):
        rows = load_jsonl(DATASET).rows[:3]
        context = {row["secondary_id"]: {"features": row["ai_features"]} for row in rows}
        result = RiskEngine(model_path=MODEL, metadata_path=METADATA).rank(
            context, model_mode="ISOLATION_FOREST", apply_mode="ACTIVE")
        self.assertEqual(result.used_mode, "ISOLATION_FOREST")
        self.assertEqual(set(result.execution_order), set(context))
        self.assertTrue(all(0 <= score["risk_score"] <= 1 and np.isfinite(score["risk_score"])
                            for score in result.scores.values()))

    def test_if_failures_fall_back_to_statistical_then_fixed(self):
        with tempfile.TemporaryDirectory() as directory:
            _, profile = self.make_profile(directory)
            engine = RiskEngine(profile_path=profile, model_path="missing", metadata_path="missing")
            result = engine.rank(self.context(), model_mode="ISOLATION_FOREST")
            self.assertEqual(result.used_mode, "STATISTICAL")
            self.assertEqual(result.fallback_reason, "MODEL_NOT_FOUND")
            self.assertEqual(result.fallback_chain[0]["mode"], "ISOLATION_FOREST")
            fixed = RiskEngine(profile_path="missing", model_path="missing", metadata_path="missing").rank(
                self.context(), model_mode="ISOLATION_FOREST")
            self.assertEqual(fixed.used_mode, "OFF")
            self.assertEqual(fixed.execution_order, list(FIXED_ORDER))
            self.assertEqual(len(fixed.fallback_chain), 2)

    def test_corrupt_hash_metadata_schema_invalid_feature_and_prediction_error(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            model = directory / "m.joblib"
            metadata = directory / "m.json"
            with self.assertRaisesRegex(ModelLoadError, "MODEL_NOT_FOUND"):
                load_model(model, metadata, expected_type="ISOLATION_FOREST")
            model.write_bytes(MODEL.read_bytes())
            with self.assertRaisesRegex(ModelLoadError, "METADATA_NOT_FOUND"):
                load_model(model, metadata, expected_type="ISOLATION_FOREST")
            content = json.loads(METADATA.read_text())
            content["model_sha256"] = "0" * 64
            metadata.write_text(json.dumps(content))
            with self.assertRaisesRegex(ModelLoadError, "MODEL_HASH_MISMATCH"):
                load_model(model, metadata, expected_type="ISOLATION_FOREST")
            content["model_sha256"] = __import__("hashlib").sha256(model.read_bytes()).hexdigest()
            content["feature_schema_version"] = 2
            metadata.write_text(json.dumps(content))
            with self.assertRaisesRegex(ModelLoadError, "FEATURE_SCHEMA_MISMATCH"):
                load_model(model, metadata, expected_type="ISOLATION_FOREST")

            corrupt = directory / "corrupt.joblib"
            corrupt.write_bytes(b"not a joblib model")
            content["feature_schema_version"] = 1
            content["model_sha256"] = __import__("hashlib").sha256(corrupt.read_bytes()).hexdigest()
            metadata.write_text(json.dumps(content))
            with self.assertRaisesRegex(ModelLoadError, "MODEL_DESERIALIZATION_FAILED"):
                load_model(corrupt, metadata, expected_type="ISOLATION_FOREST")

        invalid = self.context()
        invalid["stm32-led-001"]["features"]["temperature_c"] = float("nan")
        fallback = RiskEngine(model_path=MODEL, metadata_path=METADATA, profile_path="missing").rank(
            invalid, model_mode="ISOLATION_FOREST")
        self.assertEqual(fallback.used_mode, "OFF")
        self.assertIn("FEATURE_INVALID", fallback.fallback_chain[0]["reason"])

        rows = load_jsonl(DATASET).rows[:3]
        context = {row["secondary_id"]: {"features": row["ai_features"]} for row in rows}
        with patch("ai.isolation_forest_engine.load_model") as mocked:
            broken = unittest.mock.Mock()
            broken.score_samples.side_effect = RuntimeError("boom")
            mocked.return_value = (broken, json.loads(METADATA.read_text()))
            with self.assertRaisesRegex(RuntimeError, "PREDICTION_FAILED"):
                IsolationForestEngine(MODEL, METADATA, FIXED_ORDER).rank(context)


if __name__ == "__main__":
    unittest.main()
