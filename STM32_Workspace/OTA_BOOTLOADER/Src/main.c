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
#include "../../Common/ota_fault_protocol.h"

/* USER CODE END Includes */

/* Private typedef -----------------------------------------------------------*/
/* USER CODE BEGIN PTD */

/* USER CODE END PTD */

/* Private define ------------------------------------------------------------*/
/* USER CODE BEGIN PD */
#define SLOT_A_ADDRESS       0x08004000U
#define SLOT_A_END_ADDRESS   0x08010000U
#define SLOT_B_ADDRESS       0x08010000U
#define SLOT_B_END_ADDRESS   0x0801C000U
#define BOOT_FLAG_ADDRESS    0x0801FC00U
#define BOOT_FLAG_SLOT_A_MAGIC 0xA55AA55AU
#define BOOT_FLAG_SLOT_B_MAGIC 0xB007B007U
#define SRAM_START_ADDRESS   0x20000000U
#define SRAM_END_ADDRESS     0x20005000U
/* USER CODE END PD */

/* Private macro -------------------------------------------------------------*/
/* USER CODE BEGIN PM */

/* USER CODE END PM */

/* Private variables ---------------------------------------------------------*/
UART_HandleTypeDef huart2;

/* USER CODE BEGIN PV */

/* USER CODE END PV */

/* Private function prototypes -----------------------------------------------*/
void SystemClock_Config(void);
static void MX_GPIO_Init(void);
static void MX_USART2_UART_Init(void);
/* USER CODE BEGIN PFP */

static uint8_t Is_Application_Valid(
    uint32_t application_address,
    uint32_t application_end_address
);
static void Jump_To_Application(uint32_t application_address);
static void Select_And_Jump(void);

/* USER CODE END PFP */

/* Private user code ---------------------------------------------------------*/
/* USER CODE BEGIN 0 */
static uint8_t Is_Application_Valid(
    uint32_t application_address,
    uint32_t application_end_address
)
{
  uint32_t app_stack;
  uint32_t app_reset;

  app_stack = *(volatile uint32_t *)application_address;
  app_reset = *(volatile uint32_t *)(application_address + 4U);

  if ((app_stack < SRAM_START_ADDRESS) ||
      (app_stack > SRAM_END_ADDRESS))
  {
    return 0U;
  }

  if ((app_reset < application_address) ||
      (app_reset >= application_end_address) ||
      ((app_reset & 1U) == 0U))
  {
    return 0U;
  }

  return 1U;
}

static void Jump_To_Application(uint32_t application_address)
{
  uint32_t app_stack;
  uint32_t app_reset;
  void (*app_entry)(void);

  app_stack = *(volatile uint32_t *)application_address;
  app_reset = *(volatile uint32_t *)(application_address + 4U);
  app_entry = (void (*)(void))app_reset;

  SCB->VTOR = application_address;
  __DSB();
  __ISB();
  __set_MSP(app_stack);

  app_entry();
}

static void Select_And_Jump(void)
{
  uint32_t boot_flag = *(volatile uint32_t *)BOOT_FLAG_ADDRESS;
  uint32_t control_magic = *(volatile uint32_t *)(
      BOOT_FLAG_ADDRESS + OTA_CONTROL_MAGIC_OFFSET
  );
  uint32_t fault_scenario = *(volatile uint32_t *)(
      BOOT_FLAG_ADDRESS + OTA_CONTROL_SCENARIO_OFFSET
  );

  /*
   * BOOT_FAILED is a fail-safe test hook: reject only the requested target
   * and run the known previous slot. The running application retains the
   * control record in STATUS so the Primary can record and then clear it.
   */
  if ((control_magic == OTA_CONTROL_RECORD_MAGIC) &&
      (fault_scenario == (uint32_t)OTA_FAULT_BOOT_FAILED))
  {
    if ((boot_flag == BOOT_FLAG_SLOT_A_MAGIC) &&
        (Is_Application_Valid(SLOT_B_ADDRESS, SLOT_B_END_ADDRESS) != 0U))
    {
      Jump_To_Application(SLOT_B_ADDRESS);
    }
    if ((boot_flag == BOOT_FLAG_SLOT_B_MAGIC) &&
        (Is_Application_Valid(SLOT_A_ADDRESS, SLOT_A_END_ADDRESS) != 0U))
    {
      Jump_To_Application(SLOT_A_ADDRESS);
    }
  }

  if ((boot_flag == BOOT_FLAG_SLOT_A_MAGIC) &&
      (Is_Application_Valid(SLOT_A_ADDRESS, SLOT_A_END_ADDRESS) != 0U))
  {
    Jump_To_Application(SLOT_A_ADDRESS);
  }

  if ((boot_flag == BOOT_FLAG_SLOT_B_MAGIC) &&
      (Is_Application_Valid(SLOT_B_ADDRESS, SLOT_B_END_ADDRESS) != 0U))
  {
    Jump_To_Application(SLOT_B_ADDRESS);
  }

  if (Is_Application_Valid(SLOT_A_ADDRESS, SLOT_A_END_ADDRESS) != 0U)
  {
    Jump_To_Application(SLOT_A_ADDRESS);
  }

  if (Is_Application_Valid(SLOT_B_ADDRESS, SLOT_B_END_ADDRESS) != 0U)
  {
    Jump_To_Application(SLOT_B_ADDRESS);
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
  Select_And_Jump();
  /* USER CODE END 1 */

  /* MCU Configuration--------------------------------------------------------*/

  /* Reset of all peripherals, Initializes the Flash interface and the Systick. */
  HAL_Init();

  /* USER CODE BEGIN Init */

  /* USER CODE END Init */

  /* Configure the system clock */
  SystemClock_Config();

  /* USER CODE BEGIN SysInit */

  /* USER CODE END SysInit */

  /* Initialize all configured peripherals */
  MX_GPIO_Init();
  MX_USART2_UART_Init();
  /* USER CODE BEGIN 2 */
  const uint8_t start_message[] = "BOOTLOADER_ERROR\r\n";

  HAL_UART_Transmit(
      &huart2,
      (uint8_t *)start_message,
      sizeof(start_message) - 1,
      HAL_MAX_DELAY
  );
  /* USER CODE END 2 */

  /* Infinite loop */
  /* USER CODE BEGIN WHILE */
  while (1)
  {
    /* USER CODE END WHILE */

    /* USER CODE BEGIN 3 */
	HAL_GPIO_TogglePin(LD2_GPIO_Port, LD2_Pin);
	HAL_Delay(200);
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

