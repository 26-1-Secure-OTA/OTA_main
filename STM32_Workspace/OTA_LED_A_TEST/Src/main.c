/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file           : main.c
  * @brief          : Main program body
  ******************************************************************************
  * @attention
  *
  * Copyright (c) 2026 STMicroelectronics.
  * All rights reserved.
  *
  * This software is licensed under terms that can be found in the LICENSE file
  * in the root directory of this software component.
  * If no LICENSE file comes with this software, it is provided AS-IS.
  *
  ******************************************************************************
  */
/* USER CODE END Header */
/* Includes ------------------------------------------------------------------*/
#include "main.h"

/* Private includes ----------------------------------------------------------*/
/* USER CODE BEGIN Includes */
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

/* USER CODE END Includes */

/* Private typedef -----------------------------------------------------------*/
/* USER CODE BEGIN PTD */
typedef enum
{
  FW_STATE_WAIT_HEADER = 0,
  FW_STATE_RECEIVE_BINARY
} FirmwareReceiveState;

typedef struct
{
  uint8_t data[64];
  uint32_t data_length;
  uint64_t bit_length;
  uint32_t state[8];
} Sha256Context;

/* USER CODE END PTD */

/* Private define ------------------------------------------------------------*/
/* USER CODE BEGIN PD */
#define APPLICATION_SLOT_ADDRESS 0x08004000U
#define TARGET_SLOT_ADDRESS      0x08010000U
#define TARGET_SLOT_SIZE_BYTES   (48U * 1024U)
#define FLASH_PAGE_SIZE_BYTES   1024U
#define TARGET_SLOT_PAGE_COUNT  (TARGET_SLOT_SIZE_BYTES / FLASH_PAGE_SIZE_BYTES)
#define BOOT_FLAG_ADDRESS       0x0801FC00U
#define BOOT_FLAG_SLOT_A_MAGIC  0xA55AA55AU
#define BOOT_FLAG_SLOT_B_MAGIC  0xB007B007U
#define TARGET_BOOT_FLAG_MAGIC  BOOT_FLAG_SLOT_B_MAGIC
#define STARTUP_MESSAGE         "SLOT_A_RUNNING\r\n"
#define STRINGIFY_INNER(value)  #value
#define STRINGIFY(value)        STRINGIFY_INNER(value)
#ifndef ECU_SUFFIX
#define ECU_SUFFIX              001
#endif
#define ECU_ID                  "stm32-led-" STRINGIFY(ECU_SUFFIX)
#ifndef FW_VERSION_MAJOR
#define FW_VERSION_MAJOR        1
#endif
#ifndef FW_VERSION_MINOR
#define FW_VERSION_MINOR        8
#endif
#ifndef FW_VERSION_PATCH
#define FW_VERSION_PATCH        0
#endif
#define FW_VERSION              STRINGIFY(FW_VERSION_MAJOR) "." \
                                STRINGIFY(FW_VERSION_MINOR) "." \
                                STRINGIFY(FW_VERSION_PATCH)
#define ACTIVE_SLOT             "A"
#define TARGET_SLOT             "B"
#define HEALTH_STATUS           "OK"
#define STATUS_BUFFER_SIZE      384U
#ifndef LED_TOGGLE_INTERVAL_MS
#define LED_TOGGLE_INTERVAL_MS  200U
#endif
#define FW_HEADER_BUFFER_SIZE   128U
#define FW_DATA_BUFFER_SIZE     256U
#define SHA256_HEX_LENGTH       64U
#define ADC_SAMPLE_COUNT        16U
#define ADC_CHANNEL_TEMPERATURE 16U
#define ADC_CHANNEL_VREFINT     17U
#define VREFINT_TYPICAL_MV      1200U
#define TEMPERATURE_V25_UV      1430000L
#define TEMPERATURE_SLOPE_UV_C  4300L
#define POWER_GOOD_MIN_MV       2700U
#define POWER_GOOD_MAX_MV       3600U

/* USER CODE END PD */

/* Private macro -------------------------------------------------------------*/
/* USER CODE BEGIN PM */

/* USER CODE END PM */

/* Private variables ---------------------------------------------------------*/
UART_HandleTypeDef huart2;

/* USER CODE BEGIN PV */
static char fw_header_buffer[FW_HEADER_BUFFER_SIZE];
static uint16_t fw_header_length = 0U;
static uint32_t expected_firmware_size = 0U;
static char expected_sha256[SHA256_HEX_LENGTH + 1U];
static FirmwareReceiveState firmware_receive_state = FW_STATE_WAIT_HEADER;
static uint32_t received_firmware_size = 0U;
static uint32_t target_slot_write_address = TARGET_SLOT_ADDRESS;
static uint8_t fw_data_buffer[FW_DATA_BUFFER_SIZE];
static uint32_t uart_error_count = 0U;
static const char *reset_cause = "UNKNOWN";
extern uint8_t __flash_image_end__;

/* USER CODE END PV */

/* Private function prototypes -----------------------------------------------*/
void SystemClock_Config(void);
static void MX_GPIO_Init(void);
static void MX_USART2_UART_Init(void);
/* USER CODE BEGIN PFP */
static void Serial_Send(const char *message);
static uint8_t Is_Hex_String(const char *text, uint32_t length);
static uint8_t Parse_FW_BEGIN(char *line);
static HAL_StatusTypeDef Erase_Target_Slot(void);
static HAL_StatusTypeDef Erase_Boot_Flag(void);
static HAL_StatusTypeDef Write_Target_Slot_Data(
    uint32_t address,
    const uint8_t *data,
    uint16_t length
);
static HAL_StatusTypeDef Write_Boot_Flag(void);
static void Calculate_Target_Slot_SHA256(uint8_t digest[32]);
static uint8_t SHA256_Matches_Expected(const uint8_t digest[32]);
static void Make_UID_String(char uid_string[25]);
static const char *Detect_Reset_Cause(void);
static void Telemetry_ADC_Init(void);
static uint16_t Read_ADC_Channel_Average(uint8_t channel);
static uint8_t Read_Hardware_Telemetry(
    uint32_t *vdd_mv,
    int32_t *temperature_mc
);
static uint32_t Get_Application_Flash_Used(void);
static void Send_Status_Response(void);
static void Process_Serial_Command(void);
static void Receive_Firmware_Data(void);

/* USER CODE END PFP */

/* Private user code ---------------------------------------------------------*/
/* USER CODE BEGIN 0 */
static const uint32_t sha256_constants[64] =
{
  0x428A2F98U, 0x71374491U, 0xB5C0FBCFU, 0xE9B5DBA5U,
  0x3956C25BU, 0x59F111F1U, 0x923F82A4U, 0xAB1C5ED5U,
  0xD807AA98U, 0x12835B01U, 0x243185BEU, 0x550C7DC3U,
  0x72BE5D74U, 0x80DEB1FEU, 0x9BDC06A7U, 0xC19BF174U,
  0xE49B69C1U, 0xEFBE4786U, 0x0FC19DC6U, 0x240CA1CCU,
  0x2DE92C6FU, 0x4A7484AAU, 0x5CB0A9DCU, 0x76F988DAU,
  0x983E5152U, 0xA831C66DU, 0xB00327C8U, 0xBF597FC7U,
  0xC6E00BF3U, 0xD5A79147U, 0x06CA6351U, 0x14292967U,
  0x27B70A85U, 0x2E1B2138U, 0x4D2C6DFCU, 0x53380D13U,
  0x650A7354U, 0x766A0ABBU, 0x81C2C92EU, 0x92722C85U,
  0xA2BFE8A1U, 0xA81A664BU, 0xC24B8B70U, 0xC76C51A3U,
  0xD192E819U, 0xD6990624U, 0xF40E3585U, 0x106AA070U,
  0x19A4C116U, 0x1E376C08U, 0x2748774CU, 0x34B0BCB5U,
  0x391C0CB3U, 0x4ED8AA4AU, 0x5B9CCA4FU, 0x682E6FF3U,
  0x748F82EEU, 0x78A5636FU, 0x84C87814U, 0x8CC70208U,
  0x90BEFFFAU, 0xA4506CEBU, 0xBEF9A3F7U, 0xC67178F2U
};

static uint32_t SHA256_Rotate_Right(uint32_t value, uint32_t count)
{
  return (value >> count) | (value << (32U - count));
}

static void SHA256_Transform(Sha256Context *context, const uint8_t block[64])
{
  uint32_t words[64];
  uint32_t a;
  uint32_t b;
  uint32_t c;
  uint32_t d;
  uint32_t e;
  uint32_t f;
  uint32_t g;
  uint32_t h;
  uint32_t index;

  for (index = 0U; index < 16U; index++)
  {
    uint32_t byte_index = index * 4U;

    words[index] =
        ((uint32_t)block[byte_index] << 24U) |
        ((uint32_t)block[byte_index + 1U] << 16U) |
        ((uint32_t)block[byte_index + 2U] << 8U) |
        ((uint32_t)block[byte_index + 3U]);
  }

  for (index = 16U; index < 64U; index++)
  {
    uint32_t sigma0 =
        SHA256_Rotate_Right(words[index - 15U], 7U) ^
        SHA256_Rotate_Right(words[index - 15U], 18U) ^
        (words[index - 15U] >> 3U);
    uint32_t sigma1 =
        SHA256_Rotate_Right(words[index - 2U], 17U) ^
        SHA256_Rotate_Right(words[index - 2U], 19U) ^
        (words[index - 2U] >> 10U);

    words[index] = words[index - 16U] + sigma0 +
                   words[index - 7U] + sigma1;
  }

  a = context->state[0];
  b = context->state[1];
  c = context->state[2];
  d = context->state[3];
  e = context->state[4];
  f = context->state[5];
  g = context->state[6];
  h = context->state[7];

  for (index = 0U; index < 64U; index++)
  {
    uint32_t sum1 =
        SHA256_Rotate_Right(e, 6U) ^
        SHA256_Rotate_Right(e, 11U) ^
        SHA256_Rotate_Right(e, 25U);
    uint32_t choice = (e & f) ^ ((~e) & g);
    uint32_t temporary1 = h + sum1 + choice +
                          sha256_constants[index] + words[index];
    uint32_t sum0 =
        SHA256_Rotate_Right(a, 2U) ^
        SHA256_Rotate_Right(a, 13U) ^
        SHA256_Rotate_Right(a, 22U);
    uint32_t majority = (a & b) ^ (a & c) ^ (b & c);
    uint32_t temporary2 = sum0 + majority;

    h = g;
    g = f;
    f = e;
    e = d + temporary1;
    d = c;
    c = b;
    b = a;
    a = temporary1 + temporary2;
  }

  context->state[0] += a;
  context->state[1] += b;
  context->state[2] += c;
  context->state[3] += d;
  context->state[4] += e;
  context->state[5] += f;
  context->state[6] += g;
  context->state[7] += h;
}

static void SHA256_Init(Sha256Context *context)
{
  context->data_length = 0U;
  context->bit_length = 0U;
  context->state[0] = 0x6A09E667U;
  context->state[1] = 0xBB67AE85U;
  context->state[2] = 0x3C6EF372U;
  context->state[3] = 0xA54FF53AU;
  context->state[4] = 0x510E527FU;
  context->state[5] = 0x9B05688CU;
  context->state[6] = 0x1F83D9ABU;
  context->state[7] = 0x5BE0CD19U;
}

static void SHA256_Update(
    Sha256Context *context,
    const uint8_t *data,
    uint32_t length
)
{
  uint32_t index;

  for (index = 0U; index < length; index++)
  {
    context->data[context->data_length] = data[index];
    context->data_length++;

    if (context->data_length == 64U)
    {
      SHA256_Transform(context, context->data);
      context->bit_length += 512U;
      context->data_length = 0U;
    }
  }
}

static void SHA256_Final(Sha256Context *context, uint8_t digest[32])
{
  uint32_t index = context->data_length;
  uint32_t state_index;

  context->data[index] = 0x80U;
  index++;

  if (index > 56U)
  {
    while (index < 64U)
    {
      context->data[index] = 0U;
      index++;
    }

    SHA256_Transform(context, context->data);
    index = 0U;
  }

  while (index < 56U)
  {
    context->data[index] = 0U;
    index++;
  }

  context->bit_length += (uint64_t)context->data_length * 8U;

  for (index = 0U; index < 8U; index++)
  {
    context->data[63U - index] =
        (uint8_t)(context->bit_length >> (index * 8U));
  }

  SHA256_Transform(context, context->data);

  for (state_index = 0U; state_index < 8U; state_index++)
  {
    digest[state_index * 4U] =
        (uint8_t)(context->state[state_index] >> 24U);
    digest[state_index * 4U + 1U] =
        (uint8_t)(context->state[state_index] >> 16U);
    digest[state_index * 4U + 2U] =
        (uint8_t)(context->state[state_index] >> 8U);
    digest[state_index * 4U + 3U] =
        (uint8_t)context->state[state_index];
  }
}

static uint8_t Hex_Character_To_Value(char character)
{
  if ((character >= '0') && (character <= '9'))
  {
    return (uint8_t)(character - '0');
  }

  if ((character >= 'a') && (character <= 'f'))
  {
    return (uint8_t)(character - 'a' + 10);
  }

  return (uint8_t)(character - 'A' + 10);
}

static void Calculate_Target_Slot_SHA256(uint8_t digest[32])
{
  Sha256Context context;

  SHA256_Init(&context);
  SHA256_Update(
      &context,
      (const uint8_t *)TARGET_SLOT_ADDRESS,
      expected_firmware_size
  );
  SHA256_Final(&context, digest);
}

static uint8_t SHA256_Matches_Expected(const uint8_t digest[32])
{
  uint32_t index;

  for (index = 0U; index < 32U; index++)
  {
    uint8_t expected_byte =
        (uint8_t)(Hex_Character_To_Value(expected_sha256[index * 2U]) << 4U) |
        Hex_Character_To_Value(expected_sha256[index * 2U + 1U]);

    if (digest[index] != expected_byte)
    {
      return 0U;
    }
  }

  return 1U;
}

static void Serial_Send(const char *message)
{
  HAL_StatusTypeDef transmit_status;

  transmit_status = HAL_UART_Transmit(
      &huart2,
      (uint8_t *)message,
      (uint16_t)strlen(message),
      HAL_MAX_DELAY
  );

  if (transmit_status == HAL_ERROR)
  {
    uart_error_count++;
  }
}

static void Make_UID_String(char uid_string[25])
{
  uint32_t uid0 = *(volatile uint32_t *)0x1FFFF7E8U;
  uint32_t uid1 = *(volatile uint32_t *)0x1FFFF7ECU;
  uint32_t uid2 = *(volatile uint32_t *)0x1FFFF7F0U;

  snprintf(
      uid_string,
      25U,
      "%08lX%08lX%08lX",
      (unsigned long)uid0,
      (unsigned long)uid1,
      (unsigned long)uid2
  );
}

static const char *Detect_Reset_Cause(void)
{
  const char *cause = "UNKNOWN";

  if (__HAL_RCC_GET_FLAG(RCC_FLAG_IWDGRST) ||
      __HAL_RCC_GET_FLAG(RCC_FLAG_WWDGRST))
  {
    cause = "WATCHDOG";
  }
  else if (__HAL_RCC_GET_FLAG(RCC_FLAG_SFTRST))
  {
    cause = "SOFTWARE";
  }
  else if (__HAL_RCC_GET_FLAG(RCC_FLAG_PORRST))
  {
    cause = "POWER_ON";
  }
  else if (__HAL_RCC_GET_FLAG(RCC_FLAG_PINRST))
  {
    cause = "PIN_RESET";
  }

  __HAL_RCC_CLEAR_RESET_FLAGS();

  return cause;
}

static void Telemetry_ADC_Init(void)
{
  RCC->CFGR = (RCC->CFGR & ~RCC_CFGR_ADCPRE) |
              RCC_CFGR_ADCPRE_DIV6;
  RCC->APB2ENR |= RCC_APB2ENR_ADC1EN;
  RCC->APB2RSTR |= RCC_APB2RSTR_ADC1RST;
  RCC->APB2RSTR &= ~RCC_APB2RSTR_ADC1RST;

  ADC1->CR1 = 0U;
  ADC1->CR2 = ADC_CR2_TSVREFE | ADC_CR2_EXTSEL | ADC_CR2_ADON;
  ADC1->SMPR1 |= ADC_SMPR1_SMP16 | ADC_SMPR1_SMP17;
  ADC1->SQR1 &= ~ADC_SQR1_L;

  HAL_Delay(1U);
  ADC1->CR2 |= ADC_CR2_RSTCAL;
  while ((ADC1->CR2 & ADC_CR2_RSTCAL) != 0U)
  {
  }
  ADC1->CR2 |= ADC_CR2_CAL;
  while ((ADC1->CR2 & ADC_CR2_CAL) != 0U)
  {
  }
  HAL_Delay(1U);
}

static uint16_t Read_ADC_Channel_Average(uint8_t channel)
{
  uint32_t sum = 0U;
  uint32_t sample_index;

  ADC1->SQR3 = (ADC1->SQR3 & ~ADC_SQR3_SQ1) |
               ((uint32_t)channel & ADC_SQR3_SQ1);

  for (sample_index = 0U;
       sample_index < (ADC_SAMPLE_COUNT + 1U);
       sample_index++)
  {
    uint32_t started_at;
    uint16_t sample;

    ADC1->SR &= ~ADC_SR_EOC;
    ADC1->CR2 |= ADC_CR2_EXTTRIG | ADC_CR2_SWSTART;
    started_at = HAL_GetTick();

    while ((ADC1->SR & ADC_SR_EOC) == 0U)
    {
      if ((HAL_GetTick() - started_at) > 5U)
      {
        return 0U;
      }
    }

    sample = (uint16_t)(ADC1->DR & 0x0FFFU);
    if (sample_index != 0U)
    {
      sum += sample;
    }
  }

  return (uint16_t)(sum / ADC_SAMPLE_COUNT);
}

static uint8_t Read_Hardware_Telemetry(
    uint32_t *vdd_mv,
    int32_t *temperature_mc
)
{
  uint16_t vref_raw;
  uint16_t temperature_raw;
  int64_t vsense_uv;
  int64_t temperature_delta_mc;

  vref_raw = Read_ADC_Channel_Average(ADC_CHANNEL_VREFINT);
  temperature_raw =
      Read_ADC_Channel_Average(ADC_CHANNEL_TEMPERATURE);

  if ((vref_raw == 0U) || (temperature_raw == 0U))
  {
    *vdd_mv = 0U;
    *temperature_mc = 0;
    return 0U;
  }

  *vdd_mv = (VREFINT_TYPICAL_MV * 4095U) / vref_raw;
  vsense_uv =
      ((int64_t)temperature_raw * (int64_t)(*vdd_mv) * 1000LL) /
      4095LL;
  temperature_delta_mc =
      ((TEMPERATURE_V25_UV - vsense_uv) * 1000LL) /
      TEMPERATURE_SLOPE_UV_C;
  *temperature_mc = (int32_t)(25000LL + temperature_delta_mc);

  return 1U;
}

static uint32_t Get_Application_Flash_Used(void)
{
  uint32_t image_end = (uint32_t)&__flash_image_end__;

  if ((image_end < APPLICATION_SLOT_ADDRESS) ||
      (image_end > (APPLICATION_SLOT_ADDRESS + TARGET_SLOT_SIZE_BYTES)))
  {
    return TARGET_SLOT_SIZE_BYTES;
  }

  return image_end - APPLICATION_SLOT_ADDRESS;
}

static void Send_Status_Response(void)
{
  char status_buffer[STATUS_BUFFER_SIZE];
  char uid_string[25];
  uint32_t vdd_mv;
  int32_t temperature_mc;
  uint32_t app_used;
  uint32_t app_free;
  uint8_t telemetry_valid;
  uint8_t power_good;

  Make_UID_String(uid_string);
  telemetry_valid = Read_Hardware_Telemetry(
      &vdd_mv,
      &temperature_mc
  );
  app_used = Get_Application_Flash_Used();
  app_free = TARGET_SLOT_SIZE_BYTES - app_used;
  power_good = (uint8_t)(
      (telemetry_valid != 0U) &&
      (vdd_mv >= POWER_GOOD_MIN_MV) &&
      (vdd_mv <= POWER_GOOD_MAX_MV)
  );

  snprintf(
      status_buffer,
      sizeof(status_buffer),
      "STATUS,%s,"
      "UID=%s,"
      "VER=%s,"
      "ACTIVE=%s,"
      "TARGET=%s,"
      "READY=1,"
      "MAX=%lu,"
      "VDD_MV=%lu,"
      "TEMP_MC=%ld,"
      "APP_USED=%lu,"
      "APP_FREE=%lu,"
      "POWER_GOOD=%u,"
      "TELEMETRY_VALID=%u,"
      "UPTIME_MS=%lu,"
      "RESET=%s,"
      "UART_ERR=%lu,"
      "HEALTH=%s\r\n",
      ECU_ID,
      uid_string,
      FW_VERSION,
      ACTIVE_SLOT,
      TARGET_SLOT,
      (unsigned long)TARGET_SLOT_SIZE_BYTES,
      (unsigned long)vdd_mv,
      (long)temperature_mc,
      (unsigned long)app_used,
      (unsigned long)app_free,
      (unsigned int)power_good,
      (unsigned int)telemetry_valid,
      (unsigned long)HAL_GetTick(),
      reset_cause,
      (unsigned long)uart_error_count,
      HEALTH_STATUS
  );

  Serial_Send(status_buffer);
}

static uint8_t Is_Hex_String(const char *text, uint32_t length)
{
  uint32_t index;

  for (index = 0U; index < length; index++)
  {
    char value = text[index];

    if (!(((value >= '0') && (value <= '9')) ||
          ((value >= 'a') && (value <= 'f')) ||
          ((value >= 'A') && (value <= 'F'))))
    {
      return 0U;
    }
  }

  return 1U;
}

static uint8_t Parse_FW_BEGIN(char *line)
{
  const char prefix[] = "FW_BEGIN,";
  char *size_text;
  char *sha256_text;
  char *parse_end;
  unsigned long parsed_size;

  if (strncmp(line, prefix, sizeof(prefix) - 1U) != 0)
  {
    return 0U;
  }

  size_text = line + sizeof(prefix) - 1U;
  sha256_text = strchr(size_text, ',');

  if (sha256_text == NULL)
  {
    return 0U;
  }

  *sha256_text = '\0';
  sha256_text++;

  parsed_size = strtoul(size_text, &parse_end, 10);

  if ((size_text[0] == '\0') ||
      (*parse_end != '\0') ||
      (parsed_size == 0UL) ||
      (parsed_size > TARGET_SLOT_SIZE_BYTES))
  {
    return 0U;
  }

  if ((strlen(sha256_text) != SHA256_HEX_LENGTH) ||
      (Is_Hex_String(sha256_text, SHA256_HEX_LENGTH) == 0U))
  {
    return 0U;
  }

  expected_firmware_size = (uint32_t)parsed_size;
  memcpy(expected_sha256, sha256_text, SHA256_HEX_LENGTH + 1U);

  return 1U;
}

static HAL_StatusTypeDef Erase_Target_Slot(void)
{
  FLASH_EraseInitTypeDef erase_config = {0};
  HAL_StatusTypeDef erase_status;
  uint32_t page_error = 0U;

  erase_config.TypeErase = FLASH_TYPEERASE_PAGES;
  erase_config.PageAddress = TARGET_SLOT_ADDRESS;
  erase_config.NbPages = TARGET_SLOT_PAGE_COUNT;

  HAL_FLASH_Unlock();

  __HAL_FLASH_CLEAR_FLAG(
      FLASH_FLAG_EOP |
      FLASH_FLAG_PGERR |
      FLASH_FLAG_WRPERR
  );

  erase_status = HAL_FLASHEx_Erase(&erase_config, &page_error);

  HAL_FLASH_Lock();

  return erase_status;
}

static HAL_StatusTypeDef Erase_Boot_Flag(void)
{
  FLASH_EraseInitTypeDef erase_config = {0};
  HAL_StatusTypeDef erase_status;
  uint32_t page_error = 0U;

  erase_config.TypeErase = FLASH_TYPEERASE_PAGES;
  erase_config.PageAddress = BOOT_FLAG_ADDRESS;
  erase_config.NbPages = 1U;

  HAL_FLASH_Unlock();

  __HAL_FLASH_CLEAR_FLAG(
      FLASH_FLAG_EOP |
      FLASH_FLAG_PGERR |
      FLASH_FLAG_WRPERR
  );

  erase_status = HAL_FLASHEx_Erase(&erase_config, &page_error);

  HAL_FLASH_Lock();

  return erase_status;
}

static HAL_StatusTypeDef Write_Target_Slot_Data(
    uint32_t address,
    const uint8_t *data,
    uint16_t length
)
{
  HAL_StatusTypeDef write_status = HAL_OK;
  uint16_t offset;

  HAL_FLASH_Unlock();

  for (offset = 0U; offset < length; offset += 2U)
  {
    uint16_t halfword = data[offset];

    if ((offset + 1U) < length)
    {
      halfword |= (uint16_t)((uint16_t)data[offset + 1U] << 8U);
    }
    else
    {
      halfword |= 0xFF00U;
    }

    write_status = HAL_FLASH_Program(
        FLASH_TYPEPROGRAM_HALFWORD,
        address + offset,
        halfword
    );

    if (write_status != HAL_OK)
    {
      break;
    }
  }

  HAL_FLASH_Lock();

  return write_status;
}

static HAL_StatusTypeDef Write_Boot_Flag(void)
{
  HAL_StatusTypeDef write_status;

  write_status = Erase_Boot_Flag();

  if (write_status != HAL_OK)
  {
    return write_status;
  }

  HAL_FLASH_Unlock();

  __HAL_FLASH_CLEAR_FLAG(
      FLASH_FLAG_EOP |
      FLASH_FLAG_PGERR |
      FLASH_FLAG_WRPERR
  );

  write_status = HAL_FLASH_Program(
      FLASH_TYPEPROGRAM_HALFWORD,
      BOOT_FLAG_ADDRESS,
      (uint16_t)(TARGET_BOOT_FLAG_MAGIC & 0xFFFFU)
  );

  if (write_status == HAL_OK)
  {
    write_status = HAL_FLASH_Program(
        FLASH_TYPEPROGRAM_HALFWORD,
        BOOT_FLAG_ADDRESS + 2U,
        (uint16_t)(TARGET_BOOT_FLAG_MAGIC >> 16U)
    );
  }

  HAL_FLASH_Lock();

  if ((write_status == HAL_OK) &&
      (*(volatile uint32_t *)BOOT_FLAG_ADDRESS != TARGET_BOOT_FLAG_MAGIC))
  {
    write_status = HAL_ERROR;
  }

  return write_status;
}

static void Process_Serial_Command(void)
{
  uint8_t received_byte;
  HAL_StatusTypeDef receive_status;

  receive_status = HAL_UART_Receive(
      &huart2,
      &received_byte,
      1U,
      10U
  );

  if (receive_status == HAL_ERROR)
  {
    uart_error_count++;
  }

  if (receive_status != HAL_OK)
  {
    return;
  }

  if (received_byte == '\r')
  {
    return;
  }

  if (received_byte == '\n')
  {
    fw_header_buffer[fw_header_length] = '\0';

    if (strcmp(fw_header_buffer, "STATUS_REQ") == 0)
    {
      Send_Status_Response();
    }
    else if (Parse_FW_BEGIN(fw_header_buffer) != 0U)
    {
      if (Erase_Target_Slot() == HAL_OK)
      {
        received_firmware_size = 0U;
        target_slot_write_address = TARGET_SLOT_ADDRESS;
        firmware_receive_state = FW_STATE_RECEIVE_BINARY;
        Serial_Send("FW_READY\r\n");
      }
      else
      {
        Serial_Send("FW_ERASE_FAIL\r\n");
      }
    }
    else
    {
      Serial_Send("FW_BAD_HEADER\r\n");
    }

    fw_header_length = 0U;
    return;
  }

  if (fw_header_length < (FW_HEADER_BUFFER_SIZE - 1U))
  {
    fw_header_buffer[fw_header_length] = (char)received_byte;
    fw_header_length++;
  }
  else
  {
    fw_header_length = 0U;
    Serial_Send("FW_BAD_HEADER\r\n");
  }
}

static void Receive_Firmware_Data(void)
{
  uint32_t remaining_size;
  uint16_t receive_size;
  HAL_StatusTypeDef receive_status;

  remaining_size = expected_firmware_size - received_firmware_size;

  if (remaining_size > FW_DATA_BUFFER_SIZE)
  {
    receive_size = FW_DATA_BUFFER_SIZE;
  }
  else
  {
    receive_size = (uint16_t)remaining_size;
  }

  receive_status = HAL_UART_Receive(
      &huart2,
      fw_data_buffer,
      receive_size,
      HAL_MAX_DELAY
  );

  if (receive_status != HAL_OK)
  {
    if (receive_status == HAL_ERROR)
    {
      uart_error_count++;
    }

    Serial_Send("FW_RECEIVE_FAIL\r\n");
    firmware_receive_state = FW_STATE_WAIT_HEADER;
    return;
  }

  if (Write_Target_Slot_Data(
          target_slot_write_address,
          fw_data_buffer,
          receive_size
      ) != HAL_OK)
  {
    Serial_Send("FW_WRITE_FAIL\r\n");
    firmware_receive_state = FW_STATE_WAIT_HEADER;
    return;
  }

  target_slot_write_address += receive_size;
  received_firmware_size += receive_size;

  if (received_firmware_size == expected_firmware_size)
  {
    uint8_t calculated_digest[32];

    firmware_receive_state = FW_STATE_WAIT_HEADER;
    Calculate_Target_Slot_SHA256(calculated_digest);

    if (SHA256_Matches_Expected(calculated_digest) == 0U)
    {
      Serial_Send("FW_HASH_FAIL\r\n");
      return;
    }

    if (Write_Boot_Flag() != HAL_OK)
    {
      Serial_Send("FW_FLAG_FAIL\r\n");
      return;
    }

    Serial_Send("FW_OK\r\n");
    HAL_Delay(100U);
    HAL_NVIC_SystemReset();
  }
}

/* USER CODE END 0 */

/**
  * @brief  The application entry point.
  * @retval int
  */
int main(void)
{

  /* USER CODE BEGIN 1 */
  SCB->VTOR = APPLICATION_SLOT_ADDRESS;
  /* USER CODE END 1 */

  /* MCU Configuration--------------------------------------------------------*/

  /* Reset of all peripherals, Initializes the Flash interface and the Systick. */
  HAL_Init();

  /* USER CODE BEGIN Init */
  reset_cause = Detect_Reset_Cause();

  /* USER CODE END Init */

  /* Configure the system clock */
  SystemClock_Config();

  /* USER CODE BEGIN SysInit */

  /* USER CODE END SysInit */

  /* Initialize all configured peripherals */
  MX_GPIO_Init();
  MX_USART2_UART_Init();
  /* USER CODE BEGIN 2 */
  uint32_t last_led_toggle = HAL_GetTick();

  Telemetry_ADC_Init();
  Serial_Send(STARTUP_MESSAGE);
  /* USER CODE END 2 */

  /* Infinite loop */
  /* USER CODE BEGIN WHILE */
  while (1)
  {
    /* USER CODE END WHILE */

    /* USER CODE BEGIN 3 */
    if (firmware_receive_state == FW_STATE_WAIT_HEADER)
    {
      Process_Serial_Command();
    }
    else
    {
      Receive_Firmware_Data();
    }

    if ((HAL_GetTick() - last_led_toggle) >= LED_TOGGLE_INTERVAL_MS)
    {
      HAL_GPIO_TogglePin(LD2_GPIO_Port, LD2_Pin);
      last_led_toggle = HAL_GetTick();
    }
  }
  /* USER CODE END 3 */
  }

/**
  * @brief System Clock Configuration
  * @retval None
  */
void SystemClock_Config(void)
{
  RCC_OscInitTypeDef RCC_OscInitStruct = {0};
  RCC_ClkInitTypeDef RCC_ClkInitStruct = {0};

  /** Initializes the RCC Oscillators according to the specified parameters
  * in the RCC_OscInitTypeDef structure.
  */
  RCC_OscInitStruct.OscillatorType = RCC_OSCILLATORTYPE_HSI;
  RCC_OscInitStruct.HSIState = RCC_HSI_ON;
  RCC_OscInitStruct.HSICalibrationValue = RCC_HSICALIBRATION_DEFAULT;
  RCC_OscInitStruct.PLL.PLLState = RCC_PLL_ON;
  RCC_OscInitStruct.PLL.PLLSource = RCC_PLLSOURCE_HSI_DIV2;
  RCC_OscInitStruct.PLL.PLLMUL = RCC_PLL_MUL16;
  if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)
  {
    Error_Handler();
  }

  /** Initializes the CPU, AHB and APB buses clocks
  */
  RCC_ClkInitStruct.ClockType = RCC_CLOCKTYPE_HCLK|RCC_CLOCKTYPE_SYSCLK
                              |RCC_CLOCKTYPE_PCLK1|RCC_CLOCKTYPE_PCLK2;
  RCC_ClkInitStruct.SYSCLKSource = RCC_SYSCLKSOURCE_PLLCLK;
  RCC_ClkInitStruct.AHBCLKDivider = RCC_SYSCLK_DIV1;
  RCC_ClkInitStruct.APB1CLKDivider = RCC_HCLK_DIV2;
  RCC_ClkInitStruct.APB2CLKDivider = RCC_HCLK_DIV1;

  if (HAL_RCC_ClockConfig(&RCC_ClkInitStruct, FLASH_LATENCY_2) != HAL_OK)
  {
    Error_Handler();
  }
}

/**
  * @brief USART2 Initialization Function
  * @param None
  * @retval None
  */
static void MX_USART2_UART_Init(void)
{

  /* USER CODE BEGIN USART2_Init 0 */

  /* USER CODE END USART2_Init 0 */

  /* USER CODE BEGIN USART2_Init 1 */

  /* USER CODE END USART2_Init 1 */
  huart2.Instance = USART2;
  huart2.Init.BaudRate = 115200;
  huart2.Init.WordLength = UART_WORDLENGTH_8B;
  huart2.Init.StopBits = UART_STOPBITS_1;
  huart2.Init.Parity = UART_PARITY_NONE;
  huart2.Init.Mode = UART_MODE_TX_RX;
  huart2.Init.HwFlowCtl = UART_HWCONTROL_NONE;
  huart2.Init.OverSampling = UART_OVERSAMPLING_16;
  if (HAL_UART_Init(&huart2) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN USART2_Init 2 */

  /* USER CODE END USART2_Init 2 */

}

/**
  * @brief GPIO Initialization Function
  * @param None
  * @retval None
  */
static void MX_GPIO_Init(void)
{
  GPIO_InitTypeDef GPIO_InitStruct = {0};
  /* USER CODE BEGIN MX_GPIO_Init_1 */

  /* USER CODE END MX_GPIO_Init_1 */

  /* GPIO Ports Clock Enable */
  __HAL_RCC_GPIOC_CLK_ENABLE();
  __HAL_RCC_GPIOD_CLK_ENABLE();
  __HAL_RCC_GPIOA_CLK_ENABLE();
  __HAL_RCC_GPIOB_CLK_ENABLE();

  /*Configure GPIO pin Output Level */
  HAL_GPIO_WritePin(LD2_GPIO_Port, LD2_Pin, GPIO_PIN_RESET);

  /*Configure GPIO pin : B1_Pin */
  GPIO_InitStruct.Pin = B1_Pin;
  GPIO_InitStruct.Mode = GPIO_MODE_IT_RISING;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  HAL_GPIO_Init(B1_GPIO_Port, &GPIO_InitStruct);

  /*Configure GPIO pin : LD2_Pin */
  GPIO_InitStruct.Pin = LD2_Pin;
  GPIO_InitStruct.Mode = GPIO_MODE_OUTPUT_PP;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_LOW;
  HAL_GPIO_Init(LD2_GPIO_Port, &GPIO_InitStruct);

  /* EXTI interrupt init*/
  HAL_NVIC_SetPriority(EXTI15_10_IRQn, 0, 0);
  HAL_NVIC_EnableIRQ(EXTI15_10_IRQn);

  /* USER CODE BEGIN MX_GPIO_Init_2 */

  /* USER CODE END MX_GPIO_Init_2 */
}

/* USER CODE BEGIN 4 */

/* USER CODE END 4 */

/**
  * @brief  This function is executed in case of error occurrence.
  * @retval None
  */
void Error_Handler(void)
{
  /* USER CODE BEGIN Error_Handler_Debug */
  /* User can add his own implementation to report the HAL error return state */
  __disable_irq();
  while (1)
  {
  }
  /* USER CODE END Error_Handler_Debug */
}
#ifdef USE_FULL_ASSERT
/**
  * @brief  Reports the name of the source file and the source line number
  *         where the assert_param error has occurred.
  * @param  file: pointer to the source file name
  * @param  line: assert_param error line source number
  * @retval None
  */
void assert_failed(uint8_t *file, uint32_t line)
{
  /* USER CODE BEGIN 6 */
  /* User can add his own implementation to report the file name and line number,
     ex: printf("Wrong parameters value: file %s on line %d\r\n", file, line) */
  /* USER CODE END 6 */
}
#endif /* USE_FULL_ASSERT */
