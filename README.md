# OTA System Setup & Execution Guide

이 문서는 국민대학교 자동차융합대학 차량보안동아리 KUSE 2025-2 'Secure OTA' 프로젝트 구현 내용에 대한 설명입니다.

------------------------------------------------------------------------

## OEM Server(chunking_watchdog.py)
업데이트 할 컨테이너 이미지 압축 파일을 확인하여, FastCDC를 활용한 chunk 분할 및 manifest를 생성합니다.

- `OTA_Dircector_Server/src_add/stage`: 업데이트 이미지 업로드 디렉토리

### 수행절차
1. 컨테이너 이미지 FastCDC 기반 Chunk 분할 및 저장 -> 중복 제거
2. Global Target metadata 생성 및 OTA 서버로 배포(Image, Director)
3. Image Repository의 chunk_storage 및 manifest 업데이트

## Image Repository(Image_Repository_ver2.py)
Chunk 저장소와 Manifest를 제공하는 역할로, 전체 이미지에 대한 메타데이터 관리

- `OTA_Dircector_Server/Image_Repo/chunk_storage`: Chunk 저장소
- `OTA_Dircector_Server/Image_Repo/meta`: metadata 및 manifest 관리

### 수행절차
1. Target 메타데이터가 수정되면, Snapshot, Timestamp 메타데이터를 순차적으로 갱신
2. Flask 서버를 통해 `chunk_storage`와 `meta` 서비스
3. Vehicle로부터 메타데이터 요청을 수신하면 Timestamp 및 base Url 전달(MQTT)
4. 나머지 메타데이터 및 chunk 관련 정보는 모두 HTTPS를 통해 서비스

## Director Repository(main.py)
Vehicle의 상태 정보를 바탕으로 업데이트 정보 판단 및 생성하는 역할

- `Director/meta`: metadata 및 manifest 관리

### 수행절차
1. Vehicle이 publish 한 VVM 정보를 수신
2. Global Target metadata와 VVM 비교를 통해 업데이트 할 Target 메타데이터 생성(`target_per_vehicle.json`)
3. 생성된 Target 메타데이터를 기반으로, Snapshot, Timestamp 메타데이터 생성
4. 모든 메타데이터를 MQTT를 통해 publish

## Primary ECU(Primary_ECU.py)
VVM 정보를 기반으로 생성된 업데이트 정보 검증 및 다운로드

- `Primary_ECU/downloads`: Image Repository로부터 다운로드 한 모든 데이터 관리

### 수행절차
1. VVM 정보를 publish
2. 수신한 Director Repository의 모든 메타데이터 서명 검증
3. 수신한 Image Repository의 메타데이터 순차적 서명 검증
4. Director와 Image의 target 이미지에 대한 교차 검증
5. Manifest 및 chunk 다운로드



# 프로젝트 사용 방법
## 📌 1. MQTT 및 HTTP 인증서 제작 및 설정

MQTT 브로커와 Flask 서버 통신을 위해 필요한 인증서를 제작하고 아래
경로에 배치합니다.

-   `OTA_Director_Server/src/utils/certs`
-   `Primary_ECU/utils/certs`

인증서 제작 방법 참고:\
🔗 https://www.notion.so/Linux-2484ccbeeb28809ca4dcd98faaedbfdd?pvs=21

------------------------------------------------------------------------

## 📌 2. MQTT 브로커 및 Flask 서버 IP 설정

다음 파일에서 MQTT 및 HTTP 서버 IP 주소를 환경에 맞게 수정합니다.

-   `OTA_Director_Server/src/Image_Repository.py`
-   `Primary_ECU/Prime_ECU.py`

------------------------------------------------------------------------

## 📌 3. Chunking Watchdog 실행

이미지 chunking 상태를 모니터링하기 위해 watchdog을 실행합니다.

``` bash
python3 OTA_Director_Server/src/chunking_watchdog.py
```

------------------------------------------------------------------------

## 📌 4. Image Repository 실행

컨테이너 이미지 chunk 생성 및 OTA 파일 배포 기능을 수행하는 Image
Repository를 실행합니다.

``` bash
python3 OTA_Director_Server/src/Image_Repository.py
```

------------------------------------------------------------------------

## 📌 5. 컨테이너 이미지 준비 및 배포 파일 배치

OTA 업데이트에 사용할 `ivi_0.0.0.tar.xz` 파일을 아래 디렉터리에
배치합니다.(`ecu_버전.tar.xz` 이름 형식 주의)

-   `OTA_Director_Server/src_add/stage`
- Image Repository가 파일 변화 시점을 감지해야 하므로, Image Repository가 실행되고 있어야 함

이미지는 Docker Hub에서 가져와 oci-archive 형식으로 변환해 사용합니다.

``` bash
podman pull hanbin6157/seame_hu_app:1.0.0
podman save --format oci-archive -o ivi_2.0.0.tar.xz seame_hu_app:1.0.0
```

------------------------------------------------------------------------

## 📌 6. Director Repository 실행

VVM 정보를 수신하고, 업데이트 정보를 생성하는 Director Repository 실행

``` bash
python3 Director/main.py
```

------------------------------------------------------------------------

## 📌 6. Prime ECU 실행

ECU 측에서 업데이트 요청 및 수신을 담당하는 Prime ECU 프로그램을
실행합니다.

``` bash
python3 Primary_ECU/Prime_ECU.py
```

------------------------------------------------------------------------

## 📌 7. 컨테이너 이미지 빌드 및 자동 실행

Prime ECU 실행과 동시에 다운로드가 진행되며, Prime ECU에서 전송된 이미지로 컨테이너를 자동 빌드 및
실행 됨

------------------------------------------------------------------------

## ⚠️ 키 및 인증서 확인
    1. 최상위 디렉터리에 `root_keys`디렉터리를 생성하고, 아래의 키가 존재해야 함
    - root1_pub.pem
    - root1.pem
    - root2_pub.pem
    - root2.pem
    - root3_pub.pem
    - root3.pem
    - snapshot_pub.pem
    - snapshot.pem
    - targets_pub.pem
    - targets.pem
    - timestamp_pub.pem
    - timestamp.pem

    2. root.py 실행하면 Image, Director Repository에 root 메타데이터 생성
    3. 각 영역에 snapshot, targets, timestamp 키 쌍 존재해야 함
    - OTA_Director_Server/keys
    - Director/keys

    4. OTA_Director_Server/keys 에 업데이트할 이미지에 대한 키 쌍 임의 생성
      ex) ivi.pem, ivi_pub.pem

    5. Primary_ECU/make_vvm.py 실행으로 root_vvm 및 vvm 생성 가능

    6. 그 외 MQTT, HTTPS 관련 인증서 확인

------------------------------------------------------------------------

## STM32 A/B 슬롯 펌웨어 배포

### AI 기반 업데이트 순서 설정

Safety Policy에서 `ALLOW`된 ECU의 업데이트 순서는 기본적으로 하이브리드
Isolation Forest 방식이 결정합니다. Isolation Forest는 공급전압과 MCU 내부
온도의 정상 분포 이탈을 계산하고, 응답 지연·이미지 슬롯 점유율·과거 실패
횟수의 방향성 위험도를 가중 합산합니다. 이 다섯 값 이외의 수집 항목은
Safety Policy와 감사 로그에는 남지만 AI 업데이트 순서에는 사용하지 않습니다.

기본 가중치는 물리 이상도 40%, 응답 지연 30%, 이미지 점유율 15%, 과거 실패
15%입니다. 응답 지연은 정상 학습 데이터의 중앙값 이하에서는 위험을 추가하지
않고, 중앙값부터 Safety Policy 제한인 1000ms 사이에서 0~1로 증가합니다.

각 캠페인은 시작 시점의 모델과 현재 측정값으로 순서를 결정합니다. 캠페인 종료
후 실제 보드·정상 시나리오·정책 ALLOW·업데이트 성공·정상 텔레메트리를 모두
만족한 데이터가 신규 30건 쌓이면 최근 보드별 100건의 rolling window로 모델을
자동 재학습합니다. 새 모델과 새 정상 응답시간 중앙값은 다음 캠페인부터 적용됩니다.
fault injection, simulator, 실패 행, 물리 이상도 0.95 초과 행은 정상 학습에서
제외됩니다. 자동 재학습을 끄려면 `OTA_AI_AUTO_RETRAIN=0`을 사용합니다.

기본 실행은 `ISOLATION_FOREST` + `ACTIVE`입니다. 순서에 반영하지 않고 추천
결과만 확인하려면 다음처럼 SHADOW 모드로 실행합니다.

최초 한 번은 모델과 동일한 AI 의존성을 설치합니다.

```bash
cd Primary_ECU
python3 -m pip install --user -r requirements-ai.txt
```

```bash
cd Primary_ECU
export OTA_AI_MODE=ISOLATION_FOREST
export OTA_AI_APPLY_MODE=SHADOW
python3 Primary.py
```

검증 후 추천 순서를 실제 업데이트 순서에 적용하려면
`OTA_AI_APPLY_MODE=ACTIVE`를 사용합니다. 모델 파일은 기본적으로
`Primary_ECU/models/isolation-forest-v2.joblib`, 메타데이터는
`Primary_ECU/models/isolation-forest-v2.metadata.json`에서 읽습니다.
다른 artifact를 사용할 때는 각각 `OTA_AI_MODEL_PATH`와
`OTA_AI_METADATA_PATH`로 지정할 수 있습니다. 모델 로딩 또는 추론에
실패하면 선정하지 않은 Feature를 사용하는 구형 통계 방식으로 넘어가지 않고
`001 → 002 → 003` 고정 순서로 fallback합니다. 구형 Median/MAD 방식은
`OTA_AI_MODE=STATISTICAL`로 명시한 경우에만 실행됩니다.

STM32 펌웨어를 배포할 때는 동일한 소스와 동일한 버전으로 빌드한
A/B 바이너리 두 개가 모두 필요합니다. 두 바이너리는 링커 시작 주소와
벡터 테이블 위치만 다릅니다.

```text
stm32-led-001_1.2.0_slot_a.bin
stm32-led-001_1.2.0_slot_b.bin
```

A/B 파일을 모두 `OTA_Director_Server/src_add/stage`에 복사합니다.
watchdog는 Cortex-M 벡터 테이블을 검사하고, 서명된 targets 메타데이터에
`custom.target_slot`을 기록하며, A/B 파일을 서로 다른 target으로 관리합니다.
Director는 동일한 버전의 A/B 파일이 모두 준비된 경우에만 해당 슬롯 펌웨어를
업데이트 대상으로 공개합니다.

업데이트할 때 Primary는 다음 순서로 동작합니다.

1. Director와 Image Repository의 메타데이터를 검증합니다.
2. STM32 Secondary에 `STATUS_REQ`를 전송합니다.
3. 현재 실행 중인 `ACTIVE` 슬롯과 비활성 `TARGET` 슬롯을 확인합니다.
4. `TARGET` 슬롯에 맞는 서명된 펌웨어만 다운로드합니다.
5. 메타데이터 슬롯, 파일명 슬롯, 바이너리의 Reset_Handler 주소를 교차검증합니다.
6. 펌웨어 전송 직전에 Secondary 상태를 다시 확인하고, 슬롯이 변경되었으면 설치를 중단합니다.
7. 펌웨어를 전송한 후 대상 슬롯이 활성 슬롯으로 변경되었는지 확인합니다.

STM32는 `STATUS_REQ`에 다음 필드를 한 줄로 응답해야 합니다.

```text
STATUS,stm32-led-001,UID=12345678ABCDEF0011223344,VER=1.0.1,ACTIVE=A,TARGET=B,READY=1,MAX=49152,UPTIME_MS=125340,RESET=POWER_ON,UART_ERR=0,HEALTH=OK
```

`UID`는 STM32의 96비트 UID를 24자리 16진수로 표현한 값이며, `VER`는
`x.y.z` 형식이어야 합니다. `RESET`은 `POWER_ON`, `PIN_RESET`, `SOFTWARE`,
`WATCHDOG`, `UNKNOWN` 중 하나이고, `HEALTH`는 `OK`, `WARN`, `ERROR` 중
하나여야 합니다. 정상적인 UART 수신 timeout은 `UART_ERR`에 포함하지 않습니다.

처음 연결한 뒤 `Primary_ECU` 디렉터리에서 아래 명령으로 각 보드의 UID를
확인합니다.

```bash
python status_check.py
```

실제 STM32Cube 워크스페이스에서 빌드한 ECU별 A/B 바이너리와 Primary의
슬롯 판별·STATUS 계약 호환성을 검증하려면 저장소 루트에서 실행합니다.

```bash
python3 -m unittest discover -s STM32_Workspace/tests -v
```

이 테스트는 여섯 바이너리의 Cortex-M 벡터가 선언된 A/B 슬롯에 링크됐는지,
ECU ID와 `1.8.0` 버전이 실제 바이너리에 포함됐는지, 보드 STATUS 응답을
Primary가 정상 파싱하는지 검사합니다.

출력된 UID를 `Primary_ECU/config/secondary_registry.json`의 동일한 ECU 항목에
등록해야 합니다. UID가 `null`인 Secondary는 안전 정책에서
`UNKNOWN_SECONDARY`로 차단됩니다.

예를 들어 Secondary가 `ACTIVE=A,TARGET=B`를 보고하면 Primary는
`stm32-led-001_<version>_slot_b.bin` 파일만 요청합니다. A/B 파일 중 하나만
업로드되었거나 두 파일의 버전이 일치하지 않으면 해당 펌웨어는 차량에
업데이트 대상으로 제공되지 않습니다.

### Git 클론 후 키 복사

보안을 위해 개인키가 들어 있는 `keys` 디렉터리는 Git에 커밋되지 않으므로,
저장소를 클론해도 `OTA_Director_Server/keys`에 키가 생성되지 않습니다.
별도로 보관한 `Director/keys` 디렉터리를 다음 위치로 복사해야 합니다.

```text
Director/keys  →  OTA_Director_Server/keys
```

두 디렉터리에는 동일한 `targets`, `snapshot`, `timestamp` 키 쌍이 있어야
합니다. 기존 `root.json`의 공개키 정보와 일치해야 하므로 새 키를 임의로
생성하지 말고, 기존 키를 안전한 방법으로 복사해서 사용합니다. 개인키는
GitHub에 커밋하지 않습니다.
