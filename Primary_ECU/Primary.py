import json, ssl, time, os, requests, sys
import paho.mqtt.client as mqtt
from urllib.parse import urljoin

# 1. 기존 유틸리티 함수 임포트 활성화
# 실제 환경에 맞춰 파일 경로나 함수명을 확인하세요.
try:
    from utils.fastcdc_chunking import run_container, join_all
except ImportError:
    print("[Warning] utils.fastcdc_chunking not found. Container execution might fail.")

from ecu import (
    Updater, Storage, Transport, Verifier, Installer, Reporter,
)

# MQTT 설정
BROKER = "172.20.10.2"
PORT = 8883
TOPIC_NOTIFY_VERSION     = "primary/version"
TOPIC_DIRECTOR_TIMESTAMP = "director/timestamp"
TOPIC_DIRECTOR_SNAPSHOT  = "director/snapshot"
TOPIC_DIRECTOR_TARGETS   = "director/targets"
TOPIC_REPORT             = "primary/report"
TOPIC_IMAGE_META         = "image/metaData"

CA_CERT     = "./utils/certs/ca.crt"
CLIENT_CERT = "./utils/certs/mqtt_client.crt"
CLIENT_KEY  = "./utils/certs/mqtt_client.key"

class PrimeEcuHandler:
    def __init__(self, broker, port):
        self.client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        self._configure_tls(self.client, CA_CERT, CLIENT_CERT, CLIENT_KEY)
        
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        
        # 상태 제어 플래그: 중복 다운로드 및 루프 방지
        self.is_updating = False 

        # Updater 구성요소 초기화
        self.storage  = Storage()
        self.transport= Transport()
        self.verifier = Verifier()
        self.installer= Installer(self.storage)
        self.reporter = Reporter(self.client, TOPIC_REPORT)
        
        self.meta_buffer = {"timestamp": None, "snapshot": None, "targets": None}
        
        self.client.connect(broker, port, 60)
        self.client.loop_start()

        # VVM(차량 버전 매니페스트) 전송
        self._send_vvm_initial()

    def _configure_tls(self, client, ca_cert, client_cert, client_key):
        client.tls_set(ca_certs=ca_cert, certfile=client_cert, keyfile=client_key, tls_version=ssl.PROTOCOL_TLSv1_2)
        client.tls_insecure_set(True)

    def _send_vvm_initial(self):
        if os.path.exists("./vvm.json"):
            with open("./vvm.json", "r", encoding="utf-8") as f:
                vvm = json.load(f)
            self.client.publish(TOPIC_NOTIFY_VERSION, json.dumps(vvm).encode("utf-8"), qos=0)
            print("[Prime ECU] VVM sent to server.")

    def on_connect(self, client, userdata, flags, rc, properties=None):
        print(f"[Prime ECU] Connected to Broker (rc: {rc})")
        topics = [TOPIC_DIRECTOR_TIMESTAMP, TOPIC_DIRECTOR_SNAPSHOT, TOPIC_DIRECTOR_TARGETS, TOPIC_IMAGE_META]
        for t in topics:
            client.subscribe(t, qos=1)

    def on_message(self, client, userdata, msg):
        # 업데이트 중에는 어떤 메시지도 새로 처리하지 않음 (무한루프 방지)
        if self.is_updating:
            return

        if msg.topic == TOPIC_IMAGE_META:
            self._process_image_update(msg)
        else:
            self._process_director_meta(msg)

    def _process_image_update(self, msg):
        try:
            print("\n[Prime ECU] Start Metadata Verification...")
            timestamp_meta = json.loads(msg.payload.decode("utf-8"))
            base_url = timestamp_meta["url"]

            # 1. 검증 체인 실행
            ok, s_hash = self.verifier.verify_metadata(timestamp_meta)
            if not ok: return

            # Snapshot 획득 및 검증
            snap_res = requests.get(urljoin(base_url.rstrip('/') + "/", "meta/snapshot.json"), verify=False)
            ok, t_ver = self.verifier.verify_metadata(snap_res.json(), s_hash, snapshot_raw=snap_res.content)
            if not ok: return

            # Targets 획득 및 검증
            target_res = requests.get(urljoin(base_url.rstrip('/') + "/", "meta/targets.json"), verify=False)
            targets_data = target_res.json()
            ok, targets = self.verifier.verify_metadata(targets_data, t_ver)
            if not ok: return

            # 2. 해시 체크 및 다운로드 판단
            update_images = self.verifier.hash_check("./meta/update_target.json", targets)
            
            if update_images:
                self.is_updating = True # 플래그 고정
                print("[Primary ECU] New update found. Downloading chunks...")
                
                # 이미지 재조립을 위한 다운로드 실행
                self.installer.download_image(update_images, base_url)
                
                # 3. 설치 및 컨테이너 실행
                self._execute_container_and_exit()
            else:
                print("[Prime ECU] No update needed or Hash check failed.")

        except Exception as e:
            print(f"[Error] Update process failed: {e}")
            self.is_updating = False

    def _execute_container_and_exit(self):
        """이미지 조립 후 컨테이너를 실행하고 프로그램을 종료합니다."""
        print("\n" + "="*60)
        print("[Primary ECU] STEP: RECONSTRUCTING & RUNNING CONTAINER")
        print("="*60)

        try:
            # 1. 파일 조립 (Installer/Storage 로직에 따라 경로 설정)
            # 예시: ./downloads/ 폴더에 다운로드된 청크들을 합쳐서 tar 생성
            final_tar = "./downloads/update_image.tar"
            
            # (만약 Installer 내부에 조립 로직이 없다면 여기서 호출)
            # join_all("./downloads/chunks", final_tar) 

            # 2. 컨테이너 실행
            print(f"[Primary ECU] Running: {final_tar}")
            run_container(final_tar)
            
            # 3. 결과 보고
            self.reporter.report("update_success_and_running", {"image": final_tar})
            print("\n[SUCCESS] Update finished. Container is now active.")
            
            # 4. 루프 종료 및 프로세스 완전히 끝내기 (다시 안 받게 함)
            self.client.loop_stop()
            print("[Primary ECU] Exiting safely...")
            os._exit(0) 

        except Exception as e:
            print(f"[Critical Error] Failed to run container: {e}")
            self.is_updating = False

    def _process_director_meta(self, msg):
        role = msg.topic.split('/')[-1]
        try:
            data = json.loads(msg.payload.decode("utf-8"))
            if role in self.meta_buffer:
                self.meta_buffer[role] = data
                print(f"[Director] {role} received.")

                if all(self.meta_buffer.values()):
                    self._verify_director_all()
        except Exception as e:
            print(f"[Error] Director JSON error: {e}")

    def _verify_director_all(self):
        ts, sn, tg = self.meta_buffer["timestamp"], self.meta_buffer["snapshot"], self.meta_buffer["targets"]
        vr = self.verifier.verify_director_chain(ts, sn, tg)

        if vr.ok:
            print("[Prime ECU] Director chain verification OK.")
            self._save_target_json(tg)
            self.reporter.report("director_meta_verify_ok", {})
        else:
            print(f"[Prime ECU] Director chain FAILED: {vr.reason}")
            self.reporter.report("director_meta_verify_failed", {"reason": vr.reason})

        self.meta_buffer = {k: None for k in self.meta_buffer}

    def _save_target_json(self, data):
        os.makedirs("./meta", exist_ok=True)
        with open("./meta/update_target.json", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

if __name__ == "__main__":
    handler = PrimeEcuHandler(BROKER, PORT)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        handler.client.loop_stop()
        sys.exit(0)