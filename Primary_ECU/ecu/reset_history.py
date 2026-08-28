from __future__ import annotations

from typing import Optional


EXPECTED_RESET_CONTEXTS = frozenset({
    "OTA_ACTIVATION",
    "SOFTWARE_REBOOT",
    "BOOTLOADER_RESET",
})

MANUAL_RESET_CONTEXTS = frozenset({
    "MANUAL_RESET",
})

UNEXPECTED_RESET_REASONS = frozenset({
    "WATCHDOG",
    "BROWN_OUT",
    "TRANSFER",
    "TRANSFER_RESET",
    "BOOT_TEST",
    "BOOT_FAILURE",
    "RESET_LOOP",
})


def _valid_uptime(value: object) -> Optional[int]:
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    ):
        return value
    return None


def _observation(row: dict) -> dict:
    return {
        "boot_id": row.get("boot_id"),
        "uptime_ms": _valid_uptime(row.get("uptime_ms")),
        "reset_cause": str(row.get("reset_cause") or "").upper() or None,
        "reset_context": (
            str(row.get("reset_context") or "").upper() or None
        ),
        "success": row.get("success"),
        "slot_switched": row.get("slot_switched"),
    }


def _is_expected_reset(
    current: dict,
    previous: Optional[dict],
) -> bool:
    if current.get("reset_context") in EXPECTED_RESET_CONTEXTS:
        return True

    # Backward compatibility for old logs without RESET_CONTEXT. A successful
    # slot switch is the only legacy signal that the next reboot was intended.
    return bool(
        previous
        and previous.get("success") is True
        and previous.get("slot_switched") is True
    )


def _reset_occurred(current: dict, previous: Optional[dict]) -> bool:
    if previous is None:
        return False

    current_boot = current.get("boot_id")
    previous_boot = previous.get("boot_id")
    if current_boot is not None and previous_boot is not None:
        return current_boot != previous_boot

    current_uptime = current.get("uptime_ms")
    previous_uptime = previous.get("uptime_ms")
    return (
        current_uptime is not None
        and previous_uptime is not None
        and current_uptime < previous_uptime
    )


def count_recent_unexpected_resets(
    rows: list[dict],
    *,
    current_status: Optional[dict],
    recent_attempt_window: int = 10,
) -> int:
    if recent_attempt_window <= 0:
        raise ValueError("recent_attempt_window must be positive")

    attempted_indexes = [
        index
        for index, row in enumerate(rows)
        if row.get("update_attempted") is True
    ]
    if len(attempted_indexes) > recent_attempt_window:
        start = attempted_indexes[-recent_attempt_window]
        relevant_rows = rows[start:]
    else:
        relevant_rows = list(rows)

    observations = [_observation(row) for row in relevant_rows]
    if current_status is not None:
        observations.append(_observation(current_status))

    deduplicated = []
    seen_boot_ids = set()
    for observation in observations:
        boot_id = observation.get("boot_id")
        if boot_id is not None:
            if boot_id in seen_boot_ids:
                continue
            seen_boot_ids.add(boot_id)
        deduplicated.append(observation)

    count = 0
    previous = None
    for current in deduplicated:
        if not _reset_occurred(current, previous):
            previous = current
            continue
        if _is_expected_reset(current, previous):
            previous = current
            continue

        if current.get("reset_context") in MANUAL_RESET_CONTEXTS:
            previous = current
            continue

        reason = current.get("reset_cause")
        context = current.get("reset_context")
        if (
            reason in UNEXPECTED_RESET_REASONS
            or context in UNEXPECTED_RESET_REASONS
            or reason not in {None, "SOFTWARE"}
            or context is None
        ):
            count += 1
        previous = current

    return count
