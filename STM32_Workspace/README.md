# STM32Cube OTA workspace

This workspace contains the STM32F103RB bootloader and Slot A/B applications
used by the Primary ECU serial OTA flow.

- `OTA_BOOTLOADER`: selects Slot A (`0x08004000`) or B (`0x08010000`)
- `OTA_LED_A_TEST`: Slot A application, inactive target B
- `OTA_LED_B_TEST`: Slot B application, inactive target A
- `firmware/1.8.0`: reproducible ECU 001/002/003 A/B outputs

Both applications accept `ECU_SUFFIX` and semantic-version build defines. The
checked-in defaults report `1.8.0`; release builds set `ECU_SUFFIX` to `001`,
`002`, or `003` so each physical board retains its identity.

Compatibility tests use the production Primary parser and slot detector:

```sh
python3 -m unittest discover -s STM32_Workspace/tests -v
```

They verify the linked Reset_Handler address, image size, embedded ECU/version,
and the extended STATUS contract. Hardware flashing remains a separate test for
UART timing, flash endurance, reset behavior and electrical faults.
