from datetime import datetime, timezone


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _version_tuple(version: str) -> tuple:
    return tuple(
        int(part)
        for part in version.split(".")
    )


def _decision(
    secondary_id: str,
    decision: str,
    reason_code: str,
    reason: str,
) -> dict:
    return {
        "secondary_id": secondary_id,
        "decision": decision,
        "reason_code": reason_code,
        "reason": reason,
        "checked_at": _now_iso(),
        "policy_version": "1.0",
    }


def evaluate_policy(
    expected_ecu: str,
    status: dict | None,
    artifact_info: dict | None,
    expected_uid: str | None,
    *,
    force_reinstall: bool = False,
    maximum_response_ms: float = 1000.0,
    maximum_uart_errors: int = 10,
) -> dict:
    # 검증된 Firmware가 없으면 BLOCK
    if (
        artifact_info is None
        or artifact_info.get("status") != "OK"
    ):
        return _decision(
            expected_ecu,
            "BLOCK",
            "ARTIFACT_VERIFICATION_FAILED",
            (
                artifact_info.get("reason")
                if artifact_info
                else "Verified artifact was not found"
            ),
        )

    # 보드 STATUS를 받지 못했으면 HOLD
    if status is None:
        return _decision(
            expected_ecu,
            "HOLD",
            "SECONDARY_OFFLINE",
            "Secondary was not discovered",
        )

    actual_ecu = status.get("ecu_serial")
    if actual_ecu != expected_ecu:
        return _decision(
            expected_ecu,
            "BLOCK",
            "SECONDARY_ID_MISMATCH",
            f"expected={expected_ecu}, actual={actual_ecu}",
        )

    if not expected_uid:
        return _decision(
            expected_ecu,
            "BLOCK",
            "UNKNOWN_SECONDARY",
            "Registered UID was not found",
        )

    required_status_fields = (
        "uid",
        "version",
        "health",
        "uptime_ms",
        "reset_cause",
        "uart_error_count",
    )

    missing = [
        field
        for field in required_status_fields
        if status.get(field) is None
    ]

    if missing:
        return _decision(
            expected_ecu,
            "HOLD",
            "STATUS_INCOMPLETE",
            f"missing STATUS fields={missing}",
        )

    if status["uid"].upper() != expected_uid.upper():
        return _decision(
            expected_ecu,
            "BLOCK",
            "SECONDARY_UID_MISMATCH",
            (
                f"expected={expected_uid}, "
                f"actual={status['uid']}"
            ),
        )

    # 실제 링크 Slot 검사는 download_artifacts()에서 이미 수행된다.
    artifact_slot = artifact_info.get("target_slot")
    if (
        artifact_slot is not None
        and artifact_slot != status.get("target_slot")
    ):
        return _decision(
            expected_ecu,
            "BLOCK",
            "INVALID_TARGET_SLOT",
            (
                f"firmware={artifact_slot}, "
                f"secondary={status.get('target_slot')}"
            ),
        )

    # 버전은 metadata의 custom.version에서 전달된 값을 사용한다.
    target_version = artifact_info.get("target_version")
    if target_version is None:
        return _decision(
            expected_ecu,
            "BLOCK",
            "TARGET_VERSION_MISSING",
            "Target version was not found in metadata",
        )

    if not force_reinstall:
        if (
            _version_tuple(target_version)
            <= _version_tuple(status["version"])
        ):
            return _decision(
                expected_ecu,
                "BLOCK",
                "DOWNGRADE_ATTEMPT",
                (
                    f"current={status['version']}, "
                    f"target={target_version}"
                ),
            )

    if not status.get("ready", False):
        return _decision(
            expected_ecu,
            "HOLD",
            "SECONDARY_NOT_READY",
            "Secondary reported READY=0",
        )

    if status["health"] != "OK":
        return _decision(
            expected_ecu,
            "HOLD",
            "HEALTH_CHECK_FAILED",
            f"health={status['health']}",
        )

    if status["reset_cause"] == "WATCHDOG":
        return _decision(
            expected_ecu,
            "HOLD",
            "WATCHDOG_RESET_DETECTED",
            "Last reset was caused by watchdog",
        )

    if status["uart_error_count"] >= maximum_uart_errors:
        return _decision(
            expected_ecu,
            "HOLD",
            "UART_ERROR_THRESHOLD",
            (
                f"uart_error_count="
                f"{status['uart_error_count']}"
            ),
        )

    if (
        status.get("link_response_ms") is not None
        and status["link_response_ms"] > maximum_response_ms
    ):
        return _decision(
            expected_ecu,
            "HOLD",
            "STATUS_RESPONSE_SLOW",
            (
                f"link_response_ms="
                f"{status['link_response_ms']}"
            ),
        )

    return _decision(
        expected_ecu,
        "ALLOW",
        "POLICY_PASSED",
        "All safety checks passed",
    )
