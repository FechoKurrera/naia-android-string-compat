"""Download pinned upstream diagnostic test inputs; never import or execute them.

Developer use only. The upstream files are not bundled in Android updates.
Run this file once before test_comic_compat.py, then install Pillow in the test
environment. Existing files with the expected digest are reused unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import urllib.request


COMMIT = "f90c734803bd699c73f5854a83f96325a7071a8a"
BASE = f"https://raw.githubusercontent.com/okawaritsuika/NAIA-EXten/{COMMIT}/"
DESTINATION = Path(__file__).resolve().parents[2] / "comic-upstream"
# SHA-256 of the unmodified raw upstream bytes at COMMIT, not normalized text.
FILES = {
    "naia_exten/features/comic_maker/feature.py": (51193, "cd4d875d407daab1b426a5d35d1b229b6dab55e49722ce6b81c14314819d5d14"),
    "naia_exten/features/comic_maker/chooser_ui.py": (7258, "10eb661d089915705a4488f0f851d0d4d948a76a15b073e0ed3a3cd2f06c1f3c"),
    "naia_exten/features/comic_maker/client.py": (6543, "b72f18edf21952d844aefd173801bb6dd869966836dc25354a7ac0b5a71568e3"),
    "naia_exten/features/comic_maker/models.py": (11114, "ec9960eb799040a859fb862eb5c6cc4622ebc1763f64858188a0cb0aca1fd989"),
    "naia_exten/features/comic_maker/renderer.py": (7842, "8288fad4be271e389af4b5106074549f69d2e26a016b48ff5cfbed8f8643847a"),
    "naia_exten/features/comic_maker/stored_workflow.py": (3641, "a5c068dc673a06308c56082000ccac8169887ad988f608c3dfd517b19f41d726"),
    "naia_exten/features/base_feature.py": (3423, "7aa6d275f114d789ea4ac0190e74242bcd4d2344d67fdfe802443ceab963fc0e"),
    "tests/test_comic_maker.py": (25138, "43cfd1736fc9ea3a27ea90a25ce872b58d49d97297791bb2d6aed3d167d4264e"),
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise RuntimeError("Unexpected redirect fetching pinned test fixture")


def matches(data, specification):
    size, digest = specification
    return len(data) == size and hashlib.sha256(data).hexdigest() == digest


def fetch(*, check_only=False, destination=DESTINATION, opener=None):
    destination = Path(destination)
    opener = opener or urllib.request.build_opener(NoRedirect())
    downloads = {}
    for relative, expected in FILES.items():
        target = destination / relative
        if target.is_symlink():
            raise RuntimeError(f"Refusing fixture symlink: {relative}")
        if target.is_file() and target.stat().st_size == expected[0]:
            if matches(target.read_bytes(), expected):
                continue
        if check_only:
            raise RuntimeError(f"Missing or changed fixture: {relative}")
        request = urllib.request.Request(BASE + relative,
                                         headers={"User-Agent": "NAIA-test-fixture-fetcher"})
        with opener.open(request, timeout=15) as response:
            data = response.read(expected[0] + 1)
        if not matches(data, expected):
            raise RuntimeError(f"Pinned fixture digest mismatch: {relative}")
        downloads[relative] = data
    # Validate the complete download set before making any fixture changes.
    for relative, data in downloads.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".download")
        if temporary.is_symlink():
            raise RuntimeError(f"Refusing fixture temporary symlink: {relative}")
        temporary.write_bytes(data)
        temporary.replace(target)
    return len(FILES)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify local fixtures without network access")
    args = parser.parse_args()
    print(f"Verified {fetch(check_only=args.check)} pinned Comic Maker test files.")
