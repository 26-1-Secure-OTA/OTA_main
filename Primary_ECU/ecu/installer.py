from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from importlib.resources import path
from typing import Optional, Union, Dict, Any, List
from urllib.parse import urljoin
from pathlib import Path

import os
import shutil
import json
import subprocess
import requests
import hashlib
import re
import time

# from utils.fastcdc_chunking import join_all_by_manifest
# from utils.fastcdc_chunking import load_image_from_oci
# from utils.fastcdc_chunking import load_image_from_tar
# from utils.fastcdc_chunking import run_container

from make_vvm import (
    load_or_create_ed25519_private_key,
    calc_ed25519_keyid_from_public_key,
    sign_block_ed25519,
)

# from utils.metrics import measure

from .storage import Storage
from .secondary_state import SecondaryStateStore
from .safety_policy import evaluate_policy
from .experiment_logger import ExperimentLogger
from .failure_taxonomy import (
    classify_failure,
    label_for_outcome,
)
from .feature_collector import (
    FEATURE_NAMES,
    FeatureCollectionError,
    collect_features,
)

from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


SYSTEM = os.environ.get("SYSTEM_NAME", "")
TC = os.environ.get("TEST_CASE", "")

_HEX64_RE = re.compile(r"(?i)\b[a-f0-9]{64}\b")


def _expected_sha256_from_chunk_name(s: str) -> str:
    m = _HEX64_RE.search(s)

    if not m:
        raise ValueError(
            f"cannot extract sha256 from chunk name: {s}"
        )

    return m.group(0).lower()


@dataclass
class InstallResult:
    ok: bool
    reason: Optional[str] = None


class Installer:
    def __init__(self, storage: Storage):
        self.storage = storage
        self.secondary_states = SecondaryStateStore()

    def load_image_from_tar(self, tar_path: str) -> str:
        result = subprocess.run(
            ["podman", "load", "-i", tar_path],
            check=True,
            capture_output=True,
            text=True,
        )

        output = (
            (result.stdout or "")
            + "\n"
            + (result.stderr or "")
        )

        print(output.strip())

        m = re.search(
            r"Loaded image(?:\(s\))?:\s*(.+)",
            output,
        )

        if not m:
            raise RuntimeError(
                "podman load output에서 이미지 이름을 "
                f"찾지 못했습니다: {output}"
            )

        image_ref = m.group(1).strip()

        return image_ref

    def run_container(
        self,
        image_ref: str,
        container_name: str = "ivi-test",
    ) -> None:
        subprocess.run(
            [
                "podman",
                "rm",
                "-f",
                container_name,
            ],
            check=False,
        )

        subprocess.run(
            [
                "podman",
                "run",
                "-d",
                "--name",
                container_name,
                image_ref,
            ],
            check=True,
        )

        print(
            f"[Primary ECU] Container started: "
            f"{container_name} ({image_ref})"
        )

    def _make_session(self) -> requests.Session:
        session = requests.Session()

        retries = Retry(
            total=3,
            backoff_factor=0.3,
            status_forcelist=[
                429,
                500,
                502,
                503,
                504,
            ],
            allowed_methods=["GET"],
        )

        adapter = HTTPAdapter(
            max_retries=retries,
            pool_connections=32,
            pool_maxsize=32,
        )

        session.mount(
            "http://",
            adapter,
        )
        session.mount(
            "https://",
            adapter,
        )

        return session

    def _download_one_chunk(
        self,
        session: requests.Session,
        url: str,
        out_path: str,
    ) -> None:
        if (
            os.path.exists(out_path)
            and os.path.getsize(out_path) > 0
        ):
            return

        tmp_path = out_path + ".part"

        os.makedirs(
            os.path.dirname(out_path),
            exist_ok=True,
        )

        chunk_file_name = (
            _expected_sha256_from_chunk_name(
                os.path.basename(out_path)
            )
        )

        h = hashlib.sha256()

        try:
            with session.get(
                url,
                stream=True,
                verify=False,
                timeout=(5, 120),
            ) as response:
                response.raise_for_status()

                with open(
                    tmp_path,
                    "wb",
                ) as file:
                    for data in response.iter_content(
                        chunk_size=1024 * 1024
                    ):
                        if not data:
                            continue

                        h.update(data)
                        file.write(data)

            got = h.hexdigest()

            if got != chunk_file_name:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

                raise RuntimeError(
                    "chunk hash mismatch: "
                    f"chunk_file_name={chunk_file_name} "
                    f"got={got} "
                    f"url={url}"
                )

            os.replace(
                tmp_path,
                out_path,
            )

        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass

            raise

    def build_image_manifest_url(
        self,
        base_url,
        ecu,
        image_name,
    ):
        filename = f"{image_name}.json"

        return (
            f"{base_url}/meta/targets/"
            f"{ecu}/{ecu}_image/{filename}"
        )

    def build_image_chunk_url(
        self,
        base_url,
        chunk_name,
    ):
        return (
            f"{base_url}/chunks/"
            f"{chunk_name}"
        )

    def convert_oci_dir_to_tar(
        self,
        oci_dir: str,
        out_tar: str,
    ):
        out_tar = str(out_tar)

        image_ref = str(
            load_image_from_oci(
                oci_dir
            )
        )

        subprocess.run(
            [
                "podman",
                "save",
                "--format",
                "docker-archive",
                "-o",
                out_tar,
                image_ref,
            ],
            check=True,
        )

        subprocess.run(
            [
                "podman",
                "rmi",
                "-f",
                image_ref,
            ],
            check=False,
        )

    def run_static_verification(
        self,
        archive_path: str,
    ):
        env = os.environ.copy()
        env["ARCHIVE"] = archive_path

        static_dir = os.path.join(
            os.path.dirname(__file__),
            "..",
            "static",
        )

        runall = os.path.join(
            static_dir,
            "run_all.sh",
        )

        ts = datetime.now().strftime(
            "%Y%m%d_%H%M%S"
        )

        out_dir = (
            Path("./downloads/static_out")
            / f"{Path(archive_path).stem}_{ts}"
        )

        out_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        print(
            "[Primary ECU] "
            f"Static Verification Start  ->  {archive_path}"
        )

        print(
            "[Primary ECU] "
            f"Static Out Dir            ->  {out_dir}"
        )

        result = subprocess.run(
            [
                "bash",
                runall,
                archive_path,
                str(out_dir),
            ],
            env=env,
            capture_output=True,
            text=True,
        )

        if result.stdout.strip():
            print(result.stdout)

        if result.stderr.strip():
            print(result.stderr)

        policy_log = (
            out_dir
            / "policy.log"
        )

        if policy_log.exists():
            print(
                policy_log.read_text(
                    encoding="utf-8"
                )
            )

        if result.returncode != 0:
            raise RuntimeError(
                "Static verification FAILED for: "
                + archive_path
            )

        print(
            "[Primary ECU] "
            "Static Verification PASSED"
        )

    def download_chunk(
        self,
        update_images: List,
        base_url: str,
    ):
        updated_ecu_versions = []

        for target in update_images:
            with measure(
                "Download Chunks",
                system_name=SYSTEM,
                test_case=TC,
            ):
                chunk_list = (
                    target["images"][
                        "required_chunks"
                    ]
                )

                seen = set()
                uniq = []

                for chunk in chunk_list:
                    if chunk not in seen:
                        seen.add(chunk)
                        uniq.append(chunk)

                chunk_list = uniq

                session = (
                    self._make_session()
                )

                max_workers = int(
                    os.environ.get(
                        "CHUNKS_DL_WORKERS",
                        "8",
                    )
                )

                futures = []

                with ThreadPoolExecutor(
                    max_workers=max_workers
                ) as executor:
                    for chunk in chunk_list:
                        url = (
                            self.build_image_chunk_url(
                                base_url,
                                chunk,
                            )
                        )

                        out_path = (
                            "./downloads/"
                            "chunk_storage/"
                            f"{chunk}"
                        )

                        futures.append(
                            executor.submit(
                                self._download_one_chunk,
                                session,
                                url,
                                out_path,
                            )
                        )

                    for index, future in enumerate(
                        as_completed(futures),
                        1,
                    ):
                        exc = future.exception()

                        if exc is not None:
                            raise RuntimeError(
                                "chunk download failed: "
                                f"{exc}"
                            ) from exc

                        if (
                            index % 50 == 0
                            or index == len(futures)
                        ):
                            print(
                                "[Primary ECU] "
                                "chunk 다운로드 및 해시 "
                                "무결성 검사 진행 "
                                f"{index}/{len(futures)} "
                                f"chunks "
                                f"(workers={max_workers})"
                            )

            with measure(
                "Reassemble chunks",
                system_name=SYSTEM,
                test_case=TC,
            ):
                image_name = str(
                    target["images"][
                        "image_name"
                    ]
                )

                manifest_path = (
                    f"./downloads/"
                    f"{image_name}.json"
                )

                oci_dir = (
                    f"./downloads/"
                    f"{image_name}"
                )

                metrics, tt = (
                    join_all_by_manifest(
                        manifest_path,
                        oci_dir,
                        "./downloads/"
                        "chunk_storage",
                    )
                )

                print(
                    "[Primary ECU] "
                    f"Reassembled (OCI-DIR): "
                    f"{oci_dir}"
                )

            archive_path = (
                f"./downloads/"
                f"{image_name}.tar"
            )

            with measure(
                "",
                system_name=SYSTEM,
                test_case=TC,
            ):
                self.convert_oci_dir_to_tar(
                    oci_dir,
                    archive_path,
                )

                print(
                    "[Primary ECU] "
                    "Packed OCI layout into "
                    f"archive: {archive_path}"
                )

            with open(
                "vvm.json",
                "r",
                encoding="utf-8",
            ) as file:
                vvm = json.load(file)

            for ecu in (
                vvm["signed"][
                    "ecu_version"
                ]
            ):
                if (
                    ecu.get("ecu_serial")
                    == target["ecu"]
                ):
                    ecu[
                        "target_image"
                    ][
                        "filename"
                    ] = f"{image_name}.tar"

                    ecu[
                        "target_image"
                    ][
                        "fileinfo"
                    ][
                        "hashes"
                    ][
                        "sha256"
                    ] = (
                        target["images"][
                            "image_info"
                        ][
                            "hashes"
                        ][
                            "sha256"
                        ]
                    )

                    ecu[
                        "target_image"
                    ][
                        "fileinfo"
                    ][
                        "hashes"
                    ][
                        "sha512"
                    ] = (
                        target["images"][
                            "image_info"
                        ][
                            "hashes"
                        ][
                            "sha512"
                        ]
                    )

                    updated_ecu_versions.append(
                        ecu
                    )

                    break

            self.run_static_verification(
                str(
                    Path(
                        archive_path
                    ).resolve()
                )
            )

            with open(
                "vvm.json",
                "w",
                encoding="utf-8",
            ) as file:
                json.dump(
                    vvm,
                    file,
                    indent=2,
                )

            print(
                "[Primary ECU] "
                "Update VVM information"
            )

        return updated_ecu_versions

    def download_manifest(
        self,
        update_images: List,
        base_url: str,
    ):
        for target in update_images:
            ecu = target["ecu"]

            image_name = (
                target["images"][
                    "image_name"
                ]
            )

            url = (
                self.build_image_manifest_url(
                    base_url,
                    ecu,
                    image_name,
                )
            )

            print(
                f"[Primary ECU] GET:  {url}"
            )

            response = requests.get(
                url,
                verify=False,
            )

            if response.status_code != 200:
                raise RuntimeError(
                    f"Failed to fetch {url}: "
                    f"{response.status_code}"
                )

            image_meta = response.json()

            save_path = Path(
                f"./downloads/"
                f"{image_name}.json"
            )

            save_path.write_text(
                json.dumps(
                    image_meta,
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            installed_list_path = (
                "./meta/"
                "installed_layers.json"
            )

            if not os.path.exists(
                installed_list_path
            ):
                installed = {
                    "layers": []
                }

            else:
                with open(
                    installed_list_path,
                    "r",
                ) as file:
                    installed = (
                        json.load(file)
                    )

            installed_hashes = set(
                installed.get(
                    "layers",
                    [],
                )
            )

            chunks = (
                image_meta["signed"][
                    "chunks"
                ]
            )

            target_hashes = set()

            for key in chunks.keys():
                if key.startswith(
                    "blobs/sha256/"
                ):
                    sha = key.split(
                        "blobs/sha256/"
                    )[1]

                    target_hashes.add(
                        sha
                    )

            new_layers = [
                h
                for h in target_hashes
                if h not in installed_hashes
            ]

            if new_layers:
                installed_hashes.update(
                    new_layers
                )

                return list(
                    installed_hashes
                )

            return []

    def download_image(
        self,
        update_images: List,
        base_url: str,
    ):
        for image in update_images:
            image_name = (
                image["images"][
                    "image_name"
                ]
            )

            image_path = (
                f"{base_url}/images/"
                f"{image_name}.tar"
            )

            out_path = (
                "./downloads/"
                "image_storage/"
                f"{image_name}.tar"
            )

            print(
                "[Primary ECU] "
                f"GET:      {image_path}"
            )

            try:
                with requests.get(
                    image_path,
                    stream=True,
                    verify=False,
                ) as response:
                    response.raise_for_status()

                    with open(
                        out_path,
                        "wb",
                    ) as file:
                        for data in (
                            response.iter_content(
                                chunk_size=8192
                            )
                        ):
                            if data:
                                file.write(data)

                print(
                    f"[OK] saved image -> "
                    f"{out_path}"
                )

            except Exception as exc:
                print(
                    "[FAIL] failed to "
                    f"download chunk "
                    f"{image_name} from "
                    f"{image_path}: {exc}"
                )

            image_ref = (
                self.load_image_from_tar(
                    out_path
                )
            )

            self.run_container(
                image_ref,
                container_name=(
                    f"{image_name}-ctr"
                ),
            )

            version = (
                image_name.split("_")[1]
            )

            major, minor, patch = map(
                int,
                version.split("."),
            )

            if major == 3:
                major = 0
            else:
                major -= 1

            version = (
                f"{major}."
                f"{minor}."
                f"{patch}"
            )

    def update_info(
        self,
        layer_list,
        vvm_version,
    ):
        with open(
            "./meta/"
            "installed_layers.json",
            "w",
        ) as file:
            json.dump(
                {
                    "layers": layer_list
                },
                file,
                indent=2,
            )

        ED25519_PRIVATE_KEY_PATH = Path(
            "ed25519_private_key.pem"
        )

        vin = "VIN-TEST-0001"
        primary_ecu_serial = "primary0"

        ed_private_key = (
            load_or_create_ed25519_private_key(
                ED25519_PRIVATE_KEY_PATH
            )
        )

        vvm_keyid = (
            calc_ed25519_keyid_from_public_key(
                ed_private_key
            )
        )

        now = datetime.now(
            timezone.utc
        )

        expires_str = (
            now
            + timedelta(days=365)
        ).replace(
            microsecond=0
        ).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        signed = {
            "vin": vin,
            "primary_ecu_serial": (
                primary_ecu_serial
            ),
            "expires": expires_str,
            "ecu_version": vvm_version,
        }

        signed["keyid"] = (
            vvm_keyid
        )

        sig = sign_block_ed25519(
            ed_private_key,
            signed,
        )

        vvm_obj = {
            "signatures": [
                {
                    "keyid": vvm_keyid,
                    "sig": sig,
                }
            ],
            "signed": signed,
        }

        VVM_JSON_PATH = Path(
            "vvm.json"
        )

        VVM_JSON_PATH.write_text(
            json.dumps(
                vvm_obj,
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        print(
            "vvm.json 업데이트 완료: "
            f"{VVM_JSON_PATH}"
        )

    def install(
        self,
        image_path: str,
        version: str,
    ) -> InstallResult:
        try:
            staging = (
                self.storage.staging_dir(
                    version
                )
            )

            os.makedirs(
                staging,
                exist_ok=True,
            )

            shutil.copy2(
                image_path,
                os.path.join(
                    staging,
                    os.path.basename(
                        image_path
                    ),
                ),
            )

            active = (
                self.storage.active_symlink()
            )

            if os.path.islink(active):
                os.unlink(active)

            os.symlink(
                staging,
                active,
            )

            self.storage.write_version(
                version
            )

            return InstallResult(
                ok=True
            )

        except Exception as exc:
            return InstallResult(
                ok=False,
                reason=str(exc),
            )

    def rollback(self):
        prev = (
            self.storage.last_good_version()
        )

        if not prev:
            raise RuntimeError(
                "no previous version "
                "to rollback"
            )

        active = (
            self.storage.active_symlink()
        )

        if os.path.islink(active):
            os.unlink(active)

        os.symlink(
            self.storage.staging_dir(
                prev
            ),
            active,
        )

    def _sha256_file(
        self,
        path: str,
    ) -> str:
        h = hashlib.sha256()

        with open(
            path,
            "rb",
        ) as file:
            for chunk in iter(
                lambda: file.read(
                    1024 * 1024
                ),
                b"",
            ):
                if chunk:
                    h.update(chunk)

        return h.hexdigest()

    def _sha512_file(
        self,
        path: str,
    ) -> str:
        h = hashlib.sha512()

        with open(
            path,
            "rb",
        ) as file:
            for chunk in iter(
                lambda: file.read(
                    1024 * 1024
                ),
                b"",
            ):
                if chunk:
                    h.update(chunk)

        return h.hexdigest()

    def _parse_cfg_text(
        self,
        cfg_text: str,
    ) -> dict:
        cfg = {}

        for line in (
            cfg_text.splitlines()
        ):
            line = line.strip()

            if (
                not line
                or line.startswith("#")
            ):
                continue

            if "=" not in line:
                continue

            key, value = (
                line.split(
                    "=",
                    1,
                )
            )

            cfg[key.strip()] = (
                value.strip()
            )

        return cfg

    @staticmethod
    def _target_slot_from_update(
        item: dict,
    ) -> Optional[str]:
        """Read and cross-check the slot encoded in metadata and target name."""

        images = (
            item.get("images")
            or {}
        )

        image_name = str(
            images.get(
                "image_name"
            )
            or ""
        )

        image_info = (
            images.get(
                "image_info"
            )
            or {}
        )

        metadata_slot = (
            image_info.get(
                "custom"
            )
            or {}
        ).get(
            "target_slot"
        )

        if metadata_slot is not None:
            metadata_slot = str(
                metadata_slot
            ).upper()

            if metadata_slot not in (
                "A",
                "B",
            ):
                raise RuntimeError(
                    "invalid target_slot "
                    f"metadata for "
                    f"{image_name}: "
                    f"{metadata_slot}"
                )

        match = re.search(
            r"_slot[-_]([ab])$",
            image_name,
            re.IGNORECASE,
        )

        name_slot = (
            match.group(1).upper()
            if match
            else None
        )

        if metadata_slot != name_slot:
            raise RuntimeError(
                "slot metadata/name "
                f"mismatch for "
                f"{image_name}: "
                f"metadata={metadata_slot}, "
                f"name={name_slot}"
            )

        return metadata_slot

    def select_updates_for_secondary(
        self,
        update_images: List,
    ) -> dict:
        from .secondary_serial import (
            SecondarySerial,
        )
        parsed = [
            (
                item,
                self._target_slot_from_update(
                    item
                ),
            )
            for item in update_images
        ]

        if not any(
            slot is not None
            for _, slot in parsed
        ):
            return {
                "updates": list(
                    update_images
                ),
                "secondary_statuses": {},
            }

        discovered = (
            SecondarySerial
            .discover_secondaries()
        )

        secondary_statuses = {
            ecu_serial: (
                secondary_info[
                    "status"
                ]
            )
            for (
                ecu_serial,
                secondary_info,
            ) in discovered.items()
        }

        selected = []
        relevant_counts = {}
        match_counts = {}

        for (
            item,
            target_slot,
        ) in parsed:
            if target_slot is None:
                selected.append(item)
                continue

            ecu_serial = str(
                item.get("ecu")
                or ""
            )

            status = (
                secondary_statuses.get(
                    ecu_serial
                )
            )

            if status is None:
                selected.append(item)
                continue

            relevant_counts[
                ecu_serial
            ] = (
                relevant_counts.get(
                    ecu_serial,
                    0,
                )
                + 1
            )

            if (
                target_slot
                == status[
                    "target_slot"
                ]
            ):
                selected.append(item)

                match_counts[
                    ecu_serial
                ] = (
                    match_counts.get(
                        ecu_serial,
                        0,
                    )
                    + 1
                )

        for ecu_serial in (
            relevant_counts
        ):
            match_count = (
                match_counts.get(
                    ecu_serial,
                    0,
                )
            )

            if match_count != 1:
                status = (
                    secondary_statuses[
                        ecu_serial
                    ]
                )

                raise RuntimeError(
                    "expected exactly one "
                    "firmware target for "
                    "inactive slot "
                    f"ECU={ecu_serial}, "
                    f"TARGET="
                    f"{status['target_slot']}, "
                    f"found={match_count}"
                )

        for (
            ecu_serial,
            status,
        ) in secondary_statuses.items():
            if (
                ecu_serial
                not in relevant_counts
            ):
                continue

            print(
                "[Primary ECU] "
                "Secondary selected "
                "before download: "
                f"ECU={ecu_serial}, "
                f"ACTIVE="
                f"{status['active_slot']}, "
                f"TARGET="
                f"{status['target_slot']}"
            )

        return {
            "updates": selected,
            "secondary_statuses": (
                secondary_statuses
            ),
        }

    def download_artifacts(
        self,
        update_images: List,
        base_url: str,
    ) -> dict:
        """Download and verify update artifacts for every ECU."""

        storage_dir = Path(
            "./downloads/"
            "artifact_storage"
        )

        storage_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        supported_extensions = (
            ".bin",
            ".tar",
            ".cfg",
        )

        results = []

        for item in update_images:
            part_path = None
            ecu_serial = "UNKNOWN"

            try:
                ecu_serial = str(
                    item["ecu"]
                )

                image_name = str(
                    item["images"][
                        "image_name"
                    ]
                )

                image_info = (
                    item["images"][
                        "image_info"
                    ]
                )

                custom = (
                    image_info.get(
                        "custom"
                    )
                    or {}
                )

                target_version = (
                    custom.get(
                        "version"
                    )
                )

                expected_target_slot = (
                    self._target_slot_from_update(
                        item
                    )
                )

                if (
                    expected_target_slot
                    is not None
                    and not target_version
                ):
                    raise RuntimeError(
                        "target version "
                        f"missing: "
                        f"{image_name}"
                    )

                if (
                    Path(ecu_serial).name
                    != ecu_serial
                ):
                    raise RuntimeError(
                        "invalid ECU name: "
                        f"{ecu_serial}"
                    )

                if (
                    Path(image_name).name
                    != image_name
                ):
                    raise RuntimeError(
                        "invalid image name: "
                        f"{image_name}"
                    )

                hashes = (
                    image_info.get(
                        "hashes"
                    )
                    or {}
                )

                expected_sha256 = (
                    hashes.get("sha256")
                )

                expected_sha512 = (
                    hashes.get("sha512")
                )

                expected_length = (
                    image_info.get(
                        "length"
                    )
                )

                if (
                    not expected_sha256
                    or not expected_sha512
                    or expected_length is None
                ):
                    raise RuntimeError(
                        "artifact metadata is "
                        "missing length, "
                        "SHA-256, or SHA-512"
                    )

                selected = None
                attempts = []

                for extension in (
                    supported_extensions
                ):
                    filename = (
                        f"{image_name}"
                        f"{extension}"
                    )

                    artifact_url = urljoin(
                        base_url.rstrip("/")
                        + "/",
                        f"images/{filename}",
                    )

                    out_path = (
                        storage_dir
                        / filename
                    )

                    part_path = (
                        storage_dir
                        / f"{filename}.part"
                    )

                    print(
                        "[Primary ECU] "
                        f"TRY ARTIFACT "
                        f"({ecu_serial}): "
                        f"{artifact_url}"
                    )

                    with requests.get(
                        artifact_url,
                        stream=True,
                        verify=False,
                        timeout=30,
                    ) as response:
                        if (
                            getattr(
                                response,
                                "status_code",
                                None,
                            )
                            == 404
                        ):
                            attempts.append(
                                f"{filename}: "
                                "not found"
                            )
                            continue

                        response.raise_for_status()

                        with part_path.open(
                            "wb"
                        ) as artifact_file:
                            for data in (
                                response.iter_content(
                                    chunk_size=8192
                                )
                            ):
                                if data:
                                    artifact_file.write(
                                        data
                                    )

                    actual_length = (
                        part_path
                        .stat()
                        .st_size
                    )

                    actual_sha256 = (
                        self._sha256_file(
                            str(
                                part_path
                            )
                        )
                    )

                    actual_sha512 = (
                        self._sha512_file(
                            str(
                                part_path
                            )
                        )
                    )

                    matches_metadata = (
                        actual_length
                        == int(
                            expected_length
                        )
                        and (
                            actual_sha256.lower()
                            == str(
                                expected_sha256
                            ).lower()
                        )
                        and (
                            actual_sha512.lower()
                            == str(
                                expected_sha512
                            ).lower()
                        )
                    )

                    if not matches_metadata:
                        attempts.append(
                            f"{filename}: "
                            "metadata mismatch"
                        )

                        if part_path.exists():
                            part_path.unlink()

                        part_path = None
                        continue

                    if (
                        expected_target_slot
                        is not None
                    ):
                        if extension != ".bin":
                            raise RuntimeError(
                                "slot-specific target "
                                "resolved to a "
                                "non-bin artifact: "
                                f"{filename}"
                            )

                        from .secondary_serial import (
                            SecondarySerial,
                        )

                        linked_slot = (
                            SecondarySerial
                            .detect_firmware_slot(
                                str(
                                    part_path
                                )
                            )
                        )

                        if (
                            linked_slot
                            != expected_target_slot
                        ):
                            raise RuntimeError(
                                "firmware slot "
                                f"mismatch for "
                                f"{filename}: "
                                f"metadata="
                                f"{expected_target_slot}, "
                                f"linked="
                                f"{linked_slot}"
                            )

                    os.replace(
                        part_path,
                        out_path,
                    )

                    part_path = None

                    selected = {
                        "filename": (
                            filename
                        ),
                        "file_type": (
                            extension.lstrip(
                                "."
                            )
                        ),
                        "out_path": (
                            out_path
                        ),
                        "length": (
                            actual_length
                        ),
                        "sha256": (
                            actual_sha256
                        ),
                        "target_slot": (
                            expected_target_slot
                        ),
                    }

                    break

                if selected is None:
                    raise RuntimeError(
                        "no .bin, .tar, "
                        "or .cfg artifact "
                        "matched the signed "
                        "metadata: "
                        + "; ".join(
                            attempts
                        )
                    )

                results.append({
                    "ecu_serial": (
                        ecu_serial
                    ),
                    "artifact": (
                        selected[
                            "filename"
                        ]
                    ),
                    "path": str(
                        selected[
                            "out_path"
                        ]
                    ),
                    "file_type": (
                        selected[
                            "file_type"
                        ]
                    ),
                    "length": (
                        selected[
                            "length"
                        ]
                    ),
                    "sha256": (
                        selected[
                            "sha256"
                        ]
                    ),
                    "target_slot": (
                        selected[
                            "target_slot"
                        ]
                    ),
                    "target_version": (
                        target_version
                    ),
                    "status": "OK",
                })

                print(
                    "[Primary ECU] "
                    "Artifact verified "
                    "and saved: "
                    f"{selected['out_path']}"
                )

            except Exception as exc:
                print(
                    "[FAIL] artifact "
                    "download failed: "
                    f"{exc}"
                )

                results.append({
                    "ecu_serial": (
                        ecu_serial
                    ),
                    "status": "FAIL",
                    "reason": str(exc),
                })

            finally:
                if (
                    part_path is not None
                    and part_path.exists()
                ):
                    part_path.unlink()

        return {
            "ok": (
                bool(results)
                and all(
                    result.get(
                        "status"
                    )
                    == "OK"
                    for result in results
                )
            ),
            "results": results,
        }

    def download_firmware(
        self,
        update_images: List,
        base_url: str,
    ) -> dict:
        return self.download_artifacts(
            update_images,
            base_url,
        )

    def _install_one_serial_firmware(
        self,
        downloaded_results: List,
        port: str,
        expected_ecu: str,
        expected_secondary_status: Optional[dict] = None,
        expected_uid: Optional[str] = None,
        policy_artifact_info: Optional[dict] = None,
        force_reinstall: bool = False,
    ) -> dict:
        """Install the matching .bin on the currently connected Secondary.

        All ECU artifacts remain downloadable. At installation time the
        Secondary reports its own ECU ID and inactive target slot. The Primary
        then selects exactly one verified image matching both values.
        """
        from .secondary_serial import SecondarySerial

        target = None
        before_status = None
        policy_recheck = None
        failure_stage = "PRECHECK"
        update_attempted = False

        verified_bins = [
            item
            for item in downloaded_results
            if (
                item.get("status") == "OK"
                and item.get("file_type") == "bin"
            )
        ]

        if not verified_bins:
            self.secondary_states.transition(
                expected_ecu,
                "HOLD",
                ok=None,
                reason="no verified .bin artifact",
            )

            return {
                "ok": True,
                "skipped": True,
                "reason": "no verified .bin artifact",
            }

        try:
            with SecondarySerial(port=port) as secondary:
                # ---------------------------------------------------------
                # 업데이트 직전 Secondary STATUS
                # ---------------------------------------------------------
                failure_stage = "STATUS"
                before_status = secondary.get_status()

                if before_status["ecu_serial"] != expected_ecu:
                    raise RuntimeError(
                        "port/ECU mismatch: "
                        f"expected={expected_ecu}, "
                        f"actual={before_status['ecu_serial']}, "
                        f"port={port}"
                    )

                # ---------------------------------------------------------
                # AI ranking 이후, FW_BEGIN 직전 Fresh STATUS Safety 재검사
                # ---------------------------------------------------------
                failure_stage = "PRECHECK"
                policy_recheck = evaluate_policy(
                    expected_ecu=expected_ecu,
                    status=before_status,
                    artifact_info=policy_artifact_info,
                    expected_uid=expected_uid,
                    force_reinstall=force_reinstall,
                )

                if policy_recheck["decision"] != "ALLOW":
                    self.secondary_states.transition(
                        expected_ecu,
                        policy_recheck["decision"],
                        artifact=(
                            policy_artifact_info.get("artifact")
                            if policy_artifact_info
                            else None
                        ),
                        firmware_sha256=(
                            policy_artifact_info.get("sha256")
                            if policy_artifact_info
                            else None
                        ),
                        ok=None,
                        reason=policy_recheck["reason_code"],
                        details={
                            "policy_phase": "PRE_TRANSFER_RECHECK",
                            "policy": policy_recheck,
                            "status": before_status,
                        },
                    )
                    return {
                        "ok": False,
                        "skipped": True,
                        "ecu_serial": expected_ecu,
                        "decision": policy_recheck["decision"],
                        "reason_code": policy_recheck["reason_code"],
                        "reason": policy_recheck["reason"],
                        "update_attempted": False,
                        "success": None,
                        "secondary_before": before_status,
                        "policy_recheck": policy_recheck,
                        "failure_stage": None,
                        "failure_reason_code": None,
                    }

                # ---------------------------------------------------------
                # 다운로드 전 STATUS와 설치 직전 STATUS 일관성 검사
                # ---------------------------------------------------------
                if expected_secondary_status is not None:
                    identity_fields = (
                        "ecu_serial",
                        "uid",
                        "version",
                        "active_slot",
                        "target_slot",
                        "ready",
                        "health",
                    )

                    changed = [
                        field
                        for field in identity_fields
                        if (
                            before_status.get(field)
                            != expected_secondary_status.get(field)
                        )
                    ]

                    if changed:
                        raise RuntimeError(
                            "Secondary state changed during download: "
                            f"fields={changed}, "
                            f"before="
                            f"{expected_secondary_status.get('raw')}, "
                            f"now={before_status.get('raw')}"
                        )

                # ---------------------------------------------------------
                # 현재 ECU에 해당하는 검증 완료 .bin 선택
                # ---------------------------------------------------------
                same_ecu = [
                    item
                    for item in verified_bins
                    if (
                        item.get("ecu_serial")
                        == before_status["ecu_serial"]
                    )
                ]

                if not same_ecu:
                    reason = (
                        "no .bin artifact for connected ECU "
                        f"{before_status['ecu_serial']}"
                    )

                    self.secondary_states.transition(
                        expected_ecu,
                        "HOLD",
                        ok=None,
                        reason=reason,
                    )

                    return {
                        "ok": True,
                        "skipped": True,
                        "reason": reason,
                        "secondary_before": before_status,
                    }

                candidates = []
                inspected = []

                for item in same_ecu:
                    linked_slot = SecondarySerial.detect_firmware_slot(
                        item["path"]
                    )

                    declared_slot = item.get("target_slot")

                    inspected.append({
                        "artifact": item.get("artifact"),
                        "declared_slot": declared_slot,
                        "linked_slot": linked_slot,
                    })

                    if (
                        declared_slot is not None
                        and declared_slot != linked_slot
                    ):
                        raise RuntimeError(
                            "downloaded firmware declaration/link mismatch: "
                            f"{inspected[-1]}"
                        )

                    if linked_slot == before_status["target_slot"]:
                        candidates.append(item)

                if not candidates:
                    raise RuntimeError(
                        "no firmware image matches the inactive target slot "
                        f"{before_status['target_slot']}; "
                        f"inspected={inspected}"
                    )

                if len(candidates) != 1:
                    raise RuntimeError(
                        "multiple firmware images match connected ECU and "
                        f"target slot {before_status['target_slot']}: "
                        f"{[item.get('artifact') for item in candidates]}"
                    )

                target = candidates[0]

                # ---------------------------------------------------------
                # TRANSFERRING
                # ---------------------------------------------------------
                self.secondary_states.transition(
                    expected_ecu,
                    "TRANSFERRING",
                    artifact=target.get("artifact"),
                    firmware_sha256=target.get("sha256"),
                    ok=None,
                    details={
                        "port": port,
                        "active_slot": before_status["active_slot"],
                        "target_slot": before_status["target_slot"],
                    },
                )

                print(
                    "[Primary ECU] Install Serial firmware: "
                    f"ECU={before_status['ecu_serial']}, "
                    f"ACTIVE={before_status['active_slot']}, "
                    f"TARGET={before_status['target_slot']}, "
                    f"ARTIFACT={target['artifact']}"
                )

                # ---------------------------------------------------------
                # Serial Firmware 전송 시간 측정
                # ---------------------------------------------------------
                transfer_started = time.monotonic()

                failure_stage = "TRANSFER"
                update_attempted = True
                transfer_result = secondary.send_firmware(
                    firmware_path=target["path"],
                    expected_sha256=target["sha256"],
                    expected_target_slot=(
                        before_status["target_slot"]
                    ),
                    max_firmware_size=(
                        before_status["max_size"]
                    ),
                )

                transfer_duration_ms = int(
                    (
                        time.monotonic()
                        - transfer_started
                    )
                    * 1000
                )

                throughput_kbps = (
                    transfer_result["length"]
                    * 8
                    / transfer_duration_ms
                    if transfer_duration_ms > 0
                    else None
                )

                failure_stage = "SLOT_SWITCH"

                # ---------------------------------------------------------
                # STAGED -> ACTIVATING
                # ---------------------------------------------------------
                self.secondary_states.transition(
                    expected_ecu,
                    "STAGED",
                    artifact=target.get("artifact"),
                    firmware_sha256=target.get("sha256"),
                    ok=None,
                    details={
                        "target_slot": before_status["target_slot"],
                    },
                )

                self.secondary_states.transition(
                    expected_ecu,
                    "ACTIVATING",
                    artifact=target.get("artifact"),
                    firmware_sha256=target.get("sha256"),
                    ok=None,
                    details={
                        "target_slot": before_status["target_slot"],
                    },
                )

                # STM32가 FW_OK를 전송한 뒤 reset되고
                # Bootloader가 새 Slot으로 진입할 시간을 기다린다.
                reboot_delay = float(
                    os.environ.get(
                        "STM32_REBOOT_DELAY",
                        "2.0",
                    )
                )

                if reboot_delay > 0:
                    time.sleep(reboot_delay)

                # ---------------------------------------------------------
                # HEALTH CHECK
                # ---------------------------------------------------------
                self.secondary_states.transition(
                    expected_ecu,
                    "HEALTH_CHECK",
                    artifact=target.get("artifact"),
                    firmware_sha256=target.get("sha256"),
                    ok=None,
                    details={
                        "expected_active_slot": (
                            before_status["target_slot"]
                        ),
                    },
                )

                failure_stage = "REBOOT"
                after_status = secondary.get_status(
                    timeout_seconds=10.0
                )

                failure_stage = "POST_CHECK"

                # ---------------------------------------------------------
                # 사후 ECU ID 검사
                # ---------------------------------------------------------
                if (
                    after_status["ecu_serial"]
                    != before_status["ecu_serial"]
                ):
                    raise RuntimeError(
                        "Secondary ECU ID changed after update: "
                        f"before={before_status['ecu_serial']}, "
                        f"after={after_status['ecu_serial']}"
                    )

                # ---------------------------------------------------------
                # 사후 Slot 검사
                # ---------------------------------------------------------
                if (
                    after_status["active_slot"]
                    != before_status["target_slot"]
                ):
                    raise RuntimeError(
                        "firmware transfer finished but target slot "
                        "did not become active: "
                        f"expected={before_status['target_slot']}, "
                        f"actual={after_status['active_slot']}"
                    )

                # ---------------------------------------------------------
                # 사후 UID 검사
                # ---------------------------------------------------------
                if (
                    before_status.get("uid")
                    and after_status.get("uid")
                    != before_status.get("uid")
                ):
                    raise RuntimeError(
                        "Secondary UID changed after update"
                    )

                # ---------------------------------------------------------
                # 사후 Firmware Version 검사
                # ---------------------------------------------------------
                expected_version = target.get(
                    "target_version"
                )

                if (
                    expected_version
                    and after_status.get("version")
                    != expected_version
                ):
                    raise RuntimeError(
                        "firmware version did not change to target: "
                        f"expected={expected_version}, "
                        f"actual={after_status.get('version')}"
                    )

                # ---------------------------------------------------------
                # 사후 HEALTH 검사
                # ---------------------------------------------------------
                if (
                    after_status.get("health") is not None
                    and after_status.get("health") != "OK"
                ):
                    raise RuntimeError(
                        "Secondary health check failed: "
                        f"{after_status.get('health')}"
                    )

                # ---------------------------------------------------------
                # CONFIRMED
                # ---------------------------------------------------------
                self.secondary_states.transition(
                    expected_ecu,
                    "CONFIRMED",
                    artifact=target.get("artifact"),
                    firmware_sha256=target.get("sha256"),
                    ok=True,
                    details={
                        "port": port,
                        "active_slot": after_status["active_slot"],
                        "target_slot": after_status["target_slot"],
                        "version": after_status.get("version"),
                        "health": after_status.get("health"),
                        "transfer_duration_ms": transfer_duration_ms,
                        "throughput_kbps": throughput_kbps,
                    },
                )

                return {
                    "ok": True,
                    "skipped": False,
                    "ecu_serial": before_status["ecu_serial"],
                    "artifact": target["artifact"],
                    "secondary_before": before_status,
                    "transfer": transfer_result,
                    "secondary_after": after_status,
                    "fw_ok_received": (
                        transfer_result.get("response")
                        == "FW_OK"
                    ),
                    "slot_switched": True,
                    "version_verified": (
                        expected_version is None
                        or after_status.get("version")
                        == expected_version
                    ),
                    "transfer_duration_ms": (
                        transfer_duration_ms
                    ),
                    "throughput_kbps": (
                        throughput_kbps
                    ),
                    "retry_count": transfer_result.get(
                        "retry_count",
                        0,
                    ),
                    "update_attempted": True,
                    "failure_stage": None,
                    "failure_reason_code": None,
                    "policy_recheck": policy_recheck,
                }

        except Exception as exc:
            failure = classify_failure(
                exc,
                stage_hint=failure_stage,
            )
            self.secondary_states.transition(
                expected_ecu,
                "FAILED",
                artifact=(
                    target.get("artifact")
                    if target
                    else None
                ),
                firmware_sha256=(
                    target.get("sha256")
                    if target
                    else None
                ),
                ok=False,
                reason=str(exc),
                details={
                    "port": port,
                    "failure_stage": failure.stage,
                    "failure_reason_code": failure.reason_code,
                },
            )

            print(
                "[FAIL] Serial firmware installation failed: "
                f"{exc}"
            )

            return {
                "ok": False,
                "skipped": False,
                "ecu_serial": expected_ecu,
                "reason": str(exc),
                "failure_stage": failure.stage,
                "failure_reason_code": failure.reason_code,
                "board_related": failure.board_related,
                "ml_label_eligible": failure.ml_label_eligible,
                "update_attempted": update_attempted,
                "secondary_before": before_status,
                "policy_recheck": policy_recheck,
                # The current Serial protocol performs no retries.
                "retry_count": 0,
            }

    def install_serial_firmware(
        self,
        downloaded_results: List,
        expected_secondary_statuses: Optional[dict] = None,
    ) -> dict:
        from .secondary_serial import (
            SecondarySerial,
        )
        from ai.anomaly_scorer import (
            FIXED_ORDER,
            schedule_allow_ecus,
        )

        expected_secondary_statuses = (
            expected_secondary_statuses
            or {}
        )

        registry_path = Path(
            "./config/"
            "secondary_registry.json"
        )

        with registry_path.open(
            "r",
            encoding="utf-8",
        ) as file:
            registry = json.load(file)

        logger = ExperimentLogger()

        scenario_id = os.environ.get(
            "OTA_SCENARIO_ID",
            "UNSPECIFIED",
        )
        experiment_campaign_id = os.environ.get("OTA_CAMPAIGN_ID", "").strip()
        experiment_attempt_id = os.environ.get("OTA_ATTEMPT_ID", "").strip().upper()
        force_reinstall = (
            os.environ.get("OTA_EXPERIMENT_FORCE_REINSTALL", "0") == "1"
        )
        if force_reinstall and (
            scenario_id == "UNSPECIFIED"
            or not experiment_campaign_id
            or re.fullmatch(r"[0-9A-F]{8}", experiment_attempt_id) is None
        ):
            raise ValueError(
                "OTA_EXPERIMENT_FORCE_REINSTALL requires scenario, campaign, "
                "and an eight-digit hexadecimal attempt ID"
            )
        data_source = os.environ.get(
            "OTA_DATA_SOURCE",
            "BOARD",
        )

        if data_source not in {
            "BOARD",
            "SIMULATOR",
        }:
            raise ValueError(
                "OTA_DATA_SOURCE must be BOARD or SIMULATOR: "
                f"{data_source}"
            )

        def optional_environment_float(
            name: str,
        ) -> Optional[float]:
            raw_value = os.environ.get(name)
            if raw_value is None or not raw_value.strip():
                return None
            try:
                return float(raw_value)
            except ValueError as exc:
                raise ValueError(
                    f"{name} must be numeric: {raw_value}"
                ) from exc

        if data_source == "SIMULATOR":
            power_percent = optional_environment_float(
                "OTA_POWER_PERCENT"
            )
            temperature_c = optional_environment_float(
                "OTA_TEMPERATURE_C"
            )
        else:
            # Physical-board telemetry must come from STATUS, never
            # from environment overrides.
            power_percent = None
            temperature_c = None

        campaign_id = experiment_campaign_id or (
            datetime.now(timezone.utc).strftime("campaign-%Y%m%dT%H%M%SZ")
        )

        fixed_order = list(FIXED_ORDER)

        target_ids = {
            item.get(
                "ecu_serial"
            )
            for item in downloaded_results
            if item.get(
                "ecu_serial"
            )
        }

        discovered = (
            SecondarySerial
            .discover_secondaries(
                status_sample_count=5,
                status_sample_interval=0.05,
            )
        )

        results = []
        schedule_result = {
            "requested_mode": "OFF",
            "used_mode": "OFF",
            "apply_mode": "ACTIVE",
            "fallback_reason": None,
            "recommended_order": [],
            "execution_order": [],
            "scores": {},
            "model_version": None,
            "model_hash": None,
            "profile_version": None,
            "profile_hash": None,
        }
        recommended_rank = {}
        execution_rank = {}

        def experiment_row(
            *,
            expected_ecu: str,
            status: Optional[dict],
            artifact_info: Optional[dict],
            decision: dict,
            features: dict,
        ) -> dict:
            observed_status = status or {}
            artifact = artifact_info or {}
            score = schedule_result["scores"].get(expected_ecu, {})

            return {
                "attempt_id": (
                    experiment_attempt_id
                    or f"{campaign_id}:{expected_ecu}"
                ),
                "scenario_id": scenario_id,
                "campaign_id": campaign_id,
                "secondary_id": expected_ecu,
                "data_source": data_source,
                "feature_schema_version": 2,
                "features": dict(features),
                "current_version": observed_status.get("version"),
                "target_version": artifact.get("target_version"),
                **features,
                "policy_decision": decision["decision"],
                "policy_reason_code": decision["reason_code"],
                "update_attempted": False,
                "success": None,
                "update_duration_ms": None,
                "transfer_duration_ms": None,
                "throughput_kbps": None,
                "retry_count": None,
                "slot_switched": None,
                "version_verified": None,
                "active_slot_before": observed_status.get("active_slot"),
                "active_slot_after": None,
                "failure_stage": None,
                "failure_reason_code": None,
                "ml_label_eligible": False,
                "ml_label": None,
                "uptime_ms": observed_status.get("uptime_ms"),
                "reset_cause": observed_status.get("reset_cause"),
                "boot_id": observed_status.get("boot_id"),
                "reset_context": observed_status.get("reset_context"),
                "uart_error_count": observed_status.get(
                    "uart_error_count"
                ),
                "health": observed_status.get("health"),
                "ai_mode_requested": schedule_result["requested_mode"],
                "ai_mode_used": schedule_result["used_mode"],
                "ai_apply_mode": schedule_result["apply_mode"],
                "ai_fallback_reason": schedule_result["fallback_reason"],
                "ai_risk_score": score.get("risk_score"),
                "ai_anomaly_score": score.get("anomaly_score"),
                "ai_feature_deviations": score.get("deviations"),
                "ai_feature_contributions": score.get("contributions"),
                "ai_recommended_rank": recommended_rank.get(expected_ecu),
                "ai_execution_rank": execution_rank.get(expected_ecu),
                "ai_profile_version": schedule_result["profile_version"],
                "ai_profile_hash": schedule_result["profile_hash"],
                "ai_model_version": schedule_result["model_version"],
                "ai_model_hash": schedule_result["model_hash"],
            }

        # Complete Safety Policy and feature collection for every target before
        # scheduling. HOLD/BLOCK boards are never exposed to the AI scheduler.
        preflight = {}
        allow_context = {}

        for expected_ecu in fixed_order:
            if (
                expected_ecu
                not in target_ids
            ):
                continue

            secondary_info = (
                discovered.get(
                    expected_ecu
                )
            )

            status = (
                secondary_info[
                    "status"
                ]
                if secondary_info
                is not None
                else None
            )

            ecu_artifacts = [
                item
                for item in (
                    downloaded_results
                )
                if (
                    item.get(
                        "ecu_serial"
                    )
                    == expected_ecu
                )
            ]

            artifact_info = next(
                (
                    item
                    for item in (
                        ecu_artifacts
                    )
                    if (
                        item.get(
                            "status"
                        )
                        == "OK"
                        and (
                            item.get(
                                "file_type"
                            )
                            == "bin"
                        )
                    )
                ),
                (
                    ecu_artifacts[0]
                    if ecu_artifacts
                    else None
                ),
            )

            registry_entry = (
                registry.get(
                    expected_ecu,
                    {},
                )
            )

            decision = evaluate_policy(
                expected_ecu=(
                    expected_ecu
                ),
                status=status,
                artifact_info=(
                    artifact_info
                ),
                expected_uid=(
                    registry_entry.get(
                        "uid"
                    )
                ),
                force_reinstall=force_reinstall,
            )

            try:
                features = collect_features(
                    secondary_id=expected_ecu,
                    status=status or {},
                    artifact_info=artifact_info or {},
                    power_percent=power_percent,
                    temperature_c=temperature_c,
                    log_path=str(logger.log_path),
                )
            except FeatureCollectionError as exc:
                print(
                    "[Feature Collector] WARNING: "
                    f"ECU={expected_ecu}: {exc}"
                )
                features = {
                    name: None
                    for name in FEATURE_NAMES
                }

            preflight[expected_ecu] = {
                "secondary_info": secondary_info,
                "status": status,
                "artifact_info": artifact_info,
                "registry_entry": registry_entry,
                "decision": decision,
                "features": features,
            }

            if decision["decision"] == "ALLOW":
                allow_context[expected_ecu] = {
                    "status": status,
                    "features": features,
                }

        ai_mode = os.environ.get("OTA_AI_MODE", "ACTIVE").upper()
        ai_apply_mode = os.environ.get("OTA_AI_APPLY_MODE")
        primary_ecu_dir = Path(__file__).resolve().parents[1]
        profile_path = os.environ.get(
            "OTA_NORMAL_PROFILE",
            str(primary_ecu_dir / "models" / "normal-profile-v1.json"),
        )
        model_path = os.environ.get(
            "OTA_AI_MODEL_PATH",
            str(primary_ecu_dir / "models" / "isolation-forest-v1.joblib"),
        )
        metadata_path = os.environ.get(
            "OTA_AI_METADATA_PATH",
            str(primary_ecu_dir / "models" / "isolation-forest-v1.metadata.json"),
        )
        schedule_result = schedule_allow_ecus(
            allow_context=allow_context,
            profile_path=profile_path,
            model_path=model_path,
            metadata_path=metadata_path,
            requested_mode=ai_mode,
            apply_mode=ai_apply_mode,
            fixed_order=fixed_order,
        )
        recommended_rank = {
            secondary_id: index
            for index, secondary_id in enumerate(
                schedule_result["recommended_order"],
                start=1,
            )
        }
        execution_rank = {
            secondary_id: index
            for index, secondary_id in enumerate(
                schedule_result["execution_order"],
                start=1,
            )
        }

        scheduled_allow = iter(schedule_result["execution_order"])
        execution_order = [
            (
                next(scheduled_allow)
                if preflight[secondary_id]["decision"]["decision"]
                == "ALLOW"
                else secondary_id
            )
            for secondary_id in fixed_order
            if secondary_id in preflight
        ]

        print(
            "[AI Scheduler] "
            f"requested={schedule_result['requested_mode']}, "
            f"used={schedule_result['used_mode']}, "
            f"apply={schedule_result['apply_mode']}, "
            f"recommended={schedule_result['recommended_order']}, "
            f"execution={schedule_result['execution_order']}, "
            f"fallback={schedule_result['fallback_reason']}"
        )

        for expected_ecu in execution_order:
            preflight_entry = preflight[expected_ecu]
            secondary_info = preflight_entry["secondary_info"]
            status = preflight_entry["status"]
            artifact_info = preflight_entry["artifact_info"]
            registry_entry = preflight_entry["registry_entry"]
            decision = preflight_entry["decision"]
            features = preflight_entry["features"]

            if (
                decision["decision"]
                != "ALLOW"
            ):
                self.secondary_states.transition(
                    expected_ecu,
                    decision[
                        "decision"
                    ],
                    artifact=(
                        artifact_info.get(
                            "artifact"
                        )
                        if artifact_info
                        else None
                    ),
                    firmware_sha256=(
                        artifact_info.get(
                            "sha256"
                        )
                        if artifact_info
                        else None
                    ),
                    ok=None,
                    reason=(
                        decision[
                            "reason_code"
                        ]
                    ),
                    details={
                        "policy": (
                            decision
                        ),
                        "status": (
                            status
                            or {}
                        ),
                    },
                )

                result = {
                    "ok": False,
                    "skipped": True,
                    "ecu_serial": (
                        expected_ecu
                    ),
                    "decision": (
                        decision[
                            "decision"
                        ]
                    ),
                    "reason_code": (
                        decision[
                            "reason_code"
                        ]
                    ),
                    "update_attempted": (
                        False
                    ),
                    "success": None,
                    "ai_mode_used": schedule_result["used_mode"],
                    "ai_risk_score": None,
                    "ai_recommended_rank": None,
                    "ai_execution_rank": None,
                }

                results.append(
                    result
                )

                logger.append(
                    experiment_row(
                        expected_ecu=expected_ecu,
                        status=status,
                        artifact_info=artifact_info,
                        decision=decision,
                        features=features,
                    )
                )

                continue

            port = (
                secondary_info[
                    "port"
                ]
            )

            self.secondary_states.transition(
                expected_ecu,
                "READY",
                artifact=(
                    artifact_info.get(
                        "artifact"
                    )
                    if artifact_info
                    else None
                ),
                firmware_sha256=(
                    artifact_info.get(
                        "sha256"
                    )
                    if artifact_info
                    else None
                ),
                ok=None,
                details={
                    "port": port,
                    "active_slot": (
                        status.get(
                            "active_slot"
                        )
                    ),
                    "target_slot": (
                        status.get(
                            "target_slot"
                        )
                    ),
                    "ready": (
                        status.get(
                            "ready"
                        )
                    ),
                    "policy": (
                        decision
                    ),
                },
            )

            print(
                "[Primary ECU] "
                f"{schedule_result['used_mode']}-order update: "
                f"ECU={expected_ecu}, "
                f"PORT={port}, "
                f"RISK="
                f"{schedule_result['scores'].get(expected_ecu, {}).get('risk_score')}, "
                f"RANK={execution_rank.get(expected_ecu)}"
            )

            started_at = (
                time.monotonic()
            )

            result = (
                self._install_one_serial_firmware(
                    downloaded_results=(
                        downloaded_results
                    ),
                    expected_ecu=(
                        expected_ecu
                    ),
                    port=port,
                    expected_secondary_status=(
                        expected_secondary_statuses
                        .get(
                            expected_ecu
                        )
                    ),
                    expected_uid=(
                        registry_entry.get("uid")
                    ),
                    policy_artifact_info=(
                        artifact_info
                    ),
                    force_reinstall=force_reinstall,
                )
            )

            update_duration_ms = int(
                (
                    time.monotonic()
                    - started_at
                )
                * 1000
            )

            effective_decision = (
                result.get("policy_recheck")
                or decision
            )
            update_attempted = (
                result.get("update_attempted")
                is True
            )
            success = (
                bool(result.get("ok"))
                if update_attempted
                else None
            )

            result["decision"] = effective_decision["decision"]
            result["reason_code"] = effective_decision["reason_code"]
            result["update_attempted"] = update_attempted
            result["success"] = success

            result[
                "update_duration_ms"
            ] = (
                update_duration_ms
                if update_attempted
                else None
            )

            score = schedule_result["scores"].get(expected_ecu, {})
            result["ai_mode_used"] = schedule_result["used_mode"]
            result["ai_risk_score"] = score.get("risk_score")
            result["ai_recommended_rank"] = recommended_rank.get(
                expected_ecu
            )
            result["ai_execution_rank"] = execution_rank.get(expected_ecu)

            results.append(
                result
            )

            before_status = (
                result.get(
                    "secondary_before"
                )
                or status
                or {}
            )

            after_status = (
                result.get(
                    "secondary_after"
                )
                or {}
            )

            log_row = experiment_row(
                expected_ecu=expected_ecu,
                status=before_status,
                artifact_info=artifact_info,
                decision=effective_decision,
                features=features,
            )
            failure_reason_code = result.get(
                "failure_reason_code"
            )
            ml_label_eligible, ml_label = label_for_outcome(
                update_attempted=update_attempted,
                success=success,
                reason_code=failure_reason_code,
            )
            log_row.update({
                "policy_preflight_decision": decision["decision"],
                "policy_preflight_reason_code": decision["reason_code"],
                "policy_rechecked": result.get("policy_recheck") is not None,
                "update_attempted": update_attempted,
                "success": success,
                "update_duration_ms": (
                    update_duration_ms
                    if update_attempted
                    else None
                ),
                "transfer_duration_ms": result.get(
                    "transfer_duration_ms"
                ),
                "throughput_kbps": result.get("throughput_kbps"),
                "retry_count": result.get("retry_count", 0),
                "slot_switched": result.get("slot_switched"),
                "version_verified": result.get("version_verified"),
                "active_slot_after": after_status.get("active_slot"),
                "failure_stage": result.get("failure_stage"),
                "failure_reason_code": failure_reason_code,
                "failure_detail": (
                    None if success is not False else result.get("reason")
                ),
                "ml_label_eligible": ml_label_eligible,
                "ml_label": ml_label,
            })
            logger.append(log_row)

        attempted_results = [
            result
            for result in results
            if result.get(
                "update_attempted"
            )
        ]

        return {
            "ok": (
                bool(
                    attempted_results
                )
                and all(
                    result.get(
                        "ok",
                        False,
                    )
                    for result in (
                        attempted_results
                    )
                )
            ),
            "skipped": (
                not bool(
                    attempted_results
                )
            ),
            "results": results,
        }

    def download_config_to_secondary(
        self,
        update_images: List,
        base_url: str,
    ) -> dict:
        from .secondary_serial import (
            SecondarySerial,
        )

        os.makedirs(
            "./downloads/"
            "config_storage",
            exist_ok=True,
        )

        results = []

        secondary = (
            SecondarySerial()
        )

        try:
            for item in update_images:
                ecu_serial = (
                    item["ecu"]
                )

                image_name = (
                    item["images"][
                        "image_name"
                    ]
                )

                image_info = (
                    item["images"][
                        "image_info"
                    ]
                )

                expected_sha256 = (
                    image_info[
                        "hashes"
                    ][
                        "sha256"
                    ]
                )

                expected_sha512 = (
                    image_info[
                        "hashes"
                    ][
                        "sha512"
                    ]
                )

                expected_length = (
                    image_info.get(
                        "length"
                    )
                )

                filename = (
                    f"{image_name}.cfg"
                )

                cfg_url = (
                    f"{base_url}/"
                    f"images/{filename}"
                )

                out_path = (
                    "./downloads/"
                    "config_storage/"
                    f"{filename}"
                )

                print(
                    "[Primary ECU] "
                    f"GET CONFIG: "
                    f"{cfg_url}"
                )

                with requests.get(
                    cfg_url,
                    stream=True,
                    verify=False,
                    timeout=10,
                ) as response:
                    response.raise_for_status()

                    with open(
                        out_path,
                        "wb",
                    ) as file:
                        for chunk in (
                            response.iter_content(
                                chunk_size=8192
                            )
                        ):
                            if chunk:
                                file.write(
                                    chunk
                                )

                actual_length = (
                    os.path.getsize(
                        out_path
                    )
                )

                actual_sha256 = (
                    self._sha256_file(
                        out_path
                    )
                )

                actual_sha512 = (
                    self._sha512_file(
                        out_path
                    )
                )

                if (
                    expected_length
                    is not None
                    and (
                        actual_length
                        != expected_length
                    )
                ):
                    raise RuntimeError(
                        "length mismatch: "
                        f"expected="
                        f"{expected_length}, "
                        f"actual="
                        f"{actual_length}"
                    )

                if (
                    actual_sha256
                    != expected_sha256
                ):
                    raise RuntimeError(
                        "sha256 mismatch: "
                        f"expected="
                        f"{expected_sha256}, "
                        f"actual="
                        f"{actual_sha256}"
                    )

                if (
                    actual_sha512
                    != expected_sha512
                ):
                    raise RuntimeError(
                        "sha512 mismatch: "
                        f"expected="
                        f"{expected_sha512}, "
                        f"actual="
                        f"{actual_sha512}"
                    )

                cfg_text = (
                    Path(
                        out_path
                    ).read_text(
                        encoding="utf-8"
                    )
                )

                cfg = (
                    self._parse_cfg_text(
                        cfg_text
                    )
                )

                target_ecu = (
                    cfg.get(
                        "TARGET_ECU"
                    )
                )

                version = (
                    cfg.get(
                        "VERSION"
                    )
                )

                led_mode = (
                    cfg.get(
                        "LED_MODE"
                    )
                )

                if (
                    target_ecu
                    != ecu_serial
                ):
                    raise RuntimeError(
                        "TARGET_ECU mismatch: "
                        f"metadata="
                        f"{ecu_serial}, "
                        f"cfg="
                        f"{target_ecu}"
                    )

                if led_mode not in (
                    "ON",
                    "OFF",
                    "BLINK",
                ):
                    raise RuntimeError(
                        "invalid LED_MODE: "
                        f"{led_mode}"
                    )

                print(
                    "[Primary ECU] "
                    "Send config to Secondary"
                )

                result_resp = (
                    secondary.send_config(
                        cfg_text
                    )
                )

                print(
                    "[Secondary]\n"
                    f"{result_resp}"
                )

                status_resp = (
                    secondary.get_status()
                )

                print(
                    "[Secondary STATUS]\n"
                    f"{status_resp}"
                )

                ok = (
                    "RESULT OK"
                    in result_resp
                )

                results.append({
                    "ecu_serial": (
                        ecu_serial
                    ),
                    "target_version": (
                        version
                    ),
                    "artifact": (
                        filename
                    ),
                    "led_mode": (
                        led_mode
                    ),
                    "status": (
                        "OK"
                        if ok
                        else "FAIL"
                    ),
                    "secondary_result": (
                        result_resp
                    ),
                    "secondary_status": (
                        status_resp
                    ),
                })

        except Exception as exc:
            print(
                "[FAIL] secondary "
                "config update failed: "
                f"{exc}"
            )

            results.append({
                "status": "FAIL",
                "reason": str(exc),
            })

        finally:
            secondary.close()

        return {
            "ok": all(
                result.get(
                    "status"
                )
                == "OK"
                for result in results
            ),
            "results": results,
        }
