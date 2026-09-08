import unittest

from ai.feature_schema import SELECTED_RISK_FEATURES
from ai.hybrid_risk import combine_hybrid_risk


class HybridRiskTests(unittest.TestCase):
    config = {
        "selected_features": list(SELECTED_RISK_FEATURES),
        "weights": {
            "physical_anomaly": 0.40,
            "link_delay": 0.30,
            "image_size": 0.15,
            "previous_failures": 0.15,
        },
        "link_response": {
            "normal_threshold_ms": 30.0,
            "policy_limit_ms": 1000.0,
        },
        "previous_failures": {"saturation_count": 3.0},
    }

    def features(self, **overrides):
        values = {
            "link_response_ms": 30.0,
            "supply_voltage_mv": 3300,
            "temperature_c": 39.0,
            "image_size_ratio": 0.5,
            "previous_failures": 0,
        }
        values.update(overrides)
        return values

    def score(self, **overrides):
        return combine_hybrid_risk(
            physical_anomaly_risk=0.2,
            features=self.features(**overrides),
            config=self.config,
        )

    def test_faster_response_is_not_penalized(self):
        faster = self.score(link_response_ms=10.0)
        normal = self.score(link_response_ms=30.0)
        self.assertEqual(faster["risk_score"], normal["risk_score"])
        self.assertEqual(faster["deviations"]["link_delay_excess_ms"], 0.0)

    def test_slower_response_has_higher_risk(self):
        normal = self.score(link_response_ms=30.0)
        slower = self.score(link_response_ms=500.0)
        self.assertGreater(slower["risk_score"], normal["risk_score"])

    def test_larger_image_and_more_failures_raise_risk(self):
        normal = self.score()
        larger = self.score(image_size_ratio=0.9)
        failed = self.score(previous_failures=2)
        self.assertGreater(larger["risk_score"], normal["risk_score"])
        self.assertGreater(failed["risk_score"], normal["risk_score"])

    def test_only_five_selected_features_are_recorded(self):
        result = self.score()
        self.assertEqual(tuple(result["selected_features"]), SELECTED_RISK_FEATURES)


if __name__ == "__main__":
    unittest.main()
