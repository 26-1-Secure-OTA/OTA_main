#ifndef OTA_FAULT_PROTOCOL_H
#define OTA_FAULT_PROTOCOL_H

#include <stdint.h>
#include <string.h>

/*
 * Test-only protocol shared by Slot A, Slot B, and the bootloader.
 *
 * The first word of the final flash page remains the existing boot flag.  The
 * following words hold a compact control record so reset/boot scenarios can
 * cross an MCU reset without changing the production FW_BEGIN wire format.
 */
#define OTA_CONTROL_RECORD_MAGIC       0x4F544146UL /* "OTAF" */
#define OTA_CONTROL_MAGIC_OFFSET       4U
#define OTA_CONTROL_SCENARIO_OFFSET    8U
#define OTA_CONTROL_CONTEXT_OFFSET     12U
#define OTA_CONTROL_ATTEMPT_OFFSET     16U

#define OTA_RESET_CONTEXT_NONE         0xFFFFFFFFUL
#define OTA_RESET_CONTEXT_ACTIVATION   0x41435456UL /* "ACTV" */
#define OTA_RESET_CONTEXT_TRANSFER     0x5452414EUL /* "TRAN" */
#define OTA_RESET_CONTEXT_BOOT_TEST    0x42544553UL /* "BTES" */

typedef enum
{
  OTA_FAULT_NORMAL = 0,
  OTA_FAULT_LINK_DELAY_LOW = 1,
  OTA_FAULT_LINK_DELAY_MEDIUM = 2,
  OTA_FAULT_LINK_DELAY_HIGH_ALLOW = 3,
  OTA_FAULT_VOLTAGE_LOW_ALLOW = 4,
  OTA_FAULT_VOLTAGE_BLOCK = 5,
  OTA_FAULT_TEMP_HIGH_ALLOW = 6,
  OTA_FAULT_TEMP_BLOCK = 7,
  OTA_FAULT_TRANSFER_TIMEOUT = 8,
  OTA_FAULT_TRANSFER_INTERRUPTED = 9,
  OTA_FAULT_TRANSFER_CORRUPTION = 10,
  OTA_FAULT_RESET_DURING_TRANSFER = 11,
  OTA_FAULT_BOOT_FAILED = 12,
  OTA_FAULT_POST_REBOOT_HEALTH_FAIL = 13
} OtaFaultScenario;

typedef struct
{
  OtaFaultScenario scenario;
  uint32_t value;
  uint32_t attempt_id;
  uint32_t reset_context;
} OtaFaultState;

static inline const char *OtaFault_Name(OtaFaultScenario scenario)
{
  switch (scenario)
  {
    case OTA_FAULT_LINK_DELAY_LOW: return "LINK_DELAY_LOW";
    case OTA_FAULT_LINK_DELAY_MEDIUM: return "LINK_DELAY_MEDIUM";
    case OTA_FAULT_LINK_DELAY_HIGH_ALLOW: return "LINK_DELAY_HIGH_ALLOW";
    case OTA_FAULT_VOLTAGE_LOW_ALLOW: return "VOLTAGE_LOW_ALLOW";
    case OTA_FAULT_VOLTAGE_BLOCK: return "VOLTAGE_BLOCK";
    case OTA_FAULT_TEMP_HIGH_ALLOW: return "TEMP_HIGH_ALLOW";
    case OTA_FAULT_TEMP_BLOCK: return "TEMP_BLOCK";
    case OTA_FAULT_TRANSFER_TIMEOUT: return "TRANSFER_TIMEOUT";
    case OTA_FAULT_TRANSFER_INTERRUPTED: return "TRANSFER_INTERRUPTED";
    case OTA_FAULT_TRANSFER_CORRUPTION: return "TRANSFER_CORRUPTION";
    case OTA_FAULT_RESET_DURING_TRANSFER: return "RESET_DURING_TRANSFER";
    case OTA_FAULT_BOOT_FAILED: return "BOOT_FAILED";
    case OTA_FAULT_POST_REBOOT_HEALTH_FAIL:
      return "POST_REBOOT_HEALTH_FAIL";
    case OTA_FAULT_NORMAL:
    default:
      return "NORMAL";
  }
}

static inline const char *OtaFault_Parameter(OtaFaultScenario scenario)
{
  switch (scenario)
  {
    case OTA_FAULT_LINK_DELAY_LOW:
    case OTA_FAULT_LINK_DELAY_MEDIUM:
    case OTA_FAULT_LINK_DELAY_HIGH_ALLOW:
      return "DELAY_MS";
    case OTA_FAULT_VOLTAGE_LOW_ALLOW:
    case OTA_FAULT_VOLTAGE_BLOCK:
      return "VDD_MV";
    case OTA_FAULT_TEMP_HIGH_ALLOW:
    case OTA_FAULT_TEMP_BLOCK:
      return "TEMP_MC";
    case OTA_FAULT_TRANSFER_TIMEOUT:
    case OTA_FAULT_TRANSFER_INTERRUPTED:
    case OTA_FAULT_RESET_DURING_TRANSFER:
      return "AFTER_BYTES";
    case OTA_FAULT_TRANSFER_CORRUPTION:
      return "BYTE_OFFSET";
    case OTA_FAULT_NORMAL:
    case OTA_FAULT_BOOT_FAILED:
    case OTA_FAULT_POST_REBOOT_HEALTH_FAIL:
    default:
      return "NONE";
  }
}

static inline uint8_t OtaFault_Parse(
    const char *name,
    OtaFaultScenario *scenario
)
{
  uint32_t candidate;

  if ((name == NULL) || (scenario == NULL))
  {
    return 0U;
  }

  for (candidate = (uint32_t)OTA_FAULT_NORMAL;
       candidate <= (uint32_t)OTA_FAULT_POST_REBOOT_HEALTH_FAIL;
       candidate++)
  {
    if (strcmp(name, OtaFault_Name((OtaFaultScenario)candidate)) == 0)
    {
      *scenario = (OtaFaultScenario)candidate;
      return 1U;
    }
  }

  return 0U;
}

static inline uint8_t OtaFault_Value_Is_Valid(
    OtaFaultScenario scenario,
    uint32_t value
)
{
  switch (scenario)
  {
    case OTA_FAULT_NORMAL:
    case OTA_FAULT_BOOT_FAILED:
    case OTA_FAULT_POST_REBOOT_HEALTH_FAIL:
      return (uint8_t)(value == 0U);
    case OTA_FAULT_LINK_DELAY_LOW:
    case OTA_FAULT_LINK_DELAY_MEDIUM:
    case OTA_FAULT_LINK_DELAY_HIGH_ALLOW:
      return (uint8_t)(value <= 5000U);
    case OTA_FAULT_VOLTAGE_LOW_ALLOW:
    case OTA_FAULT_VOLTAGE_BLOCK:
      return (uint8_t)(value <= 5000U);
    case OTA_FAULT_TEMP_HIGH_ALLOW:
    case OTA_FAULT_TEMP_BLOCK:
      return (uint8_t)(value <= 125000U);
    case OTA_FAULT_TRANSFER_TIMEOUT:
    case OTA_FAULT_TRANSFER_INTERRUPTED:
    case OTA_FAULT_RESET_DURING_TRANSFER:
      return (uint8_t)((value > 0U) && (value <= (48U * 1024U)));
    case OTA_FAULT_TRANSFER_CORRUPTION:
      return (uint8_t)(value < (48U * 1024U));
    default:
      return 0U;
  }
}

static inline const char *OtaResetContext_Name(uint32_t context)
{
  switch (context)
  {
    case OTA_RESET_CONTEXT_ACTIVATION: return "OTA_ACTIVATION";
    case OTA_RESET_CONTEXT_TRANSFER: return "TRANSFER";
    case OTA_RESET_CONTEXT_BOOT_TEST: return "BOOT_TEST";
    case OTA_RESET_CONTEXT_NONE:
    default:
      return "NONE";
  }
}

#endif /* OTA_FAULT_PROTOCOL_H */
