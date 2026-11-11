"""Offline regressions through NAIA's real extension manager and serving chain.

Fixtures are the shipped source packages, not synthetic replacements. No model,
OAuth, relay, thumbnail download or generation request is allowed in this suite.
A minimal host supplies temporary paths and event stubs. The serving seam reads
actual web assets, and ExtensionManager plus extension controllers are real.
Upstream is pinned to 6e97c7f4f37e495ad21657ed29d28dd9993a808e.
This suite does not emulate Android/WebView or prove tablet rendering.
"""
from __future__ import annotations

import atexit
import faulthandler
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import urllib.request
import zipfile

_DEFAULT_ROOT = Path(__file__).resolve().parents[1]
if _DEFAULT_ROOT.name == "android-tools-repair":
    _DEFAULT_ROOT = _DEFAULT_ROOT.parent
ROOT = Path(os.environ.get("NAIA_ANDROID_PORT_ROOT", _DEFAULT_ROOT)).resolve()
SOURCE = ROOT / "source/NAIA2.0"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SOURCE))
from core.extension_runtime import ExtensionContext, ExtensionManager
from fastapi.responses import FileResponse

IDS = {
    "tools": "naia_zz_android_tools_v1",
    "chatgpt": "naia_chatgpt_connect_v2",
    "storage": "naia_storage_picker_v1",
}
PREFIXES = ("_naia_android_tools_", "_naia_chatgpt_connect_", "_naia_storage_picker_", "naia_ext_naia_")


def text(response):
    body = getattr(response, "body", None)
    return body.decode() if isinstance(body, bytes) else Path(response.path).read_text()


class Host:
    def __init__(self, root):
        self.repo_root = SOURCE
        self.runtime_paths = types.SimpleNamespace(extensions_dir=root / "extensions", config_dir=root / "config")
        self.runtime_paths.extensions_dir.mkdir()
        self.runtime_paths.config_dir.mkdir()
        self.output = root / "output"
        self.output.mkdir()
        self.save_directory_state = {"base_path": str(self.output), "control_allowed": True, "browse_allowed": True}
        self.is_generating = False
        self.headless_generation_runner_active = False
        self.pipeline_hooks = {}
        self.headless_main_loop = None

    def _output_root(self):
        return self.output

    def publish(self, *args, **kwargs):
        pass

    def subscribe(self, *args, **kwargs):
        pass

    def unsubscribe(self, *args, **kwargs):
        pass


class CombinedReloadTests(unittest.TestCase):
    def setUp(self):
        # The real manager deliberately has a non-reentrant Lock. Bound a
        # regression deadlock so CI emits the blocking stack instead of hangs.
        faulthandler.dump_traceback_later(45, exit=True)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.host = Host(self.root)
        self.controllers = []
        self.network = patch.object(urllib.request.OpenerDirector, "open", side_effect=AssertionError("External network forbidden"))
        self.network.start()
        self.route = types.ModuleType("app.backend.server.web_shell_routes")
        self.route._web_file = lambda p, m: FileResponse(p, media_type=m)
        self.route._web_asset = lambda root, asset, default_media_type="application/octet-stream": FileResponse(root / asset, media_type=default_media_type)
        self.routes = patch.dict(sys.modules, {"app.backend.server.web_shell_routes": self.route})
        self.routes.start()
        self.manager = ExtensionManager(self.host)
        self.host.extension_manager = self.manager
        # Dummy values deliberately remain offline and never contain credentials.
        auth_dir = self.host.runtime_paths.config_dir / "chatgpt-auth"
        auth_dir.mkdir()
        self.auth_path = auth_dir / "chatgpt-credentials.json"
        self.auth_path.write_text(json.dumps({"version": 1, "ext_agent_host_id": "urn:uuid:offline-regression", "active": None, "accounts": {}}))
        self.auth_bytes = self.auth_path.read_bytes()
        pair_dir = self.host.runtime_paths.config_dir / "android-tools"
        pair_dir.mkdir()
        self.pair_path = pair_dir / "pairing.json"
        self.pair_path.write_text(json.dumps({"relay_url": "https://naia-tablet-link.ugi8296.chatgpt.site",
                                             "relay_access_token": "offline-regression-token",
                                             "device_id": "offline-regression", "device_token": "offline-regression-token"}))
        self.pair_bytes = self.pair_path.read_bytes()

    def tearDown(self):
        faulthandler.cancel_dump_traceback_later()
        for name, module in list(sys.modules.items()):
            if name.startswith(PREFIXES):
                controller = getattr(module, "_controller", None)
                if controller is not None:
                    self.controllers.append(controller)
                cleanup = getattr(module, "_ACTIVE_CLEANUP", None)
                if callable(cleanup):
                    cleanup()
        for controller in reversed(self.controllers):
            controller.close()
            atexit.unregister(controller.close)
        for name in list(sys.modules):
            if name.startswith(PREFIXES):
                del sys.modules[name]
        self.routes.stop()
        self.network.stop()
        self.tmp.cleanup()

    def install(self, kind, *, original=False, tool_version="0.2.0"):
        target = self.host.runtime_paths.extensions_dir / IDS[kind]
        if target.exists():
            shutil.rmtree(target)
        target.mkdir()
        if original:
            archive = FIXTURES / (f"original-tools-{tool_version}.zip" if kind == "tools" else "original-legacy.zip")
            with zipfile.ZipFile(archive) as zipped:
                for name in zipped.namelist():
                    if kind == "tools":
                        relative = name.partition("/")[2] if tool_version == "0.1.0" else name
                    elif name.startswith(kind + "/"):
                        relative = name[len(kind) + 1:]
                    else:
                        continue
                    if not relative or name.endswith("/"):
                        continue
                    out = target / relative
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(zipped.read(name))
        else:
            source = ROOT / ("android-tools" if kind == "tools" else "compat-repair-source/" + kind)
            shutil.copytree(source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "tests", "*.pyc"))
        (target / "settings.json").write_text(json.dumps({"plugin_enabled": False, "enabled": False, "low_memory": True,
                                                         "allow_screen": False, "user_sentinel": "preserve"}))
        return target

    def boot(self, kinds, *, extra_ids=()):
        # A real blocked legacy directory must remain discoverable: the host
        # intentionally normalizes away IDs which have no installed record.
        blocked = self.host.runtime_paths.extensions_dir / "naia_chatgpt_connect_v1"
        blocked.mkdir()
        with zipfile.ZipFile(FIXTURES / "original-legacy.zip") as zipped:
            for name in zipped.namelist():
                if name.startswith("chatgpt/") and not name.endswith("/"):
                    path = blocked / name[len("chatgpt/"):]
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(zipped.read(name))
        manifest = json.loads((blocked / "extension.json").read_text())
        manifest["id"] = "naia_chatgpt_connect_v1"
        (blocked / "extension.json").write_text(json.dumps(manifest))
        config = {"approved": [IDS[kind] for kind in kinds] + list(extra_ids), "blocked": ["naia_chatgpt_connect_v1"], "inactive": [], "disarmed": []}
        (self.host.runtime_paths.config_dir / "extensions.json").write_text(json.dumps(config))
        self.manager.load_all()
        return {kind: self.manager._find(IDS[kind]) for kind in kinds}

    def controller(self, kind):
        controller = getattr(sys.modules.get("naia_ext_" + IDS[kind]), "_controller", None)
        if controller is not None:
            self.controllers.append(controller)
        return controller

    def retry(self, kind):
        old = self.controller(kind)
        record = self.manager._find(IDS[kind])
        record.status = "error"
        record.error = "offline test: retry the previously loaded entry"
        self.manager.load_by_key("retry:" + IDS[kind])
        return old, record

    def assert_loaded(self, record):
        self.assertEqual(record.status, "loaded", record.error)

    def assert_composed_ui(self):
        root = SOURCE / "app/web/remote"
        index = text(self.route._web_file(root / "index.html", "text/html"))
        self.assertEqual(index.count('id="naiaChatgptSettings"'), 1)
        self.assertEqual(index.count('id="naiaAndroidToolsSettings"'), 1)
        folder = text(self.route._web_asset(root / "js", "features/saveDirectoryPanel.mjs", default_media_type="text/javascript"))
        self.assertEqual(folder.count('id="naiaAccessibleFolderPick"'), 1)
        artist = text(self.route._web_asset(root / "js", "features/artistThumbTab.mjs", default_media_type="text/javascript"))
        self.assertEqual(artist.count("// naia-artist-memory-v1"), 1)
        self.assertEqual(artist.count("// naia-android-folder-v1:"), 1)

    def test_original_chatgpt_retry_reproduces_already_installed(self):
        self.install("chatgpt", original=True)
        record = self.boot(["chatgpt"])["chatgpt"]
        self.assert_loaded(record)
        old, record = self.retry("chatgpt")
        self.assertEqual(record.status, "error")
        self.assertIn("ChatGPT bridge is already installed", record.error)
        self.assertFalse(old._closed)

    def test_original_storage_retry_reproduces_screen_revision_error(self):
        self.install("storage", original=True)
        record = self.boot(["storage"])["storage"]
        self.assert_loaded(record)
        old, record = self.retry("storage")
        self.assertEqual(record.status, "error")
        self.assertIn("저장 폴더 화면 버전이 달라", record.error)
        self.assertFalse(old._closed)

    def test_original_tools_warm_upgrade_reproduces_unexpected_screen_service(self):
        self.install("tools", original=True, tool_version="0.1.0")
        record = self.boot(["tools"])["tools"]
        self.assert_loaded(record)
        old = self.controller("tools")
        # Match the updater: stop resources and replace files, keep interpreter.
        old.close()
        self.install("tools", original=True, tool_version="0.2.0")
        _, record = self.retry("tools")
        self.assertEqual(record.status, "error")
        self.assertIn("unexpected keyword argument 'screen_service'", record.error)

    def test_repaired_combination_retries_without_duplicate_ui_or_data_loss(self):
        for kind in IDS:
            self.install(kind)
        records = self.boot(list(IDS))
        for record in records.values():
            self.assert_loaded(record)
        self.assert_composed_ui()
        # Exercise both wrapper orders; reload ChatGPT last used to overwrite
        # the composed index and drop Android Tools settings entirely.
        for kind in ["storage", "chatgpt", "tools", "chatgpt", "storage", "tools"]:
            old, record = self.retry(kind)
            self.assert_loaded(record)
            self.assertTrue(old._closed, kind)
            self.assert_composed_ui()
        self.assertEqual(self.auth_path.read_bytes(), self.auth_bytes)
        self.assertEqual(self.pair_path.read_bytes(), self.pair_bytes)
        for kind in IDS:
            settings = json.loads((self.host.runtime_paths.extensions_dir / IDS[kind] / "settings.json").read_text())
            self.assertEqual(settings["user_sentinel"], "preserve")
            self.assertIs(settings["allow_screen"], False)
        config = json.loads((self.host.runtime_paths.config_dir / "extensions.json").read_text())
        self.assertIn("naia_chatgpt_connect_v1", config["blocked"])

    def test_repaired_tools_replace_old_helpers_in_same_interpreter(self):
        self.install("tools", original=True, tool_version="0.1.0")
        record = self.boot(["tools"])["tools"]
        self.assert_loaded(record)
        old = self.controller("tools")
        self.install("tools")
        _, record = self.retry("tools")
        self.assert_loaded(record)
        current = self.controller("tools")
        self.assertIsNot(current, old)
        self.assertTrue(old._closed)
        self.assertIsNotNone(current.screen)
        self.assertIs(current.bridge.screen_service, current.screen)

    def test_repaired_sources_recover_failed_original_loader_state(self):
        for kind in IDS:
            self.install(kind, original=True, tool_version="0.1.0")
        records = self.boot(list(IDS))
        for record in records.values():
            self.assert_loaded(record)
        previous = {kind: self.controller(kind) for kind in IDS}
        for kind in ("chatgpt", "storage"):
            _, record = self.retry(kind)
            self.assertEqual(record.status, "error")
        previous["tools"].close()
        self.install("tools", original=True)
        _, record = self.retry("tools")
        self.assertIn("unexpected keyword argument 'screen_service'", record.error)
        # Replace the reviewed code with the fixed versions while preserving
        # exactly the poisoned interpreter and orphan serving wrappers above.
        for kind in IDS:
            self.install(kind)
        for kind in ("chatgpt", "storage", "tools"):
            _, record = self.retry(kind)
            self.assert_loaded(record)
            self.assertTrue(previous[kind]._closed, kind)
        self.assert_composed_ui()
        self.assertEqual(self.auth_path.read_bytes(), self.auth_bytes)
        self.assertEqual(self.pair_path.read_bytes(), self.pair_bytes)

    def test_packaged_repair_updates_and_reloads_real_failed_extensions(self):
        repair_id = "naia_android_tools_repair_v2"
        old_updater_id = "naia_android_tools_update_020"
        repair_path = self.host.runtime_paths.extensions_dir / repair_id
        shutil.copytree(ROOT / "android-tools-repair", repair_path,
                        ignore=shutil.ignore_patterns("__pycache__", "tests", "*.pyc"))
        old_updater_path = self.host.runtime_paths.extensions_dir / old_updater_id
        shutil.copytree(ROOT / "android-tools-updater", old_updater_path,
                        ignore=shutil.ignore_patterns("__pycache__", "tests", "*.pyc"))
        old_updater_source = (old_updater_path / "main.py").read_bytes()
        for kind in IDS:
            self.install(kind, original=True, tool_version="0.1.0")
        records = self.boot(list(IDS), extra_ids=[repair_id, old_updater_id])
        for record in records.values():
            self.assert_loaded(record)
        repair_record = self.manager._find(repair_id)
        self.assert_loaded(repair_record)
        self.assert_loaded(self.manager._find(old_updater_id))
        previous = {kind: self.controller(kind) for kind in IDS}
        for kind in ("chatgpt", "storage"):
            _, record = self.retry(kind)
            self.assertEqual(record.status, "error")
        previous["tools"].close()
        self.install("tools", original=True)
        _, record = self.retry("tools")
        self.assertIn("unexpected keyword argument 'screen_service'", record.error)
        settings = {kind: (record.directory / "settings.json").read_bytes() for kind, record in records.items()}
        config_path = self.host.runtime_paths.config_dir / "extensions.json"
        config_bytes = config_path.read_bytes()
        repair = sys.modules["naia_ext_" + repair_id]
        ctx = ExtensionContext(repair_record, self.host)
        result = repair.perform_update(ctx)
        self.assertEqual(set(result["updated_ids"]), set(IDS.values()))
        self.assertEqual(config_path.read_bytes(), config_bytes)
        outcomes = repair.reload_repaired(ctx)
        self.assertEqual({item["id"]: item["status"] for item in outcomes}, {ext_id: "loaded" for ext_id in IDS.values()})
        for kind, record in records.items():
            self.assert_loaded(record)
            self.assertTrue(previous[kind]._closed, kind)
            manifest = json.loads((record.directory / "extension.json").read_text())
            self.assertEqual(record.version, manifest["version"])
            self.assertEqual((record.directory / "settings.json").read_bytes(), settings[kind])
        self.assert_composed_ui()
        self.assertEqual(config_path.read_bytes(), config_bytes)
        self.assertEqual(self.auth_path.read_bytes(), self.auth_bytes)
        self.assertEqual(self.pair_path.read_bytes(), self.pair_bytes)
        self.assertTrue(self.manager._find("naia_chatgpt_connect_v1").blocked)
        self.assertNotEqual(self.manager._find("naia_chatgpt_connect_v1").status, "loaded")
        self.assert_loaded(self.manager._find(old_updater_id))
        self.assertEqual((old_updater_path / "main.py").read_bytes(), old_updater_source)
        # Reopening/retrying the helper creates a fresh module. An already
        # applied payload must still offer a working explicit reload action.
        previous = {kind: self.controller(kind) for kind in IDS}
        repair_record.status = "error"
        self.manager.load_by_key("retry:" + repair_id)
        self.assert_loaded(repair_record)
        fresh_repair = sys.modules["naia_ext_" + repair_id]
        self.assertIsNot(fresh_repair, repair)
        ctx = ExtensionContext(repair_record, self.host)
        self.assertTrue(fresh_repair.perform_update(ctx)["already_updated"])
        outcomes = fresh_repair.reload_repaired(ctx)
        self.assertEqual({item["id"]: item["status"] for item in outcomes}, {ext_id: "loaded" for ext_id in IDS.values()})
        for kind in IDS:
            self.assertTrue(previous[kind]._closed, kind)
        self.assert_composed_ui()
        self.assertEqual(config_path.read_bytes(), config_bytes)
        self.assertEqual(self.auth_path.read_bytes(), self.auth_bytes)
        self.assertEqual(self.pair_path.read_bytes(), self.pair_bytes)


if __name__ == "__main__":
    unittest.main()
