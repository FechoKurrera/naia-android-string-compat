"""Verify and archive an existing repair bundle and two restorable source branches.

This is an offline build tool. It never authenticates or writes to GitHub.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import zipfile

HERE = Path(__file__).resolve().parent
PRODUCT = HERE.parent / "android-tools"
LEGACY = HERE.parent / "compat-repair-source"
INSTALL = ("extension.json", "main.py", "naia_repair_download.py", "update-source.json",
           "release-manifest.json", "naia-repair-payload.zip", "README.md", "LICENSE")
OWNER_RE = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")
BRANCHES = ("android-tools", "android-tools-repair")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def clean_files(directory, *, runtime_only=False):
    for path in sorted(directory.rglob("*")):
        parts = path.relative_to(directory).parts
        if any(part.startswith(".") or part == "__pycache__" for part in parts):
            continue
        if runtime_only and "tests" in parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise ValueError("Refusing symlink: " + str(path))
        if path.is_file():
            if path.suffix not in {".py", ".mjs", ".js", ".txt", ".json", ".md", ".html", ".zip"} and path.name != "LICENSE":
                raise ValueError("Unexpected release source: " + str(path.relative_to(directory)))
            yield path.relative_to(directory).as_posix(), path


def write_atomic(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        name = temp.name
        try:
            temp.write(raw)
            temp.flush()
            os.fsync(temp.fileno())
        except BaseException:
            os.unlink(name)
            raise
    try:
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def zipped(files):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, content in sorted(files.items()):
            entry = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(entry, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    raw = result.getvalue()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if archive.testzip() is not None:
            raise ValueError("Bad output archive CRC")
    return raw


def verify():
    import importlib.util
    spec = importlib.util.spec_from_file_location("delivery_validation", HERE / "main.py")
    engine = importlib.util.module_from_spec(spec)
    exec(compile((HERE / "main.py").read_bytes(), str(HERE / "main.py"), "exec"), engine.__dict__)
    release = engine._bundle(HERE)
    meta, contents = engine.validate_release(release)
    product = json.loads((PRODUCT / "extension.json").read_bytes())
    updater = json.loads((HERE / "extension.json").read_bytes())
    if (meta["target_version"] != product["version"] or product["id"] != engine.TARGET_ID
            or updater["id"] != engine.REPAIR_ID or updater["naia_ext_api"] != 1
            or engine.REPAIR_ID == "naia_android_tools_repair"):
        raise ValueError("Updater or target identity differs")
    runtime = dict(clean_files(PRODUCT, runtime_only=True))
    if set(runtime) != set(meta["main"]["files"]):
        raise ValueError("Source tree changed after build_payload.py; rebuild first")
    for name, path in runtime.items():
        if contents["main/" + name] != path.read_bytes():
            raise ValueError("Bundle not built from current source: " + name)
    for key, directory in (("naia_chatgpt_connect_v2", LEGACY / "chatgpt"),
                           ("naia_storage_picker_v1", LEGACY / "storage")):
        for name in meta["repairs"][key]["files"]:
            if (directory / name).read_bytes() != contents["repairs/" + key + "/" + name]:
                raise ValueError("Legacy repair source differs: " + key + "/" + name)
    return product["version"], updater["version"]


def make_delivery(destination, owner=None, offline=False):
    product_version, updater_version = verify()
    if owner is not None and (not isinstance(owner, str) or len(owner) > 39 or not OWNER_RE.fullmatch(owner)):
        raise ValueError("Expected a verified GitHub login, not an address or repository")
    source_path = HERE / "update-source.json"
    source = json.loads(source_path.read_bytes())
    if owner is not None or offline:
        source = {"owner": owner}
    if not isinstance(source, dict) or set(source) != {"owner"}:
        raise ValueError("Invalid update source file")
    pinned = source["owner"]
    if pinned is not None and (not isinstance(pinned, str) or len(pinned) > 39 or not OWNER_RE.fullmatch(pinned)):
        raise ValueError("Invalid pinned owner")
    encoded_source = (json.dumps(source, ensure_ascii=True) + "\n").encode()
    if source_path.read_bytes() != encoded_source:
        write_atomic(source_path, encoded_source)

    installer_name = f"NAIA-Update-{updater_version}.zip"
    sources_name = f"NAIA-Source-{product_version}.zip"
    installer = zipped({name: (HERE / name).read_bytes() for name in INSTALL})
    repo = f"{pinned}/naia-android-string-compat" if pinned is not None else "(no configured owner)"
    description = (
        f"NAIA Android tools {product_version}, updater {updater_version}\n"
        f"GitHub target: {repo}\n"
        "android-tools/: restore the contents to the android-tools branch root.\n"
        "android-tools-repair/: restore the contents to the android-tools-repair branch root.\n"
        "compat-repair-source/: only the reviewed legacy files needed to reproduce the payload.\n"
        "These snapshots do not contain prior Git history, the APK project/signing key,\n"
        "credentials, a tablet's photo archive, or unavailable legacy test fixtures.\n"
        "Run python android-tools-repair/build_payload.py, then build_delivery.py from the same directory.\n"
        "The separate NAIA-Update ZIP contains the installer directory in portable form;\n"
        "upstream NAIA v2.0.48 does not import local ZIPs from Settings.\n"
    ).encode()
    sources = {"RESTORE.txt": description}
    for prefix, directory in zip((*BRANCHES, "compat-repair-source"), (PRODUCT, HERE, LEGACY)):
        for name, path in clean_files(directory):
            sources[f"{prefix}/{name}"] = path.read_bytes()
    snapshot = zipped(sources)
    destination = Path(destination).resolve()
    for root in (PRODUCT, HERE, LEGACY):
        if destination.is_relative_to(root.resolve()):
            raise ValueError("Output must be outside the packaged source directories")
    artifacts = {installer_name: installer, sources_name: snapshot}
    checksums = "".join(f"{sha(raw)}  {name}\n" for name, raw in sorted(artifacts.items()))
    for name, raw in artifacts.items():
        write_atomic(destination / name, raw)
    write_atomic(destination / "SHA256SUMS.txt", checksums.encode())
    return {"repository": repo, "product": product_version, "updater": updater_version,
            "files": {name: sha(raw) for name, raw in artifacts.items()}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--owner", help="Verified new GitHub account; pin updater source before packaging")
    target.add_argument("--offline", action="store_true", help="Remove the remote update target")
    opts = parser.parse_args()
    print(json.dumps(make_delivery(opts.output_dir, opts.owner, opts.offline), indent=2))
