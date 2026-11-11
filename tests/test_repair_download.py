import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("repair_download_test", ROOT / "naia_repair_download.py")
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
SHA = "a" * 40


class Response(io.BytesIO):
    def __init__(self, content, url, headers=None, status=200):
        super().__init__(content); self.url = url; self.headers = headers or {}; self.status = status
    def geturl(self): return self.url


class DownloadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name) / "update-source.json"
        self.source.write_text(json.dumps({"owner": "Example-new-owner"}))
        source_patch = patch.object(mod, "SOURCE_FILE", self.source)
        source_patch.start()
        self.addCleanup(source_patch.stop)
        self.branch_url = "https://api.github.com/repos/Example-new-owner/naia-android-string-compat/branches/android-tools-repair"

    def prepare(self, branch=None, override=None):
        branch = {"commit": {"sha": SHA}} if branch is None else branch
        calls = []
        branch_url = self.branch_url
        class Opener:
            def open(self, request, timeout):
                calls.append(request)
                if override:
                    value = override(request)
                    if value is not None: return value
                content = json.dumps(branch).encode() if request.full_url == branch_url else b"payload"
                return Response(content, request.full_url)
        return calls, Opener()

    def test_same_immutable_commit_for_manifest_and_zip_without_credentials(self):
        calls, opener = self.prepare()
        with patch.object(mod, "build_opener", return_value=opener): result = mod.fetch_release()
        self.assertEqual(result, {"source_commit": SHA, "manifest_bytes": b"payload", "archive_bytes": b"payload"})
        repo = "Example-new-owner/naia-android-string-compat"
        self.assertEqual([r.full_url for r in calls], [
            f"https://api.github.com/repos/{repo}/branches/android-tools-repair",
            f"https://raw.githubusercontent.com/{repo}/{SHA}/release-manifest.json",
            f"https://raw.githubusercontent.com/{repo}/{SHA}/naia-repair-payload.zip",
        ])
        self.assertTrue(all("Authorization" not in r.headers for r in calls))

    def test_unbound_or_malformed_owner_never_fetches(self):
        for source in ({"owner": None}, {"owner": "https://example.invalid"}, {"owner":"a/b"},
                       {"owner":"x", "branch":"main"}, {"owner":"a..b"}, {}):
            with self.subTest(source=source), patch.object(mod, "build_opener") as opener:
                self.source.write_text(json.dumps(source))
                with self.assertRaises(mod.DownloadError):
                    mod.fetch_release()
                opener.assert_not_called()

    def test_untrusted_commit_never_becomes_a_url(self):
        for value in ("main", "../main", "a"*39, "https://evil.invalid", None, [], {"sha": SHA}):
            calls, opener = self.prepare({"commit": {"sha": value}})
            with self.subTest(value=value), patch.object(mod, "build_opener", return_value=opener), self.assertRaises(mod.DownloadError): mod.fetch_release()
            self.assertEqual(len(calls), 1)

    def test_redirect_content_encoding_and_oversize_are_rejected(self):
        for override in (
            lambda r: Response(b"", "https://evil.invalid"),
            lambda r: Response(b"", r.full_url, {"Content-Encoding": "gzip"}),
            lambda r: Response(b"", r.full_url, {"Content-Length": str(mod.MAX_MANIFEST+1)}),
            lambda r: Response(b"x"*(mod.MAX_MANIFEST+1), r.full_url),
        ):
            calls, opener = self.prepare(override=override)
            with patch.object(mod, "build_opener", return_value=opener), self.assertRaises(mod.DownloadError): mod.fetch_release()
            self.assertEqual(len(calls), 1)
        with self.assertRaises(mod.DownloadError): mod._NoRedirect().redirect_request(None, None, None, None, None, None)

    def test_timeout_does_not_start_download(self):
        calls, opener = self.prepare()
        with patch.object(mod, "build_opener", return_value=opener), patch.object(mod.time, "monotonic", side_effect=[0,61]), self.assertRaises(mod.DownloadError): mod.fetch_release()
        self.assertEqual(calls, [])


if __name__ == "__main__": unittest.main()
