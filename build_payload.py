"""Build a deterministic reviewed repair payload; never downloads or publishes."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import stat
import zipfile

HERE = Path(__file__).resolve().parent


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def build(android_tools, chatgpt, storage, original_hashes, replacement_hashes, output=HERE):
    spec = importlib.util.spec_from_file_location("repair_build_validator", HERE / "main.py")
    engine = importlib.util.module_from_spec(spec)
    exec(compile((HERE / "main.py").read_bytes(), str(HERE / "main.py"), "exec"), engine.__dict__)
    originals = json.loads(Path(original_hashes).read_bytes())
    replacements = json.loads(Path(replacement_hashes).read_bytes())
    files, main_hashes, repairs = {}, {}, {}
    android_tools = Path(android_tools)
    for path in sorted(android_tools.rglob("*")):
        relative = path.relative_to(android_tools)
        if any(part.startswith(".") or part in {"__pycache__", "tests"} for part in relative.parts) or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise ValueError("Source symlink is not allowed: " + str(relative))
        if not path.is_file():
            continue
        name = engine._code_path(relative.as_posix())
        raw = path.read_bytes()
        files["main/" + name], main_hashes[name] = raw, digest(raw)
    target_meta = json.loads(files["main/extension.json"])
    for source_name, source_dir, source_version, ext_id in (
            ("chatgpt", Path(chatgpt), "0.1.1", "naia_chatgpt_connect_v2"),
            ("storage", Path(storage), "0.1.0", "naia_storage_picker_v1")):
        replacement = replacements[source_name]
        if replacement["extension_id"] != ext_id:
            raise ValueError("Replacement extension identity differs")
        descriptors = {}
        for name, hashes in sorted(replacement["files"].items()):
            engine._code_path(name)
            path = source_dir / name
            if path.is_symlink():
                raise ValueError("Source symlink is not allowed")
            raw = path.read_bytes()
            old_hash = originals[source_name][name]["sha256"]
            if hashes["original_sha256"] != old_hash or hashes["replacement_sha256"] != digest(raw):
                raise ValueError("Reviewed source hash differs: " + source_name + "/" + name)
            files["repairs/" + ext_id + "/" + name] = raw
            descriptors[name] = {"from_sha256": [old_hash], "sha256": digest(raw)}
        repairs[ext_id] = {"from_versions": [source_version], "files": descriptors}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, raw in sorted(files.items()):
            entry = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o600) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, raw, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    archive_raw = buffer.getvalue()
    manifest = {"format": 1, "target_version": target_meta["version"], "archive": engine.ARCHIVE_NAME,
                "sha256": digest(archive_raw), "main": {"id": engine.TARGET_ID, "files": main_hashes}, "repairs": repairs}
    manifest_raw = json_bytes(manifest)
    engine.validate_release({"manifest_bytes": manifest_raw, "archive_bytes": archive_raw})
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / engine.MANIFEST_NAME).write_bytes(manifest_raw)
    (output / engine.ARCHIVE_NAME).write_bytes(archive_raw)
    entry = output / "main.py"
    source = entry.read_text() if entry.exists() else (HERE / "main.py").read_text()
    for constant, value in (("PAYLOAD_SHA256", digest(archive_raw)), ("MANIFEST_SHA256", digest(manifest_raw))):
        source, count = re.subn(r'^' + constant + r' = "[^"\n]*"$', constant + ' = "' + value + '"', source, flags=re.MULTILINE)
        if count != 1:
            raise ValueError("Repair source digest constant not found")
    entry.write_text(source)
    for filename, value in (("original-hashes.json", originals), ("replacement-hashes.json", replacements)):
        (output / "build-inputs").mkdir(exist_ok=True)
        (output / "build-inputs" / filename).write_bytes(json_bytes(value))
    return {"version": manifest["target_version"], "files": len(files), "archive_bytes": len(archive_raw),
            "archive_sha256": digest(archive_raw), "manifest_sha256": digest(manifest_raw)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--android-tools", type=Path, default=HERE.parent / "android-tools")
    parser.add_argument("--chatgpt", type=Path, default=HERE.parent / "compat-repair-source/chatgpt")
    parser.add_argument("--storage", type=Path, default=HERE.parent / "compat-repair-source/storage")
    parser.add_argument("--original-hashes", type=Path, default=HERE.parent / "compat-repair-source/original-hashes.json")
    parser.add_argument("--replacement-hashes", type=Path, default=HERE.parent / "compat-repair-source/replacement-hashes.json")
    parser.add_argument("--output", type=Path, default=HERE)
    options = parser.parse_args()
    print(json.dumps(build(**vars(options)), indent=2))


if __name__ == "__main__":
    main()
