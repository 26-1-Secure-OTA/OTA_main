"""Discover the three STM32 Secondaries and print their pre-update STATUS."""

import json

from ecu.secondary_serial import SecondarySerial


STATUS_FIELDS = (
    "ecu_serial",
    "uid",
    "version",
    "active_slot",
    "target_slot",
    "ready",
    "max_size",
    "uptime_ms",
    "reset_cause",
    "uart_error_count",
    "health",
    "link_response_ms",
)


def main() -> None:
    discovered = SecondarySerial.discover_secondaries()

    output = []
    for secondary_id in sorted(discovered):
        status = discovered[secondary_id]["status"]
        output.append({
            field: status.get(field)
            for field in STATUS_FIELDS
        })

    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
