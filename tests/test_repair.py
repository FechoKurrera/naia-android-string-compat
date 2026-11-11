import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("naia_repair_test", ROOT / "main.py")
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


def sha(data):
    return hashlib.sha256(data).hexdigest()


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "extensions"
        self.root.mkdir()
        self.config_dir = self.base / "config"
        self.config_dir.mkdir()
        self.directory = self.root / repair.REPAIR_ID
        self.directory.mkdir()
        self.target = self.root / repair.TARGET_ID
        self.target.mkdir()
        self.old_meta = {"id": repair.TARGET_ID, "version": "0.2.0", "entry": "main.py", "naia_ext_api": 1}
        (self.target / "extension.json").write_text(json.dumps(self.old_meta))
        (self.target / "main.py").write_text("OLD-CODE")
        self.settings = {"plugin_enabled": True, "allow_screen": True, "allow_control": False,
                         "allow_generate": True, "extra": {"saved": "value"}}
        (self.target / "settings.json").write_text(json.dumps(self.settings))
        (self.target / "unrelated-data.txt").write_text("KEEP")
        (self.target / "__pycache__").mkdir()
        (self.target / "__pycache__/main.cpython-312.pyc").write_bytes(b"STALE")
        self.private = self.config_dir / "android-tools"
        self.private.mkdir()
        (self.private / "pairing.json").write_bytes(b"PRESERVE-PAIR")
        (self.private / "device-bridge-journal.json").write_bytes(b"PRESERVE-JOURNAL")
        self.record = NS(ext_id=repair.TARGET_ID, directory=self.target, status="error", error="screen_service",
                         approved=True, enabled=True, armed=True, blocked=False)
        self.own_record = NS(ext_id=repair.REPAIR_ID, directory=self.directory, status="loaded", error="",
                             approved=True, enabled=True, armed=True, blocked=False)
        self.old_gpt = NS(ext_id="naia_chatgpt_connect_v1", status="loaded", approved=True,
                         enabled=True, armed=True, blocked=True)
        self.config = self.config_dir / "extensions.json"
        self.config_raw = json.dumps({"approved": [repair.TARGET_ID, repair.REPAIR_ID, "unrelated"],
                                      "inactive": ["unrelated"], "blocked": [self.old_gpt.ext_id],
                                      "future_setting": {"keep": True}}, indent=3).encode()
        self.config.write_bytes(self.config_raw)
        self.calls = []
        self.manager = NS(records=[self.record, self.own_record, self.old_gpt], _lock=threading.Lock(),
                          extensions_root=lambda: self.root, load_by_key=self.load_by_key)
        self.app = NS(extension_manager=self.manager, runtime_paths=NS(config_dir=self.config_dir),
                      is_generating=False, headless_generation_runner_active=False)
        self.ctx = NS(ext_dir=self.directory, _record=self.own_record, _app_context=self.app)
        self.closed = []
        old = NS(_controller=NS(ctx=NS(ext_dir=self.target), close=lambda: self.closed.append(True)))
        self.module_patch = patch.dict(sys.modules, {"naia_ext_" + repair.TARGET_ID: old})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        self.main_files = {"main.py": b'raise RuntimeError("replacement must not run during apply")',
                           "extension.json": json.dumps({**self.old_meta, "version": "0.2.1"}).encode()}
        self.legacy = {}
        self.release = self.make_release()
        self.state_patch = patch.object(repair, "_last_changed_ids", [])
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.verified_patch = patch.object(repair, "_last_verified_files", {})
        self.verified_patch.start()
        self.addCleanup(self.verified_patch.stop)

    def load_by_key(self, key):
        self.assertTrue(self.manager._lock.acquire(timeout=1), "load_by_key must acquire its own non-reentrant lock")
        try:
            self.calls.append(key)
            record = next(r for r in self.manager.records if r.ext_id == key.split(":", 1)[1])
            if record.enabled and record.approved and not record.blocked:
                record.status, record.error = "loaded", ""
        finally:
            self.manager._lock.release()

    def make_release(self, *, version="0.2.1"):
        files = {"main/" + key: value for key, value in self.main_files.items()}
        patches = {}
        for ext_id, data in self.legacy.items():
            files.update({"repairs/" + ext_id + "/" + name: content for name, content in data["new"].items()})
            patches[ext_id] = {"from_versions": [data["version"]], "files": {
                name: {"from_sha256": [sha(data["old"][name])], "sha256": sha(content)} for name, content in data["new"].items()}}
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
            for name, content in files.items():
                zipped.writestr(name, content)
        raw = archive.getvalue()
        meta = {"format": 1, "target_version": version, "archive": repair.ARCHIVE_NAME,
                "sha256": sha(raw), "main": {"id": repair.TARGET_ID, "files": {name: sha(content) for name, content in self.main_files.items()}},
                "repairs": patches}
        return {"manifest_bytes": json.dumps(meta).encode(), "archive_bytes": raw, "source_commit": "a" * 40}

    def add_legacy(self, ext_id="naia_chatgpt_connect_v2", version="0.1.1"):
        directory = self.root / ext_id
        directory.mkdir()
        old = {"main.py": b"OLD-LEGACY", "extension.json": json.dumps({"id": ext_id, "version": version, "entry": "main.py", "naia_ext_api": 1}).encode()}
        new = {"main.py": b"NEW-LEGACY", "extension.json": json.dumps({"id": ext_id, "version": "0.1.2", "entry": "main.py", "naia_ext_api": 1}).encode()}
        for name, content in old.items():
            (directory / name).write_bytes(content)
        (directory / "settings.json").write_bytes(b'{"unchanged":"legacy-settings"}')
        (directory / "oauth-tokens.json").write_bytes(b"PRESERVE-PRIVATE")
        self.legacy[ext_id] = {"version": version, "old": old, "new": new}
        record = NS(ext_id=ext_id, directory=directory, status="error", error="already installed",
                    approved=True, enabled=True, armed=True, blocked=False)
        self.manager.records.append(record)
        self.release = self.make_release()
        return record

    def apply(self):
        return repair.perform_update(self.ctx, release=self.release)

    def test_account_migration_keeps_installed_updater_and_gallery(self):
        metadata = json.loads((ROOT / "extension.json").read_bytes())
        self.assertEqual((metadata["id"], metadata["version"]), (repair.REPAIR_ID, "1.1.0"))
        self.assertNotEqual(repair.REPAIR_ID, "naia_android_tools_repair")
        old_updater = self.root / "naia_android_tools_repair"
        old_updater.mkdir()
        (old_updater / "main.py").write_bytes(b"old updater")
        original_photo = self.private / "history" / "objects" / "local-photo.png"
        original_photo.parent.mkdir(parents=True)
        original_photo.write_bytes(b"preserve copy")
        (self.target / "extension.json").write_text(json.dumps({**self.old_meta, "version": "0.2.8"}))
        self.main_files["extension.json"] = json.dumps({**self.old_meta, "version": "0.2.9"}).encode()
        self.release = self.make_release(version="0.2.9")
        self.assertTrue(self.apply()["updated"])
        self.assertEqual(json.loads((self.target / "extension.json").read_bytes())["version"], "0.2.9")
        self.assertEqual((old_updater / "main.py").read_bytes(), b"old updater")
        self.assertEqual(original_photo.read_bytes(), b"preserve copy")
        self.assertEqual((self.private / "pairing.json").read_bytes(), b"PRESERVE-PAIR")
        self.assertEqual(self.config.read_bytes(), self.config_raw)

    def test_repair_preserves_permissions_pairing_extra_data_and_removes_stale_bytecode(self):
        result = self.apply()
        self.assertTrue(result["updated"])
        self.assertFalse(result["reapproval_required"])
        self.assertEqual(self.config.read_bytes(), self.config_raw)
        self.assertEqual(json.loads((self.target / "settings.json").read_bytes()), self.settings)
        self.assertEqual((self.target / "unrelated-data.txt").read_text(), "KEEP")
        self.assertFalse((self.target / "__pycache__").exists())
        self.assertEqual((self.private / "pairing.json").read_bytes(), b"PRESERVE-PAIR")
        self.assertEqual((self.private / "device-bridge-journal.json").read_bytes(), b"PRESERVE-JOURNAL")
        self.assertEqual(self.closed, [True])
        self.assertTrue(self.record.approved and self.record.enabled and self.record.armed)
        self.assertFalse(self.record.blocked)
        self.assertEqual(self.record.status, "error")
        self.assertTrue(self.old_gpt.blocked)
        self.assertEqual(self.calls, [])

    def test_accepts_old_zero_one_and_same_version_repair_but_refuses_downgrade(self):
        for version in ("0.1.0", "0.2.1"):
            with self.subTest(version=version):
                (self.target / "extension.json").write_text(json.dumps({**self.old_meta, "version": version}))
                (self.target / "main.py").write_text("MIXED-CODE")
                self.assertTrue(self.apply()["updated"])
        (self.target / "extension.json").write_text(json.dumps({**self.old_meta, "version": "0.3.0"}))
        with self.assertRaises(repair.RepairError):
            self.apply()

    def test_second_apply_is_idempotent_without_resetting_permissions(self):
        self.apply()
        self.settings["allow_control"] = True
        (self.target / "settings.json").write_text(json.dumps(self.settings))
        self.assertTrue(self.apply()["already_updated"])
        self.assertEqual(self.closed, [True])
        self.assertEqual(json.loads((self.target / "settings.json").read_text()), self.settings)

    def test_last_settings_write_during_stop_is_preserved(self):
        controller = sys.modules["naia_ext_" + repair.TARGET_ID]._controller
        controller.close = lambda: (self.target / "settings.json").write_text('{"last_write":true}')
        self.apply()
        self.assertEqual(json.loads((self.target / "settings.json").read_text()), {"last_write": True})

    def test_targeted_legacy_sources_updated_and_tokens_preserved(self):
        legacy = self.add_legacy()
        self.assertIn(legacy.ext_id, self.apply()["updated_ids"])
        self.assertEqual((legacy.directory / "main.py").read_bytes(), b"NEW-LEGACY")
        self.assertEqual((legacy.directory / "oauth-tokens.json").read_bytes(), b"PRESERVE-PRIVATE")
        self.assertEqual((legacy.directory / "settings.json").read_bytes(), b'{"unchanged":"legacy-settings"}')
        self.assertTrue(self.old_gpt.blocked)

    def test_unknown_legacy_modifications_are_reported_and_not_overwritten(self):
        legacy = self.add_legacy()
        (legacy.directory / "main.py").write_text("USER-MODIFIED")
        result = self.apply()
        self.assertIn({"id": legacy.ext_id, "reason": "source_changed"}, result["skipped"])
        self.assertEqual((legacy.directory / "main.py").read_text(), "USER-MODIFIED")

    def test_batch_rename_failure_rolls_back_both_directories(self):
        legacy = self.add_legacy()
        original_replace = os.replace
        def replace(source, destination):
            if "new" in Path(source).parts and Path(destination) == legacy.directory:
                raise OSError("simulated final rename failure")
            return original_replace(source, destination)
        with patch.object(repair.os, "replace", side_effect=replace), self.assertRaises(repair.RepairError):
            self.apply()
        self.assertEqual((self.target / "main.py").read_text(), "OLD-CODE")
        self.assertEqual((legacy.directory / "main.py").read_bytes(), b"OLD-LEGACY")
        self.assertEqual(self.config.read_bytes(), self.config_raw)

    def test_interrupted_rename_recovers_before_new_repair(self):
        transaction = self.root / ".android-tools-repair"
        previous = transaction / "previous" / repair.TARGET_ID
        previous.parent.mkdir(parents=True)
        (transaction / "plan.json").write_text(json.dumps({"ids": [repair.TARGET_ID]}))
        os.replace(self.target, previous)
        self.assertTrue(self.apply()["updated"])
        self.assertEqual((self.target / "main.py").read_bytes(), self.main_files["main.py"])

    def test_completed_transaction_is_never_rolled_back(self):
        self.apply()
        transaction = self.root / ".android-tools-repair"
        previous = transaction / "previous" / repair.TARGET_ID
        previous.mkdir(parents=True)
        (previous / "extension.json").write_text(json.dumps(self.old_meta))
        (previous / "main.py").write_text("OLD-CODE")
        for name in ("plan.json", "committed.json"):
            (transaction / name).write_text(json.dumps({"ids": [repair.TARGET_ID]}))
        self.assertTrue(self.apply()["already_updated"])
        self.assertEqual((self.target / "main.py").read_bytes(), self.main_files["main.py"])

    def test_archive_and_manifest_pins_are_both_required_for_bundle(self):
        (self.directory / repair.MANIFEST_NAME).write_bytes(self.release["manifest_bytes"])
        (self.directory / repair.ARCHIVE_NAME).write_bytes(self.release["archive_bytes"])
        with patch.object(repair, "MANIFEST_SHA256", sha(self.release["manifest_bytes"])), patch.object(repair, "PAYLOAD_SHA256", sha(self.release["archive_bytes"])):
            loaded = repair._bundle(self.directory)
            repair.validate_release(loaded)
            (self.directory / repair.MANIFEST_NAME).write_bytes(b"{}")
            with self.assertRaises(repair.RepairError):
                repair._bundle(self.directory)

    def test_unknown_extension_id_and_data_file_overwrite_are_rejected(self):
        for change in (lambda m: m["repairs"].update({"arbitrary": {}}),
                       lambda m: m["main"]["files"].update({"settings.json": "a" * 64}),
                       lambda m: m["main"]["files"].update({"../private.py": "a" * 64})):
            value = copy.deepcopy(self.release)
            meta = json.loads(value["manifest_bytes"])
            change(meta)
            value["manifest_bytes"] = json.dumps(meta).encode()
            with self.assertRaises(repair.RepairError):
                repair.validate_release(value)

    def test_modified_archive_invalid_commit_or_version_fails_before_changes(self):
        for change in ({"archive_bytes": b"not a zip"}, {"source_commit": "branch/main"}):
            with self.subTest(change=change), self.assertRaises(repair.RepairError):
                repair.perform_update(self.ctx, release={**self.release, **change})
        meta = json.loads(self.release["manifest_bytes"])
        meta["target_version"] = "0.2.0"
        with self.assertRaises(repair.RepairError):
            repair.perform_update(self.ctx, release={**self.release, "manifest_bytes": json.dumps(meta).encode()})
        self.assertEqual(self.closed, [])
        self.assertEqual((self.target / "main.py").read_text(), "OLD-CODE")

    def test_symlinks_and_busy_generation_are_rejected(self):
        self.app.is_generating = True
        with self.assertRaises(repair.RepairError):
            self.apply()
        self.app.is_generating = False
        (self.target / "private-link").symlink_to(self.private / "pairing.json")
        with self.assertRaises(repair.RepairError):
            self.apply()
        self.assertEqual(self.closed, [])

    def test_untrusted_disabled_blocked_or_disarmed_helper_cannot_apply(self):
        for field, value in (("approved", False), ("enabled", False), ("armed", False), ("blocked", True)):
            original = getattr(self.own_record, field)
            setattr(self.own_record, field, value)
            with self.assertRaises(repair.RepairError):
                self.apply()
            setattr(self.own_record, field, original)
        self.assertEqual(self.closed, [])

    def test_explicit_reload_only_retries_changed_previously_approved_enabled_targets(self):
        legacy = self.add_legacy()
        self.apply()
        legacy.blocked = True
        results = repair.reload_repaired(self.ctx)
        self.assertEqual(self.calls, ["enabled:" + repair.TARGET_ID])
        self.assertEqual(next(r for r in results if r["id"] == repair.TARGET_ID)["status"], "loaded")
        self.assertEqual(next(r for r in results if r["id"] == legacy.ext_id)["status"], "skipped")
        self.assertTrue(self.old_gpt.blocked)
        self.assertTrue(legacy.blocked)

    def test_reloading_requires_a_prior_successful_repair(self):
        with self.assertRaises(repair.RepairError):
            repair.reload_repaired(self.ctx)
        self.assertEqual(self.calls, [])

    def test_reload_load_gate_respects_disable_after_manifest_check(self):
        self.apply()
        original_load = self.manager.load_by_key
        def disable_then_load(key):
            self.record.enabled = False
            original_load(key)
        self.manager.load_by_key = disable_then_load
        repair.reload_repaired(self.ctx)
        self.assertEqual(self.calls, ["enabled:" + repair.TARGET_ID])
        self.assertFalse(self.record.enabled)
        self.assertNotEqual(self.record.status, "loaded")
        self.assertEqual(self.config.read_bytes(), self.config_raw)

    def test_already_updated_fresh_helper_can_explicitly_reload_verified_files(self):
        self.add_legacy()
        self.apply()
        repair._last_changed_ids = []
        repair._last_verified_files = {}
        result = self.apply()
        self.assertTrue(result["already_updated"])
        self.assertEqual(len(result["reload_ids"]), 2)
        self.assertEqual(len(repair.reload_repaired(self.ctx)), 2)
        self.assertEqual(len(self.calls), 2)

    def test_explicit_reload_refuses_modified_source_and_refreshes_manifest_without_new_rights(self):
        self.apply()
        (self.target / "main.py").write_bytes(b"CHANGED-AFTER-APPLY")
        self.assertEqual(repair.reload_repaired(self.ctx)[0]["status"], "error")
        self.assertEqual(self.calls, [])
        self.apply()
        def parse_manifest(path, record):
            record.version = json.loads(path.read_text())["version"]
            record.armed = False
        self.manager._parse_manifest_into = parse_manifest
        repair.reload_repaired(self.ctx)
        self.assertEqual(self.record.version, "0.2.1")
        self.assertTrue(self.record.armed)

    def register_actions(self):
        self.messages = []
        self.ctx.log = lambda message: None
        self.ctx.show_toast = lambda message, **kwargs: self.messages.append(message)
        self.ctx.register_panel = lambda fields, **kwargs: setattr(self, "action", kwargs["on_action"])
        repair.register(self.ctx)

    def test_remote_check_is_background_and_requires_a_separate_apply_click(self):
        self.register_actions()
        entered, resume = threading.Event(), threading.Event()
        def download():
            entered.set()
            self.assertTrue(resume.wait(2))
            return self.release
        with patch.object(repair, "_download", side_effect=download):
            self.action("check_remote")
            self.assertTrue(entered.wait(2))
            self.assertTrue(repair._action_thread.is_alive())
            self.assertEqual((self.target / "main.py").read_text(), "OLD-CODE")
            resume.set()
            repair._action_thread.join(2)
        self.assertEqual((self.target / "main.py").read_text(), "OLD-CODE")
        self.action("apply_remote")
        repair._action_thread.join(2)
        self.assertEqual((self.target / "main.py").read_bytes(), self.main_files["main.py"])
        self.assertEqual(self.calls, [])

    def test_remote_confirmation_expires_without_applying(self):
        self.register_actions()
        with patch.object(repair, "_checked_release", self.release), patch.object(repair, "_checked_at", -10000):
            self.action("apply_remote")
            repair._action_thread.join(2)
        self.assertEqual((self.target / "main.py").read_text(), "OLD-CODE")
        self.assertTrue(any("10분" in message for message in self.messages))


if __name__ == "__main__":
    unittest.main()
