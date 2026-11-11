"""Offline lifecycle/cache regressions for the original legacy extension repairs."""
import importlib.util
import os
from pathlib import Path
import py_compile
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

PORT = Path(os.environ.get("NAIA_ANDROID_PORT_ROOT", Path(__file__).resolve().parents[2])).resolve()
ROOT = PORT / "compat-repair-source"
WEB = PORT / "source/NAIA2.0/app/web/remote"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    return module


class LegacyRepairTests(unittest.TestCase):
    def setUp(self):
        self.chatgpt = load(ROOT / "chatgpt/main.py", "test_chatgpt_entry")
        self.storage = load(ROOT / "storage/main.py", "test_storage_entry")
        self.cg_ui = load(ROOT / "chatgpt/naia_chatgpt_ui.py", "test_chatgpt_ui")
        self.st_ui = load(ROOT / "storage/naia_storage_ui.py", "test_storage_ui")

    def test_loader_replaces_cached_module_even_with_same_size_and_mtime(self):
        for entry in (self.chatgpt, self.storage):
            with self.subTest(package=entry._PACKAGE), tempfile.TemporaryDirectory() as temp, patch.dict(sys.modules):
                directory = Path(temp)
                entry.__file__ = str(directory / "main.py")
                package = entry._PACKAGE
                for key in list(sys.modules):
                    if key == package or key.startswith(package + "."):
                        del sys.modules[key]
                helper = directory / "helper.py"
                helper.write_text("VALUE = 'old'\n")
                py_compile.compile(str(helper), doraise=True)
                timestamp = helper.stat().st_mtime_ns
                first = entry._module("helper")
                self.assertEqual(first.VALUE, "old")
                helper.write_text("VALUE = 'new'\n")
                os.utime(helper, ns=(timestamp, timestamp))
                second = entry._module("helper")
                self.assertEqual(second.VALUE, "new")
                self.assertIsNot(first, second)
                self.assertIs(second, entry._module("helper"))

    def test_failed_source_load_restores_previous_module(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(sys.modules):
            entry = self.storage
            entry.__file__ = str(Path(temp) / "main.py")
            for key in list(sys.modules):
                if key.startswith(entry._PACKAGE):
                    del sys.modules[key]
            helper = Path(temp) / "helper.py"
            helper.write_text("VALUE = 3\n")
            previous = entry._module("helper")
            helper.write_text("raise RuntimeError('broken update')\n")
            with self.assertRaisesRegex(RuntimeError, "broken update"):
                entry._module("helper")
            self.assertIs(sys.modules[entry._PACKAGE + ".helper"], previous)

    def test_existing_package_controller_is_retired_after_entry_replacement(self):
        for entry in (self.chatgpt, self.storage):
            with self.subTest(package=entry._PACKAGE), patch.dict(sys.modules):
                directory = Path(entry.__file__).resolve().parent
                controller = types.SimpleNamespace(ctx=types.SimpleNamespace(ext_dir=directory), close=Mock())
                package = types.SimpleNamespace(__path__=[str(directory)], _controller=controller)
                sys.modules[entry._PACKAGE] = package
                entry._controller = None
                entry._retire_previous()
                controller.close.assert_called_once()
                self.assertIsNone(package._controller)

    def old_version(self, *, blocked, enabled, orphan=False):
        old_id = "naia_chatgpt_connect_v1"
        package_name = "_" + old_id
        directory = ROOT / "old-test-install"
        record = types.SimpleNamespace(ext_id=old_id, directory=directory, blocked=blocked, enabled=enabled)
        context = types.SimpleNamespace(_record=record, ext_dir=directory)
        controller = types.SimpleNamespace(ctx=context, close=Mock())
        package = types.SimpleNamespace(__path__=[str(directory)])
        sys.modules[package_name] = package
        sys.modules["naia_ext_" + old_id] = types.SimpleNamespace(_controller=None if orphan else controller)
        if orphan:
            server_type = type("PanelServer", (), {"__module__": package_name + ".naia_chatgpt_ui"})
            server = server_type()
            server.controller = controller
            def web_file(*args):
                return server.controller
            web_file.__module__ = package_name + ".naia_chatgpt_ui"
            sys.modules["app.backend.server.web_shell_routes"] = types.SimpleNamespace(_web_file=web_file)
        cleanup = Mock()
        sys.modules[package_name + ".naia_chatgpt_bridge"] = types.SimpleNamespace(_ACTIVE_CLEANUP=cleanup)
        ctx = types.SimpleNamespace(_app_context=types.SimpleNamespace(extension_manager=types.SimpleNamespace(records=[record])))
        return ctx, controller, record, cleanup

    def test_blocked_v1_retires_orphan_controller_without_unblocking(self):
        with patch.dict(sys.modules):
            ctx, controller, record, cleanup = self.old_version(blocked=True, enabled=True, orphan=True)
            self.chatgpt._close_disabled_previous_version(ctx)
            controller.close.assert_called_once()
            cleanup.assert_called_once()
            self.assertIs(record.blocked, True)
            self.assertIs(record.enabled, True)

    def test_disabled_v1_retires_without_changing_flags(self):
        with patch.dict(sys.modules):
            ctx, controller, record, cleanup = self.old_version(blocked=False, enabled=False)
            self.chatgpt._close_disabled_previous_version(ctx)
            controller.close.assert_called_once()
            self.assertIs(record.blocked, False)
            self.assertIs(record.enabled, False)

    def test_active_v1_is_not_retired(self):
        with patch.dict(sys.modules):
            ctx, controller, record, cleanup = self.old_version(blocked=False, enabled=True)
            with self.assertRaisesRegex(RuntimeError, "이전 ChatGPT"):
                self.chatgpt._close_disabled_previous_version(ctx)
            controller.close.assert_not_called()
            cleanup.assert_not_called()

    def test_features_are_idempotent_on_actual_upstream_source(self):
        for name in ("llmSetupPanel.mjs", "boostV2Panel.mjs", "assistPanel.mjs"):
            source = (WEB / "js/features" / name).read_text()
            patched = self.cg_ui.patch_feature(name, source)
            self.assertEqual(self.cg_ui.patch_feature(name, patched), patched)
        index = self.cg_ui.patch_index((WEB / "index.html").read_text())
        self.assertEqual(self.cg_ui.patch_index(index), index)

    def test_storage_repeated_patch_replaces_stale_panel_url(self):
        source = (WEB / "js/features/saveDirectoryPanel.mjs").read_text()
        first = self.st_ui.patch_feature(source, "http://127.0.0.1:1000/#old")
        self.assertEqual(self.st_ui.patch_feature(first, "http://127.0.0.1:1000/#old"), first)
        second = self.st_ui.patch_feature(first, "http://127.0.0.1:2000/#new")
        self.assertNotIn("http://127.0.0.1:1000/#old", second)
        self.assertEqual(second.count("http://127.0.0.1:2000/#new"), 1)
        self.assertEqual(second.count('id="naiaAccessibleFolderPick"'), 1)


if __name__ == "__main__":
    unittest.main()
