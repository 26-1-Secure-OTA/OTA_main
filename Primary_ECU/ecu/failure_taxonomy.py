from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional


FAILURE_STAGES = frozenset({
    "PRECHECK",
    "STATUS",
    "TRANSFER",
    "VERIFY",
    "SLOT_SWITCH",
    "REBOOT",
    "POST_CHECK",
    "EXTERNAL",
})


@dataclass(frozen=True)
class FailureDefinition:
    stage: str
    reason_code: str
    board_related: bool
    ml_label_eligible: bool

    def to_dict(self) -> dict:
        return asdict(self)


_DEFINITIONS = {
    definition.reason_code: definition
    for definition in (
        FailureDefinition("STATUS", "STATUS_TIMEOUT", True, True),
        FailureDefinition("STATUS", "UART_ERROR", True, True),
        FailureDefinition(
            "TRANSFER",
            "TRANSFER_TIMEOUT",
            True,
            True,
        ),
        FailureDefinition(
            "TRANSFER",
            "TRANSFER_INTERRUPTED",
            True,
            True,
        ),
        FailureDefinition(
            "VERIFY",
            "TRANSFER_CORRUPTION",
            True,
            True,
        ),
        FailureDefinition(
            "SLOT_SWITCH",
            "SLOT_SWITCH_FAILED",
            True,
            True,
        ),
        FailureDefinition("REBOOT", "BOOT_FAILED", True, True),
        FailureDefinition(
            "POST_CHECK",
            "POST_REBOOT_HEALTH_FAILED",
            True,
            True,
        ),
        FailureDefinition(
            "STATUS",
            "SECONDARY_COMMUNICATION_FAILED",
            True,
            True,
        ),
        FailureDefinition(
            "PRECHECK",
            "SECONDARY_NOT_READY",
            True,
            False,
        ),
        FailureDefinition(
            "EXTERNAL",
            "METADATA_FAILED",
            False,
            False,
        ),
        FailureDefinition(
            "EXTERNAL",
            "SIGNATURE_FAILED",
            False,
            False,
        ),
        FailureDefinition(
            "EXTERNAL",
            "ARTIFACT_HASH_FAILED",
            False,
            False,
        ),
        FailureDefinition(
            "EXTERNAL",
            "SERVER_CONNECTION_FAILED",
            False,
            False,
        ),
        FailureDefinition(
            "EXTERNAL",
            "REPOSITORY_FAILED",
            False,
            False,
        ),
        FailureDefinition(
            "EXTERNAL",
            "PACKAGE_INVALID",
            False,
            False,
        ),
        FailureDefinition(
            "POST_CHECK",
            "BINARY_VERSION_MISMATCH",
            False,
            False,
        ),
        FailureDefinition(
            "EXTERNAL",
            "UNKNOWN_FAILURE",
            False,
            False,
        ),
    )
}


def get_failure_definition(reason_code: str) -> FailureDefinition:
    return _DEFINITIONS.get(
        str(reason_code or "").upper(),
        _DEFINITIONS["UNKNOWN_FAILURE"],
    )


def classify_failure(
    reason: object,
    *,
    stage_hint: Optional[str] = None,
) -> FailureDefinition:
    text = str(reason or "").strip()
    upper = text.upper()
    normalized_stage = str(stage_hint or "").upper()

    if upper in _DEFINITIONS:
        return _DEFINITIONS[upper]

    if (
        normalized_stage == "REBOOT"
        and "STATUS RESPONSE TIMEOUT" in upper
    ):
        return _DEFINITIONS["BOOT_FAILED"]

    patterns = (
        (("STATUS RESPONSE TIMEOUT",), "STATUS_TIMEOUT"),
        (("FW_READY TIMEOUT", "FW_OK TIMEOUT"), "TRANSFER_TIMEOUT"),
        (("FW_RECEIVE_FAIL",), "TRANSFER_INTERRUPTED"),
        (("WRITE INCOMPLETE", "SEND SIZE MISMATCH"), "TRANSFER_INTERRUPTED"),
        (("FW_HASH_FAIL",), "TRANSFER_CORRUPTION"),
        (("FW_FLAG_FAIL",), "SLOT_SWITCH_FAILED"),
        (("TARGET SLOT DID NOT BECOME ACTIVE",), "SLOT_SWITCH_FAILED"),
        (("HEALTH CHECK FAILED",), "POST_REBOOT_HEALTH_FAILED"),
        (("VERSION DID NOT CHANGE",), "BINARY_VERSION_MISMATCH"),
        (("LOCAL FIRMWARE SHA-256 MISMATCH",), "ARTIFACT_HASH_FAILED"),
        (("DECLARATION/LINK MISMATCH",), "PACKAGE_INVALID"),
        (("NO FIRMWARE IMAGE MATCHES",), "PACKAGE_INVALID"),
        (("PORT/ECU MISMATCH", "ECU ID CHANGED"), "SECONDARY_COMMUNICATION_FAILED"),
        (("SECONDARY IS NOT READY",), "SECONDARY_NOT_READY"),
        (("UART", "SERIAL"), "UART_ERROR"),
    )

    for needles, reason_code in patterns:
        if any(needle in upper for needle in needles):
            return _DEFINITIONS[reason_code]

    default_by_stage = {
        "STATUS": "SECONDARY_COMMUNICATION_FAILED",
        "TRANSFER": "TRANSFER_INTERRUPTED",
        "VERIFY": "TRANSFER_CORRUPTION",
        "SLOT_SWITCH": "SLOT_SWITCH_FAILED",
        "REBOOT": "BOOT_FAILED",
        "POST_CHECK": "POST_REBOOT_HEALTH_FAILED",
    }
    reason_code = default_by_stage.get(normalized_stage)
    if reason_code:
        return _DEFINITIONS[reason_code]

    return _DEFINITIONS["UNKNOWN_FAILURE"]


def label_for_outcome(
    *,
    update_attempted: bool,
    success: Optional[bool],
    reason_code: Optional[str] = None,
) -> tuple[bool, Optional[int]]:
    if update_attempted is not True:
        return False, None
    if success is True:
        return True, 0
    if success is not False:
        return False, None

    definition = get_failure_definition(reason_code or "UNKNOWN_FAILURE")
    if not definition.ml_label_eligible:
        return False, None
    return True, 1


def legacy_row_label(row: dict) -> tuple[bool, Optional[int]]:
    explicit_eligible = row.get("ml_label_eligible")
    explicit_label = row.get("ml_label")

    if isinstance(explicit_eligible, bool):
        if not explicit_eligible:
            return False, None
        if explicit_label in (0, 1) and not isinstance(explicit_label, bool):
            return True, int(explicit_label)

    if row.get("update_attempted") is not True:
        return False, None
    if row.get("success") is True:
        return True, 0
    if row.get("success") is not False:
        return False, None

    reason = row.get("failure_reason_code")
    if not reason:
        # Legacy attempted failures did not always contain a stable reason.
        # Preserve their historical meaning rather than silently discarding them.
        return True, 1

    definition = classify_failure(
        reason,
        stage_hint=row.get("failure_stage"),
    )
    if definition.ml_label_eligible:
        return True, 1
    return False, None
