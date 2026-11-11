"""Warm-process upgrades must not reuse helper modules or bytecode from 0.1.0."""
import importlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import py_compile
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PREFIX = "_naia_android_tools_runtime_"
LEGACY = "_naia_android_tools_v1"


class ModuleLoaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.extension = self.root / "extension"
        shutil.copytree(ROOT, self.extension, ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc"))
        self.original_modules = {name: value for name, value in sys.modules.items()
                                 if name.startswith(PREFIX) or name == LEGACY or name.startswith(LEGACY + ".")}
        for name in self.original_modules:
            del sys.modules[name]
        self.entries = []

    def tearDown(self):
        for entry in self.entries:
            controller = getattr(entry, "_controller", None)
            if controller is not None:
                controller.close()
        for name in list(sys.modules):
            if name.startswith(PREFIX) or name == LEGACY or name.startswith(LEGACY + "."):
                del sys.modules[name]
        sys.modules.update(self.original_modules)
        self.temp.cleanup()

    def entry(self):
        # The host creates a new entry object on retry while helpers remain in
        # sys.modules. Use source compilation here so tests control pyc separately.
        path = self.extension / "main.py"
        spec = importlib.util.spec_from_file_location("loader_fixture_entry", path)
        result = importlib.util.module_from_spec(spec)
        exec(compile(path.read_bytes(), str(path), "exec"), result.__dict__)
        self.entries.append(result)
        return result

    def legacy_package(self):
        package = types.ModuleType(LEGACY)
        package.__path__ = [str(self.extension)]
        package.__package__ = LEGACY
        sys.modules[LEGACY] = package
        return package

    def test_old_fixed_namespace_reproduces_report_then_new_loader_uses_new_source(self):
        path = self.extension / "naia_device_bridge.py"
        replacement = path.read_bytes()
        path.write_text("class DeviceBridge:\n    def __init__(self, ctx, settings_getter, *, state_root=None, allowed_origins=None): pass\n")
        self.legacy_package()
        stale = importlib.import_module(LEGACY + ".naia_device_bridge")
        path.write_bytes(replacement)
        self.assertIs(importlib.import_module(LEGACY + ".naia_device_bridge"), stale)
        with self.assertRaisesRegex(TypeError, "unexpected keyword argument 'screen_service'"):
            stale.DeviceBridge(None, None, screen_service=object())
        fresh = self.entry()._module("naia_device_bridge")
        self.assertIn("screen_service", inspect.signature(fresh.DeviceBridge).parameters)
        self.assertIsNot(fresh, stale)
        self.assertEqual(Path(fresh.__file__), path)

    def test_same_size_same_timestamp_stale_pyc_is_not_executed(self):
        path = self.extension / "naia_folder_open.py"
        path.write_text("VALUE = 'old'\n")
        stamp = path.stat().st_mtime_ns
        py_compile.compile(str(path), doraise=True)
        path.write_text("VALUE = 'new'\n")
        os.utime(path, ns=(stamp, stamp))
        self.legacy_package()
        # A fresh standard import demonstrates that the .pyc still validates.
        standard = importlib.import_module(LEGACY + ".naia_folder_open")
        self.assertEqual(standard.VALUE, "old")
        fresh = self.entry()._module("naia_folder_open")
        self.assertEqual(fresh.VALUE, "new")

    def test_changed_source_and_assets_get_new_runtime_namespaces(self):
        entry = self.entry()
        first = entry._module("naia_folder_open")
        self.assertIs(entry._module("naia_folder_open"), first)
        (self.extension / "naia_folder_open.py").write_text("VALUE = 'changed'\n")
        second = entry._module("naia_folder_open")
        self.assertEqual(second.VALUE, "changed")
        self.assertNotEqual(first.__package__, second.__package__)
        with (self.extension / "naia_screen_agent.js").open("a") as stream:
            stream.write("\n// changed UI contract\n")
        third = entry._module("naia_folder_open")
        self.assertNotEqual(second.__package__, third.__package__)

    def test_bundle_snapshot_cannot_mix_later_file_replacements(self):
        entry = self.entry()
        package = entry._runtime_package()
        path = self.extension / "naia_device_bridge.py"
        path.write_text("raise RuntimeError('partially replaced bundle')\n")
        pinned = entry._module("naia_device_bridge", package)
        self.assertIn("screen_service", inspect.signature(pinned.DeviceBridge).parameters)
        with self.assertRaisesRegex(RuntimeError, "partially replaced bundle"):
            entry._module("naia_device_bridge")

    def test_bad_module_load_does_not_leave_partial_module_cached(self):
        path = self.extension / "naia_folder_open.py"
        path.write_text("VALUE = 'incomplete'\nraise RuntimeError('broken helper')\n")
        entry = self.entry()
        package = entry._runtime_package()
        with self.assertRaisesRegex(RuntimeError, "broken helper"):
            entry._module("naia_folder_open", package)
        self.assertNotIn(package.__name__ + ".naia_folder_open", sys.modules)
        with self.assertRaises(ValueError):
            entry._module("../../other")

    def test_fresh_entry_retry_closes_previous_owner_without_changing_pairing_or_settings(self):
        files = {
            "naia_folder_open.py": "def open_native_folder(path): return {'opened':False}\n",
            "naia_artist_memory.py": "from types import SimpleNamespace\ndef install(settings_getter, state_root=None):\n return SimpleNamespace(attach=lambda x:None, close=lambda:None)\n",
            "naia_screen_service.py": "class ScreenService:\n def close(self): pass\n def status(self): return {'ready':False}\n",
            "naia_device_bridge.py": "class DeviceBridge:\n def __init__(self,ctx,settings_getter,*,state_root=None,allowed_origins=None,screen_service=None):\n  self.app=ctx._app_context\n  self.app.events.append('construct')\n def start(self): self.app.events.append('start')\n def close(self): self.app.events.append('close')\n",
            "naia_tools_ui.py": "def install_ui(controller,folder,memory):\n return lambda: controller.ctx._app_context.events.append('ui-close')\n",
            "naia_comic_compat.py": "from types import SimpleNamespace\ndef install(ctx): return SimpleNamespace(close=lambda:None,status=lambda:{})\n",
            "naia_prompt_server.py": "class PromptServer:\n def __init__(self,*args): pass\n def close(self): pass\n def status(self): return {'state':'not_installed'}\n",
            "naia_history_library.py": "class HistoryLibrary:\n def __init__(self,*args): pass\n def close(self): pass\n",
        }
        for name, text in files.items():
            (self.extension / name).write_text(text)
        first = self.entry()
        saved = {**first.DEFAULTS, "cache_mb": 16, "plugin_enabled": True, "allow_edit": True}
        config = self.root / "config"
        private = config / "android-tools"
        private.mkdir(parents=True)
        pair = {"relay_url": first.RELAY_URL, "device_id": "device_example",
                "device_token": "PRIVATE_DEVICE", "relay_access_token": "PRIVATE_ACCESS"}
        pairing = private / "pairing.json"
        pairing.write_text(json.dumps(pair))
        original_pair = pairing.read_bytes()
        writes = []
        app = types.SimpleNamespace(runtime_paths=types.SimpleNamespace(config_dir=config), events=[])
        ctx = types.SimpleNamespace(_app_context=app, ext_dir=self.extension,
            _record=types.SimpleNamespace(status="loaded", enabled=True, blocked=False, armed=True),
            load_settings=lambda defaults: dict(saved), save_settings=lambda data: writes.append(data),
            register_panel=lambda *a, **kw: None, log=lambda message: None)
        first.register(ctx)
        old = first._controller
        second = self.entry()
        self.assertIsNone(second._controller)
        second.register(ctx)
        self.assertTrue(old._closed)
        self.assertIs(getattr(app, second._CONTROLLER_ATTRIBUTE), second._controller)
        self.assertEqual(app.events, ["construct", "start", "close", "ui-close", "construct", "start"])
        self.assertEqual(pairing.read_bytes(), original_pair)
        self.assertEqual(writes, [])
        second._controller.close()
        self.assertFalse(hasattr(app, second._CONTROLLER_ATTRIBUTE))

    def test_incompatible_on_disk_bridge_is_rejected_before_any_hooks_install(self):
        (self.extension / "naia_device_bridge.py").write_text("class DeviceBridge:\n def __init__(self,ctx,settings_getter): pass\n")
        entry = self.entry()
        ctx = types.SimpleNamespace(_app_context=types.SimpleNamespace())
        with self.assertRaisesRegex(RuntimeError, "실행 파일 버전이 일치하지"):
            entry.register(ctx)
        self.assertIsNone(entry._controller)

    def test_orphan_recovery_closes_only_our_controller_for_this_app(self):
        entry = self.entry()
        app, other_app = types.SimpleNamespace(), types.SimpleNamespace()
        calls = []
        controllers = {}
        for label, owner in (("ours", app), ("other_app", other_app), ("other_extension", app)):
            controllers[label] = types.SimpleNamespace(
                ctx=types.SimpleNamespace(ext_dir=self.extension, _app_context=owner),
                close=lambda label=label: calls.append(label))
        source = """class PanelServer:
    def __init__(self, controller): self.controller = controller
def wrap(previous_file, controller):
    server = PanelServer(controller)
    def web_file(*args):
        server.controller
        return previous_file(*args)
    return web_file
"""
        ours = types.ModuleType(LEGACY + ".naia_tools_ui")
        foreign = types.ModuleType("another_extension.naia_tools_ui")
        for module in (ours, foreign):
            exec(compile(source, str(self.extension / "naia_tools_ui.py"), "exec"), module.__dict__)
        wrapper = lambda *args: None
        wrapper = ours.wrap(wrapper, controllers["other_app"])
        wrapper = ours.wrap(wrapper, controllers["ours"])
        wrapper = foreign.wrap(wrapper, controllers["other_extension"])
        shell = types.ModuleType("app.backend.server.web_shell_routes")
        shell._web_file = wrapper
        with patch.dict(sys.modules, {shell.__name__: shell}):
            entry._retire_previous(types.SimpleNamespace(_app_context=app))
        self.assertEqual(calls, ["ours"])


if __name__ == "__main__":
    unittest.main()
