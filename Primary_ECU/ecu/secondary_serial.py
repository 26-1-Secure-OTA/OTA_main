from __future__ import annotations

import hashlib
import os
import re
import statistics
import struct
import time
from pathlib import Path
from typing import Optional
from serial.tools import list_ports

import serial


DEFAULT_BAUDRATE = 115200
DEFAULT_PORT = "/dev/ttyACM0"

SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
UID_PATTERN = re.compile(r"^[0-9a-fA-F]{24}$")
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
ATTEMPT_ID_PATTERN = re.compile(r"^[0-9A-Fa-f]{8}$")


# Fault parameters are deliberately centralized on the protocol owner side.
# Callers can override the value, but not the parameter name, which prevents a
# typo in an experiment plan from silently selecting different firmware logic.
FAULT_SCENARIO_DEFAULTS = {
    "NORMAL": ("NONE", 0),
    "LINK_DELAY_LOW": ("DELAY_MS", 50),
    "LINK_DELAY_MEDIUM": ("DELAY_MS", 100),
    "LINK_DELAY_HIGH_ALLOW": ("DELAY_MS", 250),
    "VOLTAGE_LOW_ALLOW": ("VDD_MV", 3200),
    "VOLTAGE_BLOCK": ("VDD_MV", 2500),
    "TEMP_HIGH_ALLOW": ("TEMP_MC", 60000),
    "TEMP_BLOCK": ("TEMP_MC", 100000),
    "TRANSFER_TIMEOUT": ("AFTER_BYTES", 1024),
    "TRANSFER_INTERRUPTED": ("AFTER_BYTES", 1024),
    "TRANSFER_CORRUPTION": ("BYTE_OFFSET", 1024),
    "RESET_DURING_TRANSFER": ("AFTER_BYTES", 1024),
    "BOOT_FAILED": ("NONE", 0),
    "POST_REBOOT_HEALTH_FAIL": ("NONE", 0),
}

FAULT_SCENARIO_LIMITS = {
    "NORMAL": (0, 0),
    "LINK_DELAY_LOW": (0, 5000),
    "LINK_DELAY_MEDIUM": (0, 5000),
    "LINK_DELAY_HIGH_ALLOW": (0, 5000),
    "VOLTAGE_LOW_ALLOW": (0, 5000),
    "VOLTAGE_BLOCK": (0, 5000),
    "TEMP_HIGH_ALLOW": (0, 125000),
    "TEMP_BLOCK": (0, 125000),
    "TRANSFER_TIMEOUT": (1, 49152),
    "TRANSFER_INTERRUPTED": (1, 49152),
    "TRANSFER_CORRUPTION": (0, 49151),
    "RESET_DURING_TRANSFER": (1, 49152),
    "BOOT_FAILED": (0, 0),
    "POST_REBOOT_HEALTH_FAIL": (0, 0),
}

VALID_RESET_CONTEXTS = {
    "NONE",
    "OTA_ACTIVATION",
    "TRANSFER",
    "BOOT_TEST",
}


# NUCLEO-F103RB flash layout used by this OTA demo.
SLOT_A_START = 0x08004000
SLOT_A_END = 0x08010000
SLOT_B_START = 0x08010000
SLOT_B_END = 0x0801C000


class FirmwareTransferError(RuntimeError):
    """Raised when the STM32 firmware transfer protocol fails."""


class FaultProtocolError(FirmwareTransferError):
    """Raised when an STM32 rejects or violates the fault-control protocol."""


class SecondarySerial:
    """Send one verified .bin firmware image to the STM32 over Serial.

    Protocol:
        Primary -> FW_BEGIN,<size>,<sha256>\n
        STM32   -> FW_READY\n
        Primary -> exactly <size> raw firmware bytes
        STM32   -> FW_OK\n

    There is no FW_END message because the STM32 already knows the exact size.
    """

    EXPECTED_SECONDARIES = {
        "stm32-led-001",
        "stm32-led-002",
        "stm32-led-003",
    }

    @staticmethod
    def discover_secondaries(
        status_sample_count: int = 1,
        status_sample_interval: float = 0.05,
    ) -> dict:
        if (
            not isinstance(status_sample_count, int)
            or isinstance(status_sample_count, bool)
            or status_sample_count <= 0
        ):
            raise ValueError(
                "status_sample_count must be a positive integer"
            )

        if (
            not isinstance(status_sample_interval, (int, float))
            or isinstance(status_sample_interval, bool)
            or status_sample_interval < 0
        ):
            raise ValueError(
                "status_sample_interval must be non-negative"
            )

        discovered = {}

        for port_info in list_ports.comports():
            port = port_info.device

            # WSL에서 STM32 Virtual COM Port만 검사
            if not port.startswith("/dev/ttyACM"):
                continue

            try:
                with SecondarySerial(
                    port=port,
                    read_timeout=0.5,
                    open_delay=2.0,
                ) as secondary:
                    sample_result = secondary.get_status_samples(
                        sample_count=status_sample_count,
                        timeout_seconds=5.0,
                        interval_seconds=status_sample_interval,
                    )

                    status = sample_result["status"]
                    status_samples = sample_result["samples"]

                ecu_serial = status["ecu_serial"]

                if (
                    ecu_serial
                    not in SecondarySerial.EXPECTED_SECONDARIES
                ):
                    print(
                        f"[DISCOVERY] Unknown Secondary: "
                        f"ECU={ecu_serial}, PORT={port}"
                    )
                    continue

                if ecu_serial in discovered:
                    raise FirmwareTransferError(
                        f"duplicate Secondary ID: {ecu_serial}"
                    )

                discovered[ecu_serial] = {
                    "port": port,
                    "status": status,
                    "status_samples": status_samples,
                }

                print(
                    f"[DISCOVERY] ECU={ecu_serial}, "
                    f"PORT={port}, "
                    f"ACTIVE={status['active_slot']}, "
                    f"TARGET={status['target_slot']}, "
                    f"UID={status.get('uid')}, "
                    f"VER={status.get('version')}, "
                    f"HEALTH={status.get('health')}, "
                    f"RESPONSE={status.get('link_response_ms')}ms, "
                    f"AI_RESPONSE_MEDIAN="
                    f"{status.get('link_response_median_ms')}ms, "
                    f"SAMPLES={status.get('status_sample_count')}"
                )

            except Exception as exc:
                print(
                    f"[DISCOVERY] Failed on {port}: {exc}"
                )

        return discovered

    def __init__(
        self,
        port: Optional[str] = None,
        baudrate: int = DEFAULT_BAUDRATE,
        read_timeout: float = 1.0,
        write_timeout: float = 10.0,
        open_delay: float = 2.0,
    ) -> None:
        self.port = (
            port
            or os.environ.get(
                "STM32_PORT",
                DEFAULT_PORT,
            )
        )

        self.baudrate = baudrate

        self.ser = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            timeout=read_timeout,
            write_timeout=write_timeout,
        )

        # Opening a Virtual COM Port can reset some STM32 boards.
        if open_delay > 0:
            time.sleep(open_delay)

        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()

        with path.open("rb") as firmware_file:
            for data in iter(
                lambda: firmware_file.read(1024 * 1024),
                b"",
            ):
                digest.update(data)

        return digest.hexdigest()

    def _wait_for_line_prefix(
        self,
        prefix: str,
        timeout_seconds: float,
    ) -> str:
        """Wait for the next decoded line beginning with ``prefix``."""

        deadline = (
            time.monotonic()
            + timeout_seconds
        )

        original_timeout = self.ser.timeout

        try:
            while time.monotonic() < deadline:
                remaining = (
                    deadline
                    - time.monotonic()
                )

                self.ser.timeout = min(
                    0.5,
                    max(
                        remaining,
                        0.01,
                    ),
                )

                raw_line = self.ser.readline()

                if not raw_line:
                    continue

                line = (
                    raw_line
                    .decode(
                        "utf-8",
                        errors="ignore",
                    )
                    .strip()
                )

                if not line:
                    continue

                print(
                    f"[STM32 -> Primary] {line}"
                )

                # Ignore boot banners or unrelated debug messages.
                if line.startswith(prefix):
                    return line

        finally:
            self.ser.timeout = (
                original_timeout
            )

        return ""

    def _wait_for_fw_response(
        self,
        timeout_seconds: float,
    ) -> str:
        return self._wait_for_line_prefix(
            "FW_",
            timeout_seconds,
        )

    @staticmethod
    def parse_status_response(
        response: str,
    ) -> dict:
        parts = (
            response
            .strip()
            .split(",")
        )

        if len(parts) < 2:
            raise FirmwareTransferError(
                "invalid STM32 STATUS response: "
                f"{response}"
            )

        if (
            parts[0] != "STATUS"
            or not parts[1]
        ):
            raise FirmwareTransferError(
                "invalid STM32 STATUS response: "
                f"{response}"
            )

        values = {}

        for field in parts[2:]:
            if "=" not in field:
                raise FirmwareTransferError(
                    "invalid STM32 STATUS field: "
                    f"{field}"
                )

            key, value = field.split(
                "=",
                1,
            )

            key = key.strip()
            value = value.strip()

            if not key or not value:
                raise FirmwareTransferError(
                    "empty STM32 STATUS field: "
                    f"{field}"
                )

            if key in values:
                raise FirmwareTransferError(
                    "duplicate STM32 STATUS field: "
                    f"{key}"
                )

            values[key] = value

        try:
            active_slot = values["ACTIVE"]
            target_slot = values["TARGET"]
            ready_value = values["READY"]
            max_size = int(
                values["MAX"]
            )

        except (
            KeyError,
            ValueError,
        ) as exc:
            raise FirmwareTransferError(
                "invalid STM32 STATUS values: "
                f"{response}"
            ) from exc

        if active_slot not in (
            "A",
            "B",
        ):
            raise FirmwareTransferError(
                "invalid active slot: "
                f"{active_slot}"
            )

        if (
            target_slot not in (
                "A",
                "B",
            )
            or target_slot == active_slot
        ):
            raise FirmwareTransferError(
                "invalid target slot: "
                f"{target_slot}"
            )

        if ready_value not in (
            "0",
            "1",
        ):
            raise FirmwareTransferError(
                "invalid READY value: "
                f"{ready_value}"
            )

        if max_size <= 0:
            raise FirmwareTransferError(
                "invalid MAX value: "
                f"{max_size}"
            )

        uid = values.get("UID")
        version = values.get("VER")
        uptime_value = values.get(
            "UPTIME_MS"
        )
        reset_cause = values.get(
            "RESET"
        )
        uart_error_value = values.get(
            "UART_ERR"
        )
        health = values.get(
            "HEALTH"
        )
        vdd_value = values.get(
            "VDD_MV"
        )
        temperature_value = values.get(
            "TEMP_MC"
        )
        app_used_value = values.get(
            "APP_USED"
        )
        app_free_value = values.get(
            "APP_FREE"
        )
        power_good_value = values.get(
            "POWER_GOOD"
        )
        telemetry_valid_value = values.get(
            "TELEMETRY_VALID"
        )
        scenario = values.get("SCENARIO")
        boot_id_value = values.get("BOOT_ID")
        reset_context = values.get("RESET_CONTEXT")
        attempt_id = values.get("ATTEMPT_ID")

        if (
            uid is not None
            and not UID_PATTERN.fullmatch(
                uid
            )
        ):
            raise FirmwareTransferError(
                "invalid UID value: "
                f"{uid}"
            )

        if (
            version is not None
            and not VERSION_PATTERN.fullmatch(
                version
            )
        ):
            raise FirmwareTransferError(
                "invalid VER value: "
                f"{version}"
            )

        try:
            uptime_ms = (
                int(uptime_value)
                if uptime_value
                is not None
                else None
            )

            uart_error_count = (
                int(uart_error_value)
                if uart_error_value
                is not None
                else None
            )

            supply_voltage_mv = (
                int(vdd_value)
                if vdd_value is not None
                else None
            )
            temperature_mc = (
                int(temperature_value)
                if temperature_value is not None
                else None
            )
            app_flash_used_bytes = (
                int(app_used_value)
                if app_used_value is not None
                else None
            )
            app_flash_free_bytes = (
                int(app_free_value)
                if app_free_value is not None
                else None
            )
            boot_id = (
                int(boot_id_value)
                if boot_id_value is not None
                else None
            )

        except ValueError as exc:
            raise FirmwareTransferError(
                "invalid numeric STATUS value: "
                f"{response}"
            ) from exc

        if (
            uptime_ms is not None
            and uptime_ms < 0
        ):
            raise FirmwareTransferError(
                "invalid UPTIME_MS value: "
                f"{uptime_ms}"
            )

        if (
            uart_error_count is not None
            and uart_error_count < 0
        ):
            raise FirmwareTransferError(
                "invalid UART_ERR value: "
                f"{uart_error_count}"
            )

        if boot_id is not None and not 0 <= boot_id <= 65535:
            raise FirmwareTransferError(
                "invalid BOOT_ID value: "
                f"{boot_id}"
            )

        if (
            supply_voltage_mv is not None
            and not 0 <= supply_voltage_mv <= 5000
        ):
            raise FirmwareTransferError(
                "invalid VDD_MV value: "
                f"{supply_voltage_mv}"
            )

        if (
            temperature_mc is not None
            and not -40000 <= temperature_mc <= 125000
        ):
            raise FirmwareTransferError(
                "invalid TEMP_MC value: "
                f"{temperature_mc}"
            )

        for field_name, field_value in (
            ("POWER_GOOD", power_good_value),
            ("TELEMETRY_VALID", telemetry_valid_value),
        ):
            if (
                field_value is not None
                and field_value not in ("0", "1")
            ):
                raise FirmwareTransferError(
                    f"invalid {field_name} value: "
                    f"{field_value}"
                )

        if (
            app_flash_used_bytes is not None
            and app_flash_used_bytes < 0
        ):
            raise FirmwareTransferError(
                "invalid APP_USED value: "
                f"{app_flash_used_bytes}"
            )

        if (
            app_flash_free_bytes is not None
            and app_flash_free_bytes < 0
        ):
            raise FirmwareTransferError(
                "invalid APP_FREE value: "
                f"{app_flash_free_bytes}"
            )

        if (
            app_flash_used_bytes is not None
            and app_flash_free_bytes is not None
            and (
                app_flash_used_bytes
                + app_flash_free_bytes
                != max_size
            )
        ):
            raise FirmwareTransferError(
                "APP_USED + APP_FREE does not match MAX"
            )

        valid_reset_causes = {
            "POWER_ON",
            "PIN_RESET",
            "SOFTWARE",
            "WATCHDOG",
            "UNKNOWN",
        }

        if (
            reset_cause is not None
            and reset_cause
            not in valid_reset_causes
        ):
            raise FirmwareTransferError(
                "invalid RESET value: "
                f"{reset_cause}"
            )

        valid_health_values = {
            "OK",
            "WARN",
            "ERROR",
        }

        if (
            health is not None
            and health
            not in valid_health_values
        ):
            raise FirmwareTransferError(
                "invalid HEALTH value: "
                f"{health}"
            )

        if (
            scenario is not None
            and scenario not in FAULT_SCENARIO_DEFAULTS
        ):
            raise FirmwareTransferError(
                "invalid SCENARIO value: "
                f"{scenario}"
            )

        if (
            reset_context is not None
            and reset_context not in VALID_RESET_CONTEXTS
        ):
            raise FirmwareTransferError(
                "invalid RESET_CONTEXT value: "
                f"{reset_context}"
            )

        if (
            attempt_id is not None
            and not ATTEMPT_ID_PATTERN.fullmatch(attempt_id)
        ):
            raise FirmwareTransferError(
                "invalid ATTEMPT_ID value: "
                f"{attempt_id}"
            )

        return {
            "ecu_serial": parts[1],
            "active_slot": active_slot,
            "target_slot": target_slot,
            "ready": (
                ready_value == "1"
            ),
            "max_size": max_size,
            "raw": response,
            "uid": uid,
            "version": version,
            "uptime_ms": uptime_ms,
            "reset_cause": reset_cause,
            "uart_error_count": (
                uart_error_count
            ),
            "health": health,
            "supply_voltage_mv": supply_voltage_mv,
            "temperature_mc": temperature_mc,
            "temperature_c": (
                temperature_mc / 1000.0
                if temperature_mc is not None
                else None
            ),
            "app_flash_used_bytes": app_flash_used_bytes,
            "app_flash_free_bytes": app_flash_free_bytes,
            "app_flash_free_ratio": (
                app_flash_free_bytes / max_size
                if app_flash_free_bytes is not None
                else None
            ),
            "power_good": (
                power_good_value == "1"
                if power_good_value is not None
                else None
            ),
            "telemetry_valid": (
                telemetry_valid_value == "1"
                if telemetry_valid_value is not None
                else None
            ),
            "scenario": scenario,
            "boot_id": boot_id,
            "reset_context": reset_context,
            "attempt_id": (
                attempt_id.upper()
                if attempt_id is not None
                else None
            ),
        }

    def get_status(
        self,
        timeout_seconds: float = 5.0,
    ) -> dict:
        self.ser.reset_input_buffer()

        request = b"STATUS_REQ\n"

        print(
            "[Primary -> STM32] STATUS_REQ"
        )

        started_at = time.monotonic()

        written = self.ser.write(
            request
        )

        if written != len(request):
            raise FirmwareTransferError(
                "STATUS_REQ write incomplete: "
                f"expected={len(request)}, "
                f"written={written}"
            )

        self.ser.flush()

        response = (
            self._wait_for_line_prefix(
                "STATUS,",
                timeout_seconds,
            )
        )

        if not response:
            raise FirmwareTransferError(
                "STM32 STATUS response timeout"
            )

        status = (
            self.parse_status_response(
                response
            )
        )

        status["link_response_ms"] = round(
            (
                time.monotonic()
                - started_at
            )
            * 1000,
            3,
        )

        return status

    def set_fault(
        self,
        scenario: str,
        *,
        value: Optional[int] = None,
        attempt_id: Optional[str] = None,
        timeout_seconds: float = 5.0,
    ) -> dict:
        """Select one deterministic, test-only STM32 fault scenario.

        The wire command remains compatible with the documented four-field
        protocol. An optional eight-hex-digit attempt ID is appended for
        reset/boot correlation and is echoed later in STATUS.
        """

        if not isinstance(scenario, str):
            raise FaultProtocolError("scenario must be a string")

        scenario = scenario.strip().upper()
        if scenario not in FAULT_SCENARIO_DEFAULTS:
            raise FaultProtocolError(
                "unsupported fault scenario: "
                f"{scenario or '<empty>'}"
            )

        parameter, default_value = FAULT_SCENARIO_DEFAULTS[scenario]
        if value is None:
            value = default_value

        minimum_value, maximum_value = FAULT_SCENARIO_LIMITS[scenario]
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not minimum_value <= value <= maximum_value
        ):
            raise FaultProtocolError(
                f"{scenario} value must be an integer between "
                f"{minimum_value} and {maximum_value}"
            )

        if attempt_id is not None:
            if not isinstance(attempt_id, str):
                raise FaultProtocolError("attempt_id must be a string")
            attempt_id = attempt_id.strip().upper()
            if not ATTEMPT_ID_PATTERN.fullmatch(attempt_id):
                raise FaultProtocolError(
                    "attempt_id must be exactly 8 hexadecimal characters"
                )

        command = f"FAULT_SET,{scenario},{parameter},{value}"
        if attempt_id is not None:
            command += f",{attempt_id}"
        command += "\n"
        command_bytes = command.encode("ascii")

        self.ser.reset_input_buffer()
        print(f"[Primary -> STM32] {command.strip()}")
        written = self.ser.write(command_bytes)
        if written != len(command_bytes):
            raise FaultProtocolError(
                "FAULT_SET write incomplete: "
                f"expected={len(command_bytes)}, written={written}"
            )
        self.ser.flush()

        response = self._wait_for_line_prefix(
            "FAULT_",
            timeout_seconds,
        )
        expected_response = f"FAULT_ACK,{scenario}"
        if response != expected_response:
            raise FaultProtocolError(
                "STM32 rejected FAULT_SET: "
                f"{response or 'FAULT_ACK timeout'}"
            )

        return {
            "ok": True,
            "scenario": scenario,
            "parameter": parameter,
            "value": value,
            "attempt_id": attempt_id,
            "response": response,
        }

    def clear_fault(
        self,
        *,
        timeout_seconds: float = 5.0,
    ) -> dict:
        """Return the board to NORMAL without changing the OTA API."""

        return self.set_fault(
            "NORMAL",
            timeout_seconds=timeout_seconds,
        )

    def get_status_samples(
        self,
        sample_count: int = 5,
        timeout_seconds: float = 5.0,
        interval_seconds: float = 0.05,
    ) -> dict:
        """Read repeated STATUS samples and add AI-only median fields.

        The returned ``status`` is based on the final raw sample so the
        existing Safety Policy continues to evaluate an actual latest value.
        Median fields are separate and are consumed only by AI features.
        """

        if (
            not isinstance(sample_count, int)
            or isinstance(sample_count, bool)
            or sample_count <= 0
        ):
            raise ValueError("sample_count must be a positive integer")

        if (
            not isinstance(interval_seconds, (int, float))
            or isinstance(interval_seconds, bool)
            or interval_seconds < 0
        ):
            raise ValueError("interval_seconds must be non-negative")

        samples = []

        for sample_index in range(sample_count):
            samples.append(
                self.get_status(timeout_seconds=timeout_seconds)
            )

            if (
                sample_index + 1 < sample_count
                and interval_seconds > 0
            ):
                time.sleep(interval_seconds)

        identity_fields = (
            "ecu_serial",
            "uid",
            "version",
            "active_slot",
            "target_slot",
            "max_size",
            "boot_id",
            "scenario",
            "attempt_id",
        )
        first = samples[0]

        for sample_index, sample in enumerate(samples[1:], start=2):
            for field in identity_fields:
                if sample.get(field) != first.get(field):
                    raise FirmwareTransferError(
                        "STATUS identity/state changed during sampling: "
                        f"sample={sample_index}, field={field}, "
                        f"first={first.get(field)!r}, "
                        f"current={sample.get(field)!r}"
                    )

        def numeric_values(field: str) -> list:
            return [
                sample[field]
                for sample in samples
                if (
                    isinstance(sample.get(field), (int, float))
                    and not isinstance(sample.get(field), bool)
                )
            ]

        measurements = {
            "link_response_ms": numeric_values("link_response_ms"),
            "supply_voltage_mv": numeric_values("supply_voltage_mv"),
            "temperature_c": numeric_values("temperature_c"),
        }

        status = dict(samples[-1])
        status["status_sample_count"] = len(samples)
        status["status_sample_values"] = measurements

        median_fields = (
            ("link_response_ms", "link_response_median_ms", 3),
            ("supply_voltage_mv", "supply_voltage_median_mv", 3),
            ("temperature_c", "temperature_median_c", 3),
        )

        for source, target, digits in median_fields:
            values = measurements[source]
            status[target] = (
                round(statistics.median(values), digits)
                if len(values) == len(samples)
                else None
            )

        telemetry_values = [
            sample.get("telemetry_valid")
            for sample in samples
        ]
        status["telemetry_valid_all"] = (
            all(value is True for value in telemetry_values)
            if all(value is not None for value in telemetry_values)
            else None
        )

        return {
            "status": status,
            "samples": samples,
        }

    @staticmethod
    def detect_firmware_slot(
        firmware_path: str,
    ) -> str:
        """Determine whether a raw STM32 image was linked for Slot A or B.

        A Cortex-M image begins with the initial stack pointer and Reset_Handler
        vector. The Reset_Handler address must belong to the slot for which the
        application was linked.
        """

        firmware = Path(
            firmware_path
        )

        if not firmware.is_file():
            raise FirmwareTransferError(
                "firmware file not found: "
                f"{firmware}"
            )

        with firmware.open(
            "rb"
        ) as firmware_file:
            vector_table = (
                firmware_file.read(8)
            )

        if len(vector_table) != 8:
            raise FirmwareTransferError(
                "firmware is too small to "
                "contain a vector table: "
                f"{firmware}"
            )

        initial_sp, reset_vector = (
            struct.unpack(
                "<II",
                vector_table,
            )
        )

        reset_handler = (
            reset_vector
            & ~1
        )

        # STM32F103RBT6 has 20 KiB SRAM.
        # The initial SP may equal the first address immediately above SRAM.
        if not (
            0x20000000
            < initial_sp
            <= 0x20005000
        ):
            raise FirmwareTransferError(
                "invalid initial stack pointer "
                "in firmware: "
                f"0x{initial_sp:08X}"
            )

        if (
            reset_vector
            & 1
        ) == 0:
            raise FirmwareTransferError(
                "Reset_Handler is not a Thumb "
                "address: "
                f"0x{reset_vector:08X}"
            )

        if (
            SLOT_A_START
            <= reset_handler
            < SLOT_A_END
        ):
            return "A"

        if (
            SLOT_B_START
            <= reset_handler
            < SLOT_B_END
        ):
            return "B"

        raise FirmwareTransferError(
            "Reset_Handler is outside "
            "Slot A/B: "
            f"0x{reset_handler:08X}"
        )

    def send_firmware(
        self,
        firmware_path: str,
        expected_sha256: str,
        expected_target_slot: Optional[str] = None,
        max_firmware_size: Optional[int] = None,
        ready_timeout: float = 30.0,
        result_timeout: float = 60.0,
        io_buffer_size: int = 256,
        pacing_delay: float = 0.05,
    ) -> dict:
        """Send a metadata-verified .bin file to the STM32.

        ``io_buffer_size`` is only a local file/Serial buffer. It does not add
        sequence numbers, per-buffer ACKs, CRC frames, or a retry protocol.
        """

        firmware = Path(
            firmware_path
        )

        if not firmware.is_file():
            raise FirmwareTransferError(
                "firmware file not found: "
                f"{firmware}"
            )

        if (
            firmware.suffix.lower()
            != ".bin"
        ):
            raise FirmwareTransferError(
                "only .bin firmware is "
                "supported: "
                f"{firmware.name}"
            )

        if io_buffer_size <= 0:
            raise FirmwareTransferError(
                "io_buffer_size must be positive"
            )

        expected_sha256 = (
            expected_sha256
            .strip()
            .lower()
        )

        if not SHA256_PATTERN.fullmatch(
            expected_sha256
        ):
            raise FirmwareTransferError(
                "expected_sha256 must be exactly "
                "64 hexadecimal characters"
            )

        firmware_size = (
            firmware.stat().st_size
        )

        if firmware_size <= 0:
            raise FirmwareTransferError(
                "firmware file is empty"
            )

        firmware_slot = (
            self.detect_firmware_slot(
                str(firmware)
            )
        )

        if (
            expected_target_slot
            is not None
        ):
            expected_target_slot = (
                expected_target_slot.upper()
            )

            if (
                firmware_slot
                != expected_target_slot
            ):
                raise FirmwareTransferError(
                    "firmware slot mismatch: "
                    f"STM32 target="
                    f"{expected_target_slot}, "
                    f"firmware linked for Slot "
                    f"{firmware_slot}"
                )

        if (
            max_firmware_size
            is not None
            and firmware_size
            > max_firmware_size
        ):
            raise FirmwareTransferError(
                "firmware is larger than "
                "the target slot: "
                f"size={firmware_size}, "
                f"max={max_firmware_size}"
            )

        # Verify the local file once more immediately before Serial transfer.
        actual_sha256 = (
            self._sha256_file(
                firmware
            )
        )

        if (
            actual_sha256
            != expected_sha256
        ):
            raise FirmwareTransferError(
                "local firmware SHA-256 mismatch: "
                f"expected={expected_sha256}, "
                f"actual={actual_sha256}"
            )

        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()

        header = (
            f"FW_BEGIN,"
            f"{firmware_size},"
            f"{expected_sha256}\n"
        )

        header_bytes = (
            header.encode("ascii")
        )

        print(
            f"[Primary -> STM32] "
            f"{header.strip()}"
        )

        written = self.ser.write(
            header_bytes
        )

        if (
            written
            != len(header_bytes)
        ):
            raise FirmwareTransferError(
                "FW_BEGIN write incomplete: "
                f"expected="
                f"{len(header_bytes)}, "
                f"written={written}"
            )

        self.ser.flush()

        ready_response = (
            self._wait_for_fw_response(
                ready_timeout
            )
        )

        if (
            ready_response
            != "FW_READY"
        ):
            raise FirmwareTransferError(
                "STM32 rejected FW_BEGIN: "
                f"{ready_response or 'FW_READY timeout'}"
            )

        sent_size = 0

        with firmware.open(
            "rb"
        ) as firmware_file:
            while True:
                data = firmware_file.read(
                    io_buffer_size
                )

                if not data:
                    break

                written = self.ser.write(
                    data
                )

                if written != len(data):
                    raise FirmwareTransferError(
                        "firmware write incomplete: "
                        f"expected={len(data)}, "
                        f"written={written}"
                    )

                sent_size += written

                # This is simple pacing, not a chunk ACK protocol.
                if pacing_delay > 0:
                    time.sleep(
                        pacing_delay
                    )

        self.ser.flush()

        if (
            sent_size
            != firmware_size
        ):
            raise FirmwareTransferError(
                "firmware send size mismatch: "
                f"expected={firmware_size}, "
                f"sent={sent_size}"
            )

        final_response = (
            self._wait_for_fw_response(
                result_timeout
            )
        )

        if (
            final_response
            != "FW_OK"
        ):
            raise FirmwareTransferError(
                "STM32 firmware verification "
                "failed: "
                f"{final_response or 'FW_OK timeout'}"
            )

        return {
            "ok": True,
            "response": final_response,
            "port": self.port,
            "baudrate": self.baudrate,
            "path": str(firmware),
            "length": firmware_size,
            "sha256": actual_sha256,
            "target_slot": firmware_slot,
            # The current protocol has no ACK retry or retransmission.
            "retry_count": 0,
        }

    def close(self) -> None:
        if (
            getattr(
                self,
                "ser",
                None,
            )
            is not None
            and self.ser.is_open
        ):
            self.ser.close()

    def __enter__(
        self,
    ) -> "SecondarySerial":
        return self

    def __exit__(
        self,
        exc_type,
        exc_value,
        traceback,
    ) -> None:
        self.close()
