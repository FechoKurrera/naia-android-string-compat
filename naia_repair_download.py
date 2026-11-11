"""Explicit updates from one fixed public repository, pinned to one commit.

No credentials, arbitrary URLs, redirects, or code execution are accepted here.
The repair engine validates the release manifest, archive and target identities.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

SOURCE_FILE = Path(__file__).with_name("update-source.json")
REPO_NAME = "naia-android-string-compat"
BRANCH = "android-tools-repair"
MAX_MANIFEST = 128 * 1024
MAX_ARCHIVE = 4 * 1024 * 1024
TIMEOUT = 15
TOTAL_TIMEOUT = 60


class DownloadError(Exception):
    pass


def repository_urls():
    """Address belongs to this publisher's installer, never to panel/user input."""
    try:
        if SOURCE_FILE.is_symlink() or SOURCE_FILE.stat().st_size > 512:
            raise ValueError
        data = json.loads(SOURCE_FILE.read_bytes())
        if not isinstance(data, dict) or set(data) != {"owner"}:
            raise ValueError
        owner = data["owner"]
        if owner is None:
            raise DownloadError("이 복원본은 GitHub 업데이트 주소가 지정되지 않았습니다. 내장 수정본만 적용할 수 있습니다.")
        if (not isinstance(owner, str) or len(owner) > 39 or
                not re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", owner)):
            raise ValueError
    except (ValueError, OSError, UnicodeError, RecursionError):
        raise DownloadError("업데이트 도구의 저장소 주소를 확인할 수 없습니다.") from None
    repo = owner + "/" + REPO_NAME
    return ("https://api.github.com/repos/" + repo + "/branches/" + BRANCH,
            "https://raw.githubusercontent.com/" + repo + "/")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise DownloadError("업데이트 주소가 다른 곳으로 이동해 다운로드를 중단했습니다.")


def _read(opener, url, limit, deadline, *, json_response=False):
    if time.monotonic() >= deadline:
        raise DownloadError("업데이트 확인 시간이 초과되었습니다. 잠시 후 다시 시도하세요.")
    request = Request(url, headers={"Accept": "application/json" if json_response else "application/octet-stream",
                                   "User-Agent": "NAIA-Android-Repair/1.1", "Accept-Encoding": "identity"})
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            if response.geturl() != url or response.status != 200:
                raise DownloadError("요청한 업데이트 파일을 받지 못했습니다.")
            encoding = response.headers.get("Content-Encoding", "identity").lower()
            if encoding not in ("", "identity"):
                raise DownloadError("지원하지 않는 업데이트 응답 형식입니다.")
            declared = response.headers.get("Content-Length")
            if declared is not None and (not declared.isdigit() or int(declared) > limit):
                raise DownloadError("업데이트 파일이 허용 크기를 초과합니다.")
            chunks, size = [], 0
            while True:
                if time.monotonic() >= deadline:
                    raise DownloadError("업데이트 확인 시간이 초과되었습니다.")
                chunk = response.read(min(65536, limit - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise DownloadError("업데이트 파일이 허용 크기를 초과합니다.")
                chunks.append(chunk)
            return b"".join(chunks)
    except HTTPError as error:
        if error.code in (403, 429):
            raise DownloadError("GitHub의 요청 제한으로 확인하지 못했습니다. 잠시 후 다시 시도하세요.") from None
        raise DownloadError("GitHub에서 업데이트 파일을 받지 못했습니다.") from None
    except (URLError, TimeoutError, OSError):
        raise DownloadError("업데이트 서버에 연결하지 못했습니다. 연결 상태를 확인하세요.") from None


def fetch_release():
    branch_url, raw_prefix = repository_urls()
    opener = build_opener(_NoRedirect())
    deadline = time.monotonic() + TOTAL_TIMEOUT
    try:
        branch = json.loads(_read(opener, branch_url, MAX_MANIFEST, deadline, json_response=True))
        commit = branch["commit"]["sha"]
    except (ValueError, TypeError, KeyError, RecursionError):
        raise DownloadError("GitHub의 수정본 정보를 확인하지 못했습니다.") from None
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise DownloadError("업데이트 버전 식별자가 올바르지 않습니다.")
    base = raw_prefix + commit + "/"
    manifest = _read(opener, base + "release-manifest.json", MAX_MANIFEST, deadline)
    archive = _read(opener, base + "naia-repair-payload.zip", MAX_ARCHIVE, deadline)
    return {"manifest_bytes": manifest, "archive_bytes": archive, "source_commit": commit}
