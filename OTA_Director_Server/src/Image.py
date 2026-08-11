import os
import tarfile
import time
import json
import base64
import hashlib
import ssl
import threading
import requests
from datetime import datetime, timezone

from flask import Flask, request, jsonify, send_from_directory
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import paho.mqtt.client as mqtt
from ecdsa import SigningKey

# from utils.json_handler import JsonHandler
from utils.signature.pub_signature import make_payload_with_signatures
from utils.signature.sub_signature import verify_multi_signature
from utils.snapshot import generate_snapshot
from utils.timestamp import generate_timestamp

BASE_DIR   = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WATCH_DIR  = os.path.join(BASE_DIR, "Image_Repo/meta")          # new_chunks.tar.gz, manifests.tar.gz 위치
IMAGE_DIR = os.path.join(WATCH_DIR, "../image_storage")  # 항상 서비스할 이미지 디렉터리

os.makedirs(IMAGE_DIR, exist_ok=True)

class FlaskServer:
    def __init__(self, host="0.0.0.0", port=8443,
                 cert="./utils/certs/https_server.crt",
                 key="./utils/certs/https_server.key"):
        self.app = Flask(__name__)
        self.host = host
        self.port = port
        self.cert = cert
        self.key = key
        self._register_routes()

    def _register_routes(self):
        # 이미지 서비스
        @self.app.route("/images/<path:image_name>", methods=["GET"])
        def get_chunk(image_name):
            """
            예: GET /images/ivi_1.0.0
              → WATCH_DIR/image_storage/ivi_1.0.0
            """
            full_path = os.path.join(IMAGE_DIR, image_name)
            if not os.path.exists(full_path):
                return jsonify({"error": "chunk not found"}), 404
            return send_from_directory(IMAGE_DIR, image_name, as_attachment=True)

        # Metadata 서비스
        @self.app.route("/meta/<path:meta_type>", methods=["GET"])
        def get_manifests(meta_type):
            """
            예: GET /meta/targets.json
              → WATCH_DIR/meta/targets.json
            """
            full_path = os.path.join(WATCH_DIR, meta_type)
            if not os.path.exists(full_path):
                return jsonify({"error": "manifests not ready"}), 404
            
            return send_from_directory(
                WATCH_DIR,
                meta_type,
                as_attachment=True
            )

    def run(self):
        threading.Thread(
            target=self.app.run,
            kwargs={
                "host": self.host,
                "port": self.port,
                "ssl_context": (self.cert, self.key),
                "threaded": True,
            },
            daemon=True,
        ).start()


class FileHandler:
    def __init__(self, mqtt_broker, mqtt_port, watch_dir, files_path):
        self.MQTT_BROKER = mqtt_broker
        self.MQTT_PORT = mqtt_port
        self.WATCH_DIR = watch_dir
        self.files_path = files_path

        self.MQTT_REQUEST_TOPIC = "primary/request"
        self.MQTT_META_TOPIC = "image/metaData"

        self.ca_cert = "./utils/certs/ca.crt"
        self.client_cert = "./utils/certs/mqtt_client.crt"
        self.client_key = "./utils/certs/mqtt_client.key"

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        configure_tls(self.client, self.ca_cert, self.client_cert, self.client_key)

        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

        self.observer = Observer()
        self.event_handler = FileChangeHandler(self.client, self.WATCH_DIR, self.files_path)
        self.observer.schedule(self.event_handler, self.WATCH_DIR, recursive=False)

    def connect_mqtt(self):
        try:
            self.client.connect(self.MQTT_BROKER, self.MQTT_PORT, 60)
        except Exception as e:
            print(f"[Error] MQTT connection failed: {e}")

    def start_watching(self):
        print(f"[Watcher] Watching directory: {self.WATCH_DIR}")
        self.observer.start()

    def stop_watching(self):
        self.observer.stop()

    def loop_mqtt(self):
        self.client.loop_start()

    def on_connect(self, client, userdata, flags, rc, properties=None):
        print(f"[MQTT] Connected: {rc}")
        client.subscribe(self.MQTT_REQUEST_TOPIC)

    def on_message(self, client, userdata, msg):
        # Vehicle 업데이트 요청 수신
        if msg.topic == self.MQTT_REQUEST_TOPIC:
            print("[MQTT] Meta Data request received from Primary ECU")

            # Send the timestamp metadata
            with open("../Image_Repo/meta/timestamp.json", "r", encoding="utf-8") as f:
                data = json.load(f)
            
            upload_url = f"https://{self.MQTT_BROKER}:8443"
            data["url"] = upload_url
            
            # Timestamp + URL Json 데이터 전송
            self.client.publish(self.MQTT_META_TOPIC, json.dumps(data, ensure_ascii=False).encode("utf-8"), qos=2)
            print(f"[MQTT] 📡 Meta Data published metadata")

def wait_until_file_stable(path: str, interval_sec: float = 0.5, stable_rounds: int = 3, timeout_sec: float = 30.0) -> bool:
    start = time.time()
    last_size = None
    last_mtime = None
    stable = 0

    while True:
        if not os.path.exists(path):
            stable = 0
        else:
            try:
                st = os.stat(path)
                cur_size = st.st_size
                cur_mtime = st.st_mtime

                if cur_size == last_size and cur_mtime == last_mtime and cur_size > 0:
                    stable += 1
                else:
                    stable = 0

                last_size = cur_size
                last_mtime = cur_mtime

                if stable >= stable_rounds:
                    return True
            except OSError:
                stable = 0

        if (time.time() - start) >= timeout_sec:
            return False
        
        time.sleep(interval_sec)

class FileChangeHandler(FileSystemEventHandler):
    def __init__(self, client, watch_dir, files_path):
        self.client = client
        self.watch_dir = watch_dir
        self.files_path = files_path
        self.signing_key_path = "./utils/signature/image_private.pem"

        self._debounce_lock = threading.Lock()
        self._debounce_timer = None
        self._targets_name = "targets.json"

    def _schedule_targets_refresh(self, targets_path: str, delay_sec: float = 0.8):
        """
        targets.json 이벤트가 연속으로 발생해도 delay_sec 이후 1회만 처리.
        """
        with self._debounce_lock:
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()

            self._debounce_timer = threading.Timer(
                delay_sec,
                self._refresh_from_targets,
                args=(targets_path,),
            )
            self._debounce_timer.daemon = True
            self._debounce_timer.start()

    def _refresh_from_targets(self, targets_path: str):
        """
        targets.json이 완전히 기록된 뒤 snapshot/timestamp 갱신.
        """
        # 1) 파일 쓰기 완료 대기
        if not wait_until_file_stable(targets_path, interval_sec=0.5, stable_rounds=3, timeout_sec=60.0):
            print(f"[Watcher] targets.json 안정화 대기 실패(Timeout): {targets_path}")
            return

        # 2) JSON 파싱 레이스를 방지하기 위해 예외 처리 + 짧은 재시도
        last_err = None
        for attempt in range(3):
            try:
                s_path = generate_snapshot()
                print(f"[Image] Update a new snapshot metadata: {s_path}\n")
                ts_path = generate_timestamp()
                print(f"[Image] Update a new timestamp metadata: {ts_path}\n")
                return
            except json.JSONDecodeError as e:
                last_err = e
                print(f"[Watcher] JSONDecodeError (attempt={attempt+1}/3): {e}")
                time.sleep(0.5)

        print(f"[Watcher] snapshot/timestamp 갱신 실패: {last_err}")

    
    # targets.json 변경 감지
    def on_modified(self, event):
        if event.is_directory:
            return

        fname = os.path.basename(event.src_path)
        if fname != self._targets_name:
            return

        # targets.json에 대해서만 반응
        self._schedule_targets_refresh(event.src_path)

    # targets.json “생성” 감지
    def on_created(self, event):
        if event.is_directory:
            return

        fname = os.path.basename(event.src_path)
        if fname != self._targets_name:
            return

        # targets.json 생성에 대해서만 반응
        self._schedule_targets_refresh(event.src_path)
    


def configure_tls(client, ca_cert, client_cert, client_key):
    client.tls_set(
        ca_certs=ca_cert,
        certfile=client_cert,
        keyfile=client_key,
        tls_version=ssl.PROTOCOL_TLSv1_2
    )
    client.tls_insecure_set(True)


if __name__ == "__main__":
    flask_server = FlaskServer()
    flask_server.run()
    print("[Image] Flask repository server started")

    # threading.Thread(
    #     target=process_new_chunks_periodically,
    #     kwargs={"interval_sec": 5},
    #     daemon=True,
    # ).start()
    print("[Main] new_chunks watcher started")

<<<<<<< HEAD
    MQTT_BROKER = "172.20.10.2"
=======
    MQTT_BROKER = "172.20.10.7"
>>>>>>> a266665 (Update three STM32 OTA configuration)
    MQTT_PORT = 8883
    WATCH_DIR = "../Image_Repo/meta"
    files_path = "./data/update_image.tar.xz"

    file_handler = FileHandler(MQTT_BROKER, MQTT_PORT, WATCH_DIR, files_path)
    file_handler.connect_mqtt()
    file_handler.start_watching()
    file_handler.loop_mqtt()

    try:
        while True:
            time.sleep(1)
    
    except KeyboardInterrupt:
        print("[Watcher] Shutting down...")
        
        file_handler.stop_watching()
        file_handler.client.loop_stop()
        file_handler.observer.join()