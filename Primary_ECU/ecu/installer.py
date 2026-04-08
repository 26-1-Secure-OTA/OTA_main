from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
