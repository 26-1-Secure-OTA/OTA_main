# Secure OTA AI 업데이트 스케줄링 계획

이 문서는 현재 `next1` 구현을 기준으로 작성한다.

- 현재 구현된 기능은 **현재 구현**으로 표시한다.
- 앞으로 추가할 기능은 **AI 구현 예정**으로 표시한다.
- AI는 업데이트 허용 여부를 결정하지 않는다.
- 전압·온도·Flash 실측값은 현재 Safety Policy 판단에 사용하지 않는다.

---

## 1. 전체 구조와 AI의 역할

### 현재 구현

```text
Metadata/Artifact 검증
→ STM32 STATUS 수집
→ Safety Policy의 ALLOW/HOLD/BLOCK 판단
→ 고정 순서 001 → 002 → 003 업데이트
→ 결과와 실측 Feature를 JSONL에 기록
```

### AI 구현 예정

```text
Metadata/Artifact 검증
→ STM32 STATUS 수집
→ Safety Policy의 ALLOW/HOLD/BLOCK 판단
→ ALLOW 보드만 AI에 입력
→ AI가 OTA 최종 실패 위험 점수 예측
→ 위험이 낮은 ALLOW 보드부터 순서 결정
→ 각 보드 업데이트 직전 Safety Policy 재검사
→ OTA 실행
```

AI는 다음 결정을 할 수 없다.

```text
ALLOW/HOLD/BLOCK 변경
HOLD/BLOCK 보드 강제 업데이트
Metadata/Hash/UID 검증 우회
대상 ECU 또는 Slot 변경
다운그레이드 허용
```

AI가 실패하거나 결과가 유효하지 않으면 다음 고정 순서를 사용한다.

```text
stm32-led-001 → stm32-led-002 → stm32-led-003
```

---

## 2. 현재 구현 상태

### 완료된 항목

- STM32 3대의 ECU ID와 MCU UID 검증
- Bootloader와 Slot A/B OTA
- Metadata 및 Firmware SHA-256 검증
- Slot 전환 후 Version·HEALTH 확인
- 실제 STM32 VDD, 내부 다이 온도, Flash 사용량 수집
- 실측값을 `ota_experiments.jsonl`에 기록
- Safety Policy와 실측 AI Feature 분리
- 고정 순서 업데이트와 실패 시 상태 기록

### 현재 실제 업데이트 순서

`Primary_ECU/ecu/installer.py`는 다음 순서를 사용한다.

```text
001 → 002 → 003
```

### 아직 구현되지 않은 항목

- Decision Tree 모델 학습 및 저장
- `failure_risk` 예측
- AI 기반 보드 순서 정렬
- `model_version`, `model_hash`, `feature_schema_version` 검증
- `link_response_median_ms`
- `link_response_jitter_ms`
- `recent_timeout_rate`
- 실제 Serial 재시도 및 재전송
- 세분화된 실패 단계
- AI Scheduler와 Fixed Scheduler 선택 로직
- Primary Scheduler 내부의 dependency graph 검사

---

## 3. 데이터 용어

```text
행 1개
= STM32 한 대의 OTA 판단 및 시도 결과 1개

Campaign 1개
= 한 번의 전체 OTA 실행
= 하나의 campaign_id

Scenario 1개
= 동일한 실험 조건으로 실행한 하나의 Campaign
= 하나의 scenario_id를 세 STM32 행이 공유

Dataset 1개
= 여러 Campaign의 모든 실험 행을 모은 데이터
```

예:

```text
Scenario 20개 × 보드 3대
= 최대 60행
```

단, HOLD/BLOCK 또는 OTA 미실행 행은 AI 학습 행에서 제외하므로 실제 학습 행은 60행보다 적다.

---

## 4. AI 학습 행 선택 기준

다음 조건을 모두 만족하는 행만 최종 실패 위험 모델 학습에 사용한다.

```python
policy_decision == "ALLOW"
update_attempted is True
success in (True, False)
```

다음 행은 학습에서 제외한다.

```text
HOLD
BLOCK
OTA 미실행
success == null
필수 Feature 누락
telemetry_valid != true인 텔레메트리 모델 입력
```

HOLD/BLOCK 행은 삭제하지 않는다. Safety Policy 검증용 데이터로 별도 분석한다.

---

## 5. Label 정의

필터링된 학습 행에서 다음과 같이 Label을 생성한다.

```python
failure_label = 0 if success is True else 1
```

### `failure_label = 0`

- `FW_OK` 수신
- 목표 Slot 활성화
- 목표 Version 확인
- `HEALTH=OK`
- 최종 OTA 성공

### `failure_label = 1`

- Safety Policy는 ALLOW였음
- 실제 OTA를 시도함
- 전송, Hash 검증, 재부팅, Slot 전환, Version 확인 또는 Health Check 중 최종 실패

현재 Serial 프로토콜에는 재시도가 없으며 `retry_count=0`이다.

향후 재시도를 추가한 뒤 재시도 후 최종 성공한 행은 최종 실패 모델에서 `failure_label=0`으로 처리한다. 단, 불안정성 분석을 위해 다음 별도 결과 필드를 둘 수 있다.

```text
had_retry
retry_count
degraded_update_label
```

---

## 6. 데이터 Leakage 방지

AI 입력에는 OTA 시작 전에 알 수 있는 값만 사용한다.

### 입력 금지

```text
success
failure_label
failure_stage
failure_reason_code
이번 OTA의 retry_count
이번 OTA의 update_duration_ms
이번 OTA의 transfer_duration_ms
이번 OTA의 throughput_kbps
FW_OK 수신 여부
slot_switched
version_verified
active_slot_after
Rollback 발생 여부
```

이 값들은 결과 분석과 Label 생성에만 사용한다.

---

## 7. 현재 수집되는 Feature

현재 `Primary_ECU/ecu/feature_collector.py`가 생성하는 값은 다음과 같다.

| 필드 | 의미 | 현재 Board 값 |
| --- | --- | --- |
| `supply_voltage_mv` | STM32 VREFINT 기반 VDD | 실제 측정값 |
| `temperature_c` | STM32 내부 다이 온도 | 실제 측정값 |
| `power_good` | STM32가 계산한 VDD 범위 플래그 | 실제 측정 기반 |
| `telemetry_valid` | ADC 텔레메트리 유효성 | 실제 측정 기반 |
| `app_flash_free_ratio` | 현재 실행 이미지 기준 Flash 여유율 | 실제 Linker 정보 기반 |
| `free_flash_ratio` | 대상 이미지 설치 후 Slot 여유율 | 이미지 크기 기반 |
| `link_response_ms` | 업데이트 전 STATUS 1회 응답시간 | 실제 통신값 |
| `recent_retry_rate` | 과거 OTA 중 retry가 있었던 비율 | 현재 항상 0에 가까움 |
| `previous_failures` | 과거 최종 실패 횟수 | 로그 이력 기반 |
| `image_size` | 대상 Firmware 크기 | bytes |
| `recent_reset_count` | 최근 uptime 감소 횟수 | Primary가 로그에서 계산 |
| `power_percent` | Simulator 전용 값 | Board에서는 `null` |

### 값 구분

```text
app_flash_free_ratio
→ 현재 실행 중인 Firmware가 차지한 공간을 제외한 비율

free_flash_ratio
→ 새 대상 Firmware를 Slot에 썼을 때 남을 비율
```

두 값은 의미가 다르지만 상관관계가 높을 수 있으므로 첫 모델에 동시에 넣지 않는다.

---

## 8. 첫 AI 모델 Feature Schema v1

첫 모델은 현재 실제로 수집되고 의미가 있는 다음 6개부터 사용한다.

```text
link_response_ms
previous_failures
supply_voltage_mv
temperature_c
app_flash_free_ratio
recent_reset_count
```

다음 값은 기록하지만 첫 모델 입력에서는 제외한다.

```text
power_good
telemetry_valid
free_flash_ratio
image_size
recent_retry_rate
power_percent
scenario_id
campaign_id
secondary_id
current_version
target_version
active_slot_before
health
reset_cause
```

제외 이유:

- `telemetry_valid`는 Feature가 아니라 입력 유효성 검사에 사용한다.
- `power_good`은 `supply_voltage_mv`에서 파생되어 중복될 수 있다.
- `recent_retry_rate`는 현재 재시도 프로토콜이 없어 정보량이 없다.
- ID와 Version 문자열은 소규모 데이터에서 과적합을 유발하기 쉽다.
- `health`는 Safety Policy를 통과한 ALLOW 데이터에서 대부분 `OK`로 고정된다.

### Feature Schema v2 후보

충분한 이력이 쌓인 다음 아래 값을 추가 검토한다.

```text
link_response_median_ms
link_response_jitter_ms
recent_timeout_rate
recent_retry_rate
uart_error_rate
image_size_ratio
```

Schema v2 값은 구현과 테스트를 완료하기 전에는 모델 입력으로 사용하지 않는다.

---

## 9. Safety Policy와 AI Feature 분리

### 현재 Safety Policy 판단 항목

현재 `Primary_ECU/ecu/safety_policy.py`는 다음을 검사한다.

```text
검증된 Artifact 존재 여부
Secondary 온라인 여부
ECU ID 일치
Registry UID 존재 및 실제 UID 일치
필수 STATUS 필드 존재
대상 Slot 일치
현재 Version과 목표 Version
동일 Version 및 다운그레이드
READY
HEALTH
WATCHDOG Reset
UART Error 임계치
STATUS 응답시간 임계치
```

Metadata 서명, Firmware Hash, Firmware 크기와 실제 Link Slot 검사는 Policy 호출 전후의 Artifact 검증 및 Serial 전송 계층에서도 수행한다. 모두 AI보다 먼저 통과해야 하는 보안·안전 Gate다.

### 현재 Safety Policy에 사용하지 않는 실측값

```text
supply_voltage_mv
temperature_c
power_good
app_flash_free_ratio
```

이 값들은 로그와 AI 위험도 예측에만 사용한다.

향후 온도·전압을 Safety Policy 절대 임계치로 추가하려면 별도 팀 합의, 테스트 및 `policy_version` 변경이 필요하다. AI 구현 과정에서 임의로 정책에 추가하지 않는다.

---

## 10. 현재 공통 로그 Schema

로그 파일:

```text
Primary_ECU/logs/ota_experiments.jsonl
```

현재 기록되는 주요 필드는 다음과 같다.

### 식별 및 실험 조건

```text
scenario_id
campaign_id
secondary_id
data_source
collected_at
current_version
target_version
```

### 업데이트 전 Feature

```text
supply_voltage_mv
temperature_c
power_good
telemetry_valid
app_flash_free_ratio
free_flash_ratio
link_response_ms
recent_retry_rate
previous_failures
image_size
recent_reset_count
power_percent
uptime_ms
reset_cause
uart_error_count
health
active_slot_before
```

### 정책 및 결과

```text
policy_decision
policy_reason_code
update_attempted
success
failure_stage
failure_reason_code
update_duration_ms
transfer_duration_ms
throughput_kbps
retry_count
slot_switched
version_verified
active_slot_after
```

측정할 수 없는 값은 가짜 숫자를 넣지 않고 `null`로 기록한다.

---

## 11. 단위와 값 형식

| 필드 | 단위·형식 |
| --- | --- |
| `scenario_id` | 문자열 |
| `campaign_id` | 문자열 |
| `secondary_id` | 문자열 |
| `link_response_ms` | ms |
| `supply_voltage_mv` | mV |
| `temperature_c` | ℃ |
| `app_flash_free_ratio` | 0.0~1.0 |
| `free_flash_ratio` | 0.0~1.0 |
| `recent_retry_rate` | 0.0~1.0 |
| `image_size` | bytes |
| `update_duration_ms` | ms |
| `transfer_duration_ms` | ms |
| `throughput_kbps` | kilobits/second |
| `success` | true/false/null |

비율은 `0.0~1.0`으로 통일한다. `5%`는 `5`가 아니라 `0.05`로 저장한다.

---

## 12. Scenario ID 규칙

한 Campaign에서 생성되는 세 STM32 행은 동일한 `scenario_id`를 사용한다.

동일 조건을 반복할 때는 뒤의 실행 번호를 증가시킨다.

```text
NORMAL-001
NORMAL-002
LINK_DELAY_LOW-001
LINK_DELAY_MEDIUM-001
LINK_DELAY_HIGH_ALLOW-001
TRANSFER_DROP-001
TRANSFER_CORRUPT-001
RESET_DURING_TRANSFER-001
POST_REBOOT_HEALTH_FAIL-001
OFFLINE-001
NOT_READY-001
HEALTH_ERROR-001
UID_MISMATCH-001
SAME_VERSION-001
DOWNGRADE-001
HASH_MISMATCH-001
```

실험 전에 반드시 다음 환경변수를 지정한다.

```bash
export OTA_SCENARIO_ID=NORMAL-001
```

`UNSPECIFIED`인 행은 정식 AI 학습 데이터에서 제외한다.

---

## 13. AI 학습용 Scenario와 정책 검증 Scenario

### AI 학습에 포함될 수 있는 Scenario

Policy가 ALLOW한 뒤 실제 OTA를 시도해야 한다.

```text
정상 ALLOW
낮은 Link 지연 ALLOW
중간 Link 지연 ALLOW
임계치 미만의 높은 Link 지연 ALLOW
ALLOW 후 Serial 연결 중단
ALLOW 후 전송 데이터 손상
ALLOW 후 전송 도중 Reset
ALLOW 후 재부팅·Slot·Version·Health 확인 실패
```

### Safety Policy 검증 전용 Scenario

다음은 일반적으로 HOLD/BLOCK되므로 최종 실패 위험 모델 학습에 넣지 않는다.

```text
STATUS 응답 임계치 초과
OFFLINE
READY=0
HEALTH 이상
UID 불일치
동일 Version
다운그레이드
Metadata/Hash 검증 실패
대상 Slot 불일치
```

Hash가 잘못된 파일을 Artifact 검증 단계에서 차단한 행과, 검증된 파일이 전송 중 손상되어 STM32가 Hash 실패를 보고한 행을 구분해야 한다.

---

## 14. 데이터 분리 기준

행 단위 무작위 분리를 사용하지 않는다.

```text
학습: 60%
중간 검증: 20%
최종 평가: 20%
```

`scenario_id`를 Group으로 사용한다.

- 같은 Scenario의 세 STM32 행은 반드시 같은 Group에 둔다.
- 동일 실행에서 생성된 행을 학습과 평가에 나누지 않는다.
- 최종 평가 Scenario는 Feature 선택이나 모델 설정 변경에 사용하지 않는다.
- 성공·실패 Label이 각 분할에 포함되는지 별도로 검증한다.

---

## 15. 모델 기본 설정

첫 모델은 설명 가능성이 높은 Decision Tree를 사용한다.

```python
DecisionTreeClassifier(
    max_depth=3,
    min_samples_leaf=5,
    class_weight="balanced",
    random_state=42,
)
```

평가 항목:

```text
Confusion matrix
실패 class precision
실패 class recall
실패 class F1
Balanced accuracy
Feature importance
Decision path 기반 개별 예측 이유
Decision Tree 그림
```

일반 Accuracy만으로 모델을 평가하지 않는다.

소규모 Decision Tree의 `predict_proba()` 값은 정밀한 실제 확률이 아니라 정렬용 위험 점수로 해석한다.

---

## 16. AI 출력 Schema

```json
{
  "model_version": "decision-tree-v1",
  "model_hash": "sha256:...",
  "feature_schema_version": 1,
  "predictions": [
    {
      "secondary_id": "stm32-led-002",
      "failure_risk": 0.12,
      "reasons": [
        "link_response_ms <= 50.0",
        "previous_failures <= 0"
      ]
    }
  ]
}
```

검증 조건:

```text
failure_risk는 유한한 0~1 숫자
입력 Secondary와 출력 Secondary가 일치
누락·중복 Secondary 없음
Registry에 없는 ID 없음
model_hash 일치
feature_schema_version 일치
필수 Feature 이름과 단위 일치
```

AI는 `failure_risk` 오름차순으로 정렬 후보를 제시한다. 동일 점수는 기존 고정 순서 `001 → 002 → 003`을 사용한다.

---

## 17. AI 장애와 Fallback

다음 경우 AI 결과 전체를 폐기한다.

```text
모델 파일 없음
모델 Hash 불일치
Feature Schema 불일치
필수 Feature 누락
telemetry_valid != true
예측 Timeout
예측 Exception
위험 점수 NaN/Infinity
위험 점수 범위 오류
Secondary 결과 누락·중복
Registry에 없는 Secondary
dependency 검사 실패
```

Fallback:

```text
AI 결과 폐기
→ 고정 순서 001 → 002 → 003
→ 각 업데이트 직전 최신 STATUS 재수집
→ Safety Policy 전체 재검사
→ ALLOW 보드만 업데이트
```

로그 예정 Schema:

```json
{
  "scheduler_requested": "AI",
  "scheduler_used": "FIXED",
  "fallback_reason": "MODEL_TIMEOUT",
  "policy_recheck": true
}
```

현재 코드는 설치 직전 STATUS 재조회와 일관성 검사를 수행하지만, AI Scheduler 통합 시에는 Safety Policy 함수 전체를 다시 호출하도록 명시적으로 구현한다.

---

## 18. AI 구현 전에 반드시 해야 할 일

### P0. 데이터 식별과 로그 정리

- 모든 실험에 고유한 `OTA_SCENARIO_ID`를 지정한다.
- `UNSPECIFIED` 행을 정식 학습 데이터에서 제외한다.
- `secondary_update.jsonl`에 남은 Git 충돌 표시를 제거한다.
- JSONL 전체 행에 대해 JSON 파싱 검증을 수행한다.
- Feature 이름, 단위, `null` 규칙을 고정한다.
- `feature_schema_version=1` 명세를 확정한다.
- 원본 JSONL은 수정본과 별도로 보관한다.

### P0. 실패 Label 데이터 생성

현재 데이터 상태:

```text
전체 ota_experiments 행: 15
학습 조건 만족 행: 6
성공 행: 6
실패 행: 0
정식 scenario_id 행: 0
```

현재 데이터만으로는 실패 위험 모델을 학습할 수 없다.

첫 데모 학습 전 목표:

```text
ALLOW + update_attempted 행 60개 이상
그중 최종 실패 행 20개 이상
서로 다른 Scenario와 Campaign 포함
```

이 숫자는 최종 제품 기준이 아니라 첫 Decision Tree가 성공·실패를 모두 학습하고 Group 평가를 수행하기 위한 데모 목표다.

### P0. 실패 주입 위치 구분

AI 실패 데이터는 반드시 Policy가 ALLOW한 뒤 발생해야 한다.

```text
Policy 이전 실패
→ HOLD/BLOCK 검증 데이터

Policy 이후 실패
→ AI 최종 실패 위험 학습 후보
```

권장 Policy 이후 실패 지점:

```text
전송 중 Serial 연결 중단
전송 중 일부 데이터 손상
전송 Timeout
전송 중 Reset
FW_OK 미수신
재부팅 후 Slot 불일치
재부팅 후 Version 불일치
재부팅 후 HEALTH 오류
```

### P1. 실패 단계 세분화

현재 `failure_stage="INSTALL"` 하나로 기록되는 실패를 다음처럼 구분한다.

```text
DISCOVERY
POLICY
TRANSFER_READY
TRANSFER
HASH_VERIFY
BOOT_FLAG
REBOOT
SLOT_VERIFY
VERSION_VERIFY
HEALTH_CHECK
```

각 실패에 안정적인 `failure_reason_code`를 부여한다.

### P1. Feature 수집 안정화

- STATUS를 여러 번 측정해 median/jitter를 계산할지 결정한다.
- v1에서 단일 `link_response_ms`만 쓸 경우 문서와 코드를 그대로 유지한다.
- Timeout 이력을 기록한 뒤에만 `recent_timeout_rate`를 추가한다.
- 실제 재시도를 구현한 뒤에만 `recent_retry_rate`를 모델에 추가한다.
- `app_flash_free_ratio`와 `free_flash_ratio` 중 첫 모델에 하나만 사용한다.
- 모든 필수 숫자에 NaN/Infinity/범위 검사를 적용한다.
- Board 데이터에 환경변수로 가짜 온도·전압을 주입하지 않는다.

### P1. Scheduler 통합 계약

- AI 입력은 Safety Policy가 ALLOW한 보드로 제한한다.
- AI 입력과 출력 Secondary 집합이 완전히 같은지 검증한다.
- 낮은 위험 점수부터 정렬한다.
- 동점은 고정 순서를 사용한다.
- 모델 및 Feature Schema 오류 시 전체 AI 결과를 폐기한다.
- 각 보드 업데이트 직전에 STATUS와 Safety Policy를 재검사한다.
- dependency 기능을 이번 범위에 포함할지 명시적으로 결정한다.
- 포함한다면 AI 정렬 결과보다 dependency 제약이 우선한다.

### P1. 테스트

- 성공/실패 Label 생성 테스트
- Leakage 금지 필드 테스트
- Feature 순서·이름·단위 테스트
- `null`, NaN, Infinity 입력 테스트
- 모델 Hash 및 Schema 불일치 테스트
- 누락·중복·알 수 없는 ECU 출력 테스트
- AI Timeout/Exception Fallback 테스트
- Policy가 HOLD/BLOCK한 보드가 AI 입력에 들어가지 않는 테스트
- AI 순서와 관계없이 업데이트 직전 Policy가 재검사되는 테스트
- 같은 Scenario가 train/test에 동시에 들어가지 않는 테스트

---

## 19. 구현 순서

```text
1. Scenario Runner와 고유 scenario_id 적용
2. JSONL 정리 및 Feature Schema v1 고정
3. ALLOW 성공/실패 Scenario 데이터 수집
4. 실패 단계 및 reason_code 세분화
5. Dataset 생성·검증 스크립트 작성
6. Scenario Group 단위 train/validation/test 분리
7. Decision Tree v1 학습·평가
8. 모델·Schema Hash 저장
9. AI Scheduler 출력 검증 구현
10. Fixed Fallback 구현 및 테스트
11. 업데이트 직전 Safety Policy 전체 재검사
12. 보드 3대 End-to-End 검증
```

median/jitter/timeout/retry Feature는 v1 완료 후 v2에서 추가한다.

---

## 20. 팀 분업

### 데이터 수집 담당

```text
Scenario 실행
OTA_SCENARIO_ID 관리
Campaign과 Scenario 매핑
JSONL 원본 보관
누락값·중복값 검증
```

### STM32 담당

```text
VDD 실측
내부 다이 온도 실측
APP_USED/APP_FREE 제공
uptime_ms와 reset_cause 제공
STATUS 응답
안전한 실패 주입 Hook
```

### Primary/로그 담당

```text
link_response_ms 수집
과거 실패·Reset 이력 계산
실패 단계와 reason_code
공통 JSONL Schema
Dataset 변환
업데이트 직전 Policy 재검사
```

### AI 담당

```text
데이터 필터링
Leakage 제거
Scenario Group 분리
Decision Tree 학습·평가
model_hash와 feature_schema_version 관리
개별 예측 이유 생성
```

### Scheduler 담당

```text
ALLOW 대상만 AI 입력
AI 출력 검증
위험 점수 정렬
dependency 제약 적용 여부 결정
Fixed Fallback
Policy 재검사
```

---

## 21. 병합 전 확인표

- [ ] AI가 Safety Policy의 결정을 변경하지 않는다.
- [ ] 온도·전압은 현재 Safety Policy 판단에 사용하지 않는다.
- [ ] AI 입력은 ALLOW 보드로 제한된다.
- [ ] Board 실측값을 환경변수나 고정값으로 대체하지 않는다.
- [ ] 모든 정식 실험에 고유한 `scenario_id`가 있다.
- [ ] 학습 데이터에 성공과 실패 Label이 모두 있다.
- [ ] OTA 이후 결과값이 AI 입력에 포함되지 않는다.
- [ ] 같은 Scenario가 학습과 평가에 동시에 들어가지 않는다.
- [ ] Feature Schema 이름, 순서, 단위가 고정돼 있다.
- [ ] 모델 Hash와 Feature Schema를 검증한다.
- [ ] AI 결과 누락·중복·범위 오류를 검증한다.
- [ ] AI 실패 시 고정 순서로 전환된다.
- [ ] 각 업데이트 직전에 Safety Policy 전체를 재검사한다.
- [ ] HOLD/BLOCK 보드는 AI 순서와 무관하게 업데이트하지 않는다.
- [ ] dependency 제약의 포함 여부와 우선순위가 문서화돼 있다.

