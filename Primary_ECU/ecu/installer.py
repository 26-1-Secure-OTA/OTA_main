from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from importlib.resources import path
from importlib.resources import path
from typing import Optional, Union, Dict, Any, List
from urllib.parse import urljoin
from pathlib import Path
import os, shutil, json, subprocess
import requests
import hashlib
import re
import time
#from utils.fastcdc_chunking import join_all_by_manifest
#from utils.fastcdc_chunking import load_image_from_oci
#from utils.fastcdc_chunking import load_image_from_tar
#from utils.fastcdc_chunking import run_container
from make_vvm import load_or_create_ed25519_private_key, calc_ed25519_keyid_from_public_key, sign_block_ed25519
#from utils.metrics import measure
from .storage import Storage

from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SYSTEM = os.environ.get("SYSTEM_NAME", "")
TC = os.environ.get("TEST_CASE", "")

_HEX64_RE = re.compile(r"(?i)\b[a-f0-9]{64}\b")

def _expected_sha256_from_chunk_name(s: str) -> str:
    m = _HEX64_RE.search(s)
    if not m:
        raise ValueError(f"cannot extract sha256 from chunk name: {s}")
    return m.group(0).lower()


@dataclass
class InstallResult:
    ok: bool
    reason: Optional[str] = None

class Installer:
    def __init__(self, storage: Storage):
        self.storage = storage
    

    def load_image_from_tar(self, tar_path: str) -> str:
        result = subprocess.run(
            ["podman", "load", "-i", tar_path],
            check=True,
            capture_output=True,
            text=True,
        )

        output = (result.stdout or "") + "\n" + (result.stderr or "")
        print(output.strip())

        # 예: "Loaded image(s): localhost/ivi:2.0.0"
        m = re.search(r"Loaded image(?:\(s\))?:\s*(.+)", output)
        if not m:
            raise RuntimeError(f"podman load output에서 이미지 이름을 찾지 못했습니다: {output}")

        image_ref = m.group(1).strip()
        return image_ref


    def run_container(self, image_ref: str, container_name: str = "ivi-test") -> None:
        # 기존 컨테이너 있으면 삭제
        subprocess.run(["podman", "rm", "-f", container_name], check=False)

        # 일단 백그라운드 실행
        subprocess.run(
            ["podman", "run", "-d", "--name", container_name, image_ref],
            check=True,
        )

        print(f"[Primary ECU] Container started: {container_name} ({image_ref})")

        

    def _make_session(self) -> requests.Session:
        s = requests.Session()
        retries = Retry(
            total=3,
            backoff_factor=0.3,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(
            max_retries=retries,
            pool_connections=32,
            pool_maxsize=32,
        )
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        return s
    

    def _download_one_chunk(self, session: requests.Session, url: str, out_path: str) -> None:
        # 이미 있으면 스킵(캐시) - 기존 파일 해시 검증은 안 함
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return

        tmp_path = out_path + ".part"
        os.makedirs(os.path.dirname(out_path), exist_ok=True)

        # expected sha256: 청크 이름(또는 경로)에서 64hex 추출
        # (out_path에 chunk_name이 포함되므로 여기서 뽑는 게 안전)
        chunk_file_name = _expected_sha256_from_chunk_name(os.path.basename(out_path))


        h = hashlib.sha256()

        try:
            with session.get(url, stream=True, verify=False, timeout=(5, 120)) as r:
                r.raise_for_status()
                with open(tmp_path, "wb") as f:
                    for data in r.iter_content(chunk_size=1024 * 1024):
                        if not data:
                            continue
                        h.update(data)   # ✅ 다운로드 중 해시 업데이트
                        f.write(data)

            got = h.hexdigest()
            if got != chunk_file_name:
                # ✅ mismatch면 파일 확정 금지 + 임시 파일 삭제
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                raise RuntimeError(f"chunk hash mismatch: chunk_file_name={chunk_file_name} got={got} url={url}")

            # ✅ 검증 통과 시에만 최종 파일로 확정
            os.replace(tmp_path, out_path)

        except Exception:
            # ✅ 실패 시 .part가 남지 않게 정리
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            raise


    def build_image_manifest_url(self, base_url, ecu, image_name):
        filename = f"{image_name}.json"
        return f"{base_url}/meta/targets/{ecu}/{ecu}_image/{filename}"
    
    def build_image_chunk_url(self, base_url, chunk_name):
        return f"{base_url}/chunks/{chunk_name}"


    def convert_oci_dir_to_tar(self, oci_dir: str, out_tar: str):
        out_tar = str(out_tar)   # 추가
        image_ref = str(load_image_from_oci(oci_dir))

        subprocess.run(["podman", "save", "--format", "docker-archive", "-o", out_tar, image_ref], check=True)
        subprocess.run(["podman", "rmi", "-f", image_ref], check=False)

    # Static verification pipeline 실행
    def run_static_verification(self, archive_path: str):
        env = os.environ.copy()
        env["ARCHIVE"] = archive_path

        static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
        runall = os.path.join(static_dir, "run_all.sh")

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path("./downloads/static_out") / f"{Path(archive_path).stem}_{ts}"
        out_dir.mkdir(parents=True, exist_ok=True)

        print(f"[Primary ECU] Static Verification Start  ->  {archive_path}")
        print(f"[Primary ECU] Static Out Dir            ->  {out_dir}")

        result = subprocess.run(
            ["bash", runall, archive_path, str(out_dir)],
            env=env,
            capture_output=True,
            text=True
        )

        if result.stdout.strip():
            print(result.stdout)
        
        if result.stderr.strip():
            print(result.stderr)

        policy_log = out_dir / "policy.log"
        if policy_log.exists():
            print(policy_log.read_text(encoding="utf-8"))

        if result.returncode != 0:
            raise RuntimeError("Static verification FAILED for: " + archive_path)

        print("[Primary ECU] Static Verification PASSED")

    # Chunk 다운로드 및 재조립
    def download_chunk(self, update_images: List, base_url: str):
        updated_ecu_versions = []
        for t in update_images:
            with measure("Download Chunks", system_name=SYSTEM, test_case=TC):
                chunk_list = t["images"]["required_chunks"]

                seen = set()
                uniq = []
                for c in chunk_list:
                    if c not in seen:
                        seen.add(c)
                        uniq.append(c)
                chunk_list = uniq

                session = self._make_session()

                max_workers = int(os.environ.get("CHUNKS_DL_WORKERS", "8"))

                futures = []
                with ThreadPoolExecutor(max_workers=max_workers) as ex:
                    for c in chunk_list:
                        url = self.build_image_chunk_url(base_url, c)
                        out_path = f"./downloads/chunk_storage/{c}"
                        futures.append(ex.submit(self._download_one_chunk, session, url, out_path))

                    # 에러를 여기서 모아서 한번에 터뜨리기
                    for i, fut in enumerate(as_completed(futures), 1):
                        exc = fut.exception()
                        if exc is not None:
                            raise RuntimeError(f"chunk download failed: {exc}") from exc

                        # 진행률 로그(원하시면)
                        if i % 50 == 0 or i == len(futures):
                            print(f"[Primary ECU] chunk 다운로드 및 해시 무결성 검사 진행 {i}/{len(futures)} chunks (workers={max_workers})")

            # 재조립
            with measure("Reassemble chunks", system_name=SYSTEM, test_case=TC):
                image_name = str(t["images"]["image_name"])
                downloads = Path("./downloads")
                
                manifest_path = f"./downloads/{image_name}.json"
                oci_dir = f"./downloads/{image_name}"

                metrics, tt = join_all_by_manifest(
                    manifest_path,
                    oci_dir,
                    "./downloads/chunk_storage"
                )
                print(f"[Primary ECU] Reassembled (OCI-DIR): {oci_dir}")

            # OCI DIR → OCI TAR 변환 (정적 검증 입력)
            archive_path = f"./downloads/{image_name}.tar"

            with measure("", system_name=SYSTEM, test_case=TC):
                self.convert_oci_dir_to_tar(oci_dir, archive_path)
                print(f"[Primary ECU] Packed OCI layout into archive: {archive_path}")

            # VVM Update
            with open("vvm.json", "r", encoding="utf-8") as f:
                vvm = json.load(f)

            for ecu in vvm["signed"]["ecu_version"]:
                if ecu.get("ecu_serial") == t["ecu"]:
                    ecu["target_image"]["filename"] = f"{image_name}.tar"
                    ecu["target_image"]["fileinfo"]["hashes"]["sha256"] = \
                        t["images"]["image_info"]["hashes"]["sha256"]
                    ecu["target_image"]["fileinfo"]["hashes"]["sha512"] = \
                        t["images"]["image_info"]["hashes"]["sha512"]
                    updated_ecu_versions.append(ecu)
                    break

            # Static Verification
            self.run_static_verification(str(Path(archive_path).resolve()))

            # 정적 통과 후, VVM 저장 
            with open("vvm.json", "w", encoding="utf-8") as f:
                json.dump(vvm, f, indent=2)
            print("[Primary ECU] Update VVM information")

            # 동적 실행
            # subprocess.run(["podman", "load", "-i", archive_path], check=True)
            # run_container()

        return updated_ecu_versions

    # Manifest 다운로드 -> 컨테이너 재조립을 위한 chunk 목록
    def download_manifest(self, update_images:List, base_url:str):
        
        for t in update_images:
            ecu = t["ecu"]
            image_name = t["images"]["image_name"]

            url = self.build_image_manifest_url(base_url, ecu, image_name)
            print(f"[Primary ECU] GET:  {url}")

            resp = requests.get(url, verify=False)
            if resp.status_code != 200:
                # 상황에 따라 raise / continue 등 정책 선택
                raise RuntimeError(f"Failed to fetch {url}: {resp.status_code}")

            image_meta = resp.json()
            save_path = Path(f"./downloads/{image_name}.json")
            save_path.write_text(json.dumps(image_meta, indent=2, ensure_ascii=False), encoding="utf-8")

            # 이미지 재활용 여부 확인
            installed_list_path = "./meta/installed_layers.json"
            if not os.path.exists(installed_list_path):
                installed = {"layers": []}
            else:
                with open(installed_list_path, 'r') as f:
                    installed = json.load(f)

            installed_hashes = set(installed.get("layers", []))

            chunks = image_meta["signed"]["chunks"]

            target_hashes = set()
            for key in chunks.keys():
                if key.startswith("blobs/sha256/"):
                    sha = key.split("blobs/sha256/")[1]
                    target_hashes.add(sha)

            new_layers = [h for h in target_hashes if h not in installed_hashes]

            if new_layers:
                installed_hashes.update(new_layers)

                # with open(installed_list_path, 'w') as f:
                #     json.dump({"layers": list(installed_hashes)}, f, indent=2)

                return list(installed_hashes)
            
            return []

    def download_image(self, update_images:List, base_url:str):
        #with measure("Download Images", system_name=SYSTEM, test_case=TC):
        for image in update_images:
            image_name = image["images"]["image_name"]
            image_path = f"{base_url}/images/{image_name}.tar"
            out_path = f"./downloads/image_storage/{image_name}.tar"

            print(f"[Primary ECU] GET:      {image_path}")

            try:
                with requests.get(image_path, stream=True, verify=False) as response:
                    response.raise_for_status()
                    with open(out_path, "wb") as f:
                        for image in response.iter_content(chunk_size=8192):
                            if image:
                                f.write(image)
                print(f"[OK] saved image -> {out_path}")
            except Exception as e:
                print(f"[FAIL] failed to download chunk {image_name} from {image_path}: {e}")

        #with measure("Build container image", system_name=SYSTEM, test_case=TC):
            image_ref = self.load_image_from_tar(out_path)
            self.run_container(image_ref, container_name=f"{image_name}-ctr")

            version = image_name.split('_')[1]
            major, minor, patch = map(int, version.split('.'))
            if major == 3:
                major = 0
            else:
                major -= 1
            version = f"{major}.{minor}.{patch}"
        # run_container(version)

    def update_info(self, layer_list, vvm_version):
        # 설치된 레이어 리스트 업데이트
        with open("./meta/installed_layers.json", 'w') as f:
            json.dump({"layers": layer_list}, f, indent=2)

        # VVM 업데이트
        ED25519_PRIVATE_KEY_PATH = Path("ed25519_private_key.pem")
        vin = "VIN-TEST-0001"
        primary_ecu_serial = "primary0"
        ed_private_key = load_or_create_ed25519_private_key(ED25519_PRIVATE_KEY_PATH)

        vvm_keyid = calc_ed25519_keyid_from_public_key(ed_private_key)

        now = datetime.now(timezone.utc)
        expires_str = (
            now + timedelta(days=365)
        ).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")

        signed = {
            "vin": vin,
            "primary_ecu_serial": primary_ecu_serial,
            "expires": expires_str,
            "ecu_version": vvm_version
        }

        signed["keyid"] = vvm_keyid
        sig = sign_block_ed25519(ed_private_key, signed)

        vvm_obj = {
            "signatures": [
                {
                    "keyid": vvm_keyid,
                    "sig": sig,
                }
            ],
            "signed": signed,
        }

        VVM_JSON_PATH = Path("vvm.json")

        VVM_JSON_PATH.write_text(
            json.dumps(vvm_obj, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"vvm.json 업데이트 완료: {VVM_JSON_PATH}")


    
    def install(self, image_path: str, version: str) -> InstallResult:
        try:
            staging = self.storage.staging_dir(version)
            os.makedirs(staging, exist_ok=True)
            shutil.copy2(image_path, os.path.join(staging, os.path.basename(image_path)))

            active = self.storage.active_symlink()
            if os.path.islink(active): os.unlink(active)
            os.symlink(staging, active)

            self.storage.write_version(version)
            return InstallResult(ok=True)
        except Exception as e:
            return InstallResult(ok=False, reason=str(e))

    def rollback(self):
        prev = self.storage.last_good_version()
        if not prev:
            raise RuntimeError("no previous version to rollback")
        active = self.storage.active_symlink()
        if os.path.islink(active): os.unlink(active)
        os.symlink(self.storage.staging_dir(prev), active)
    
    def _sha256_file(self, path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                if chunk:
                    h.update(chunk)
        return h.hexdigest()


    def _sha512_file(self, path: str) -> str:
        h = hashlib.sha512()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                if chunk:
                    h.update(chunk)
        return h.hexdigest()


    def _parse_cfg_text(self, cfg_text: str) -> dict:
        cfg = {}

        for line in cfg_text.splitlines():
            line = line.strip()

            if not line or line.startswith("#"):
                continue

            if "=" not in line:
                continue

            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()

        return cfg


    @staticmethod
    def _target_slot_from_update(item: dict) -> Optional[str]:
        """Read and cross-check the slot encoded in metadata and target name."""
        images = item.get("images") or {}
        image_name = str(images.get("image_name") or "")
        image_info = images.get("image_info") or {}
        metadata_slot = (image_info.get("custom") or {}).get("target_slot")
        if metadata_slot is not None:
            metadata_slot = str(metadata_slot).upper()
            if metadata_slot not in ("A", "B"):
                raise RuntimeError(
                    f"invalid target_slot metadata for {image_name}: "
                    f"{metadata_slot}"
                )

        match = re.search(r"_slot[-_]([ab])$", image_name, re.IGNORECASE)
        name_slot = match.group(1).upper() if match else None
        if metadata_slot != name_slot:
            raise RuntimeError(
                f"slot metadata/name mismatch for {image_name}: "
                f"metadata={metadata_slot}, name={name_slot}"
            )
        return metadata_slot


    def select_updates_for_secondary(self, update_images: List) -> dict:
        """Choose only the firmware for the Secondary's inactive slot.

        Non-slot artifacts are preserved. If slot-specific artifacts for the
        connected Secondary are present, its STATUS is queried before any
        image download and exactly one matching target is retained.
        """
        from .secondary_serial import SecondarySerial

        parsed = [
            (item, self._target_slot_from_update(item))
            for item in update_images
        ]
        if not any(slot is not None for _, slot in parsed):
            return {
                "updates": list(update_images),
                "secondary_status": None,
            }

        with SecondarySerial() as secondary:
            status = secondary.get_status()

        if not status["ready"]:
            raise RuntimeError(f"Secondary is not ready: {status['raw']}")

        selected = []
        relevant = []
        matches = []
        for item, target_slot in parsed:
            if target_slot is None:
                selected.append(item)
                continue

            if item.get("ecu") != status["ecu_serial"]:
                # This Primary controls one serial Secondary. Do not download
                # firmware intended for another Secondary ECU.
                continue

            relevant.append(item)
            if target_slot == status["target_slot"]:
                selected.append(item)
                matches.append(item)

        if relevant and len(matches) != 1:
            raise RuntimeError(
                "expected exactly one firmware target for inactive slot "
                f"{status['target_slot']}, found={len(matches)}"
            )

        print(
            "[Primary ECU] Secondary selected before download: "
            f"ECU={status['ecu_serial']}, ACTIVE={status['active_slot']}, "
            f"TARGET={status['target_slot']}"
        )
        return {
            "updates": selected,
            "secondary_status": status,
        }


    def download_artifacts(self, update_images: List, base_url: str) -> dict:
        """Download and verify update artifacts for every ECU.

        Target names do not include an extension. Try each supported extension
        and keep only the file that matches the signed length and hashes.
        Installation is deliberately left to each ECU's installer.
        """
        storage_dir = Path("./downloads/artifact_storage")
        storage_dir.mkdir(parents=True, exist_ok=True)
        supported_extensions = (".bin", ".tar", ".cfg")

        results = []

        for item in update_images:
            part_path = None

            try:
                ecu_serial = str(item["ecu"])
                image_name = str(item["images"]["image_name"])
                image_info = item["images"]["image_info"]
                expected_target_slot = self._target_slot_from_update(item)

                if Path(ecu_serial).name != ecu_serial:
                    raise RuntimeError(f"invalid ECU name: {ecu_serial}")

                if Path(image_name).name != image_name:
                    raise RuntimeError(f"invalid image name: {image_name}")

                hashes = image_info.get("hashes") or {}
                expected_sha256 = hashes.get("sha256")
                expected_sha512 = hashes.get("sha512")
                expected_length = image_info.get("length")

                if not expected_sha256 or not expected_sha512 or expected_length is None:
                    raise RuntimeError(
                        "artifact metadata is missing length, SHA-256, or SHA-512"
                    )

                selected = None
                attempts = []

                for extension in supported_extensions:
                    filename = f"{image_name}{extension}"
                    artifact_url = urljoin(
                        base_url.rstrip("/") + "/",
                        f"images/{filename}",
                    )
                    out_path = storage_dir / filename
                    part_path = storage_dir / f"{filename}.part"

                    print(
                        f"[Primary ECU] TRY ARTIFACT ({ecu_serial}): "
                        f"{artifact_url}"
                    )

                    with requests.get(
                        artifact_url,
                        stream=True,
                        verify=False,
                        timeout=30,
                    ) as response:
                        if getattr(response, "status_code", None) == 404:
                            attempts.append(f"{filename}: not found")
                            continue

                        response.raise_for_status()

                        with part_path.open("wb") as artifact_file:
                            for data in response.iter_content(chunk_size=8192):
                                if data:
                                    artifact_file.write(data)

                    actual_length = part_path.stat().st_size
                    actual_sha256 = self._sha256_file(str(part_path))
                    actual_sha512 = self._sha512_file(str(part_path))

                    matches_metadata = (
                        actual_length == int(expected_length)
                        and actual_sha256.lower() == str(expected_sha256).lower()
                        and actual_sha512.lower() == str(expected_sha512).lower()
                    )

                    if not matches_metadata:
                        attempts.append(f"{filename}: metadata mismatch")
                        if part_path.exists():
                            part_path.unlink()
                        part_path = None
                        continue

                    if expected_target_slot is not None:
                        if extension != ".bin":
                            raise RuntimeError(
                                "slot-specific target resolved to a non-bin "
                                f"artifact: {filename}"
                            )
                        from .secondary_serial import SecondarySerial
                        linked_slot = SecondarySerial.detect_firmware_slot(
                            str(part_path)
                        )
                        if linked_slot != expected_target_slot:
                            raise RuntimeError(
                                f"firmware slot mismatch for {filename}: "
                                f"metadata={expected_target_slot}, "
                                f"linked={linked_slot}"
                            )

                    os.replace(part_path, out_path)
                    part_path = None
                    selected = {
                        "filename": filename,
                        "file_type": extension.lstrip("."),
                        "out_path": out_path,
                        "length": actual_length,
                        "sha256": actual_sha256,
                        "target_slot": expected_target_slot,
                    }
                    break

                if selected is None:
                    raise RuntimeError(
                        "no .bin, .tar, or .cfg artifact matched the signed metadata: "
                        + "; ".join(attempts)
                    )

                results.append({
                    "ecu_serial": ecu_serial,
                    "artifact": selected["filename"],
                    "path": str(selected["out_path"]),
                    "file_type": selected["file_type"],
                    "length": selected["length"],
                    "sha256": selected["sha256"],
                    "target_slot": selected["target_slot"],
                    "status": "OK",
                })
                print(
                    f"[Primary ECU] Artifact verified and saved: "
                    f"{selected['out_path']}"
                )

            except Exception as e:
                print(f"[FAIL] artifact download failed: {e}")
                results.append({
                    "status": "FAIL",
                    "reason": str(e),
                })

            finally:
                if part_path is not None and part_path.exists():
                    part_path.unlink()

        return {
            "ok": bool(results) and all(r.get("status") == "OK" for r in results),
            "results": results,
        }


    def download_firmware(self, update_images: List, base_url: str) -> dict:
        """Backward-compatible wrapper for the former STM32-only entry point."""
        return self.download_artifacts(update_images, base_url)


    def install_serial_firmware(
        self,
        downloaded_results: List,
        expected_secondary_status: Optional[dict] = None,
    ) -> dict:
        """Install the matching .bin on the currently connected Secondary.

        All ECU artifacts remain downloadable. At installation time the
        Secondary reports its own ECU ID and inactive target slot. The Primary
        then selects exactly one verified image matching both values.
        """
        from .secondary_serial import SecondarySerial

        verified_bins = [
            item
            for item in downloaded_results
            if item.get("status") == "OK" and item.get("file_type") == "bin"
        ]

        if not verified_bins:
            return {
                "ok": True,
                "skipped": True,
                "reason": "no verified .bin artifact",
            }

        try:
            with SecondarySerial() as secondary:
                before_status = secondary.get_status()

                if not before_status["ready"]:
                    raise RuntimeError(
                        f"Secondary is not ready: {before_status['raw']}"
                    )

                if expected_secondary_status is not None:
                    identity_fields = ("ecu_serial", "active_slot", "target_slot")
                    changed = [
                        field
                        for field in identity_fields
                        if before_status[field]
                        != expected_secondary_status.get(field)
                    ]
                    if changed:
                        raise RuntimeError(
                            "Secondary slot state changed during download: "
                            f"fields={changed}, "
                            f"before={expected_secondary_status['raw']}, "
                            f"now={before_status['raw']}"
                        )

                same_ecu = [
                    item
                    for item in verified_bins
                    if item.get("ecu_serial") == before_status["ecu_serial"]
                ]

                if not same_ecu:
                    return {
                        "ok": True,
                        "skipped": True,
                        "reason": (
                            "no .bin artifact for connected ECU "
                            f"{before_status['ecu_serial']}"
                        ),
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

                    if declared_slot is not None and declared_slot != linked_slot:
                        raise RuntimeError(
                            "downloaded firmware declaration/link mismatch: "
                            f"{inspected[-1]}"
                        )

                    if linked_slot == before_status["target_slot"]:
                        candidates.append(item)

                if not candidates:
                    raise RuntimeError(
                        "no firmware image matches the inactive target slot "
                        f"{before_status['target_slot']}; inspected={inspected}"
                    )

                if len(candidates) != 1:
                    raise RuntimeError(
                        "multiple firmware images match connected ECU and "
                        f"target slot {before_status['target_slot']}: "
                        f"{[item.get('artifact') for item in candidates]}"
                    )

                target = candidates[0]

                print(
                    "[Primary ECU] Install Serial firmware: "
                    f"ECU={before_status['ecu_serial']}, "
                    f"ACTIVE={before_status['active_slot']}, "
                    f"TARGET={before_status['target_slot']}, "
                    f"ARTIFACT={target['artifact']}"
                )

                transfer_result = secondary.send_firmware(
                    firmware_path=target["path"],
                    expected_sha256=target["sha256"],
                    expected_target_slot=before_status["target_slot"],
                    max_firmware_size=before_status["max_size"],
                )

                # The application sends FW_OK, waits briefly, resets, and then
                # the bootloader starts the newly selected slot.
                reboot_delay = float(
                    os.environ.get("STM32_REBOOT_DELAY", "2.0")
                )
                if reboot_delay > 0:
                    time.sleep(reboot_delay)

                after_status = secondary.get_status(timeout_seconds=10.0)

                if after_status["ecu_serial"] != before_status["ecu_serial"]:
                    raise RuntimeError(
                        "Secondary ECU ID changed after update: "
                        f"before={before_status['ecu_serial']}, "
                        f"after={after_status['ecu_serial']}"
                    )

                if after_status["active_slot"] != before_status["target_slot"]:
                    raise RuntimeError(
                        "firmware transfer finished but target slot did not "
                        f"become active: expected={before_status['target_slot']}, "
                        f"actual={after_status['active_slot']}"
                    )

                return {
                    "ok": True,
                    "skipped": False,
                    "ecu_serial": before_status["ecu_serial"],
                    "artifact": target["artifact"],
                    "secondary_before": before_status,
                    "transfer": transfer_result,
                    "secondary_after": after_status,
                }

        except Exception as e:
            print(f"[FAIL] Serial firmware installation failed: {e}")
            return {
                "ok": False,
                "skipped": False,
                "reason": str(e),
            }


    def download_config_to_secondary(self, update_images: List, base_url: str) -> dict:
        from .secondary_serial import SecondarySerial

        os.makedirs("./downloads/config_storage", exist_ok=True)

        results = []

        secondary = SecondarySerial()

        try:
            for item in update_images:
                ecu_serial = item["ecu"]
                image_name = item["images"]["image_name"]
                image_info = item["images"]["image_info"]

                expected_sha256 = image_info["hashes"]["sha256"]
                expected_sha512 = image_info["hashes"]["sha512"]
                expected_length = image_info.get("length")

                filename = f"{image_name}.cfg"
                cfg_url = f"{base_url}/images/{filename}"
                out_path = f"./downloads/config_storage/{filename}"

                print(f"[Primary ECU] GET CONFIG: {cfg_url}")

                with requests.get(cfg_url, stream=True, verify=False, timeout=10) as response:
                    response.raise_for_status()
                    with open(out_path, "wb") as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)

                actual_length = os.path.getsize(out_path)
                actual_sha256 = self._sha256_file(out_path)
                actual_sha512 = self._sha512_file(out_path)

                if expected_length is not None and actual_length != expected_length:
                    raise RuntimeError(
                        f"length mismatch: expected={expected_length}, actual={actual_length}"
                    )

                if actual_sha256 != expected_sha256:
                    raise RuntimeError(
                        f"sha256 mismatch: expected={expected_sha256}, actual={actual_sha256}"
                    )

                if actual_sha512 != expected_sha512:
                    raise RuntimeError(
                        f"sha512 mismatch: expected={expected_sha512}, actual={actual_sha512}"
                    )

                cfg_text = Path(out_path).read_text(encoding="utf-8")
                cfg = self._parse_cfg_text(cfg_text)

                target_ecu = cfg.get("TARGET_ECU")
                version = cfg.get("VERSION")
                led_mode = cfg.get("LED_MODE")

                if target_ecu != ecu_serial:
                    raise RuntimeError(
                        f"TARGET_ECU mismatch: metadata={ecu_serial}, cfg={target_ecu}"
                    )

                if led_mode not in ("ON", "OFF", "BLINK"):
                    raise RuntimeError(f"invalid LED_MODE: {led_mode}")

                print("[Primary ECU] Send config to Secondary")
                result_resp = secondary.send_config(cfg_text)
                print(f"[Secondary]\n{result_resp}")

                status_resp = secondary.get_status()
                print(f"[Secondary STATUS]\n{status_resp}")

                ok = "RESULT OK" in result_resp

                results.append({
                    "ecu_serial": ecu_serial,
                    "target_version": version,
                    "artifact": filename,
                    "led_mode": led_mode,
                    "status": "OK" if ok else "FAIL",
                    "secondary_result": result_resp,
                    "secondary_status": status_resp,
                })

        except Exception as e:
            print(f"[FAIL] secondary config update failed: {e}")
            results.append({
                "status": "FAIL",
                "reason": str(e),
            })

        finally:
            secondary.close()

        return {
            "ok": all(r.get("status") == "OK" for r in results),
            "results": results,
        }
