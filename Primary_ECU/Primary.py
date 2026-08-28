import json
import os
import ssl
import time
from urllib.parse import urljoin

import paho.mqtt.client as mqtt
import requests

from ecu import (
    Updater,
    Storage,
    Transport,
    Verifier,
    Installer,
    Reporter,
)


BROKER = "10.101.161.146"
PORT = 8883

TOPIC_NOTIFY_VERSION = "primary/version"
TOPIC_DIRECTOR_TIMESTAMP = "director/timestamp"
TOPIC_DIRECTOR_SNAPSHOT = "director/snapshot"
TOPIC_DIRECTOR_TARGETS = "director/targets"
TOPIC_REPORT = "primary/report"
TOPIC_REQUEST_UPDATE = "primary/request"
TOPIC_IMAGE_META = "image/metaData"

CA_CERT = "./utils/certs/ca.crt"
CLIENT_CERT = "./utils/certs/mqtt_client.crt"
CLIENT_KEY = "./utils/certs/mqtt_client.key"

SYSTEM = os.environ.get("SYSTEM_NAME", "")
TC = os.environ.get("TEST_CASE", "")


class PrimeEcuHandler:
    def __init__(self, broker, port):
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2
        )

        self._configure_tls(
            self.client,
            CA_CERT,
            CLIENT_CERT,
            CLIENT_KEY,
        )

        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

        # Updater 구성요소
        self.storage = Storage()
        self.transport = Transport()
        self.verifier = Verifier()
        self.installer = Installer(self.storage)
        self.reporter = Reporter(
            self.client,
            TOPIC_REPORT,
        )

        self.updater = Updater(
            self.storage,
            self.transport,
            self.verifier,
            self.installer,
            self.reporter,
        )

        self.meta_buffer = {
            "timestamp": None,
            "snapshot": None,
            "targets": None,
        }

        # 중복 Director metadata로 인한
        # 중복 요청 방지
        self.update_request_sent = False

        # 중복 Image metadata로 인한
        # 중복 설치 방지
        self.image_update_started = False

        self.client.connect(
            broker,
            port,
            60,
        )

        self.client.loop_start()

        # VVM 전송
        with open(
            "./vvm.json",
            "r",
            encoding="utf-8",
        ) as f:
            vvm = json.load(f)

        self.client.publish(
            TOPIC_NOTIFY_VERSION,
            json.dumps(
                vvm,
                ensure_ascii=False,
            ).encode("utf-8"),
            qos=0,
        )

    def _save_update_target(
        self,
        targets_meta: dict,
    ) -> None:
        os.makedirs(
            "./meta",
            exist_ok=True,
        )

        with open(
            "./meta/update_target.json",
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                targets_meta,
                f,
                indent=2,
                ensure_ascii=False,
            )

        print(
            "[Prime ECU] "
            "saved ./meta/update_target.json"
        )

    def _configure_tls(
        self,
        client,
        ca_cert,
        client_cert,
        client_key,
    ):
        client.tls_set(
            ca_certs=ca_cert,
            certfile=client_cert,
            keyfile=client_key,
            tls_version=ssl.PROTOCOL_TLSv1_2,
        )

        client.tls_insecure_set(True)

    def on_connect(
        self,
        client,
        userdata,
        flags,
        rc,
        properties=None,
    ):
        print(
            f"[Prime ECU] Connected: {rc}"
        )

        client.subscribe(
            TOPIC_DIRECTOR_TIMESTAMP,
            qos=1,
        )

        client.subscribe(
            TOPIC_DIRECTOR_SNAPSHOT,
            qos=1,
        )

        client.subscribe(
            TOPIC_DIRECTOR_TARGETS,
            qos=1,
        )

        client.subscribe(
            TOPIC_IMAGE_META,
            qos=1,
        )

    def on_message(
        self,
        client,
        userdata,
        msg,
    ):
        # ==============================================================
        # Image Repository metadata 수신
        # ==============================================================
        if msg.topic == TOPIC_IMAGE_META:
            # 같은 Image metadata가 다시 들어오면
            # 설치 재시도 방지
            if self.image_update_started:
                print(
                    "[Prime ECU] "
                    "duplicate image metadata ignored"
                )
                return

            try:
                timestamp_meta = json.loads(
                    msg.payload.decode(
                        "utf-8"
                    )
                )

            except Exception as e:
                print(
                    "[Prime ECU] "
                    f"invalid JSON on "
                    f"{msg.topic}: {e}"
                )
                return

            # JSON 검증이 끝난 다음
            # 처리 시작 상태로 변경
            self.image_update_started = True

            print(
                "[Prime ECU] "
                "received image metadata\n"
            )

            try:
                base_url = (
                    timestamp_meta["url"]
                )

                # ------------------------------------------------------
                # Timestamp metadata 검증
                # ------------------------------------------------------
                ok, snapshot_hash = (
                    self.verifier
                    .verify_metadata(
                        timestamp_meta
                    )
                )

                if (
                    not ok
                    or snapshot_hash is None
                ):
                    print(
                        "[FAIL] "
                        "Timestamp metadata "
                        "is not correct"
                    )

                    # Metadata 검증 실패는
                    # 기존처럼 즉시 중단
                    return

                # ------------------------------------------------------
                # Snapshot metadata 다운로드
                # ------------------------------------------------------
                snapshot_url = urljoin(
                    base_url.rstrip("/")
                    + "/",
                    "meta/snapshot.json",
                )

                print(
                    "Downloading manifests "
                    f"from {snapshot_url}"
                )

                response = requests.get(
                    snapshot_url,
                    verify=False,
                    timeout=30,
                )

                response.raise_for_status()

                raw_snapshot_bytes = (
                    response.content
                )

                snapshot_meta = (
                    response.json()
                )

                print(
                    "[Prime ECU] "
                    "received Snapshot metadata\n"
                )

                # ------------------------------------------------------
                # Snapshot metadata 검증
                # ------------------------------------------------------
                ok, target_version = (
                    self.verifier
                    .verify_metadata(
                        snapshot_meta,
                        snapshot_hash,
                        snapshot_raw=(
                            raw_snapshot_bytes
                        ),
                    )
                )

                if (
                    not ok
                    or target_version is None
                ):
                    print(
                        "[FAIL] "
                        "Snapshot metadata "
                        "is not correct"
                    )

                    # Metadata 검증 실패는
                    # 기존처럼 즉시 중단
                    return

                # ------------------------------------------------------
                # Targets metadata 다운로드
                # ------------------------------------------------------
                targets_url = urljoin(
                    base_url.rstrip("/")
                    + "/",
                    "meta/targets.json",
                )

                print(
                    "Downloading manifests "
                    f"from {targets_url}"
                )

                response = requests.get(
                    targets_url,
                    verify=False,
                    timeout=30,
                )

                response.raise_for_status()

                targets_meta = (
                    response.json()
                )

                print(
                    "[Prime ECU] "
                    "received Target metadata\n"
                )

                # ------------------------------------------------------
                # Targets metadata 검증
                # ------------------------------------------------------
                ok, targets = (
                    self.verifier
                    .verify_metadata(
                        targets_meta,
                        target_version,
                    )
                )

                if not ok:
                    print(
                        "[FAIL] "
                        "Targets metadata "
                        "is not correct"
                    )

                    # Metadata 검증 실패는
                    # 기존처럼 즉시 중단
                    return

                print(
                    "[OK] "
                    "All metadata "
                    "verified successfully"
                )

                print(targets)

                # ------------------------------------------------------
                # Director / Image Target 교차 검증
                # ------------------------------------------------------
                update_images = (
                    self.verifier.hash_check(
                        "./meta/"
                        "update_target.json",
                        targets,
                    )
                )

                if not update_images:
                    print(
                        "[FAIL] "
                        "Hash Check is failed"
                    )

                    self.reporter.report(
                        "hash_check_failed",
                        {
                            "reason": (
                                "Director target "
                                "and Image target "
                                "hash mismatch"
                            )
                        },
                    )

                    # 전체 Target 교차 검증 실패는
                    # 기존처럼 즉시 중단
                    return

                print(
                    "[Primary ECU] "
                    "Download verified "
                    "ECU artifacts"
                )

                # ------------------------------------------------------
                # Secondary STATUS 기반 Target 선택
                # ------------------------------------------------------
                try:
                    selection = (
                        self.installer
                        .select_updates_for_secondary(
                            update_images
                        )
                    )

                except Exception as exc:
                    print(
                        "[FAIL] Secondary target "
                        "selection failed: "
                        f"{exc}"
                    )

                    self.reporter.report(
                        "secondary_target_selection_failed",
                        {
                            "reason": str(exc)
                        },
                    )

                    return

                artifact_updates = (
                    selection["updates"]
                )

                secondary_statuses = (
                    selection[
                        "secondary_statuses"
                    ]
                )

                if not artifact_updates:
                    print(
                        "[Primary ECU] "
                        "No ECU update target"
                    )

                    self.reporter.report(
                        "artifact_download_skipped",
                        {
                            "reason": (
                                "no ECU update target"
                            )
                        },
                    )

                    return

                # ------------------------------------------------------
                # Artifact 다운로드 및 해시 검증
                # ------------------------------------------------------
                download_result = (
                    self.installer
                    .download_artifacts(
                        artifact_updates,
                        base_url,
                    )
                )

                # 개별 Artifact 실패가 있어도
                # 전체 설치를 여기서 중단하지 않는다.
                if not download_result["ok"]:
                    self.reporter.report(
                        "artifact_download_failed",
                        download_result,
                    )

                    print(
                        "[Primary ECU] "
                        "Some artifacts failed "
                        "verification. "
                        "Continue with per-ECU policy."
                    )

                else:
                    self.reporter.report(
                        "artifact_download_ok",
                        download_result,
                    )

                # ------------------------------------------------------
                # 001 -> 002 -> 003 고정 순서 설치
                #
                # 다운로드 실패 ECU도 downloaded_results에
                # ecu_serial이 남아 있으므로
                # install_serial_firmware() 내부 정책에서
                # BLOCK 처리할 수 있다.
                # ------------------------------------------------------
                install_result = (
                    self.installer
                    .install_serial_firmware(
                        download_result["results"],
                        expected_secondary_statuses=(
                            secondary_statuses
                        ),
                    )
                )

                if install_result.get(
                    "skipped"
                ):
                    print(
                        "[Primary ECU] "
                        "Serial firmware "
                        "install skipped"
                    )

                    self.reporter.report(
                        "serial_firmware_install_skipped",
                        install_result,
                    )

                elif install_result["ok"]:
                    print(
                        "[Primary ECU] "
                        "Serial firmware "
                        "install succeeded"
                    )

                    self.reporter.report(
                        "serial_firmware_install_ok",
                        install_result,
                    )

                else:
                    print(
                        "[Primary ECU] "
                        "Serial firmware "
                        "install failed"
                    )

                    self.reporter.report(
                        "serial_firmware_install_failed",
                        install_result,
                    )

            except Exception as e:
                print(
                    "[FAIL] "
                    "Image update processing "
                    f"failed: {e}"
                )

                self.reporter.report(
                    "image_update_processing_failed",
                    {
                        "reason": str(e)
                    },
                )

            return

        # ==============================================================
        # Director Repository metadata 수신
        # ==============================================================
        try:
            meta = json.loads(
                msg.payload.decode(
                    "utf-8"
                )
            )

        except Exception as e:
            print(
                "[Prime ECU] "
                f"invalid JSON on "
                f"{msg.topic}: {e}"
            )
            return

        role = None

        if (
            msg.topic
            == TOPIC_DIRECTOR_TIMESTAMP
        ):
            role = "timestamp"

            print(
                "[Prime ECU] "
                "received Director "
                "Timestamp metadata\n"
            )

        elif (
            msg.topic
            == TOPIC_DIRECTOR_SNAPSHOT
        ):
            role = "snapshot"

            print(
                "[Prime ECU] "
                "received Director "
                "Snapshot metadata\n"
            )

        elif (
            msg.topic
            == TOPIC_DIRECTOR_TARGETS
        ):
            role = "targets"

            print(
                "[Prime ECU] "
                "received Director "
                "Targets metadata\n"
            )

        if role is None:
            return

        self.meta_buffer[role] = meta

        print(
            f"[Prime ECU] "
            f"received {role} metadata"
        )

        if all(
            self.meta_buffer[item]
            is not None
            for item in (
                "timestamp",
                "snapshot",
                "targets",
            )
        ):
            self._on_all_director_meta_received()

    def _on_all_director_meta_received(
        self,
    ):
        timestamp_meta = (
            self.meta_buffer[
                "timestamp"
            ]
        )

        snapshot_meta = (
            self.meta_buffer[
                "snapshot"
            ]
        )

        targets_meta = (
            self.meta_buffer[
                "targets"
            ]
        )

        print(
            "[Prime ECU] "
            "all director metadata received, "
            "start verification"
        )

        verify_result = (
            self.verifier
            .verify_director_chain(
                timestamp_meta,
                snapshot_meta,
                targets_meta,
            )
        )

        if not verify_result.ok:
            print(
                "[Prime ECU] "
                "director metadata "
                "verify FAILED: "
                f"{verify_result.reason}"
            )

            try:
                self.reporter.report(
                    "director_meta_verify_failed",
                    {
                        "reason": (
                            verify_result.reason
                        )
                    },
                )

            except Exception as e:
                print(
                    "[Prime ECU] "
                    f"report failed: {e}"
                )

        else:
            print(
                "[Prime ECU] "
                "director metadata verify OK"
            )

            # 이미 한 번 업데이트 요청을 보냈으면
            # 중복된 Director metadata는 무시
            if self.update_request_sent:
                print(
                    "[Prime ECU] "
                    "duplicate Director "
                    "metadata ignored"
                )

            else:
                # publish 전에 먼저 True로 변경하여
                # 재진입 또는 중복 콜백 방지
                self.update_request_sent = True

                self._save_update_target(
                    targets_meta
                )

                try:
                    self.reporter.report(
                        "director_meta_verify_ok",
                        {},
                        request_next=True,
                    )

                except Exception as e:
                    print(
                        "[Prime ECU] "
                        f"report failed: {e}"
                    )

        # 다음 metadata 묶음 수신을 위해
        # 버퍼 초기화
        self.meta_buffer = {
            key: None
            for key in self.meta_buffer
        }


if __name__ == "__main__":
    handler = PrimeEcuHandler(
        BROKER,
        PORT,
    )

    try:
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        handler.client.loop_stop()

        print(
            "[Prime ECU] stopped"
        )