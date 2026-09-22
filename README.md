# Safety-First Hybrid AI Secure OTA

### 🖥️ STM32 3대의 A/B 펌웨어 업데이트와 AI 기반 실행 순서 결정

신뢰할 수 있는 펌웨어를 여러 ECU에 언제, 어떤 순서로 설치할 것인가? 이 프로젝트는 보안 검증을 통과한 업데이트를 실제 보드 상태에 맞춰 배포하는 과정을 다룬다.

기존 Uptane/TUF 계열 서버·Primary 구조를 확장해, Raspberry Pi Primary ECU와 STM32 Secondary ECU 3대로 이를 실험할 수 있는 OTA 프로토타입을 제작하였다.

> 구현 기준: [`adaptive-ai`](https://github.com/26-1-Secure-OTA/OTA_main/tree/adaptive-ai) 브랜치. 서버–Primary는 MQTT·HTTPS, Primary–STM32는 보드별 USB 시리얼로 통신한다. 향후 CAN으로 확장 예정.

[실행 방법](#-실행-방법)

## 🚗 프로젝트 배경과 문제

OTA는 장치에 직접 접근하지 않고 펌웨어를 배포할 수 있지만, 잘못된 이미지나 대상 보드의 상태를 확인하지 않은 설치는 장치 운용에 영향을 줄 수 있다. 서명·해시·대상 검증은 무엇을 신뢰하고 어느 ECU에 전달할지 확인하는 출발점이다. 그러나 여러 ECU를 한 번에 갱신할 때는 지금 설치할 수 있는지, 허용된 ECU 중 무엇부터 진행할지, 결과를 어떻게 추적할지도 결정해야 한다.

예를 들어 고정 순서가 `001 → 002 → 003`인데 001의 응답이 계속 느리다면, 다른 보드의 준비 상태가 양호해도 뒤 순서의 설치가 늦어질 수 있다. 이 예시는 문제를 설명하기 위한 가정이며, AI 순서가 실제로 전체 시간을 줄이는지는 별도 실험으로 검증해야 한다.

## 🎯 목표와 검증 질문

> 동일한 메타데이터 검증과 안전 정책 아래에서, Secondary 상태를 반영한 업데이트 순서가 고정 순서보다 배포 진행에 도움이 되는가?

이를 확인하기 위해 세 가지를 구현했다.

1. 서버가 제공한 메타데이터와 이미지를 Primary에서 검증하고, 각 보드의 비활성 A/B 슬롯에 설치하는 업데이트 경로
2. 보드 식별·버전·준비 상태 등을 검사해 업데이트 허용 여부를 정하는 Safety Policy와, 허용된 보드의 실행 순서만 정하는 하이브리드 AI 스케줄러
3. 추천 순서, 실제 실행 순서, 소요시간, 성공·실패와 슬롯 변화를 남겨 고정 순서와 비교할 수 있는 기록 체계

목표는 AI가 보안·안전 판단을 대체하게 만드는 것이 아니다. 동일한 검증 조건을 유지한 채 순서 결정만 바꾸고, 시간별 완료 ECU 수·전체 소요시간·실패 및 재시도 결과를 비교하는 것이다. 순서 변경의 성능 이점은 아직 입증된 결과가 아닌 평가 과제이다.

## 🏗️ 시스템 구성

![Secure OTA 시스템 구성: 서버, Primary 내부 판단 흐름, STM32 A/B 슬롯](secure-ota-architecture-detailed.png)

서버는 펌웨어를 배포하고, Raspberry Pi Primary는 대상·상태를 검증한 뒤 실행 순서를 정하며, STM32 Secondary 3대는 각각 비활성 A/B 슬롯에 새 펌웨어를 기록한다. MQTT는 VVM·Director 메타데이터·보고에, HTTPS는 Image Repository의 메타데이터·펌웨어 다운로드에 사용한다. Primary와 보드들은 USB 시리얼로 연결된다.

| 구성 요소 | 역할 |
|---|---|
| 배포 Watchdog | 배포 디렉터리의 펌웨어를 검사하고 업데이트 카탈로그 생성 |
| Director Repository | 차량 버전 정보를 바탕으로 대상별 업데이트 메타데이터 생성 |
| Image Repository | 메타데이터와 펌웨어 파일 제공 |
| Primary ECU | 검증, 보드 상태 수집, 안전 정책, AI 순서 결정, 전송 및 결과 기록 |
| STM32 Secondary ECU × 3 | 상태 응답, 비활성 슬롯 기록, 재부팅 및 새 펌웨어 실행 |

🛠️ 기술 구성: Python · C · STM32F103RB · STM32CubeIDE/HAL · Raspberry Pi · MQTT · HTTPS/Flask · scikit-learn · JSONL

## 🗺️ 설계 선택과 이유

| 선택 | 이유 |
|---|---|
| Safety Policy → AI 순서 | 허용·보류·차단 판단을 명시적 규칙으로 유지하고, AI에는 허용된 ECU의 순서 결정만 맡긴다. |
| Isolation Forest + 방향성 점수 | 실패 사례가 부족해 정상 전압·온도 분포로 물리적 이상을 찾고, 응답 지연·이미지 점유율·과거 실패는 각각 위험이 커지는 방향으로 계산한다. |
| A/B 슬롯 + 위험도 기반 순서 | 실행 중인 슬롯을 보존하고, 안전 정책을 통과한 ECU를 위험도가 낮은 순서로 업데이트한다. |

낮은 위험도부터 실행하는 것은 안정적인 ECU부터 완료시키려는 설계 가정이다. 실제 개선 효과와 장애별 복구 범위는 반복 실험으로 확인해야 한다.

## 🔧 핵심 구현

### 1. 메타데이터 검증과 안전 정책

Director와 Image Repository를 분리한 Uptane/TUF 계열 구조로 메타데이터 서명과 배포 파일의 일치 여부를 확인한다. Primary는 등록된 UID, 현재 버전, 대상 슬롯, 준비 상태, 상태 응답 등을 검사해 `ALLOW`, `HOLD`, `BLOCK`을 결정한다.

- `ALLOW`: 현재 검사 조건을 통과해 업데이트 대상으로 진행한다.
- `HOLD`: 보드 미응답, 준비 미완료 등으로 실행을 보류한다.
- `BLOCK`: 보드 식별 불일치, 검증 실패, 일반 업데이트에서의 동일·하위 버전 설치 등으로 실행을 차단한다.

AI는 `ALLOW` 집합 안에서 순서를 계산한다. 온도와 공급전압은 현재 안전 정책의 직접적인 허용·차단 임계값으로 사용하지 않으며, AI 위험도 계산에 활용한다. 응답시간은 AI 입력이면서 안전 정책의 응답 지연 검사에도 사용된다.

### 2. STM32 A/B 슬롯 업데이트

각 보드는 Bootloader와 두 애플리케이션 슬롯을 사용한다. 세 보드가 공통 프로젝트 소스를 사용하며, 빌드 설정으로 ECU ID와 펌웨어 버전을 지정한다.

| 영역 | 시작 주소 | 역할 |
|---|---|---|
| Bootloader | `0x08000000` | 부트 플래그와 슬롯 상태를 확인하고 애플리케이션 선택 |
| Slot A | `0x08004000` | A 위치에 링크된 애플리케이션 |
| Slot B | `0x08010000` | B 위치에 링크된 애플리케이션 |

각 애플리케이션 슬롯의 펌웨어 최대 크기는 48 KiB이다. 배포 시에는 같은 ECU·같은 버전에 대한 A/B 바이너리를 함께 준비한다.

```text
현재 Slot A 실행 → Slot B 펌웨어 검증·전송 → 재부팅 → Slot B·새 버전 확인
현재 Slot B 실행 → Slot A 펌웨어 검증·전송 → 재부팅 → Slot A·새 버전 확인
```

Primary는 파일명, 메타데이터의 대상 슬롯, 바이너리의 실행 주소를 교차검증하고 전송 직전에도 보드 상태를 다시 확인한다. `BOOT_FAILED` 오류 주입 시에는 이전 유효 슬롯으로 돌아가는 시험 경로를 제공한다. 재부팅 후 HEALTH·버전 오류를 감지하는 기능과 자동 슬롯 복귀 기능은 구분한다. 임의의 전원 차단을 포함한 모든 장애에서의 복구 보장은 별도의 검증 과제이다.

### 3. 보드 상태 수집과 5개 위험도 입력

상태 수집 계층은 15개 항목을 다루며, 현재 업데이트 순서 결정에는 아래 5개를 선택했다. 센서값, 통신 측정값, 펌웨어 크기, 과거 실행 이력을 함께 사용한다.

| 입력값 | 측정·산출 방법과 선정 이유 | 위험도 반영 |
|---|---|---|
| `supply_voltage_mv` | 내부 기준전압의 ADC 측정으로 MCU 공급전압을 추정해 전원 상태 반영 | 온도와 함께 Isolation Forest 입력 |
| `temperature_c` | 내부 온도센서의 ADC 값을 환산해 MCU 열적 상태 반영 | 전압과 함께 Isolation Forest 입력 |
| `link_response_ms` | Primary에서 상태 요청·응답 시간을 측정해 통신 상태 반영 | 정상 기준보다 느린 정도 반영 |
| `image_size_ratio` | 이미지 크기 ÷ 대상 슬롯 최대 크기로 상대적인 설치 부담 표현 | 점유율이 클수록 높은 점수 |
| `previous_failures` | 보드별 최근 유효 이력에서 실패 횟수를 집계해 과거 수행 이력 반영 | 실패 횟수가 많을수록 높은 점수 |

온도는 MCU 내부 온도이며 주변 공기 온도와 다르다. 공급전압은 내부 기준값으로 계산한 추정치로, USB 입력 전압이나 배터리 잔량을 의미하지 않는다. `image_size_ratio`는 배포 이미지의 슬롯 점유율이다.

각 항목은 순서 결정을 위한 입력으로 선정했다. 특히 이미지 점유율을 설치 부담의 지표로 사용하는 것은 설계 가정이며, 큰 점유율이 실제 실패를 유발한다는 인과관계를 입증한 것은 아니다.

### 4. 하이브리드 AI 스케줄러

정상 데이터로 학습한 `scikit-learn`의 🌲Isolation Forest가 공급전압·온도 조합의 이상도를 계산한다. 나머지 세 입력은 위험이 증가하는 방향에 맞춰 점수화한 뒤 가중 합산한다.

```text
최종 위험도 = 0.40 × 전압·온도 이상도
            + 0.30 × 응답 지연 점수
            + 0.15 × 이미지 슬롯 점유율
            + 0.15 × 과거 실패 점수
```

| 구성 점수 | 계산 방식 |
|---|---|
| 전압·온도 이상도 | `RobustScaler → Isolation Forest`의 이상 점수를 정상 학습 데이터의 백분위 기준으로 0~1 변환 |
| 응답 지연 | `clip((응답시간 − 정상 중앙값) / (1000ms − 정상 중앙값), 0, 1)` |
| 이미지 점유율 | `image_size_ratio` 그대로 사용 |
| 과거 실패 | `min(previous_failures / 3, 1)` |

`clip(x, 0, 1)`은 결과를 0~1 범위로 제한하는 연산이다. 정상 중앙값보다 빠른 응답에는 지연 점수를 더하지 않는다. `previous_failures`는 기본 설정에서 최근 10개의 유효 이력을 사용한다.

최종 점수는 업데이트 순서를 비교하기 위한 상대 위험도이다. 정상 학습 데이터의 백분위를 사용하는 항목은 전압·온도 이상도이며, 최종 점수 전체를 정상 분포와의 거리로 해석하지 않는다. 가중치 `0.40 / 0.30 / 0.15 / 0.15`는 초기 실험을 위해 정한 휴리스틱 값으로, 실험을 통해 최적화한 값은 아니다. 낮은 점수의 ECU부터 실행하고, 동점이면 기존 고정 순서를 유지한다.

- 계산된 추천 순서를 실제 업데이트 순서로 사용한다.
- 모델 로딩·추론 실패: 사유를 기록하고 허용된 ECU를 `001 → 002 → 003` 순서로 처리한다.

### 5. 실행 데이터 축적과 모델 갱신

초기 정상 데이터는 3개 보드 × 30회 수집 × 보드별 5회 측정 = 원시 측정 450건이다. 이를 보드·수집 회차별로 집계한 90행을 초기 모델 학습에 사용했다. 원시 센서·응답 측정과 실제 OTA 성공·실패 이력은 다른 데이터이다.

캠페인은 시작 시점의 측정값과 모델로 순서를 정한다. 종료 후에는 학습 조건을 만족하는 신규 데이터가 30건 쌓일 때 보드별 최근 100건을 사용해 모델을 다시 학습하며, 새 모델은 다음 캠페인부터 적용된다.

재학습에는 실제 보드의 정상 시나리오 중 정책을 통과하고 업데이트에 성공한 데이터만 사용한다. 텔레메트리·전원 상태·보드 건강 상태가 유효해야 하며, 물리 이상도가 0.95를 초과한 데이터와 오류 주입·시뮬레이터·실패 데이터는 제외한다.

모델 파일과 함께 입력 스키마, 학습 데이터 해시, 모델 해시, 가중치, 라이브러리 버전을 저장해 실행 당시의 판단 기준을 추적한다.

## 🚀 실행 방법

아래 절차는 노트북 WSL에서 서버 구성요소를 실행하고, Raspberry Pi에서 Primary를 실행하며, STM32 3대를 Raspberry Pi에 USB로 연결하는 환경을 기준으로 한다.

### 1. 저장소와 Python 패키지 준비

서버와 Raspberry Pi에 같은 브랜치를 준비한다.

```bash
git clone --branch adaptive-ai https://github.com/26-1-Secure-OTA/OTA_main.git
cd OTA_main

python3 -m pip install --upgrade pip
python3 -m pip install paho-mqtt requests Flask watchdog pyserial cryptography ecdsa
python3 -m pip install -r Primary_ECU/requirements-ai.txt
```

이미 저장소가 있다면 다시 clone하기 전에 현재 브랜치와 로컬 변경사항을 확인한다.

```bash
git branch --show-current
git status --short
```

### 2. 키·인증서와 네트워크 주소 확인

메타데이터 서명키와 MQTT·HTTPS 인증서는 서로 다른 용도이다. 다음 디렉터리의 키와 인증서가 동일한 실험 환경의 신뢰 설정으로 준비돼 있어야 한다.

```text
OTA_Director_Server/keys/
OTA_Director_Server/src/utils/certs/
Director/keys/
Director/src/utils/certs/
Primary_ECU/utils/certs/
```

MQTT Broker 주소와 포트는 다음 세 파일에서 동일하게 설정한다.

| 파일 | 설정값 |
|---|---|
| `OTA_Director_Server/src/Image.py` | `MQTT_BROKER`, `MQTT_PORT` |
| `Director/config.py` | `MQTT_BROKER`, `MQTT_PORT` |
| `Primary_ECU/Primary.py` | `BROKER`, `PORT` |

Image Repository 주소에는 Raspberry Pi에서 접근할 수 있는 노트북의 LAN/Wi-Fi 주소를 사용한다. Raspberry Pi에서 `localhost`는 Raspberry Pi 자신을 가리키므로 사용할 수 없다.

```bash
export IMAGE_REPOSITORY_URL=https://<SERVER_LAN_IP>:8443
```

Raspberry Pi에서 서버의 MQTT와 HTTPS 포트에 접근 가능한지 확인한다.

```bash
nc -vz <SERVER_LAN_IP> 8883
nc -vz <SERVER_LAN_IP> 8443
```

### 3. STM32 연결과 UID 확인

세 보드에는 각각 Bootloader와 초기 애플리케이션이 기록돼 있어야 한다.

| 프로젝트 | 시작 주소 |
|---|---:|
| `OTA_BOOTLOADER` | `0x08000000` |
| `OTA_LED_A_TEST` | `0x08004000` |
| `OTA_LED_B_TEST` | `0x08010000` |

Raspberry Pi에 보드 3대를 연결한 뒤 시리얼 장치와 상태를 확인한다.

```bash
ls -l /dev/ttyACM*
ls -l /dev/serial/by-id/

cd Primary_ECU
python3 status_check.py
```

출력된 `ecu_serial`과 96비트 `uid`가 `Primary_ECU/config/secondary_registry.json`의 `stm32-led-001`, `002`, `003`과 일치해야 한다. 포트 번호만으로 보드를 구분하지 않는다.

### 4. 펌웨어 6개 준비

하나의 새 버전에는 보드별 Slot A/B 바이너리가 모두 필요하다.

```text
stm32-led-001_<VERSION>_slot_a.bin
stm32-led-001_<VERSION>_slot_b.bin
stm32-led-002_<VERSION>_slot_a.bin
stm32-led-002_<VERSION>_slot_b.bin
stm32-led-003_<VERSION>_slot_a.bin
stm32-led-003_<VERSION>_slot_b.bin
```

파일 이름만 바꾸지 말고 실제 펌웨어의 ECU ID, 버전, 링크 주소를 맞춰 빌드한다. 배포 버전은 각 보드의 현재 버전보다 높아야 한다.

### 5. 서버 구성요소 실행

MQTT Broker를 먼저 시작한 뒤, 저장소 루트를 기준으로 각 터미널에서 다음 프로그램을 실행한다.

```bash
# 터미널 1: 배포 Watchdog
cd OTA_Director_Server/src
python3 uptane_watchdog.py
```

```bash
# 터미널 2: Image Repository
cd OTA_Director_Server/src
export IMAGE_REPOSITORY_URL=https://<SERVER_LAN_IP>:8443
python3 Image.py
```

```bash
# 터미널 3: Director
cd Director
python3 Director.py
```

Watchdog와 Image Repository가 실행된 다음 새 펌웨어 6개를 `OTA_Director_Server/src_add/stage/`에 복사한다.

```bash
cp OTA_Director_Server/src_add/firmware_storage/<VERSION>/stm32-led-*.bin \
  OTA_Director_Server/src_add/stage/
```

Watchdog가 targets를 갱신·서명하고 Image Repository가 snapshot과 timestamp를 갱신하는지 로그에서 확인한다. Watchdog 실행 전에 이미 `stage/`에 있던 파일이나 같은 이름으로 덮어쓴 파일은 새 파일 생성 이벤트가 발생하지 않을 수 있다.

### 6. Raspberry Pi에서 Primary 실행

```bash
cd Primary_ECU
python3 Primary.py
```

Primary는 VVM을 전송하고, Director와 Image Repository의 메타데이터를 검증한 다음 STM32 상태를 수집한다. 안전 정책에서 허용된 보드는 계산된 위험도가 낮은 순서로 업데이트하며, 모델을 사용할 수 없으면 `001 → 002 → 003` 순서로 처리한다.

일반 업데이트에서는 동일하거나 낮은 버전의 설치를 차단하므로, 업데이트를 다시 실행하려면 보드의 현재 버전보다 높은 새 펌웨어가 필요하다.

### 7. 실행 결과 확인

콘솔의 `[AI Scheduler]`에서 사용된 모델, 추천 순서, 실제 실행 순서와 fallback 사유를 확인한다. 보드별 상세 결과는 다음 파일에 기록된다.

```text
Primary_ECU/logs/ota_experiments.jsonl
```

같은 `campaign_id`의 `policy_decision`, `ai_risk_score`, `ai_recommended_rank`, `ai_execution_rank`, `update_attempted`, `success`, 업데이트 전후 버전과 슬롯을 함께 확인한다. 완료 후 `python3 status_check.py`를 다시 실행해 실제 보드 버전과 슬롯이 변경됐는지 검증한다.

### ⚠️ 실행 중 자주 확인할 문제

| 증상 | 확인할 항목 |
|---|---|
| `No route to host` | Broker 주소, 같은 네트워크 연결, 8883 포트와 방화벽 |
| HTTPS 연결 실패 | `IMAGE_REPOSITORY_URL`, 8443 포트, WSL 포트 전달 |
| STM32를 찾지 못함 | `/dev/ttyACM*`, USB 케이블, `dialout` 권한 |
| `UNKNOWN_SECONDARY` | 실제 UID와 `secondary_registry.json` |
| `DOWNGRADE_ATTEMPT` | 펌웨어 내부 버전과 현재 보드 버전 |
| `INVALID_TARGET_SLOT` | 현재 슬롯, 파일명 슬롯, 메타데이터 슬롯과 링크 주소 |
| 고정 순서로 실행됨 | 모델 경로, AI 의존성 버전, `[AI Scheduler]`의 fallback 사유 |

## 📊 판단 결과 확인

캠페인 실행 시 Primary는 `[AI Scheduler]` 로그에 요청 모드, 실제 사용 모드, 추천 순서, 실행 순서, fallback 사유를 출력한다. `Primary_ECU/logs/ota_experiments.jsonl`에는 보드별 정책 결과, 위험도 구성 점수, 추천·실행 순위, 모델 버전·해시, 설치 소요시간, 성공 여부, 업데이트 전후 슬롯이 남는다. 따라서 AI 추천 순서가 실제 설치에 적용됐는지 보드별로 확인할 수 있다.

실제 캠페인 결과표와 시연 영상·사진은 촬영 자료를 정리한 뒤 이 절에 추가한다.

## 🔒 구현 현황과 검증 범위

| 항목 | 현재 범위 |
|---|---|
| 실물 구성 | Raspberry Pi Primary와 STM32 Secondary 3대의 USB 시리얼 업데이트 환경 |
| 업데이트 경로 | 메타데이터 검증, UID 확인, A/B 전송 및 재부팅 후 상태 확인 구현 |
| AI | 5개 입력을 사용하는 하이브리드 위험도와 낮은 위험도 우선 순서 구현 |
| 초기 모델 | 모델 메타데이터 기준 원시 측정 450건을 집계한 정상 데이터 90행으로 학습 |
| 모델 갱신 | 정상·성공 데이터 조건부 수집과 배치 재학습 로직 구현. 실제 재학습 횟수는 실험 결과 확인 필요 |
| 오류 주입 | 지연, 전송 중단·손상, 리셋, 부팅 실패 등을 설정하는 프로토콜과 실험 실행기 구현 |

실험 실행기는 기본적으로 20개 캠페인 × 3개 보드 = 60회 시도의 계획을 제공한다. 이는 20대의 실물 보드를 의미하지 않는다. 온도·전압 오류 주입 시나리오는 보고값을 변경하는 시험을 포함하므로 실제 가열·전원 변동 시험과 구분한다.

후속 평가에서는 동일한 조건에서 고정 순서와 AI 순서를 비교하고, 시간별 완료 ECU 수, 전체 완료 시간, 실패·복구 결과를 기록한다. 현재 README는 성능 개선을 주장하지 않는다.

## ⚠️ 한계와 공개 준비

현재 프로젝트는 3보드 연구·시연용 프로토타입이다. 차량 실환경 적용, CAN/Ethernet 연결, 센서 보정, 물리적 장애 시험은 확장 과제이다.

검토 기준 저장소에는 서명 개인키 파일이 추적되고 일부 통신 경로의 인증서 검증이 완화돼 있다. 공개 또는 외부 네트워크 배포 전에 키 교체와 인증서 설정을 정리해야 한다. 현재 실행 조건은 위의 실행 방법에 정리했다.

## 🗂️ 저장소 둘러보기

```text
OTA_main/
├── Director/                     # 대상별 업데이트 메타데이터
├── OTA_Director_Server/
│   ├── src/                      # 배포 Watchdog · Image Repository
│   ├── src_add/                  # 펌웨어 준비·배포 디렉터리
│   └── Image_Repo/                # 메타데이터·이미지 저장소
├── Primary_ECU/
│   ├── ai/                       # 학습·추론·위험도·재학습
│   ├── ecu/                      # 검증·정책·설치·상태·로그
│   ├── config/                   # 보드 UID 등록
│   ├── models/                   # 모델과 메타데이터
│   ├── experiments/              # 오류 주입 실험 실행·검증
│   └── tests/                    # 정책·AI·프로토콜 테스트
└── STM32_Workspace/
    ├── OTA_BOOTLOADER/            # 부팅 슬롯 선택
    ├── OTA_LED_A_TEST/            # Slot A 애플리케이션
    ├── OTA_LED_B_TEST/            # Slot B 애플리케이션
    ├── Common/                    # 공통 오류 주입 프로토콜
    └── firmware/                  # 버전별 빌드 산출물
```

설계를 자세히 볼 때는 `Primary_ECU/ecu/safety_policy.py`, `Primary_ECU/ai/hybrid_risk.py`, `Primary_ECU/ai/adaptive_model.py`, `STM32_Workspace/OTA_LED_A_TEST/Src/main.c`부터 살펴보면 된다.

## 📚 팀과 문서

팀 : KUSE 2026-1 Secure OTA 팀

- [STM32 워크스페이스 설명](https://github.com/26-1-Secure-OTA/OTA_main/blob/adaptive-ai/STM32_Workspace/README.md)
- [오류 주입 실험 설명](https://github.com/26-1-Secure-OTA/OTA_main/blob/adaptive-ai/Primary_ECU/experiments/README.md)
- [AI 의존성 버전](https://github.com/26-1-Secure-OTA/OTA_main/blob/adaptive-ai/Primary_ECU/requirements-ai.txt)
