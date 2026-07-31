# Primary_ECU/Prime_ECU.py
import json, ssl, time, tarfile, os, shutil, binascii, hashlib
import paho.mqtt.client as mqtt
import requests
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urljoin
#from utils.fastcdc_chunking import join_all, load_image_from_oci, run_container
#from utils.metrics import measure

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:
    from backports.zoneinfo import ZoneInfo  # backports 패키지 필요
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from ecu import (
    Updater,
    Storage,
    Transport,
    Verifier,
    Installer,
    Reporter,
)

BROKER = "172.20.10.2"
PORT = 8883

TOPIC_NOTIFY_VERSION     = "primary/version"     # VVM 전송
TOPIC_DIRECTOR_TIMESTAMP = "director/timestamp"  # timestamp 메타 수신
TOPIC_DIRECTOR_SNAPSHOT  = "director/snapshot"   # snapshot 메타 수신
TOPIC_DIRECTOR_TARGETS   = "director/targets"    # targets_per_vehicle 메타 수신
TOPIC_REPORT             = "primary/report"      # 상태 보고
TOPIC_REQUEST_UPDATE     = "primary/request"  # 업데이트 요청
TOPIC_IMAGE_META         = "image/metaData"   # 이미지 메타 수신

CA_CERT     = "./utils/certs/ca.crt"
CLIENT_CERT = "./utils/certs/mqtt_client.crt"
CLIENT_KEY  = "./utils/certs/mqtt_client.key"

SYSTEM = os.environ.get("SYSTEM_NAME", "")
TC = os.environ.get("TEST_CASE", "")

class PrimeEcuHandler:
    def __init__(self, broker, port):
        self.client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        self._configure_tls(self.client, CA_CERT, CLIENT_CERT, CLIENT_KEY)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        

        # Updater 구성요소
        self.storage  = Storage()
        self.transport= Transport()
        self.verifier = Verifier()
        self.installer= Installer(self.storage)
        self.reporter = Reporter(self.client, TOPIC_REPORT)
        self.updater  = Updater(self.storage, self.transport, self.verifier, self.installer, self.reporter)

        self.meta_buffer = {
            "timestamp": None,
            "snapshot":  None,
            "targets":   None,
        }
        self.client.connect(broker, port, 60)
        self.client.loop_start()

        # VVM 전송
        with open("./vvm.json","r",encoding="utf-8") as f:
            vvm = json.load(f)
        self.client.publish(TOPIC_NOTIFY_VERSION, json.dumps(vvm, ensure_ascii=False).encode("utf-8"), qos=0)
        
        #=========================================================================================================
        # Director 메타데이터 검증
        #=========================================================================================================

        # self.client.publish(TOPIC_REQUEST_UPDATE, json.dumps({}, ensure_ascii=False).encode("utf-8"), qos=0)
    def _save_update_target(self, targets_meta: dict) -> None:
        os.makedirs("./meta", exist_ok=True)
        with open("./meta/update_target.json", "w", encoding="utf-8") as f:
            json.dump(targets_meta, f, indent=2, ensure_ascii=False)
        print("[Prime ECU] saved ./meta/update_target.json")

    def _configure_tls(self, client, ca_cert, client_cert, client_key):
        client.tls_set(ca_certs=ca_cert, certfile=client_cert, keyfile=client_key, tls_version=ssl.PROTOCOL_TLSv1_2)
        client.tls_insecure_set(True)

    def on_connect(self, client, userdata, flags, rc, properties=None):
        print(f"[Prime ECU] Connected: {rc}")
        client.subscribe(TOPIC_DIRECTOR_TIMESTAMP, qos=1)
        client.subscribe(TOPIC_DIRECTOR_SNAPSHOT, qos=1)
        client.subscribe(TOPIC_DIRECTOR_TARGETS, qos=1)
        client.subscribe(TOPIC_IMAGE_META, qos=1)

    def on_message(self, client, userdata, msg):
        # Image Repository 메타데이터 수신
        if msg.topic == TOPIC_IMAGE_META:
            # Timestamp 메타 데이터 및 base url 구분
            #with measure("Image Metadata Verification", system_name=SYSTEM, test_case=TC):
            try:
                timestamp_meta = json.loads(msg.payload.decode("utf-8"))
            except Exception as e:
                print(f"[Prime ECU] invalid JSON on {msg.topic}: {e}")
                return

            print("[Prime ECU] received image metadata\n")

            base_url = timestamp_meta["url"]

            # Timestamp 메타데이터 검증 -> 유효시간 확인, snapshot 해시값 획득
            #ok, s_hash = self.verify_metadata(timestamp_meta)
            ok, s_hash = self.verifier.verify_metadata(timestamp_meta)
            if not ok or s_hash is None:
                print("[FAIL] Timestamp metadata is not correct")
                return

            # Snapshot 메타데이터 GET
            url = urljoin(base_url.rstrip('/') + "/", "meta/snapshot.json")
            print(f"Downloading manifests from {url}")
            response = requests.get(url, verify=False)
            response.raise_for_status()

            raw_snapshot_bytes = response.content 
            snapshot_meta = response.json()
            print("[Prime ECU] received Snapshot metadata\n")
            
            # Snapshot 메타데이터 검증 -> timestamp 해시 정보와의 일치 확인, target 메타데이터 버전 정보 획득
            # ok, target_version = self.verify_metadata(snapshot_meta, s_hash, snapshot_raw=raw_snapshot_bytes)
            ok, target_version = self.verifier.verify_metadata(snapshot_meta, s_hash, snapshot_raw=raw_snapshot_bytes)
            if not ok or target_version is None:
                print("[FAIL] Snapshot metadata is not correct")
                return

            # Target 메타데이터 GET
            url = urljoin(base_url.rstrip('/') + "/", "meta/targets.json")
            print(f"Downloading manifests from {url}")
            response = requests.get(url, verify=False)
            response.raise_for_status()

            # Target 메타데이터 검증
            targets_meta = response.json()
            print("[Prime ECU] received Target metadata\n")
            # ok, targets = self.verify_metadata(targets_meta, target_version)
            ok, targets = self.verifier.verify_metadata(targets_meta, target_version)
            if not ok:
                print("[FAIL] Targets metadata is not correct")
                return

            print("[OK] All metadata verified successfully")
            print(targets)

            # Director target의 이미지 해시와 Image target의 해시 교차 검증
            #with measure("Cross check metadatas", system_name=SYSTEM, test_case=TC):
            update_images = self.verifier.hash_check("./meta/update_target.json", targets)

            # 메타데이터 검증 통과 여부 판단
            if not update_images:
                print("[FAIL] Hash Check is failed")
                self.reporter.report("hash_check_failed", {
                    "reason": "Director target and Image target hash mismatch"
                })

            else:
                print("[Primary ECU] Download verified ECU artifacts")

                try:
                    selection = self.installer.select_updates_for_secondary(
                        update_images
                    )
                except Exception as exc:
                    print(f"[FAIL] Secondary target selection failed: {exc}")
                    self.reporter.report(
                        "secondary_target_selection_failed",
                        {"reason": str(exc)},
                    )
                    return

                artifact_updates = selection["updates"]
                secondary_status = selection["secondary_status"]

                if not artifact_updates:
                    print("[Primary ECU] No ECU update target")
                    self.reporter.report("artifact_download_skipped", {
                        "reason": "no ECU update target"
                    })
                    return

                result = self.installer.download_artifacts(artifact_updates, base_url)

                if result["ok"]:
                    self.reporter.report("artifact_download_ok", result)

                    install_result = self.installer.install_serial_firmware(
                        result["results"],
                        expected_secondary_status=secondary_status,
                    )

                    if install_result.get("skipped"):
                        print(
                            "[Primary ECU] Serial firmware install skipped: "
                            f"{install_result.get('reason')}"
                        )
                        self.reporter.report(
                            "serial_firmware_install_skipped",
                            install_result,
                        )
                    elif install_result["ok"]:
                        print("[Primary ECU] Serial firmware install succeeded")
                        self.reporter.report(
                            "serial_firmware_install_ok",
                            install_result,
                        )
                    else:
                        self.reporter.report(
                            "serial_firmware_install_failed",
                            install_result,
                        )
                else:
                    self.reporter.report("artifact_download_failed", result)

            return

        # Director Repository 메타데이터 수신
        else:
            try:
                meta = json.loads(msg.payload.decode("utf-8"))
            except Exception as e:
                print(f"[Prime ECU] invalid JSON on {msg.topic}: {e}")
                return
            
            role = None
            s_hash = None
            target_version = None
            targets = None

            # 메타데이터 구분
            if msg.topic == TOPIC_DIRECTOR_TIMESTAMP:
                role = "timestamp"
                print("[Prime ECU] received Director Timestamp metadata\n")

            elif msg.topic == TOPIC_DIRECTOR_SNAPSHOT:
                role = "snapshot"
                print("[Prime ECU] received Director Snapshot metadata\n")

            elif msg.topic == TOPIC_DIRECTOR_TARGETS:
                role = "targets"
                print("[Prime ECU] received Director Targets metadata\n")


            if role is not None:
                try:
                    meta = json.loads(msg.payload.decode("utf-8"))
                except Exception as e:
                    print(f"[Prime ECU] invalid JSON on {msg.topic}: {e}")
                    return

                self.meta_buffer[role] = meta
                print(f"[Prime ECU] received {role} metadata")

                # Director 메타데이터 모두 들어왔는지 확인
                if all(self.meta_buffer[r] is not None for r in ("timestamp", "snapshot", "targets")):
                    self._on_all_director_meta_received()
    
    # Director 메타데이터 모두 수신 후 서명 검증
    def _on_all_director_meta_received(self):
        ts = self.meta_buffer["timestamp"]
        sn = self.meta_buffer["snapshot"]
        tg = self.meta_buffer["targets"]

        print("[Prime ECU] all director metadata received, start verification")

        #with measure("Director Metadata Verification", system_name=SYSTEM, test_case=TC):
        vr = self.verifier.verify_director_chain(ts, sn, tg)

        if not vr.ok:
            print(f"[Prime ECU] director metadata verify FAILED: {vr.reason}")
            try:
                self.reporter.report("director_meta_verify_failed", {
                    "reason": vr.reason,
                })
            except Exception as e:
                print(f"[Prime ECU] report failed: {e}")
        else:
            print("[Prime ECU] director metadata verify OK")
            self._save_update_target(tg)
            try:
                self.reporter.report("director_meta_verify_ok", {}, request_next=True)
            except Exception as e:
                print(f"[Prime ECU] report failed: {e}")

        # 다음 업데이트를 위해 버퍼 초기화
        self.meta_buffer = {k: None for k in self.meta_buffer}


if __name__ == "__main__":
    handler = PrimeEcuHandler(BROKER, PORT)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        handler.client.loop_stop()
