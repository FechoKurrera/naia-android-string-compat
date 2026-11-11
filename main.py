"""Explicit repair of fixed NAIA extensions, preserving existing permission choices."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import threading
import time
import zipfile
import io

TARGET_ID = "naia_zz_android_tools_v1"
REPAIR_ID = "naia_android_tools_repair_v2"
LEGACY_IDS = frozenset({"naia_chatgpt_connect_v2", "naia_storage_picker_v1"})
ALLOWED_IDS = frozenset({TARGET_ID}) | LEGACY_IDS
MIN_VERSION = (0, 2, 1)
MANIFEST_NAME = "release-manifest.json"
ARCHIVE_NAME = "naia-repair-payload.zip"
PAYLOAD_SHA256 = "7b0753902c77deb12a3e68b5d422e90d2520bff72e108d42d08a96390dfce6ac"
MANIFEST_SHA256 = "c4335e777c211a558d5da979bdd4ebe61cb5315962e6bb1012f4d68987facb43"
MAX_ARCHIVE = 4 * 1024 * 1024
MAX_CONTENT = 12 * 1024 * 1024
MAX_MANIFEST = 128 * 1024
MAX_CONFIG = 256 * 1024
MAX_FILES = 200
_HASH = re.compile(r"^[0-9a-f]{64}$")
_operation = threading.Lock()
_checked_release = None
_checked_at = 0.0
_last_changed_ids = []
_last_verified_files = {}
_action_thread = None
_action_lock = threading.Lock()


class RepairError(Exception):
    pass


def _safe_path(path):
    path = Path(os.path.abspath(path))
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise RepairError("링크가 있는 경로는 수정하지 않습니다.")
    return path


def _version(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,4}\.\d{1,4}\.\d{1,4}", value):
        raise RepairError("수정본 버전 형식이 올바르지 않습니다.")
    return tuple(map(int, value.split(".")))


def _json_bytes(raw, maximum=MAX_CONFIG):
    if not isinstance(raw, bytes) or len(raw) > maximum:
        raise RepairError("설정 또는 수정본 크기가 올바르지 않습니다.")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise RepairError("설정 또는 수정본 형식이 올바르지 않습니다.") from None
    if not isinstance(value, dict):
        raise RepairError("설정 또는 수정본 형식이 올바르지 않습니다.")
    return value


def _read(path, maximum):
    path = _safe_path(path)
    if not path.is_file() or path.stat().st_size > maximum:
        raise RepairError("필요한 파일을 안전하게 읽을 수 없습니다.")
    return path.read_bytes()


def _write(path, raw):
    path = _safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o600)


def _atomic_json(path, value):
    temporary = path.with_name(path.name + ".repair.tmp")
    _write(temporary, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode())
    os.replace(temporary, path)


def _code_path(name):
    if not isinstance(name, str) or len(name) > 180 or "\\" in name:
        raise RepairError("수정본 파일 경로가 올바르지 않습니다.")
    path = PurePosixPath(name)
    if (path.is_absolute() or str(path) != name or any(p.startswith(".") for p in path.parts)
            or not path.parts or name == "settings.json" or len(path.parts) > 4):
        raise RepairError("설정 파일 또는 안전하지 않은 경로는 덮어쓰지 않습니다.")
    if path.suffix.lower() not in {".py", ".js", ".mjs", ".css", ".html", ".json", ".md", ".txt"} and name != "LICENSE":
        raise RepairError("지원하지 않는 수정본 파일입니다.")
    return name


def _hash(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise RepairError("수정본 해시 형식이 올바르지 않습니다.")
    return value


def _identity(meta, ext_id):
    return (meta.get("id") == ext_id and meta.get("entry", "main.py") == "main.py"
            and meta.get("naia_ext_api") == 1)


def validate_release(release):
    """Validate bytes from the pinned bundle or fixed-repository downloader."""
    if not isinstance(release, dict) or set(release) - {"manifest_bytes", "archive_bytes", "source_commit"}:
        raise RepairError("수정본 전달 형식이 올바르지 않습니다.")
    commit = release.get("source_commit")
    if commit is not None and (not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit)):
        raise RepairError("수정본 커밋을 확인할 수 없습니다.")
    meta = _json_bytes(release.get("manifest_bytes"), MAX_MANIFEST)
    if set(meta) != {"format", "target_version", "archive", "sha256", "main", "repairs"} or type(meta["format"]) is not int or meta["format"] != 1:
        raise RepairError("지원하지 않는 수정본 형식입니다.")
    if _version(meta["target_version"]) < MIN_VERSION or meta["archive"] != ARCHIVE_NAME:
        raise RepairError("이전 버전의 수정본은 적용하지 않습니다.")
    archive = release.get("archive_bytes")
    if not isinstance(archive, bytes) or len(archive) > MAX_ARCHIVE or hashlib.sha256(archive).hexdigest() != _hash(meta["sha256"]):
        raise RepairError("수정본 묶음 해시가 일치하지 않습니다.")
    base = meta["main"]
    if (not isinstance(base, dict) or set(base) != {"id", "files"} or base["id"] != TARGET_ID
            or not isinstance(base["files"], dict) or not {"main.py", "extension.json"} <= base["files"].keys()):
        raise RepairError("Android 도구 수정본을 확인할 수 없습니다.")
    expected = {"main/" + _code_path(name): _hash(digest) for name, digest in base["files"].items()}
    repairs = meta["repairs"]
    if not isinstance(repairs, dict) or set(repairs) - LEGACY_IDS:
        raise RepairError("허용되지 않은 확장 수정 요청입니다.")
    for ext_id, repair in repairs.items():
        if (not isinstance(repair, dict) or set(repair) != {"from_versions", "files"}
                or not isinstance(repair["from_versions"], list) or not 1 <= len(repair["from_versions"]) <= 8
                or not isinstance(repair["files"], dict) or not {"main.py", "extension.json"} <= repair["files"].keys()):
            raise RepairError("추가 확장 수정본 형식이 올바르지 않습니다.")
        for version in repair["from_versions"]:
            _version(version)
        for name, descriptor in repair["files"].items():
            _code_path(name)
            if (not isinstance(descriptor, dict) or set(descriptor) != {"from_sha256", "sha256"}
                    or not isinstance(descriptor["from_sha256"], list) or not 1 <= len(descriptor["from_sha256"]) <= 8):
                raise RepairError("추가 확장 원본 해시가 올바르지 않습니다.")
            for digest in descriptor["from_sha256"]:
                _hash(digest)
            expected["repairs/" + ext_id + "/" + name] = _hash(descriptor["sha256"])
    if not 1 <= len(expected) <= MAX_FILES:
        raise RepairError("수정본 파일 수가 너무 많습니다.")
    contents = {}
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            infos = zipped.infolist()
            if len(infos) != len(expected) or {entry.filename for entry in infos} != set(expected):
                raise RepairError("수정본 파일 목록이 일치하지 않습니다.")
            if sum(entry.file_size for entry in infos) > MAX_CONTENT:
                raise RepairError("수정본 압축 해제 크기가 너무 큽니다.")
            for entry in infos:
                if stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG) or entry.is_dir():
                    raise RepairError("수정본에 지원하지 않는 파일이 있습니다.")
                content = zipped.read(entry)
                if hashlib.sha256(content).hexdigest() != expected[entry.filename]:
                    raise RepairError("수정본 파일 해시가 일치하지 않습니다.")
                contents[entry.filename] = content
    except (zipfile.BadZipFile, RuntimeError, OSError):
        raise RepairError("수정본 압축 파일을 읽지 못했습니다.") from None
    base_meta = _json_bytes(contents["main/extension.json"])
    if not _identity(base_meta, TARGET_ID) or base_meta.get("version") != meta["target_version"]:
        raise RepairError("Android 도구 ID 또는 버전이 일치하지 않습니다.")
    for ext_id in repairs:
        extension = _json_bytes(contents["repairs/" + ext_id + "/extension.json"])
        if not _identity(extension, ext_id):
            raise RepairError("추가 확장 ID가 일치하지 않습니다.")
        _version(extension.get("version"))
    return meta, contents


def _bundle(directory):
    manifest = _read(directory / MANIFEST_NAME, MAX_MANIFEST)
    archive = _read(directory / ARCHIVE_NAME, MAX_ARCHIVE)
    if hashlib.sha256(manifest).hexdigest() != MANIFEST_SHA256 or hashlib.sha256(archive).hexdigest() != PAYLOAD_SHA256:
        raise RepairError("내장 수정본이 검증한 파일과 다릅니다.")
    return {"manifest_bytes": manifest, "archive_bytes": archive}


def _check_tree(directory):
    total = 0
    for parent, dirs, files in os.walk(directory, followlinks=False):
        for name in [*dirs, *files]:
            item = _safe_path(Path(parent) / name)
            if not item.is_file() and not item.is_dir():
                raise RepairError("지원하지 않는 기존 파일이 있습니다.")
            if item.is_file():
                total += item.stat().st_size
                if total > MAX_CONTENT:
                    raise RepairError("기존 확장 파일이 예상한 크기를 초과합니다.")


def _require_active(ctx):
    r = ctx._record
    if not (r.status == "loaded" and r.approved and r.enabled and r.armed and not r.blocked):
        raise RepairError("복구 도구를 신뢰하고 활성화한 뒤 눌러 주세요.")


def _same_files(target, files):
    return all(_safe_path(target / name).is_file() and (target / name).read_bytes() == content for name, content in files.items())


def _recover(root, transaction):
    if not transaction.exists():
        return
    _check_tree(transaction)
    plan_path = transaction / "plan.json"
    if not plan_path.exists():
        shutil.rmtree(transaction)
        return
    plan = _json_bytes(_read(plan_path, MAX_CONFIG))
    ids = plan.get("ids")
    if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids) or len(ids) != len(set(ids)) or set(ids) - ALLOWED_IDS:
        raise RepairError("중단된 복구 기록을 확인할 수 없습니다.")
    if (transaction / "committed.json").exists():
        committed = _json_bytes(_read(transaction / "committed.json", MAX_CONFIG))
        if committed != {"ids": ids}:
            raise RepairError("완료된 복구 기록을 확인할 수 없습니다.")
        _cleanup_transaction(transaction)
        return
    for ext_id in reversed(ids):
        previous = transaction / "previous" / ext_id
        if not previous.exists():
            continue
        if not _identity(_json_bytes(_read(previous / "extension.json", MAX_CONFIG)), ext_id):
            raise RepairError("중단된 복구의 원본 ID가 다릅니다.")
        target = _safe_path(root / ext_id)
        if target.exists():
            failed = transaction / "failed" / ext_id
            failed.parent.mkdir(parents=True, exist_ok=True)
            if failed.exists():
                raise RepairError("중단된 복구 파일이 겹칩니다. 기존 파일을 유지했습니다.")
            os.replace(target, failed)
        os.replace(previous, target)
    shutil.rmtree(transaction)


def _cleanup_transaction(transaction):
    # Never partially delete a transaction that recovery could mistake for an
    # interrupted swap. Retire its name atomically before best-effort cleanup.
    retired = transaction.with_name(".android-tools-repair-cleanup-" + str(time.time_ns()))
    os.replace(transaction, retired)
    shutil.rmtree(retired, ignore_errors=True)


def perform_update(ctx, *, release=None):
    _require_active(ctx)
    if not _operation.acquire(blocking=False):
        raise RepairError("이미 복구 중입니다.")
    try:
        return _perform_update(ctx, release)
    finally:
        _operation.release()


def _perform_update(ctx, release):
    global _last_changed_ids, _last_verified_files
    app = ctx._app_context
    manager = getattr(app, "extension_manager", None)
    paths = getattr(app, "runtime_paths", None)
    if manager is None or paths is None or not callable(getattr(manager, "extensions_root", None)):
        raise RepairError("지원하는 확장 관리자를 찾을 수 없습니다.")
    root = _safe_path(manager.extensions_root())
    directory = _safe_path(ctx.ext_dir)
    if directory != root / REPAIR_ID or not root.is_dir():
        raise RepairError("복구 도구의 설치 위치가 올바르지 않습니다.")
    if bool(getattr(app, "is_generating", False) or getattr(app, "headless_generation_runner_active", False)):
        raise RepairError("이미지 생성이 끝난 뒤 복구해 주세요.")
    meta, contents = validate_release(_bundle(directory) if release is None else release)
    config_path = _safe_path(Path(paths.config_dir) / "extensions.json")
    backup_root = _safe_path(Path(paths.config_dir) / "android-tools" / "repair-backups")
    if backup_root.is_relative_to(root) or root.is_relative_to(backup_root):
        raise RepairError("안전한 백업 위치를 확인할 수 없습니다.")
    transaction = _safe_path(root / ".android-tools-repair")
    _recover(root, transaction)
    plans, skipped, verified = [], [], {}
    targets = {TARGET_ID: {name: contents["main/" + name] for name in meta["main"]["files"]}}
    for ext_id, repair in meta["repairs"].items():
        targets[ext_id] = {name: contents["repairs/" + ext_id + "/" + name] for name in repair["files"]}
    for ext_id, files in targets.items():
        target = _safe_path(root / ext_id)
        if not target.exists():
            if ext_id == TARGET_ID:
                raise RepairError("기존 Android 도구가 설치되어 있지 않습니다.")
            skipped.append({"id": ext_id, "reason": "not_installed"})
            continue
        _check_tree(target)
        installed = _json_bytes(_read(target / "extension.json", MAX_CONFIG))
        if not _identity(installed, ext_id):
            raise RepairError("기존 확장의 ID가 설치 위치와 다릅니다.")
        if ext_id == TARGET_ID:
            version = _version(installed.get("version"))
            if version < (0, 1, 0) or version > _version(meta["target_version"]):
                raise RepairError("이전 버전으로 바꾸거나 알 수 없는 버전을 교체하지 않습니다.")
        record = next((r for r in manager.records if r.ext_id == ext_id), None)
        if record is None or _safe_path(record.directory) != target:
            raise RepairError("기존 확장의 실행 정보가 설치 위치와 다릅니다.")
        if _same_files(target, files):
            verified[ext_id] = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
            continue
        if ext_id in LEGACY_IDS:
            spec = meta["repairs"][ext_id]
            source_matches = installed.get("version") in spec["from_versions"]
            for name, descriptor in spec["files"].items():
                source = _safe_path(target / name)
                source_matches = source_matches and source.is_file() and hashlib.sha256(source.read_bytes()).hexdigest() in descriptor["from_sha256"]
            if not source_matches:
                skipped.append({"id": ext_id, "reason": "source_changed"})
                continue
        plans.append({"id": ext_id, "target": target, "files": files, "record": record})
    if not plans:
        _last_changed_ids, _last_verified_files = sorted(verified), verified
        return {"updated": False, "already_updated": True, "version": meta["target_version"], "skipped": skipped,
                "reload_ids": sorted(verified), "restart_required": True, "reapproval_required": False}
    config = _json_bytes(_read(config_path, MAX_CONFIG))
    if not isinstance(config.get("approved"), list):
        raise RepairError("기존 확장 승인 정보를 확인할 수 없습니다.")
    backup = _safe_path(backup_root / str(time.time_ns()))
    backup.mkdir(mode=0o700, parents=True)
    _atomic_json(backup / "extensions-before.json", config)
    transaction.mkdir()
    _atomic_json(transaction / "plan.json", {"ids": [plan["id"] for plan in plans]})
    for plan in plans:
        shutil.copytree(plan["target"], backup / plan["id"], ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    ids = {plan["id"] for plan in plans}
    stopped = False
    try:
        with manager._lock:
            _require_active(ctx)
            for plan in plans:
                record = plan["record"]
                record.status = "error"
                record.error = "수정본 적용 중입니다. 완료 후 ‘수정한 확장 다시 불러오기’를 누르세요."
            # The runtime gates callbacks on loaded status. Keep all persisted
            # approval/enabled/armed/blocked choices; normal restart loads only
            # extensions the user had already approved.
            stopped = True
        for plan in plans:
            module = sys.modules.get("naia_ext_" + plan["id"])
            controller = getattr(module, "_controller", None)
            if controller is not None and callable(getattr(controller, "close", None)):
                controller_dir = getattr(getattr(controller, "ctx", None), "ext_dir", None)
                if controller_dir is not None and _safe_path(controller_dir) == plan["target"]:
                    controller.close()
            stage = transaction / "new" / plan["id"]
            # Preserve every existing data/settings file. Only reviewed source
            # files are overwritten; no OAuth or pairing files are enumerated.
            shutil.copytree(plan["target"], stage, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            for name, content in plan["files"].items():
                _write(stage / name, content)
        for plan in plans:
            previous = transaction / "previous" / plan["id"]
            previous.parent.mkdir(parents=True, exist_ok=True)
            os.replace(plan["target"], previous)
            os.replace(transaction / "new" / plan["id"], plan["target"])
        _atomic_json(backup / "completed.json", {"version": meta["target_version"], "ids": sorted(ids)})
        _atomic_json(transaction / "committed.json", {"ids": [plan["id"] for plan in plans]})
    except BaseException as error:
        try:
            _recover(root, transaction)
        except Exception:
            raise RepairError("복구가 중단되어 백업을 보존했습니다. 추가 설치 없이 진단을 요청해 주세요.") from None
        if stopped:
            raise RepairError("복구에 실패해 기존 파일을 유지했습니다. 추가 설치 없이 오류를 확인해 주세요.") from error
        raise
    _cleanup_transaction(transaction)
    for plan in plans:
        verified[plan["id"]] = {name: hashlib.sha256(content).hexdigest() for name, content in plan["files"].items()}
    _last_changed_ids, _last_verified_files = sorted(verified), verified
    for plan in plans:
        plan["record"].error = "수정본을 적용했습니다. ‘수정한 확장 다시 불러오기’를 눌러 실행 결과를 확인하세요."
    return {"updated": True, "already_updated": False, "version": meta["target_version"],
            "updated_ids": sorted(ids), "reload_ids": sorted(verified), "skipped": skipped,
            "restart_required": True, "reapproval_required": False}


def _download():
    path = Path(__file__).with_name("naia_repair_download.py")
    spec = importlib.util.spec_from_file_location("_naia_repair_download", path)
    module = importlib.util.module_from_spec(spec)
    # Read source directly; a repair must not reuse stale timestamp bytecode.
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    try:
        return module.fetch_release()
    except module.DownloadError as error:
        raise RepairError(str(error)) from None


def reload_repaired(ctx):
    """Explicit action only; retry reviewed files without changing permissions."""
    _require_active(ctx)
    if not _last_changed_ids:
        raise RepairError("먼저 수정본을 적용해 주세요. 다시 불러올 확장이 없습니다.")
    if not _operation.acquire(blocking=False):
        raise RepairError("이미 복구 또는 다시 불러오기가 진행 중입니다.")
    try:
        manager = ctx._app_context.extension_manager
        outcomes = []
        for ext_id in list(_last_changed_ids):
            if ext_id not in ALLOWED_IDS:
                raise RepairError("다시 불러올 확장 목록이 올바르지 않습니다.")
            with manager._lock:
                _require_active(ctx)
                record = next((r for r in manager.records if r.ext_id == ext_id), None)
                if record is None or not record.approved or not record.enabled or record.blocked:
                    outcomes.append({"id": ext_id, "status": "skipped", "reason": "existing_permission_choice"})
                    continue
                target = _safe_path(manager.extensions_root()) / ext_id
                expected = _last_verified_files.get(ext_id, {})
                if not expected or _safe_path(record.directory) != target or any(
                        hashlib.sha256(_read(target / name, MAX_CONTENT)).hexdigest() != digest
                        for name, digest in expected.items()):
                    outcomes.append({"id": ext_id, "status": "error", "error": "확인 후 파일이 달라졌습니다. 수정본을 다시 확인해 주세요."})
                    continue
                parser = getattr(manager, "_parse_manifest_into", None)
                if callable(parser):
                    choices = {key: getattr(record, key) for key in ("approved", "enabled", "armed", "blocked")}
                    try:
                        parser(target / "extension.json", record)
                    finally:
                        for key, value in choices.items():
                            setattr(record, key, value)
            # The real manager uses a non-reentrant Lock. Its `enabled` load
            # gate checks enabled/approved/blocked again under its own lock and
            # does not enable anything; `retry` lacks the enabled recheck.
            manager.load_by_key("enabled:" + ext_id)
            with manager._lock:
                outcomes.append({"id": ext_id, "status": record.status,
                                 "error": str(getattr(record, "error", ""))[:300]})
        return outcomes
    finally:
        _operation.release()


def register(ctx):
    def run_action(key):
        global _checked_release, _checked_at
        try:
            _require_active(ctx)
            if key == "check_remote":
                _checked_release = None
                release = _download()
                meta, _ = validate_release(release)
                _require_active(ctx)
                _checked_release, _checked_at = release, time.monotonic()
                message = f"GitHub 수정본 {meta['target_version']} 확인 완료. 적용하려면 ‘확인한 수정본 적용’을 누르세요."
            elif key == "reload_repaired":
                outcomes = reload_repaired(ctx)
                message = "\n".join(item["id"] + ": " + (
                    "불러오기 완료" if item["status"] == "loaded" else "기존 권한에 따라 건너뜀" if item["status"] == "skipped"
                    else "오류 · " + item.get("error", item["status"])) for item in outcomes)
                message += "\nNAIA 화면을 새로고침해 주세요."
                success = all(item["status"] in ("loaded", "skipped") for item in outcomes)
                ctx.log(message)
                ctx.show_toast(message, level="success" if success else "error")
                return
            elif key in ("apply_repair", "apply_remote"):
                release = None
                if key == "apply_remote":
                    if _checked_release is None or time.monotonic() - _checked_at > 600:
                        raise RepairError("먼저 ‘GitHub 최신 수정본 확인’을 눌러 주세요. 확인 결과는 10분 동안 유효합니다.")
                    release, _checked_release = _checked_release, None
                result = perform_update(ctx, release=release)
                message = ("수정본이 이미 적용되어 있습니다." if result["already_updated"] else "수정본을 적용했고 기존 연결·설정 파일을 보존했습니다.")
                if result.get("reload_ids"):
                    message += " ‘수정한 확장 다시 불러오기’를 누르세요."
                message += " 기존 승인·작동·차단 설정은 유지했습니다."
                changed = [item["id"] for item in result.get("skipped", []) if item["reason"] == "source_changed"]
                if changed:
                    message += " 원본이 달라 수정하지 않은 확장: " + ", ".join(changed)
            else:
                return
            ctx.log(message)
            ctx.show_toast(message, level="success")
        except Exception as error:
            message = str(error) if isinstance(error, RepairError) else "수정 작업을 완료하지 못했습니다. 연결·설정을 삭제하지 말고 진단을 요청해 주세요."
            ctx.log(message)
            ctx.show_toast(message, level="error")

    def action(key):
        global _action_thread
        if key not in ("check_remote", "reload_repaired", "apply_repair", "apply_remote"):
            return
        try:
            _require_active(ctx)
            with _action_lock:
                if _action_thread is not None and _action_thread.is_alive():
                    raise RepairError("이미 수정 작업을 진행 중입니다. 완료 결과를 기다려 주세요.")
                # Every network/import/copy operation runs outside the backend
                # event loop and the panel callback's manager lock.
                _action_thread = threading.Thread(target=run_action, args=(key,), name="naia-repair-action", daemon=True)
                _action_thread.start()
        except RepairError as error:
            ctx.show_toast(str(error), level="error")

    ctx.register_panel([
        {"key": "apply_repair", "type": "action", "scope": "global", "label": "내장 0.2.9 수정본 적용", "help": "기존 Android 도구를 업데이트합니다. 연결·설정 파일은 보존합니다."},
        {"key": "reload_repaired", "type": "action", "scope": "global", "label": "수정한 확장 다시 불러오기", "help": "수정본과 일치하고 기존에 승인한 확장만 다시 불러옵니다. 꺼 두거나 차단한 확장은 켜지 않습니다."},
        {"key": "check_remote", "type": "action", "scope": "global", "label": "GitHub 최신 수정본 확인", "help": "정해진 수정본 저장소에서 파일을 확인합니다. 자동으로 적용하지 않습니다."},
        {"key": "apply_remote", "type": "action", "scope": "global", "label": "확인한 수정본 적용", "help": "10분 이내 확인한 수정본만 적용합니다. 완료 후 ‘수정한 확장 다시 불러오기’를 누르세요."},
    ], title="NAIA Android 업데이트 1.1", on_action=action)

