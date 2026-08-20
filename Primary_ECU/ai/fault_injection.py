import os


SUPPORTED_SCENARIOS = frozenset({
    "TRANSFER_DROP", "TRANSFER_CORRUPT", "TRANSFER_TIMEOUT",
    "RESET_DURING_TRANSFER", "FW_OK_MISSING", "SLOT_VERIFY_FAIL",
    "VERSION_VERIFY_FAIL", "POST_REBOOT_HEALTH_FAIL",
})


class FaultInjectionError(RuntimeError):
    def __init__(self, message, failure_stage, reason_code):
        super().__init__(message)
        self.failure_stage = failure_stage
        self.reason_code = reason_code


class FaultInjector:
    """Explicit experiment hook. Default/production execution is inert."""

    def __init__(self, enabled=False, scenario=None):
        self.enabled = enabled
        self.scenario = scenario
        if enabled and scenario not in SUPPORTED_SCENARIOS:
            raise ValueError(f"unsupported fault scenario: {scenario}")

    @classmethod
    def from_environment(cls):
        enabled = os.environ.get("OTA_ENABLE_FAULT_INJECTION") == "1"
        scenario_id = os.environ.get("OTA_SCENARIO_ID", "")
        scenario = next((name for name in SUPPORTED_SCENARIOS if scenario_id.startswith(name + "-")), None)
        return cls(enabled=enabled, scenario=scenario)

    def trigger(self, point: str) -> None:
        if not self.enabled:
            return
        # Hooks are intentionally raised only after an ALLOW policy decision.
        mapping = {
            "before_transfer": {"TRANSFER_DROP", "TRANSFER_CORRUPT", "TRANSFER_TIMEOUT", "RESET_DURING_TRANSFER"},
            "after_transfer": {"FW_OK_MISSING"},
            "slot_verify": {"SLOT_VERIFY_FAIL"},
            "version_verify": {"VERSION_VERIFY_FAIL"},
            "health_check": {"POST_REBOOT_HEALTH_FAIL"},
        }
        if self.scenario in mapping.get(point, set()):
            classifications = {
                "TRANSFER_DROP": ("TRANSFER", "SERIAL_DISCONNECTED"),
                "TRANSFER_CORRUPT": ("HASH_VERIFY", "FW_HASH_MISMATCH"),
                "TRANSFER_TIMEOUT": ("TRANSFER", "TRANSFER_TIMEOUT"),
                "RESET_DURING_TRANSFER": ("TRANSFER", "SERIAL_DISCONNECTED"),
                "FW_OK_MISSING": ("HASH_VERIFY", "FW_OK_NOT_RECEIVED"),
                "SLOT_VERIFY_FAIL": ("SLOT_VERIFY", "SLOT_MISMATCH"),
                "VERSION_VERIFY_FAIL": ("VERSION_VERIFY", "VERSION_MISMATCH"),
                "POST_REBOOT_HEALTH_FAIL": ("HEALTH_CHECK", "HEALTH_NOT_OK"),
            }
            stage, reason = classifications[self.scenario]
            raise FaultInjectionError(
                f"fault injection {self.scenario} at {point}", stage, reason
            )
