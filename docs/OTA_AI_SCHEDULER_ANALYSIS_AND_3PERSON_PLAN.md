# OTA AI Scheduler 코드 분석 및 3인 업무분배 계획

> 분석 기준: `basic_ai_test` 브랜치의 실제 Repository 코드
> 문서 목적: 코드 수정 전 현황 분석, AI/ML 적용 설계, 정확히 3개 Work Package 정의

## 핵심 결론

현재 구현은 Safety가 `ALLOW`한 ECU만 통계 위험도로 정렬한다는 핵심 경계는 대체로 지키지만 다음 항목은 다음 구현 전에 반드시 보완해야 한다.

1. AI 정렬 후 업데이트 직전 `evaluate_policy()` 재검사가 없다.
2. `previous_failures`가 실패 원인을 구분하지 않고 전체 이력을 누적한다.
3. `recent_reset_count`가 정상 OTA 재부팅도 비정상 reset으로 계산한다.
4. `app_flash_free_ratio`는 target slot 여유 공간이 아니라 현재 실행 이미지 크기를 반영하며 실제 데이터에서 모든 보드가 동일하다.
5. 실패 단계가 `INSTALL`, 원인이 자유 문자열이라 ML label을 신뢰성 있게 만들 수 없다.

---

## 1. Repository 구조 분석

### 핵심 디렉터리

| 영역 | 실제 경로 | 역할 |
|---|---|---|
| Primary 진입점 | `Primary_ECU/Primary.py` | MQTT, Director/Image metadata 처리, artifact 다운로드와 OTA 시작 |
| OTA orchestration | `Primary_ECU/ecu/installer.py` | 3보드 discovery, Safety, Feature, AI ranking, 순차 업데이트, 결과 기록 |
| Serial/STATUS | `Primary_ECU/ecu/secondary_serial.py` | STM32 탐색, STATUS 요청/파싱, firmware transfer |
| Safety | `Primary_ECU/ecu/safety_policy.py` | ALLOW/HOLD/BLOCK 판정 |
| Feature/History | `Primary_ECU/ecu/feature_collector.py` | 현재 STATUS, artifact, 과거 JSONL에서 Feature 생성 |
| Statistical AI | `Primary_ECU/ai/anomaly_scorer.py` | profile load, 위험도 계산, ALLOW ECU 정렬, fallback |
| 정상 profile | `Primary_ECU/ai/normal_profile.py` | baseline에서 Median/MAD profile 생성 |
| Baseline 수집 | `Primary_ECU/ai/baseline_collector.py` | 3보드 정상 STATUS raw/aggregate 수집 |
| 실험 로그 | `Primary_ECU/ecu/experiment_logger.py` | `ota_experiments.jsonl` append |
| ECU 상태 로그 | `Primary_ECU/ecu/secondary_state.py` | READY/TRANSFERRING/CONFIRMED/FAILED 상태 저장 |
| Slot A application | `STM32_Workspace/OTA_LED_A_TEST/Src/main.c` | STATUS, telemetry, inactive Slot B update |
| Slot B application | `STM32_Workspace/OTA_LED_B_TEST/Src/main.c` | STATUS, telemetry, inactive Slot A update |
| Bootloader | `STM32_Workspace/OTA_BOOTLOADER/Src/main.c` | boot flag에 따른 Slot 선택 및 jump |
| 정상 데이터 | `Primary_ECU/data/ai_baseline/run30/normal_status_aggregated.jsonl` | 보드당 30행, 총 90 aggregate rows |
| 현재 실험 로그 | `Primary_ECU/logs/ota_experiments.jsonl` | 12행, 그중 실제 OTA 성공 3행 |

### 기능별 실제 구현 위치

| 기능 | 파일·클래스·함수 | 입력 → 출력 | 호출 위치·호출 대상·상태/로그 |
|---|---|---|---|
| Primary orchestration | `PrimeEcuHandler.on_message()` | MQTT message → 검증/설치 결과 | `Verifier`, `Installer`, `Reporter`; `image_update_started` |
| Director metadata | `PrimeEcuHandler._on_all_director_meta_received()` | timestamp/snapshot/targets → `VerifyResult` | `Verifier.verify_director_chain()`; 성공 시 `update_target.json` |
| Image metadata | `Verifier.verify_metadata()` | metadata, 이전 hash/version → `(ok, next_reference)` | `Primary.py:on_message()` |
| Director/Image 교차검증 | `Verifier.hash_check()` | Director targets + Image targets → verified target list | hash, length, target slot 비교 |
| Artifact validation | `Installer.download_artifacts()` | update list, base URL → `{ok, results}` | SHA-256, SHA-512, length, linked Slot 확인 |
| STM32 STATUS 요청 | `SecondarySerial.get_status()` | timeout → parsed status | `STATUS_REQ`, 응답시간 측정 |
| STATUS parsing | `SecondarySerial.parse_status_response()` | CSV STATUS string → status dict | UID, slots, telemetry, reset, uptime 파싱 |
| 반복 STATUS | `SecondarySerial.get_status_samples()` | sample count/interval → latest status + medians | Safety는 latest, AI는 median 사용 |
| 보드 탐색 | `SecondarySerial.discover_secondaries()` | `/dev/ttyACM*` → ECU별 port/status | ID 중복과 예상 ECU 집합 확인 |
| Safety Policy | `evaluate_policy()` | ECU/status/artifact/UID → decision dict | `install_serial_firmware()` preflight |
| Feature extraction | `collect_features()` | status/artifact/history → feature dict | Safety 판단 후 호출 |
| Normal profile | `build_profile()` | aggregate rows/registry → profile JSON | Median, MAD, minimum scale |
| Statistical risk | `score_secondary()` | UID/features/profile → risk/deviation/contribution | `schedule_allow_ecus()` |
| ECU ranking | `schedule_allow_ecus()` | ALLOW context/profile/mode → schedule result | risk 오름차순, 고정순서 tie-break |
| OTA loop | `Installer.install_serial_firmware()` | artifact results/statuses → per-ECU results | preflight, ranking, 실행, JSONL logging |
| 단일 ECU OTA | `Installer._install_one_serial_firmware()` | port/artifacts/expected status → result | 직전 STATUS, transfer, reboot 대기, post-check |
| Transfer | `SecondarySerial.send_firmware()` | firmware/hash/slot/max size → transfer result | `FW_BEGIN → FW_READY → binary → FW_OK` |
| STM32 수신 | `Process_Serial_Command()`, `Receive_Firmware_Data()` | UART frame/data → flash/write/hash/boot flag | `FW_*` 응답 |
| Slot switch | `Write_Boot_Flag()` | target slot magic → flash boot flag | hash 성공 후 호출 |
| Reboot | `Receive_Firmware_Data()` | `FW_OK` 후 → system reset | `HAL_NVIC_SystemReset()` |
| Boot | `Select_And_Jump()` | boot flag/vector → Slot A/B jump | 잘못된 target이면 다른 유효 Slot로 fallback |
| Post health/version | `_install_one_serial_firmware()` | post STATUS → confirmed/failure | active slot, UID, version, HEALTH 검사 |
| Experiment logging | `experiment_row()`, `ExperimentLogger.append()` | features/decision/result/AI → JSONL | `ota_experiments.jsonl` |
| `previous_failures` | `_history_features()` | 과거 experiment rows → count | 현재 전체 누적 실패 |
| `recent_reset_count` | `_history_features()` | 과거/current uptime → count | 최근 20 uptime 감소 |
| AI fallback | `schedule_allow_ecus()` | scorer/profile 오류 → OFF fixed order | 이유는 `ai_fallback_reason` |

`Updater`, `Transport`, `Storage`는 `PrimeEcuHandler.__init__()`에서 생성되지만 현재 STM32 serial OTA의 주 호출 경로는 `Installer` 직접 호출이다.

---

## 2. 실제 OTA 호출 흐름

```text
PrimeEcuHandler.__init__()
  └─ VVM을 primary/version으로 publish

Director metadata 수신
PrimeEcuHandler.on_message()
  └─ meta_buffer에 timestamp/snapshot/targets 저장
      └─ PrimeEcuHandler._on_all_director_meta_received()
          └─ Verifier.verify_director_chain()
              ├─ Verifier._verify_signature()
              ├─ timestamp → snapshot version 확인
              └─ snapshot → targets version 확인
          └─ PrimeEcuHandler._save_update_target()
          └─ Reporter.report(request_next=True)

Image metadata 수신
PrimeEcuHandler.on_message()
  ├─ Verifier.verify_metadata(timestamp)
  ├─ requests.get(snapshot.json)
  ├─ Verifier.verify_metadata(snapshot)
  ├─ requests.get(targets.json)
  ├─ Verifier.verify_metadata(targets)
  ├─ Verifier.hash_check()
  ├─ Installer.select_updates_for_secondary()
  │   └─ SecondarySerial.discover_secondaries()
  │       └─ SecondarySerial.get_status()
  ├─ Installer.download_artifacts()
  │   ├─ length / SHA-256 / SHA-512 확인
  │   └─ SecondarySerial.detect_firmware_slot()
  └─ Installer.install_serial_firmware()
      ├─ SecondarySerial.discover_secondaries(sample_count=5)
      │   └─ SecondarySerial.get_status_samples()
      ├─ evaluate_policy()                 ← 최초 Safety
      ├─ collect_features()
      │   └─ _read_secondary_history()
      │       └─ _history_features()
      ├─ schedule_allow_ecus()
      │   ├─ load_profile()
      │   ├─ score_secondary()
      │   └─ risk 오름차순 정렬
      └─ ALLOW ECU 순차 실행
          └─ Installer._install_one_serial_firmware()
              ├─ SecondarySerial.get_status()  ← 새 STATUS
              ├─ ID/ready/이전 상태 일관성 검사
              ├─ SecondarySerial.send_firmware()
              │   ├─ FW_BEGIN
              │   ├─ FW_READY
              │   ├─ firmware bytes
              │   └─ FW_OK
              ├─ STM32 Write_Boot_Flag()
              ├─ STM32 HAL_NVIC_SystemReset()
              ├─ Bootloader Select_And_Jump()
              ├─ reboot delay
              ├─ SecondarySerial.get_status()
              ├─ active slot 검사
              ├─ UID 검사
              ├─ target version 검사
              ├─ HEALTH 검사
              └─ ExperimentLogger.append()
```

중요한 결함은 `_install_one_serial_firmware()`의 첫 `get_status()` 이후 `evaluate_policy()`를 다시 호출하지 않는다는 점이다. ID·READY·일부 상태 일관성만 확인하고 전압, 온도, reset, UART threshold, link threshold를 재평가하지 않는다.

---

## 3. 현재 AI 구조

### Safety와 AI 경계

| 확인 항목 | 현재 결과 | 근거 |
|---|---|---|
| HOLD ECU가 AI ranking에 포함되는가 | 아니오 | `decision == "ALLOW"`만 `allow_context`에 추가 |
| BLOCK ECU가 AI ranking에 포함되는가 | 아니오 | 동일 |
| AI가 Safety를 override할 수 있는가 | 아니오 | scheduler는 순서만 반환 |
| ranking 후 Safety 재검사가 존재하는가 | **아니오** | `_install_one_serial_firmware()`에서 `evaluate_policy()` 미호출 |
| scorer/profile 오류 시 Safety가 우회되는가 | 아니오 | 이미 preflight Safety 완료 후 fixed order fallback |
| 모든 AI 오류가 격리되는가 | 완전하지 않음 | AI module import 오류나 예상 밖 scheduler 예외는 전체 설치를 중단할 수 있음 |
| AI 오류가 unsafe OTA를 실행시키는가 | 현재는 아니오 | 실패는 실행 중단 또는 ALLOW ECU fixed order로 귀결 |

추가로 현재 Safety Policy는 전압과 온도를 검사하지 않는다. `power_good`, `telemetry_valid`, `supply_voltage_mv`, `temperature_c`가 실제 ALLOW/HOLD/BLOCK 조건에 없다. 전압·온도 BLOCK scenario를 만들려면 먼저 Safety threshold가 명시되어야 한다.

### 현재 Mode와 목표 Mode

현재 Mode:

- `OFF`: ALLOW ECU 고정 순서
- `SHADOW`: 위험 순서는 계산하지만 실행은 고정 순서
- `ACTIVE`: 위험도가 낮은 순서로 실행

향후에는 알고리즘 Mode를 다음과 같이 분리한다.

```text
OFF
STATISTICAL
ISOLATION_FOREST
SUPERVISED
```

`SHADOW`는 알고리즘 종류가 아니라 적용 방법이므로 별도 옵션으로 둔다.

```text
OTA_AI_MODE=ISOLATION_FOREST
OTA_AI_APPLY=SHADOW | ACTIVE
```

기존 호환:

```text
OFF    → OFF
ACTIVE → STATISTICAL + ACTIVE
SHADOW → STATISTICAL + SHADOW
```

---

## 4. 6개 Feature 분석

| Feature | 현재 파일/함수 | Source·계산 | 값 특성 | 문제점 | ML 적합성·추천 |
|---|---|---|---|---|---|
| `link_response_ms` | `SecondarySerial.get_status()`, `get_status_samples()`, `collect_features()` | `STATUS_REQ` write 전부터 응답 파싱까지 측정; 5회 median 사용 | 정상 약 24.3~25.6ms | OS scheduling, USB serial, port open 영향; timeout은 값이 아니라 STATUS 누락 | 적합. median과 jitter, timeout rate를 v2에 추가 |
| `supply_voltage_mv` | STM32 `Read_Hardware_Telemetry()`, Primary parser | VREFINT ADC로 VDD 계산; AI는 5회 median | 보드별 약 3291~3302mV | 현재 Safety 미사용; MAD가 0인 보드 존재 | 적합. Safety threshold와 ML anomaly 분리 |
| `temperature_c` | STM32 `Read_Hardware_Telemetry()`, Primary parser/collector | 내부 die temperature ADC, 5회 median | 약 38.7~41.3°C | ambient가 아닌 die temperature; 보드 편차; 현재 Safety 미사용 | 조건부 적합. 장시간 baseline과 보드별 calibration 필요 |
| `app_flash_free_ratio` | STM32 `Get_Application_Flash_Used()`, `Send_Status_Response()`, Primary parser | `(49152 - 현재 active image linked size) / 49152` | run30 전체 90행이 `0.76416015625` | target slot 실제 free나 target image 크기를 반영하지 않음 | v1 호환만 유지하고 v2에서 교체 |
| `previous_failures` | `_read_secondary_history()`, `_history_features()` | `update_attempted=True && success=False` 전체 누적 | 비감소 누적 count | 원인 필터 없음, window 없음 | 분류/window 개선 후 적합 |
| `recent_reset_count` | `_history_features()` | 마지막 20개 uptime에서 감소 횟수 | 정상 OTA 후에도 증가 | reset 원인 무시, 정상 OTA reset 포함 | 현재 형태는 부적합. reset event 분류 후 사용 |

현재 `FEATURE_NAMES`에는 이 6개 외에도 `free_flash_ratio`, `image_size`, `recent_retry_rate`, `power_good`, `telemetry_valid` 등이 존재하지만 statistical scorer는 위 6개만 사용한다.

---

## 5. previous_failures 분석

### 현재 동작

`feature_collector.py`의 `_history_features()`는 해당 ECU의 전체 JSONL에서 다음 조건을 만족하는 행을 센다.

```python
update_attempted is True
success is False
```

`recent_window`는 retry와 reset 계산에는 일부 적용되지만 `previous_failures`에는 적용되지 않는다. 기본값도 목표 10이 아니라 20이다.

Metadata, repository 연결, server 연결 실패는 `install_serial_firmware()` 호출 전 발생하므로 대부분 experiment log에 행 자체가 없다. Artifact 실패는 Safety의 `ARTIFACT_VERIFICATION_FAILED` BLOCK으로 처리되고 `update_attempted=False`라 현재 count에 포함되지 않는다.

반면 OTA를 시작한 뒤 발생하는 모든 예외는 자유 문자열 `failure_reason_code`와 `success=False`가 되므로 다음이 구분 없이 포함된다.

- Serial/STATUS/transfer 문제
- slot switch 실패
- reboot/post-health 실패
- post-update version mismatch
- 잘못 packaging된 binary가 전송까지 진행된 경우

### 권장 분류

```python
{
    "stage": "TRANSFER",
    "reason_code": "TRANSFER_TIMEOUT",
    "board_related": True,
    "ml_label_eligible": True,
}
```

Board/Communication history 포함:

```text
STATUS_TIMEOUT
UART_ERROR
TRANSFER_TIMEOUT
TRANSFER_INTERRUPTED
TRANSFER_CORRUPTION
SLOT_SWITCH_FAILED
BOOT_FAILED
POST_REBOOT_HEALTH_FAILED
SECONDARY_COMMUNICATION_FAILED
```

제외:

```text
METADATA_FAILED
SIGNATURE_FAILED
ARTIFACT_HASH_FAILED
SERVER_CONNECTION_FAILED
REPOSITORY_FAILED
PACKAGE_INVALID
BINARY_VERSION_MISMATCH
```

`HASH_VERIFY_FAILED`는 위치에 따라 구분한다.

- 다운로드 artifact와 metadata hash 불일치: `ARTIFACT_HASH_FAILED`, 외부 실패
- UART 전송 뒤 STM32 target slot hash 불일치: `TRANSFER_CORRUPTION`, 보드/통신 실패

### 최근 10회 계산

```python
eligible_attempts = [
    row for row in history
    if row.update_attempted
    and row.ml_label_eligible
][-10:]

previous_failures = count(row.ml_label == 1)
previous_failure_rate = failures / len(eligible_attempts)
```

초기 이력이 10회 미만일 수 있으므로 `history_attempt_count`도 같이 기록한다. `previous_failure_rate`는 보드마다 이력 수가 다를 때 비교 가능성이 좋아 v2 Feature로 적절하다.

---

## 6. recent_reset_count 분석

### 현재 문제

현재는 reset reason을 사용하지 않고 uptime 감소만 계산한다.

```text
이전 pre-update uptime = 1,500,000
정상 OTA로 reboot
다음 pre-update uptime = 300,000
→ recent_reset_count +1
```

실제 체크인 성공 로그 3행 모두 정상 OTA였는데 `recent_reset_count=1`이다. 정상 Slot 전환 reset이 위험 이력에 포함되고 있다는 직접적인 근거다.

또한 `SecondarySerial.__init__()` 주석에 Virtual COM Port open이 일부 STM32를 reset할 수 있다고 되어 있다. 따라서 `PIN_RESET`도 곧바로 manual/비정상 reset으로 단정할 수 없다.

### 현재 STM32 reset 구분

```text
WATCHDOG
SOFTWARE
POWER_ON
PIN_RESET
UNKNOWN
```

Brown-out은 `POWER_ON`과 분리되지 않고 manual reset과 ST-LINK/USB/DTR reset은 모두 `PIN_RESET`일 수 있다.

### 권장 ResetEvent

```json
{
  "secondary_id": "stm32-led-001",
  "boot_id": 42,
  "observed_at": "...",
  "reset_reason": "SOFTWARE",
  "reset_context": "OTA_ACTIVATION",
  "expected": true,
  "active_slot_before": "A",
  "active_slot_after": "B",
  "campaign_id": "...",
  "attempt_id": "...",
  "uptime_ms": 1234
}
```

| Reset | 포함 여부 | 식별 방법 |
|---|---:|---|
| 정상 OTA Slot switch | 제외 | activation marker + slot 변경 + campaign/attempt |
| 정상 software reboot | 제외 | 명시적 reboot command marker |
| bootloader 의도 reset | 제외 | boot context marker |
| Watchdog | 포함 | RCC watchdog flag |
| Brown-out | 포함 | RCC BOR flag를 별도 `BROWN_OUT`으로 노출 |
| transfer 중 reset | 포함 | transfer state/received bytes + attempt marker |
| boot failure/reset loop | 포함 | boot counter 증가, confirmation 없음 |
| manual reset | 별도 분류 | `PIN_RESET` + 사용자 marker가 있을 때만 `MANUAL`; 없으면 `EXTERNAL_PIN_UNKNOWN` |

### 정상 OTA reset 식별

```text
FW_BEGIN에 attempt_id 전달
→ STM32가 transfer context 저장
→ hash/boot flag 성공 시 reset_context=OTA_ACTIVATION 기록
→ reboot 후 새 application이 STATUS에 BOOT_ID/RESET_CONTEXT 전송
→ Primary가 정상 slot 전환 및 attempt_id와 대조
→ 확인 후 marker clear
```

저장 후보:

- STM32 backup register: reset 간 유지되며 flash 마모가 적음
- reserved flash page: power cycle에도 유지되지만 wear 관리 필요
- Primary `logs/reset_events.jsonl`: 분류 결과와 관찰 이력

추천은 STM32 persistent boot record와 Primary JSONL history 조합이다. `recent_reset_count`는 마지막 10 OTA attempt 사이의 `expected=false` reset event만 계산하고 같은 boot의 반복 STATUS 5회는 `boot_id`로 중복 제거한다.

---

## 7. app_flash_free_ratio 분석

### 실제 의미

```text
APP_USED
= linker symbol __flash_image_end__ - 현재 APPLICATION_SLOT_ADDRESS

APP_FREE
= TARGET_SLOT_SIZE_BYTES - APP_USED

MAX
= 48 KiB
```

따라서 실제 의미는 다음과 같다.

```text
현재 실행 firmware와 같은 크기의 image가 slot 하나에서 차지하지 않은 비율
```

다음을 의미하지 않는다.

- inactive target slot의 실제 erase/free 상태
- target firmware의 크기
- target firmware 설치 후 남는 비율

모든 보드가 동일한 이유는 세 보드 firmware가 `ECU_SUFFIX`만 다르고 linker image 크기가 같으며 Slot 용량도 모두 49,152 bytes이기 때문이다. run30의 90개 aggregate 행 모두 정확히 `0.76416015625`다.

이미 `collect_features()`에는 다음 값이 구현되어 있다.

```python
free_flash_ratio = (
    max_firmware_size - artifact.length
) / max_firmware_size
```

추천 Feature v2:

```text
image_size_ratio = artifact.length / target_slot_capacity
```

값이 클수록 위험이 증가한다는 방향이 명확하다. `target_slot_free_after_update_ratio = 1 - image_size_ratio`는 같은 정보이므로 둘 중 하나만 사용한다.

결론:

- schema v1 호환을 위해 `app_flash_free_ratio` 기록은 유지
- 신규 model schema v2에서는 `image_size_ratio`로 교체
- baseline STATUS 데이터만으로 target image 크기를 알 수 없으므로 v2 학습은 OTA experiment dataset 사용

---

## 8. Statistical Risk 분석

### 현재 수식

```text
center = median(values)
mad = median(abs(value - center))
scale = max(1.4826 × MAD, minimum_scale)
deviation = abs(current - center) / scale
normalized = min(deviation / 5, 1)
contribution = weight × normalized
```

Minimum scale:

| Feature | Minimum scale |
|---|---:|
| `link_response_ms` | 0.25 |
| `supply_voltage_mv` | 2.0 |
| `temperature_c` | 0.25 |
| `app_flash_free_ratio` | 0.005 |

History Feature:

```text
normalized = min(count / 3, 1)
contribution = weight × normalized
```

최종:

```text
risk_score = clip(sum(contributions), 0, 1)
```

### 실제 가중치

| Feature | Weight |
|---|---:|
| `link_response_ms` | 30% |
| `supply_voltage_mv` | 15% |
| `temperature_c` | 20% |
| `app_flash_free_ratio` | 5% |
| `previous_failures` | 20% |
| `recent_reset_count` | 10% |

### Ranking과 Fallback

- 위험도 오름차순: 낮은 ECU부터 OTA
- 점수가 같으면 `stm32-led-001 → 002 → 003`
- `SHADOW`에서는 recommended order만 바뀌고 execution은 fixed order
- risk는 feature별 합으로 0~1이지만 calibration된 실패확률은 아님

현재 fallback reason:

```text
PROFILE_NOT_FOUND
PROFILE_INVALID
PROFILE_SCHEMA_MISMATCH
SECONDARY_NOT_IN_PROFILE
PROFILE_UID_MISMATCH
FEATURE_MISSING
INVALID_TELEMETRY
SCORER_EXCEPTION
```

현재 구조:

```text
STATISTICAL 실패 → FIXED ORDER
```

목표 구조:

```text
ISOLATION_FOREST 실패
→ STATISTICAL
→ FIXED ORDER
```

현재 `schedule_allow_ecus()`가 profile loading, scoring, mode, ranking, fallback을 한 함수에서 수행하므로 공통 `RiskEngine` interface와 모델별 adapter로 분리해야 한다.

---

## 9. Isolation Forest 적용 설계

### 현재 데이터셋

- `normal_status_raw.jsonl`: 450행, 30 cycles × 3 boards × 5 samples
- `normal_status_aggregated.jsonl`: 90행, 30 cycles × 3 boards
- 단일 session
- firmware version `2.1.1`
- history Feature는 모두 0
- `app_flash_free_ratio`는 전부 동일
- 보드별 voltage/temperature 중심값 차이가 있음

정상 데이터로 사용할 때는 raw 450행이 아니라 aggregate 90행을 사용한다. 실제 inference도 5회 median을 사용하기 때문이다.

### 추천 파일 구조

```text
Primary_ECU/ai/
├─ feature_schema.py
├─ risk_engine.py
├─ statistical_engine.py
├─ isolation_forest_engine.py
├─ supervised_engine.py
├─ model_loader.py
├─ dataset_loader.py
└─ train_isolation_forest.py

Primary_ECU/models/
├─ isolation-forest-v1.joblib
└─ isolation-forest-v1.metadata.json
```

현재 `anomaly_scorer.py`는 `statistical_engine.py`로 역할을 이동하거나 compatibility wrapper로 유지한다.

### Feature schema

```json
{
  "schema_version": 1,
  "feature_order": [
    "link_response_ms",
    "supply_voltage_mv",
    "temperature_c",
    "app_flash_free_ratio",
    "previous_failures",
    "recent_reset_count"
  ],
  "dtypes": {
    "link_response_ms": "float64"
  },
  "missing_policy": "reject",
  "non_finite_policy": "reject"
}
```

Feature order는 Python dict 순서에 의존하지 않는다.

### Preprocessing 선택

Isolation Forest, Decision Tree, Random Forest는 tree 계열이므로 선형 scale 변화에 크게 민감하지 않다. StandardScaler가 필수는 아니다.

추천은 `RobustScaler + model`을 하나의 저장 pipeline으로 묶는 방식이다.

- Median/MAD 기반 현재 구조와 잘 맞음
- scale 차이가 큰 Feature의 진단을 일관화
- 향후 tree 이외 모델 확장 가능
- inference preprocessing 누락 방지

현재 statistical deviation을 먼저 clipping한 값을 IF에 넣지 않고 raw canonical Feature vector를 pipeline에 넣는다. scaler와 model도 별도로 저장하지 않고 같은 artifact에 저장한다.

### Training metadata

```json
{
  "model_type": "ISOLATION_FOREST",
  "model_version": "isolation-forest-v1",
  "model_schema_version": 1,
  "feature_schema_version": 1,
  "feature_order": ["..."],
  "preprocessor": "RobustScaler",
  "training_source_sha256": "...",
  "training_rows": 90,
  "training_session_ids": ["..."],
  "board_uids": {"...": "..."},
  "sklearn_version": "...",
  "random_state": 42,
  "contamination": "auto",
  "score_calibration": {
    "normal_p50": 0.0,
    "normal_p99": 0.0
  },
  "model_sha256": "..."
}
```

### Anomaly score → risk

`-score_samples()`처럼 클수록 비정상인 값으로 방향을 통일한 뒤 normal training distribution percentile로 정규화한다.

```text
risk = clip(
    (anomaly_score - normal_p50)
    / (normal_p99 - normal_p50),
    0,
    1
)
```

### Model load 검증

- model/metadata 존재 및 JSON 정상
- model hash 일치
- model type/version 허용
- schema version 일치
- feature order 정확히 일치
- sklearn compatibility
- Feature 누락 없음
- bool 제외한 숫자
- NaN/Inf 없음
- prediction 결과 유한값
- UID/board scope 일치

### 목표 Mode/Fallback

```text
OFF
└─ fixed order

STATISTICAL
├─ statistical score
└─ 실패 시 fixed order

ISOLATION_FOREST
├─ IF inference
├─ 실패 시 statistical
└─ statistical 실패 시 fixed order

SUPERVISED
├─ predict_proba(failure=1)
├─ 실패 시 statistical
└─ statistical 실패 시 fixed order
```

`ai_mode_requested`는 요청값, `ai_mode_used`는 최종 성공한 engine을 기록한다.

---

## 10. Fault Injection 설계

### 구현 원칙

- 실제 전압/온도를 위험하게 만들지 않음
- Safety BLOCK 실험과 ML ALLOW 실패 실험을 별도 dataset으로 분리
- Primary 환경변수만으로 값을 바꾸는 방식은 board telemetry 실험으로 간주하지 않음
- 재현 가능한 runtime scenario protocol을 STM32에 추가
- Primary simulation은 unit/integration test용 보조 수단

추천 protocol:

```text
FAULT_SET,<scenario_id>,<parameter>,<value>
STM32 → FAULT_ACK,<scenario_id>
STATUS에 SCENARIO=<id>, BOOT_ID=<n>, RESET_CONTEXT=<...> 추가
```

### Scenario 표

| Scenario | 변경 Feature | 예상 실패 | Safety 상태 | 구현 위치 | 난이도 |
|---|---|---|---|---|---|
| `NORMAL` | 없음 | 성공 | ALLOW | scenario runner + 기본 firmware | 하 |
| `LINK_DELAY_LOW` | response 50ms | 성공 | ALLOW | STM32 STATUS 응답 전 delay | 하 |
| `LINK_DELAY_MEDIUM` | response 100ms | 성공 | ALLOW | 동일 | 하 |
| `LINK_DELAY_HIGH_ALLOW` | 200~300ms | 성공/지연 | ALLOW | 동일, policy 최대값 미만 | 하 |
| `VOLTAGE_LOW_ALLOW` | VDD 3150~3250mV | 보통 성공 | ALLOW | `Send_Status_Response()` override | 중 |
| `TEMP_HIGH_ALLOW` | 높은 die temp 보고 | 보통 성공 | ALLOW | `Send_Status_Response()` override | 중 |
| `TRANSFER_TIMEOUT` | 통신 결과 실패 | `TRANSFER_TIMEOUT` | preflight ALLOW | STM32 finite timeout + Primary stall | 상 |
| `TRANSFER_INTERRUPTED` | 통신 이력 악화 | `TRANSFER_INTERRUPTED` | preflight ALLOW | Primary byte threshold 중단 + STM32 timeout | 상 |
| `TRANSFER_CORRUPTION` | 직접 Feature 변화 없음 | `TRANSFER_CORRUPTION` | preflight ALLOW | 송신 byte flip 또는 STM32 test hook | 중 |
| `RESET_DURING_TRANSFER` | reset history 증가 | 실패 | preflight ALLOW | STM32 receive byte threshold reset | 상 |
| `BOOT_FAILED` | reset/boot history | `BOOT_FAILED` | preflight ALLOW | bootloader test flag/context | 최상 |
| `POST_REBOOT_HEALTH_FAIL` | post HEALTH=ERROR | `POST_REBOOT_HEALTH_FAILED` | preflight ALLOW | 새 application runtime scenario | 중 |
| `VOLTAGE_BLOCK` | VDD policy 밖 | OTA 미시도 | BLOCK/HOLD | telemetry override + Safety threshold | 중 |
| `TEMP_BLOCK` | temp policy 밖 | OTA 미시도 | BLOCK/HOLD | 동일 | 중 |
| `STATUS_TIMEOUT_BLOCK` | STATUS 없음 | OTA 미시도 | HOLD | STATUS reply suppression | 중 |

### 세부 판단

- STATUS delay는 firmware injection이 우선이다. 최초 preflight STATUS부터 실제 지연이 보인다.
- Voltage/temperature는 ADC 측정 후 telemetry output만 override하고 `telemetry_valid=1`, `power_good`를 override 값 기준으로 다시 계산한다.
- `Receive_Firmware_Data()`가 현재 `HAL_MAX_DELAY`를 사용하므로 transfer interruption 구분을 위해 finite timeout이 필요하다.
- corruption은 Primary가 원본 hash는 유지한 채 한 byte를 변조하면 STM32 `FW_HASH_FAIL`로 재현 가능하다.
- boot failure는 보드를 복구 불가능하게 만들지 않도록 test-only boot flag와 recovery timeout을 설계한다.
- post-health failure는 `HEALTH_STATUS` 상수 대신 scenario 상태 기반 응답으로 구현한다.

### 60개 이상 수집

최소 기준을 ALLOW attempt row 60개로 정의한다.

```text
20 campaigns × 3 boards = 60 rows
```

예시:

- NORMAL 4 campaigns
- LINK delay 3종 × 2 campaigns = 6
- voltage/temp ALLOW 각 2 = 4
- transfer failure 3종 각 1 = 3
- reset/boot/health 각 1 = 3

총 20 campaigns다. 실패 scenario도 preflight에서는 반드시 ALLOW여야 한다. BLOCK scenario는 별도 safety validation 데이터 또는 `ml_label_eligible=false`로 보존한다.

---

## 11. Dataset / Label 설계

### 현재 `ota_experiments.jsonl`

- 체크인 파일 12행
- HOLD/BLOCK 9행
- 실제 성공 OTA 3행
- 실패 OTA 0행
- 최신 코드가 추가한 AI field는 기존 12행에는 없음
- 별도 parser는 없고 history reader가 `.get()`으로 필요한 key만 읽음
- Feature는 `features` object가 아니라 최상위에 flatten됨
- `failure_stage`는 코드상 실패 시 `INSTALL`
- `failure_reason_code`는 exception 자유 문자열

### 추천 backward-compatible row

```json
{
  "log_schema_version": 2,
  "campaign_id": "...",
  "attempt_id": "...",
  "secondary_id": "...",
  "features": {
    "schema_version": 2,
    "values": {
      "link_response_ms": 25.0
    }
  },
  "update_attempted": true,
  "success": false,
  "failure_stage": "TRANSFER",
  "failure_reason_code": "TRANSFER_TIMEOUT",
  "ml_label_eligible": true,
  "ml_label": 1
}
```

기존 top-level fields는 당분간 dual-write한다.

```text
features.values 존재 → 신규 형식
없음 → 기존 top-level fields
```

### Stage

```text
PRECHECK
STATUS
TRANSFER
VERIFY
SLOT_SWITCH
REBOOT
POST_CHECK
EXTERNAL
```

### Reason mapping

| 현재 위치/문자열 | 표준 Stage | 표준 Reason | Label |
|---|---|---|---:|
| STATUS response timeout | STATUS | STATUS_TIMEOUT | 1 |
| Serial read/write 오류 | STATUS/TRANSFER | UART_ERROR | 1 |
| FW_READY/FW_OK timeout | TRANSFER | TRANSFER_TIMEOUT | 1 |
| 송신 중 중단 | TRANSFER | TRANSFER_INTERRUPTED | 1 |
| STM32 `FW_HASH_FAIL` | VERIFY | TRANSFER_CORRUPTION | 1 |
| `FW_FLAG_FAIL` | SLOT_SWITCH | SLOT_SWITCH_FAILED | 1 |
| post STATUS 없음 | REBOOT | BOOT_FAILED | 1 |
| active slot 불일치 | SLOT_SWITCH | SLOT_SWITCH_FAILED | 1 |
| post HEALTH != OK | POST_CHECK | POST_REBOOT_HEALTH_FAILED | 1 |
| metadata signature | EXTERNAL | METADATA_FAILED/SIGNATURE_FAILED | null |
| artifact download/hash | EXTERNAL | ARTIFACT_FAILED | null |
| HTTP/repository | EXTERNAL | SERVER_CONNECTION_FAILED | null |
| packaging/slot/name | EXTERNAL | PACKAGE_INVALID | null |
| version mismatch | POST_CHECK | BINARY_VERSION_MISMATCH | null |

기존 문자열은 compatibility mapping으로 읽을 수 있지만 신규 코드는 regex에 의존하지 않고 `OtaFailure(stage, reason_code, detail)` 같은 typed exception/result를 사용한다.

### AI logging 확장

현재 코드가 생성하는 필드:

```text
ai_mode_requested
ai_mode_used
ai_fallback_reason
ai_risk_score
ai_recommended_rank
ai_execution_rank
ai_feature_deviations
ai_feature_contributions
ai_profile_version
ai_profile_hash
```

추가:

```text
ai_model_type
ai_model_version
ai_model_hash
ai_feature_schema_version
ai_anomaly_score
ai_failure_probability
ai_fallback_chain
ml_label_eligible
ml_label
failure_stage
failure_reason_code
```

`SUPERVISED`에서는 `ai_risk_score`를 failure probability로 정의하고 IF에서는 normalized anomaly risk로 정의한다. 원본 score는 `ai_anomaly_score`에 보존한다.

---

## 12. 수정 대상 파일 및 함수 목록

| 파일 | 함수 | 현재 역할 | 수정 내용 |
|---|---|---|---|
| `ecu/installer.py` | `install_serial_firmware()` | Safety/Feature/AI/OTA 통합 | RiskEngine 호출, AI 전체 예외 격리, 실행 직전 Safety 재검사 |
| `ecu/installer.py` | `_install_one_serial_firmware()` | 단일 보드 transfer/post-check | typed failure stage/reason, pre-update status 반환/재검사 연결 |
| `ecu/safety_policy.py` | `evaluate_policy()` | ALLOW/HOLD/BLOCK | voltage/temp/telemetry 정책, 재검사용 동일 API |
| `ecu/feature_collector.py` | `_history_features()` | failure/reset history | 최근 10회, label eligibility, 예상 reset 제외 |
| `ecu/feature_collector.py` | `collect_features()` | Feature 생성 | image size ratio, schema version, history denominator |
| `ecu/experiment_logger.py` | `append()` | JSONL append | log schema version, validation, dual-write |
| 신규 `ecu/failure_taxonomy.py` | classifier/API | 없음 | Stage/Reason/label eligibility 중앙 정의 |
| 신규 `ecu/reset_history.py` | history API | 없음 | boot/reset event 저장·분류·window 계산 |
| `ecu/secondary_serial.py` | parser/status/transfer | STATUS, UART OTA | scenario fields, typed Serial 오류, fault protocol |
| `ai/anomaly_scorer.py` | 기존 함수 | Statistical AI | compatibility wrapper 또는 statistical adapter |
| `ai/normal_profile.py` | profile 함수 | Median/MAD profile | schema metadata 강화 |
| 신규 `ai/feature_schema.py` | schema/validator | 없음 | Feature order/type/finite validation |
| 신규 `ai/risk_engine.py` | `rank_allow_ecus()` | 없음 | 4 Mode와 fallback chain |
| 신규 `ai/statistical_engine.py` | score/rank | 없음 | 기존 scorer 이동 |
| 신규 `ai/dataset_loader.py` | load/validate | 없음 | baseline/experiment loader |
| 신규 `ai/train_isolation_forest.py` | train/export | 없음 | pipeline 학습, metadata/hash |
| 신규 `ai/model_loader.py` | load/verify | 없음 | artifact/metadata 검증과 cache |
| 신규 `ai/isolation_forest_engine.py` | inference | 없음 | anomaly/risk 계산 |
| 신규 `ai/supervised_engine.py` | inference adapter | 없음 | DT/RF 확장점 |
| Slot A/B `main.c` | STATUS/transfer/reset | STM32 application | scenario, reset context, fault injection |
| Bootloader `main.c` | `Select_And_Jump()` | Slot boot | boot event/confirmation/failure injection |
| 신규 `experiments/scenario_runner.py` | runner | 없음 | 20+ campaign 자동화 |
| 신규 `experiments/validate_dataset.py` | validator | 없음 | schema/label/class balance/중복 검사 |

---

## 13. 전체 세부 Task

| Task | 목적 | 대상 파일·함수 / 구현·I/O | 선행→후속 | 테스트 | 난이도·작업량 |
|---|---|---|---|---|---|
| T1 Failure taxonomy | 안정적 실패 정의 | 신규 `failure_taxonomy.py`; exception/result → stage/reason/eligibility | 없음 → T2,T6,T13 | 모든 reason mapping | 중·2일 |
| T2 previous failures | 보드 실패 최근 10회 계산 | `_history_features()`; history → count/rate/n | T1 → T7,T10 | 외부 실패 제외/window | 중·2일 |
| T3 Reset classification | 정상 OTA reset 제외 | 신규 `reset_history.py`, collector; STATUS/events → unexpected count | C reset contract → T6 | expected/unexpected/dedup | 상·3일 |
| T4 Feature schema v2 | Feature order/type 고정 | 신규 `feature_schema.py`; dict → validated vector | 계약 → ML 전부 | missing/NaN/Inf/order | 중·2일 |
| T5 Flash Feature 개선 | target image 부담 반영 | `collect_features()`; length/MAX → ratio | T4 → supervised | boundary/oversize | 하·1일 |
| T6 Logging/ML label | 학습 가능한 결과 기록 | installer/logger/taxonomy; result → schema v2 row | T1~T3 → dataset | old/new 호환 | 상·3일 |
| T7 Safety 재검사 | ranking 후 안전 보장 | installer/policy; fresh status → decision | T4 → integration | ALLOW→HOLD 시 미전송 | 상·3일 |
| T8 AI Mode/RiskEngine | 4 Mode interface | 신규 `risk_engine.py`; ALLOW context → RiskResult | T4 → T9,T10 | mode/shadow/order | 상·3일 |
| T9 Statistical adapter | 현재 동작 보존 | scorer/profile → statistical engine | T8 → fallback | 기존 test 동등성 | 중·2일 |
| T10 IF loader/training | 정상 모델 artifact 생성 | loader/train; JSONL → joblib/metadata | T4 → T11 | deterministic/hash | 상·4일 |
| T11 IF inference/fallback | runtime anomaly ranking | loader/IF engine; vectors → risks | T8~T10 | corrupt/missing/version | 상·4일 |
| T12 Supervised extension | DT/RF 연결점 | supervised engine; vectors → failure probability | T4,T6,T8 | mock probability | 중·2일 |
| T13 STATUS/telemetry injection | Feature scenario 재현 | Slot A/B STATUS handler | scenario contract → T16 | ACK/status 반영 | 상·4일 |
| T14 Transfer injection | timeout/interruption/corruption | serial send + STM32 receive | T1,T13 → dataset | reason/복구 | 최상·4일 |
| T15 Reset/boot/health injection | reboot 계열 실패 | Slot A/B + bootloader | T3,T13 → dataset | recovery/context | 최상·5일 |
| T16 Scenario runner | 60+ ALLOW rows 수집 | experiments 도구; matrix → JSONL | T6,T11,T13~15 | 중복/label/count | 상·4일 |
| T17 통합 테스트 | 전체 계약 확인 | Primary/AI/STM32 tests | 전체 → supervised | 23절 Test | 상·3일 |

---

## 14. 정확히 3개의 Work Package

```text
팀원 1 — Work Package A
Primary ECU / Data / History / Safety / Logging

팀원 2 — Work Package B
AI / ML / Model / Risk Engine

팀원 3 — Work Package C
STM32 / Serial Fault Injection / Experiment
```

기본 권장 구조를 유지하는 것이 실제 repository에도 가장 적합하다. 충돌 방지를 위해 `secondary_serial.py`는 Primary 디렉터리에 있지만 UART protocol과 fault injection을 담당하는 팀원 3 소유로 배치한다.

---

## 15. Work Package별 상세 구현 범위

### 팀원 1 — Work Package A: Primary ECU / Data & History

책임:

- Failure Stage/Reason taxonomy
- `previous_failures` 최근 10회와 eligibility
- reset history 소비 및 `recent_reset_count`
- Feature extraction과 image size ratio
- ML label
- experiment log schema v2
- Safety threshold와 ranking 후 재검사
- B의 RiskEngine을 `installer.py`에 연결

주요 소유 파일:

```text
Primary_ECU/ecu/installer.py
Primary_ECU/ecu/safety_policy.py
Primary_ECU/ecu/feature_collector.py
Primary_ECU/ecu/experiment_logger.py
Primary_ECU/ecu/secondary_state.py
Primary_ECU/ecu/failure_taxonomy.py
Primary_ECU/ecu/reset_history.py
Primary_ECU/tests/test_ota_*.py
```

산출물:

- 신뢰 가능한 history Feature
- 표준 Stage/Reason
- 학습 label
- 업데이트 직전 Safety 재검사
- backward-compatible JSONL

예상 난이도/작업량: 상, 약 10~12일
기존/신규 포함 수정 파일: 약 9~11개
Primary 수정: 예
STM32/ML 내부 수정: 아니오
테스트: policy, history, label, orchestration

### 팀원 2 — Work Package B: AI / ML

책임:

- Feature schema와 order validation
- Statistical scorer adapter
- 4 Mode RiskEngine
- baseline/experiment Dataset Loader
- RobustScaler pipeline
- Isolation Forest training/export
- model metadata/hash/load/inference
- anomaly risk calibration
- IF→Statistical→Fixed fallback
- supervised model adapter

주요 소유 파일:

```text
Primary_ECU/ai/*
Primary_ECU/models/*
Primary_ECU/tests/test_ai_*.py
```

산출물:

- `RiskResult` API
- Isolation Forest artifact/metadata
- deterministic model loader
- Decision Tree/Random Forest 확장점

예상 난이도/작업량: 상, 약 10~12일
수정/추가 파일: 약 9~12개
ML 수정: 예
Primary orchestration/STM32 수정: 아니오
테스트: schema, train determinism, load, inference, fallback, ranking

### 팀원 3 — Work Package C: STM32 / Fault Injection & Experiment

책임:

- scenario control protocol
- STATUS delay
- voltage/temperature telemetry override
- transfer timeout/interruption/corruption
- reset during transfer
- boot/post-health failure
- reset context/boot ID
- Primary UART parser/protocol extension
- scenario runner
- 60+ ALLOW dataset 실험과 validation

주요 소유 파일:

```text
Primary_ECU/ecu/secondary_serial.py
STM32_Workspace/OTA_LED_A_TEST/Src/main.c
STM32_Workspace/OTA_LED_B_TEST/Src/main.c
STM32_Workspace/OTA_BOOTLOADER/Src/main.c
STM32_Workspace/tests/*
Primary_ECU/experiments/*
```

산출물:

- 재현 가능한 fault firmware/protocol
- scenario 자동화 도구
- raw experiment dataset
- reset context telemetry

예상 난이도/작업량: 최상, 약 10~13일
수정/추가 파일: 약 8~10개
STM32/Primary protocol 수정: 예
ML 코드 수정: 아니오
테스트: firmware build, UART, recovery, repeatability, hardware E2E

### 균형 평가

| 항목 | A | B | C |
|---|---:|---:|---:|
| 난이도 | 상 | 상 | 최상 |
| 예상 작업량 | 10~12일 | 10~12일 | 10~13일 |
| 파일 수 | 9~11 | 9~12 | 8~10 |
| STM32 수정 | 없음 | 없음 | 있음 |
| Primary 수정 | 핵심 orchestration | API/AI 내부 | Serial protocol |
| ML 수정 | schema 소비 | 핵심 | 없음 |
| 테스트 범위 | 정책/로그/history | model/fallback | firmware/hardware |

C는 hardware 부담이 크므로 Dataset Loader와 label 작업은 A/B에 남기고 C는 데이터 생성과 검증 실행까지만 담당한다.

---

## 16. 팀 간 Interface

### A → B: FeatureContext

```json
{
  "secondary_id": "stm32-led-001",
  "uid": "066C...",
  "feature_schema_version": 2,
  "features": {
    "link_response_ms": 25.1,
    "supply_voltage_mv": 3295.0,
    "temperature_c": 39.8,
    "image_size_ratio": 0.36,
    "previous_failures": 1,
    "recent_reset_count": 0
  },
  "telemetry_valid": true
}
```

규칙:

- ALLOW ECU만 전달
- pre-update에서 알 수 있는 값만 전달
- 값은 bool이 아닌 finite number
- key order가 아니라 `feature_schema_version`과 B의 schema가 순서를 결정

### B → A: RiskResult

```json
{
  "requested_mode": "ISOLATION_FOREST",
  "used_mode": "ISOLATION_FOREST",
  "recommended_order": [
    "stm32-led-002",
    "stm32-led-001"
  ],
  "execution_order": [
    "stm32-led-002",
    "stm32-led-001"
  ],
  "scores": {
    "stm32-led-001": {
      "risk_score": 0.42,
      "raw_score": -0.11
    }
  },
  "model_type": "ISOLATION_FOREST",
  "model_version": "isolation-forest-v1",
  "model_hash": "...",
  "fallback_chain": [],
  "fallback_reason": null
}
```

규칙:

- Safety decision을 포함하거나 변경하지 않음
- 입력 ECU 집합과 출력 ECU 집합이 동일
- risk가 같으면 fixed order
- 실패 시에도 deterministic execution order 반환

### C → A: STATUS/Scenario contract

```text
STATUS,<ecu>,
UID=...,
VER=...,
ACTIVE=A,
TARGET=B,
READY=1,
MAX=49152,
VDD_MV=...,
TEMP_MC=...,
APP_USED=...,
APP_FREE=...,
UPTIME_MS=...,
RESET=SOFTWARE,
RESET_CONTEXT=OTA_ACTIVATION,
BOOT_ID=42,
SCENARIO=TRANSFER_TIMEOUT,
UART_ERR=0,
HEALTH=OK
```

A는 parsed status와 reset event를 소비하고 C는 wire format과 parser를 함께 소유한다.

### A → C: Attempt context

```json
{
  "campaign_id": "...",
  "attempt_id": "...",
  "secondary_id": "...",
  "scenario_id": "RESET_DURING_TRANSFER",
  "target_slot": "B",
  "target_version": "2.2.0"
}
```

---

## 17. Git Conflict 위험 분석

| 팀 | 주요 소유 파일 | 공동 가능 파일 | 높은 충돌 위험 | 방지 방법 |
|---|---|---|---|---|
| A | `installer.py`, policy, collector, logger | `feature_schema.py` import | `installer.py` | B는 installer를 수정하지 않고 API만 제공 |
| B | `ai/*`, `models/*`, AI tests | feature contract | 기존 `anomaly_scorer.py` | B 단독 소유, A는 wrapper/API만 호출 |
| C | `secondary_serial.py`, STM32 `main.c`, experiments | failure codes | `secondary_serial.py` | A는 serial exception text를 수정하지 않고 taxonomy API로 소비 |

단독 소유 권장:

```text
installer.py          → 팀원 1
ai/risk_engine.py     → 팀원 2
secondary_serial.py   → 팀원 3
Slot A/B main.c       → 팀원 3
```

통합 계약은 코드보다 먼저 합의하고 각 브랜치에서 임의 변경하지 않는다.

---

## 18. 병렬 개발 가능 범위

### 즉시 병렬 시작

```text
팀원 1
- Failure taxonomy
- history filter/window
- log schema/label
- Safety 재검사 설계

팀원 2
- Feature schema validator
- RiskEngine interface
- Statistical adapter
- Dataset Loader/IF training 구조

팀원 3
- Fault scenario protocol
- STATUS delay/telemetry override
- reset marker 설계
- transfer injection 구조
```

### 선행 계약이 필요한 부분

- A의 `previous_failures`: failure taxonomy 합의 후
- B의 model: Feature schema 확정 후
- C의 reset event: `RESET_CONTEXT/BOOT_ID` 계약 후
- A의 installer integration: B의 `RiskResult` API 확정 후
- Dataset 수집: A의 logging/label과 C의 scenario 통합 후

---

## 19. 업무 의존성

```text
                  공통 계약
      Feature Schema / Failure Code / STATUS Schema
            ┌──────────┼──────────┐
            ↓          ↓          ↓
   Work Package A  Work Package B  Work Package C
   History/Label   Risk Engine     Fault Protocol
   Safety/Logging  IF Model        STM32 Scenario
            │          │          │
            └──────┐   │   ┌──────┘
                   ↓   ↓   ↓
              Primary Integration
                      ↓
               Scenario Campaign
                      ↓
             60+ ALLOW OTA Dataset
                      ↓
          Decision Tree / Random Forest
```

A와 B가 같은 파일을 수정하는 대신 B가 `RiskResult`를 제공하고 A가 installer에 연결한다. C는 wire protocol과 실험 환경을 제공하며 A의 logger가 결과를 label한다.

---

## 20. 추천 구현 순서

### Phase 1 — 공통 기반 계약

- Failure Stage/Reason enum
- `ml_label_eligible` 기준
- Feature schema v1/v2와 order
- RiskResult interface
- STATUS의 `BOOT_ID`, `RESET_CONTEXT`, `SCENARIO`
- Safety voltage/temperature threshold
- scenario matrix와 recovery 규칙

### Phase 2 — 3인 병렬 개발

- 팀원 1: history, taxonomy, logging, label, Safety 재검사
- 팀원 2: RiskEngine, Statistical adapter, loader, IF pipeline
- 팀원 3: firmware scenario, serial parser, fault injection

### Phase 3 — Statistical 구조 통합

- 기존 Statistical 결과와 동등성 확인
- `OFF / STATISTICAL`
- SHADOW/ACTIVE 적용 옵션
- 전체 AI 예외가 fixed order로 격리되는지 확인

### Phase 4 — Isolation Forest 통합

```text
FeatureContext
→ schema validator
→ persisted preprocessing
→ Isolation Forest
→ calibrated risk
→ ranking
→ fresh Safety recheck
→ OTA
```

### Phase 5 — Fault Injection Dataset

```text
Scenario set/ACK
→ preflight STATUS
→ Safety ALLOW 확인
→ OTA/fault
→ standardized result
→ label
→ JSONL validation
```

20 campaigns × 3 boards로 최소 60 ALLOW attempts를 확보한다.

### Phase 6 — Supervised model

- label eligible row만 선택
- campaign 단위 train/test split
- Decision Tree baseline
- Random Forest 비교
- class imbalance와 calibration 확인
- failure probability 기반 scheduling
- `SUPERVISED → STATISTICAL → FIXED` fallback

---

## 21. 최종 3인 업무분배 표

| 구분 | 팀원 1 | 팀원 2 | 팀원 3 |
|---|---|---|---|
| 담당 영역 | Primary/Data/History/Safety/Logging | AI/ML/Model/Risk Engine | STM32/Serial/Fault/Experiment |
| 주요 Task | failure taxonomy, previous/reset history, label, Safety 재검사 | 4 Mode, statistical adapter, IF train/load/inference, fallback | STATUS/telemetry injection, transfer/reset/boot faults, runner |
| 주요 수정 파일 | `installer.py`, `safety_policy.py`, `feature_collector.py`, logger/history | `ai/*`, `models/*`, AI tests | `secondary_serial.py`, Slot A/B `main.c`, bootloader, experiments |
| 핵심 산출물 | schema v2 experiment row, 신뢰 가능한 history Feature | RiskResult, IF artifact/metadata, deterministic fallback | 재현 가능한 fault protocol, 60+ attempt dataset |
| 선행 조건 | failure/reset contract | Feature schema | STATUS/scenario/recovery contract |
| 다른 팀원과 Interface | FeatureContext ↔ RiskResult, ResetEvent | FeatureContext 입력, RiskResult 출력 | STATUS/Scenario/AttemptContext |
| 난이도 | 상 | 상 | 최상 |
| 작업량 | 10~12일 | 10~12일 | 10~13일 |

---

## 22. 각 팀원의 체크리스트

### 팀원 1

- [ ] Failure Stage enum 확정
- [ ] Reason code와 board-related 여부 구현
- [ ] 외부 실패 label 제외
- [ ] 최근 10회 `previous_failures` 구현
- [ ] `previous_failure_rate`, denominator 기록
- [ ] ResetEvent history 소비
- [ ] 정상 OTA reset 제외
- [ ] `image_size_ratio` Feature 추가
- [ ] log schema version 추가
- [ ] `ml_label_eligible`, `ml_label` 기록
- [ ] AI field와 model metadata 기록
- [ ] ranking 후 fresh STATUS Safety 재검사
- [ ] Safety 결과 변경 시 OTA 미실행 확인
- [ ] 기존 JSONL backward compatibility 테스트
- [ ] policy/history/orchestration unit test

### 팀원 2

- [ ] Feature schema/version/order 구현
- [ ] NaN/Inf/missing/type validator
- [ ] 기존 Statistical scorer adapter화
- [ ] OFF/STATISTICAL/IF/SUPERVISED Mode 구현
- [ ] SHADOW/ACTIVE 적용 옵션 분리
- [ ] baseline Dataset Loader 구현
- [ ] experiment Dataset Loader 호환 처리
- [ ] persisted preprocessing pipeline 구현
- [ ] deterministic IF training
- [ ] model metadata/hash 생성
- [ ] model loader 검증
- [ ] anomaly score calibration
- [ ] ALLOW ECU만 ranking되는지 테스트
- [ ] IF→Statistical→Fixed fallback 테스트
- [ ] supervised `predict_proba()` adapter
- [ ] 모델/feature version mismatch 테스트

### 팀원 3

- [ ] Scenario control protocol 정의
- [ ] `FAULT_SET`/`FAULT_ACK` 구현
- [ ] STATUS에 `SCENARIO`, `BOOT_ID`, `RESET_CONTEXT` 추가
- [ ] STATUS delay 3단계 구현
- [ ] Voltage ALLOW/BLOCK override
- [ ] Temperature ALLOW/BLOCK override
- [ ] finite firmware receive timeout
- [ ] transfer interruption injection
- [ ] transfer corruption injection
- [ ] reset-during-transfer injection
- [ ] persistent OTA activation marker
- [ ] boot failure test hook과 recovery
- [ ] post-reboot health failure
- [ ] Primary serial parser 확장
- [ ] scenario runner 구현
- [ ] 20 campaign 반복 실행
- [ ] 60개 이상 ALLOW attempt 검증
- [ ] 다음 scenario 전 보드 정상 복구 확인

---

## 23. 통합 시 확인할 Test Case

### Safety–AI 경계

1. HOLD ECU가 `RiskEngine` 입력에 없음
2. BLOCK ECU가 score/rank에 없음
3. AI 결과에 임의 ECU가 추가되면 전체 결과 거부
4. AI 순서와 관계없이 Safety 결정은 변경되지 않음
5. preflight ALLOW 후 voltage가 악화되면 직전 재검사에서 HOLD
6. 직전 재검사 실패 ECU에 `FW_BEGIN`이 전송되지 않음
7. model/import/inference 오류가 Safety를 우회하지 않음

### Feature/History

8. 최근 10 eligible attempts만 사용
9. metadata/server/package 실패는 previous failure에서 제외
10. transfer/boot/health 실패는 포함
11. 정상 OTA activation reset 제외
12. Watchdog/Brown-out/reset loop 포함
13. 동일 boot의 STATUS 5회가 reset 5회로 계산되지 않음
14. image size ratio 0/1 경계와 oversize 거부

### AI/Model

15. missing/corrupt model → Statistical
16. corrupt statistical profile → fixed order
17. NaN/Inf/missing Feature → deterministic fallback
18. feature order mismatch → model 사용 거부
19. model hash/version mismatch → model 사용 거부
20. risk tie → 001/002/003 fixed tie-break
21. SHADOW에서 recommended와 execution 분리

### OTA/Fault

22. STATUS delay가 preflight Feature에 반영
23. transfer timeout reason이 `TRANSFER_TIMEOUT`
24. 변조 byte가 `TRANSFER_CORRUPTION`
25. 중간 reset이 표준 reason으로 기록
26. bootloader target 실패가 `BOOT_FAILED`
27. HEALTH=ERROR가 `POST_REBOOT_HEALTH_FAILED`
28. version mismatch는 `ml_label=null`
29. 실패 후 다음 campaign에서 보드 복구
30. 최소 60 ALLOW attempts, ID 중복 없음

### Dataset

31. `ml_label=0`은 eligible 성공에만 존재
32. `ml_label=1`은 eligible ECU/통신 실패에만 존재
33. 외부 실패는 `ml_label=null`
34. train/test split이 행 단위가 아니라 campaign 단위
35. 동일 scenario 반복이 train/test에 과도하게 중복되지 않음

---

## 24. 구현 전에 팀원끼리 결정해야 할 사항

1. 전압/온도 Safety threshold와 경계값에서 HOLD/BLOCK 중 무엇을 사용할지
2. `HASH_VERIFY_FAILED`를 artifact hash와 transfer corruption으로 어떻게 분리할지
3. post-update version mismatch를 항상 외부 실패로 볼지 firmware 오동작 reason을 별도로 둘지
4. reset marker 저장을 backup register와 reserved flash 중 무엇으로 할지
5. `recent_reset_count` window를 최근 10 OTA attempts로 고정할지 시간 window도 병행할지
6. Feature schema v2에서 `app_flash_free_ratio`를 즉시 제거할지 dual-record할지
7. IF를 전체 보드 공통 모델로 할지 보드별 모델로 할지
8. `RobustScaler`를 사용할지 raw tree pipeline으로 갈지
9. IF anomaly score normalization percentile을 p95/p99 중 무엇으로 할지
10. 기존 `ACTIVE/SHADOW` 환경변수 호환 기간
11. JSONL Feature top-level dual-write 기간
12. failure taxonomy enum을 A/C가 공유하는 방식
13. fault scenario의 보드 복구 절차와 hardware watchdog
14. 실패 scenario별 반복 횟수와 class balance
15. model artifact와 metadata를 Git에 포함할지 release artifact로 배포할지
16. sklearn/joblib 버전 고정 방법
17. Dataset에 UID·port 같은 식별 정보를 어느 수준까지 남길지
18. SUPERVISED 평가 지표를 recall/false-negative 중심으로 둘지

가장 먼저 합의해야 할 세 가지는 `Feature schema`, `Failure taxonomy`, `STATUS/reset protocol`이다. 이 세 계약만 고정하면 팀원 1은 `installer.py`, 팀원 2는 `ai/`, 팀원 3은 STM32와 `secondary_serial.py`를 독립 브랜치에서 진행할 수 있고 최종 통합 지점도 `RiskResult` 호출과 STATUS dict 두 곳으로 제한된다.
