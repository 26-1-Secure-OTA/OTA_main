import math
import unittest

from ai.feature_schema import FeatureSchema, FeatureValidationError


class FeatureSchemaTests(unittest.TestCase):
    def setUp(self):
        self.schema = FeatureSchema(1)
        self.features = {
            "recent_reset_count": 0,
            "temperature_c": 39.0,
            "link_response_ms": 25.0,
            "previous_failures": 0,
            "app_flash_free_ratio": 0.75,
            "supply_voltage_mv": 3300,
        }

    def test_canonical_vector_does_not_depend_on_dict_order(self):
        self.assertEqual(self.schema.to_vector(self.features), [25.0, 3300.0, 39.0, 0.75, 0.0, 0.0])

    def test_missing_feature_is_rejected(self):
        del self.features["temperature_c"]
        with self.assertRaisesRegex(FeatureValidationError, "FEATURE_MISSING"):
            self.schema.to_vector(self.features)

    def test_none_nan_inf_string_and_bool_are_rejected(self):
        for invalid in (None, math.nan, math.inf, "25", True):
            with self.subTest(invalid=invalid):
                values = dict(self.features, link_response_ms=invalid)
                with self.assertRaisesRegex(FeatureValidationError, "FEATURE_INVALID"):
                    self.schema.to_vector(values)

    def test_schema_and_feature_order_are_explicitly_validated(self):
        with self.assertRaisesRegex(FeatureValidationError, "UNSUPPORTED_SCHEMA_VERSION"):
            FeatureSchema(99)
        with self.assertRaisesRegex(FeatureValidationError, "FEATURE_ORDER_MISMATCH"):
            self.schema.validate_feature_order(reversed(self.schema.feature_order))

    def test_v2_keeps_v1_intact_and_uses_image_ratio(self):
        self.assertIn("app_flash_free_ratio", FeatureSchema(1).feature_order)
        self.assertIn("image_size_ratio", FeatureSchema(2).feature_order)

    def test_v3_is_physical_isolation_forest_schema(self):
        self.assertEqual(
            FeatureSchema(3).feature_order,
            ("supply_voltage_mv", "temperature_c"),
        )


if __name__ == "__main__":
    unittest.main()
