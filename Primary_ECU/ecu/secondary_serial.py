from __future__ import annotations

import hashlib
import os
import re
import struct
import time
from pathlib import Path
from typing import Optional
from serial.tools import list_ports

import serial


DEFAULT_BAUDRATE = 115200
DEFAULT_PORT = "/dev/ttyACM0"
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")

# NUCLEO-F103RB flash layout used by this OTA demo.
SLOT_A_START = 0x08004000
SLOT_A_END = 0x08010000
SLOT_B_START = 0x08010000
SLOT_B_END = 0x0801C000


class FirmwareTransferError(RuntimeError):
    """Raised when the STM32 firmware transfer protocol fails."""


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
    def discover_secondaries() -> dict:
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
                    status = secondary.get_status(timeout_seconds=5.0)

                ecu_serial = status["ecu_serial"]

                if ecu_serial not in SecondarySerial.EXPECTED_SECONDARIES:
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
                }

                print(
                    f"[DISCOVERY] ECU={ecu_serial}, "
                    f"PORT={port}, "
                    f"ACTIVE={status['active_slot']}, "
                    f"TARGET={status['target_slot']}"
                )

            except Exception as exc:
                print(f"[DISCOVERY] Failed on {port}: {exc}")

        return discovered

    def __init__(
        self,
        port: Optional[str] = None,
        baudrate: int = DEFAULT_BAUDRATE,
        read_timeout: float = 1.0,
        write_timeout: float = 10.0,
        open_delay: float = 2.0,
    ) -> None:
        self.port = port or os.environ.get("STM32_PORT", DEFAULT_PORT)
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
            for data in iter(lambda: firmware_file.read(1024 * 1024), b""):
                digest.update(data)

        return digest.hexdigest()

    def _wait_for_line_prefix(
        self,
        prefix: str,
        timeout_seconds: float,
    ) -> str:
        """Wait for the next decoded line beginning with ``prefix``."""
        deadline = time.monotonic() + timeout_seconds
        original_timeout = self.ser.timeout

        try:
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                self.ser.timeout = min(0.5, max(remaining, 0.01))

                raw_line = self.ser.readline()
                if not raw_line:
                    continue

                line = raw_line.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue

                print(f"[STM32 -> Primary] {line}")

                # Ignore boot banners or unrelated debug messages.
                if line.startswith(prefix):
                    return line

        finally:
            self.ser.timeout = original_timeout

        return ""

    def _wait_for_fw_response(self, timeout_seconds: float) -> str:
        return self._wait_for_line_prefix("FW_", timeout_seconds)

    def get_status(self, timeout_seconds: float = 5.0) -> dict:
        """Ask the running STM32 application which slot should be updated.

        Expected response example::

            STATUS,stm32-led-001,ACTIVE=B,TARGET=A,READY=1,MAX=49152
        """
        self.ser.reset_input_buffer()

        request = b"STATUS_REQ\n"
        print("[Primary -> STM32] STATUS_REQ")

        written = self.ser.write(request)
        if written != len(request):
            raise FirmwareTransferError(
                f"STATUS_REQ write incomplete: expected={len(request)}, "
                f"written={written}"
            )

        self.ser.flush()
        response = self._wait_for_line_prefix("STATUS,", timeout_seconds)

        if not response:
            raise FirmwareTransferError("STM32 STATUS response timeout")

        parts = response.split(",")
        if len(parts) != 6 or parts[0] != "STATUS" or not parts[1]:
            raise FirmwareTransferError(
                f"invalid STM32 STATUS response: {response}"
            )

        values = {}
        for field in parts[2:]:
            if "=" not in field:
                raise FirmwareTransferError(
                    f"invalid STM32 STATUS field: {field}"
                )
            key, value = field.split("=", 1)
            values[key] = value

        try:
            active_slot = values["ACTIVE"]
            target_slot = values["TARGET"]
            ready_value = values["READY"]
            max_size = int(values["MAX"])
        except (KeyError, ValueError) as exc:
            raise FirmwareTransferError(
                f"invalid STM32 STATUS values: {response}"
            ) from exc

        if active_slot not in ("A", "B"):
            raise FirmwareTransferError(
                f"invalid active slot in STATUS: {active_slot}"
            )
        if target_slot not in ("A", "B") or target_slot == active_slot:
            raise FirmwareTransferError(
                f"invalid target slot in STATUS: {target_slot}"
            )
        if ready_value not in ("0", "1"):
            raise FirmwareTransferError(
                f"invalid READY value in STATUS: {ready_value}"
            )
        if max_size <= 0:
            raise FirmwareTransferError(
                f"invalid MAX value in STATUS: {max_size}"
            )

        return {
            "ecu_serial": parts[1],
            "active_slot": active_slot,
            "target_slot": target_slot,
            "ready": ready_value == "1",
            "max_size": max_size,
            "raw": response,
        }

    @staticmethod
    def detect_firmware_slot(firmware_path: str) -> str:
        """Determine whether a raw STM32 image was linked for Slot A or B.

        A Cortex-M image begins with the initial stack pointer and Reset_Handler
        vector. The Reset_Handler address must belong to the slot for which the
        application was linked.
        """
        firmware = Path(firmware_path)
        if not firmware.is_file():
            raise FirmwareTransferError(
                f"firmware file not found: {firmware}"
            )

        with firmware.open("rb") as firmware_file:
            vector_table = firmware_file.read(8)

        if len(vector_table) != 8:
            raise FirmwareTransferError(
                f"firmware is too small to contain a vector table: {firmware}"
            )

        initial_sp, reset_vector = struct.unpack("<II", vector_table)
        reset_handler = reset_vector & ~1

        # STM32F103RBT6 has 20 KiB SRAM. The initial SP may equal the first
        # address immediately above SRAM (0x20005000).
        if not (0x20000000 < initial_sp <= 0x20005000):
            raise FirmwareTransferError(
                f"invalid initial stack pointer in firmware: 0x{initial_sp:08X}"
            )
        if (reset_vector & 1) == 0:
            raise FirmwareTransferError(
                f"Reset_Handler is not a Thumb address: 0x{reset_vector:08X}"
            )

        if SLOT_A_START <= reset_handler < SLOT_A_END:
            return "A"
        if SLOT_B_START <= reset_handler < SLOT_B_END:
            return "B"

        raise FirmwareTransferError(
            "Reset_Handler is outside Slot A/B: "
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
        firmware = Path(firmware_path)

        if not firmware.is_file():
            raise FirmwareTransferError(
                f"firmware file not found: {firmware}"
            )

        if firmware.suffix.lower() != ".bin":
            raise FirmwareTransferError(
                f"only .bin firmware is supported: {firmware.name}"
            )

        if io_buffer_size <= 0:
            raise FirmwareTransferError("io_buffer_size must be positive")

        expected_sha256 = expected_sha256.strip().lower()
        if not SHA256_PATTERN.fullmatch(expected_sha256):
            raise FirmwareTransferError(
                "expected_sha256 must be exactly 64 hexadecimal characters"
            )

        firmware_size = firmware.stat().st_size
        if firmware_size <= 0:
            raise FirmwareTransferError("firmware file is empty")

        firmware_slot = self.detect_firmware_slot(str(firmware))
        if expected_target_slot is not None:
            expected_target_slot = expected_target_slot.upper()
            if firmware_slot != expected_target_slot:
                raise FirmwareTransferError(
                    "firmware slot mismatch: "
                    f"STM32 target={expected_target_slot}, "
                    f"firmware linked for Slot {firmware_slot}"
                )

        if max_firmware_size is not None and firmware_size > max_firmware_size:
            raise FirmwareTransferError(
                "firmware is larger than the target slot: "
                f"size={firmware_size}, max={max_firmware_size}"
            )

        # Verify the local file once more immediately before Serial transfer.
        actual_sha256 = self._sha256_file(firmware)
        if actual_sha256 != expected_sha256:
            raise FirmwareTransferError(
                "local firmware SHA-256 mismatch: "
                f"expected={expected_sha256}, actual={actual_sha256}"
            )

        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()

        header = f"FW_BEGIN,{firmware_size},{expected_sha256}\n"
        header_bytes = header.encode("ascii")

        print(f"[Primary -> STM32] {header.strip()}")

        written = self.ser.write(header_bytes)
        if written != len(header_bytes):
            raise FirmwareTransferError(
                f"FW_BEGIN write incomplete: expected={len(header_bytes)}, "
                f"written={written}"
            )

        self.ser.flush()

        ready_response = self._wait_for_fw_response(ready_timeout)
        if ready_response != "FW_READY":
            raise FirmwareTransferError(
                "STM32 rejected FW_BEGIN: "
                f"{ready_response or 'FW_READY timeout'}"
            )

        sent_size = 0

        with firmware.open("rb") as firmware_file:
            while True:
                data = firmware_file.read(io_buffer_size)
                if not data:
                    break

                written = self.ser.write(data)
                if written != len(data):
                    raise FirmwareTransferError(
                        "firmware write incomplete: "
                        f"expected={len(data)}, written={written}"
                    )

                sent_size += written

                # This is simple pacing, not a chunk ACK protocol.
                if pacing_delay > 0:
                    time.sleep(pacing_delay)

        self.ser.flush()

        if sent_size != firmware_size:
            raise FirmwareTransferError(
                "firmware send size mismatch: "
                f"expected={firmware_size}, sent={sent_size}"
            )

        final_response = self._wait_for_fw_response(result_timeout)
        if final_response != "FW_OK":
            raise FirmwareTransferError(
                "STM32 firmware verification failed: "
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
        }

    def close(self) -> None:
        if getattr(self, "ser", None) is not None and self.ser.is_open:
            self.ser.close()

    def __enter__(self) -> "SecondarySerial":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
