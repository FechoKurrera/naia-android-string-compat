"""Offline installer, isolation and loopback boundary regression tests."""
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prompt_server_test_target", ROOT / "naia_prompt_server.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class PromptServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.token_reads = 0
        def token():
            self.token_reads += 1
            return "DO-NOT-EXPOSE"
        self.server = mod.PromptServer(self.root, token)
        self.addCleanup(self.server.close)
        self.files = {"app.py": b"VALUE = 1\n", "data/seed.txt": b"seed"}
        self.server.manifest = {"repository": "author/PromptServer", "commit": "abc123",
            "files": {p: {"sha": mod._blob_hash(b), "size": len(b)} for p, b in self.files.items()}}
        self.server.source = self.server.root / "source-abc123"

    def archive(self, files=None):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            for p, b in (files or self.files).items():
                z.writestr("PromptServer-abc123/" + p, b)
        return out.getvalue()

    def wait_worker(self):
        self.server._worker.join(3)
        self.assertFalse(self.server._worker.is_alive())

    def test_constructor_and_status_do_not_touch_token_or_network(self):
        self.assertEqual(self.server.status(), {"state": "not_installed", "error": "", "installed": False})
        self.assertEqual(self.token_reads, 0)

    def test_restart_missing_source_never_downloads(self):
        with patch.object(self.server, "_port_open", return_value=False), patch.object(self.server, "_download") as download:
            self.server.start_installed()
            self.wait_worker()
        download.assert_not_called()
        self.assertEqual(self.server.status()["state"], "error")
        self.assertIn("Comic Maker 실행부 준비", self.server.status()["error"])

    def test_verified_install_does_not_touch_existing_user_data(self):
        self.server.data.mkdir(parents=True)
        saved = self.server.data / "scenarios.db"
        saved.write_bytes(b"keep user data")
        self.server._install(self.archive(), threading.Event())
        self.assertTrue(self.server._verified())
        self.assertEqual(saved.read_bytes(), b"keep user data")
        (self.server.source / "app.py").write_text("VALUE = 2\n")
        self.assertFalse(self.server._verified())

    def test_corrupt_or_duplicate_archive_never_replaces_source(self):
        self.server._install(self.archive(), threading.Event())
        with self.assertRaises(mod.PromptServerError):
            self.server._install(self.archive({**self.files, "app.py": b"VALUE = 9\n"}), threading.Event())
        self.assertTrue(self.server._verified())
        out = io.BytesIO(self.archive())
        with zipfile.ZipFile(out, "a") as z:
            z.writestr("PromptServer-abc123/app.py", self.files["app.py"])
        with self.assertRaises(mod.PromptServerError):
            self.server._install(out.getvalue(), threading.Event())
        self.assertTrue(self.server._verified())

    def test_cancelled_prepare_cannot_install_or_start_later(self):
        started, finish = threading.Event(), threading.Event()
        def download():
            started.set(); finish.wait(3)
            return self.archive()
        with patch.object(self.server, "_port_open", return_value=False), patch.object(self.server, "_download", side_effect=download):
            self.server.prepare()
            self.assertTrue(started.wait(2))
            self.server.close()
            finish.set()
            self.wait_worker()
        self.assertEqual(self.server.status()["state"], "closed")
        self.assertFalse(self.server.source.exists())
        self.assertIsNone(self.server._server)

    def test_existing_server_never_downloaded_reconfigured_or_stopped(self):
        with patch.object(self.server, "_port_open", return_value=True), patch.object(self.server, "_probe", return_value=True), patch.object(self.server, "_download") as download:
            self.server.prepare()
            self.wait_worker()
        self.assertEqual(self.server.status()["state"], "external")
        self.server.stop()
        download.assert_not_called()
        self.assertIsNone(self.server._server)
        self.assertEqual(self.token_reads, 0)

    def test_restart_during_owned_shutdown_is_explicit(self):
        waiting = threading.Event()
        thread = threading.Thread(target=lambda: waiting.wait(3), daemon=True)
        self.server._server_thread = thread
        self.server._server = types.SimpleNamespace(should_exit=False)
        thread.start()
        try:
            self.server.stop()
            self.assertEqual(self.server.status()["state"], "stopping")
            with self.assertRaisesRegex(mod.PromptServerError, "종료 중"):
                self.server.prepare()
            self.assertTrue(self.server._server.should_exit)
        finally:
            waiting.set(); thread.join(2)

    def test_retry_during_cancelled_download_is_explicit(self):
        started, finish = threading.Event(), threading.Event()
        def download():
            started.set(); finish.wait(3)
            return self.archive()
        with patch.object(self.server, "_port_open", return_value=False), patch.object(self.server, "_download", side_effect=download):
            self.server.prepare()
            self.assertTrue(started.wait(2))
            self.server.stop()
            try:
                with self.assertRaisesRegex(mod.PromptServerError, "종료 중"):
                    self.server.prepare()
                self.assertFalse(self.server.source.exists())
            finally:
                finish.set(); self.wait_worker()

    def test_private_imports_do_not_shadow_host_modules_or_change_path(self):
        source = self.root / "private"; source.mkdir()
        sources = {
            "app_paths": "from pathlib import Path\nDATA_DIR = Path('/wrong')\n",
            "database": "from app_paths import DATA_DIR\nVALUE = 'private'\n",
            "app": "import database\nfrom database import DATA_DIR\nVALUE = database.VALUE\n",
        }
        for name, content in sources.items():
            (source / (name + ".py")).write_text(content)
        host = types.ModuleType("database"); host.VALUE = "host"
        before = list(sys.path)
        loader = mod._PrivateModules(source, self.root / "userdata", {"files": {n + ".py": {} for n in sources}}, lambda: self.fail("token read"))
        self.addCleanup(loader.close)
        with patch.dict(sys.modules, {"database": host}):
            app = loader.load("app")
            self.assertEqual(app.VALUE, "private")
            self.assertEqual(app.DATA_DIR, self.root / "userdata")
            self.assertIs(sys.modules["database"], host)
            self.assertEqual(sys.path, before)
        prefix = loader.prefix
        loader.close()
        self.assertFalse(any(k.startswith(prefix) for k in sys.modules))

    def test_settings_ignore_legacy_token_and_hostile_url(self):
        original = {"token": "LEGACY-SECRET", "base_url": "https://hostile.example"}
        fake = types.SimpleNamespace(_read_disk=lambda: dict(original), _normalize=lambda x: dict(x))
        writes = []
        fake._write_settings = writes.append
        mod._PrivateModules._adapt_settings(fake)
        self.assertEqual(fake.load_settings()["token"], "")
        self.assertEqual(fake.load_settings()["base_url"], "https://text.novelai.net")
        self.assertNotIn("LEGACY-SECRET", json.dumps(fake.public_settings()))
        with self.assertRaises(ValueError):
            fake.save_settings({"token": "NEW-SECRET"})
        self.assertEqual(writes, [])
        self.assertEqual(original["token"], "LEGACY-SECRET")

    def test_novelai_endpoint_and_redirects_are_rejected(self):
        with patch.object(mod.urllib.request, "build_opener") as opener:
            for url in ("http://text.novelai.net/oa/v1/completions", "https://evil.test/oa/v1/completions", "https://text.novelai.net@evil.test/oa/v1/completions"):
                with self.assertRaises(OSError):
                    mod._novelai_urlopen(urllib.request.Request(url))
            opener.assert_not_called()
        with self.assertRaises(mod.urllib.error.HTTPError):
            mod._NoRedirect().redirect_request(urllib.request.Request("https://text.novelai.net/oa/v1/completions"), None, 302, "redirect", {}, "https://evil.test")

    def test_app_has_only_comic_routes_and_rejects_web_origin(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        calls = []
        @app.get("/api/novelai/settings")
        def secrets(): return {"token": "secret"}
        @app.get("/api/comics/progress")
        def progress(): return {"active": False}
        @app.post("/api/comics/generate")
        def generate(): calls.append(1); return {}
        mod.PromptServer._restrict_app(app)
        with TestClient(app, base_url=mod.URL) as client:
            self.assertEqual(client.get("/api/comics/progress").status_code, 200)
            self.assertEqual(client.get("/api/novelai/settings").status_code, 404)
            self.assertEqual(client.post("/api/comics/generate", headers={"Origin": "https://evil.test"}).status_code, 403)
            self.assertEqual(client.post("/api/comics/generate", headers={"Host": "evil.test"}).status_code, 403)
            self.assertEqual(calls, [])
            paths = client.get("/openapi.json").json()["paths"]
            self.assertNotIn("/api/novelai/settings", paths)


@unittest.skipUnless(os.environ.get("NAIA_PROMPTSERVER_TEST_FIXTURE"), "Optional exact upstream source fixture")
class PinnedUpstreamIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.token_reads = 0
        def token():
            self.token_reads += 1
            return "FAKE-UNIT-TEST-TOKEN"
        self.server = mod.PromptServer(self.root, token)
        self.addCleanup(self.server.close)
        files = json.loads(Path(os.environ["NAIA_PROMPTSERVER_TEST_FIXTURE"]).read_text())
        out = io.BytesIO()
        prefix = "PromptServer-" + self.server.manifest["commit"] + "/"
        with zipfile.ZipFile(out, "w") as z:
            for path, content in files.items():
                z.writestr(prefix + path, content.encode("utf-8"))
        self.raw = out.getvalue()
        self.server._install(self.raw, threading.Event())
        self.assertTrue(self.server._verified())

    def test_original_app_lifespan_database_and_fake_model_generation(self):
        from fastapi.testclient import TestClient
        modules = mod._PrivateModules(self.server.source, self.server.data,
                                      self.server.manifest, self.server.token_provider)
        self.addCleanup(modules.close)
        with patch.object(mod, "_novelai_urlopen", side_effect=AssertionError("Real NAI request forbidden")):
            app = modules.load("app").app
            self.server._restrict_app(app, modules.load("api_routes").router.routes)
            service = modules.load("comic_auto_service")
            client_module = modules.load("novelai_client")
            plan = {"title": "offline test", "preset_id": "comic_auto", "width": 1024, "height": 1024,
                    "page_count": 1, "male_count": 0, "female_count": 0, "character_prompts": {},
                    "pages": [{"page_number": 1}], "source": "ai_import"}
            with TestClient(app, base_url=mod.URL) as client:
                self.assertEqual(self.token_reads, 0)
                self.assertTrue((self.server.data / "scenarios.db").exists())
                self.assertFalse((self.server.source / "data" / "scenarios.db").exists())
                self.assertEqual(modules.load("database").DB_PATH, self.server.data / "scenarios.db")
                self.assertEqual(client.get("/api/comics/progress").status_code, 200)
                schema = client.get("/openapi.json").json()
                self.assertIn("/api/comics/generate/stream", schema["paths"])
                self.assertNotIn("/api/novelai/settings", schema["paths"])
                self.assertEqual(self.token_reads, 0)
                with patch.object(client_module.NovelAIClient, "chat", side_effect=["test story", '{"pages": []}']), patch.object(service, "build_comic_plan", return_value=plan):
                    response = client.post("/api/comics/generate/stream", json={"source_prompt": "an empty landscape", "male_count": 0, "female_count": 0})
                events = [json.loads(line) for line in response.text.splitlines()]
                self.assertEqual(events[-1]["type"], "complete", events)
                self.assertEqual(self.token_reads, 1)
                saved_id = events[-1]["plan"]["id"]
                self.assertEqual(client.get("/api/comics/random?mark_used=false").json()["id"], saved_id)
                self.assertEqual(client.post(f"/api/comics/{saved_id}/use").status_code, 200)
                self.assertNotIn("FAKE-UNIT-TEST-TOKEN", response.text)
                self.assertFalse(client.get("/api/comics/progress").json()["active"])
            modules.close()
            with self.assertRaises(OSError):
                service.load_settings()
            with self.assertRaises(OSError):
                client_module.urllib.request.urlopen(urllib.request.Request("https://text.novelai.net/oa/v1/completions"))
        self.assertEqual(self.token_reads, 1)

    def test_real_loopback_start_stop_restart_without_token_read(self):
        if mod.PromptServer._port_open():
            self.skipTest("8765 is already owned by another process")
        def wait_ready(server):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                status = server.status()
                if status["state"] in ("ready", "error"):
                    self.assertEqual(status["state"], "ready", status)
                    return
                time.sleep(.05)
            self.fail("Server startup timed out")
        with patch.object(mod, "_novelai_urlopen", side_effect=AssertionError("Real NAI request forbidden")), patch.object(self.server, "_download", side_effect=AssertionError("Unexpected download")):
            self.server.start_installed(); wait_ready(self.server)
            self.assertTrue(self.server._probe())
            marker = self.server.data / "user-note.txt"; marker.write_text("preserve")
            self.server.close()
            self.assertFalse(self.server._server_thread.is_alive())
            replacement = mod.PromptServer(self.root, self.server.token_provider)
            self.addCleanup(replacement.close)
            replacement.start_installed(); wait_ready(replacement)
            self.assertEqual(marker.read_text(), "preserve")
            self.assertEqual(self.token_reads, 0)
            replacement.close()
            self.assertFalse(replacement._server_thread.is_alive())


if __name__ == "__main__":
    unittest.main()
