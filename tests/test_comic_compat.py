"""Regression tests against pinned upstream diagnostic inputs.

From android-tools, run:
    python tests/fetch_comic_test_fixtures.py
    python tests/test_comic_compat.py

The first command downloads and verifies source bytes only; it does not execute
them. The second command executes those test inputs and requires Pillow.
Upstream inputs live in ../comic-upstream, outside the published extension.
"""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT.parent / "comic-upstream"
sys.path.insert(0, str(FIXTURE))
sys.path.insert(0, str(FIXTURE / "tests"))
spec = importlib.util.spec_from_file_location("comic_compat_test_module", ROOT / "naia_comic_compat.py")
compat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compat)
from naia_exten.features.comic_maker.feature import ComicMakerFeature
from naia_exten.features.comic_maker.client import ComicServerError
from test_comic_maker import _FakeContext, _FakeClient, comic_plan


class ComicCompatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = SimpleNamespace()
        self.snapshots = []

        class Context(_FakeContext):
            def __init__(inner, app, ext_id):
                super().__init__(Path(self.temp.name))
                inner._app_context, inner.ext_id = app, ext_id
            def register_panel(inner, fields, *, title=None, on_action=None):
                inner.panel = fields
                inner.handler = on_action

        self.context_class = Context
        self.ctx = Context(self.app, "naia_exten")
        self.tools_ctx = Context(self.app, "naia_zz_android_tools_v1")
        self.feature = ComicMakerFeature()
        self.extension = SimpleNamespace(ctx=self.ctx,
            features=SimpleNamespace(features={"comic_maker": self.feature}))

        class Handler:
            def __init__(inner):
                inner.ctx = self.ctx
                inner.features = self.extension.features
            def action(inner, key):
                return None
        self.handler_owner = Handler()

        def refresh():
            fields = self.feature.panel_fields()
            self.snapshots.append(json.loads(next(f for f in fields if f["key"] == "chooser_state")["placeholder"]))
            self.ctx.register_panel(fields, on_action=self.handler_owner.action)
        self.extension.refresh_panel = refresh
        self.feature._attach(self.extension)
        self.feature._client = _FakeClient(comic_plan())
        self.record = SimpleNamespace(ext_id="naia_exten", action_handler=self.handler_owner.action)
        self.app.extension_manager = SimpleNamespace(records=[self.record])
        self.handle = None
        self.addCleanup(lambda: self.handle.close() if self.handle else None)

    def install(self):
        self.handle = compat.install(self.tools_ctx)
        return self.handle

    def failing_worker(self, stored=False, unexpected=False):
        entered, release = threading.Event(), threading.Event()
        def fail(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError("test worker timed out")
            if unexpected:
                raise RuntimeError("secret-token-must-not-appear")
            raise ComicServerError("PromptServer connection refused")
        if stored:
            self.feature._client.random_plan = fail
            self.feature._start_stored()
            worker = self.feature._stored_worker
        else:
            self.feature._client.generate_plan = fail
            self.feature._start_nai()
            worker = self.feature._nai_worker
        self.assertTrue(entered.wait(3))
        return worker, release

    def test_real_upstream_reproduces_cached_busy_then_install_repairs(self):
        worker, release = self.failing_worker()
        release.set(); worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(self.feature._nai_busy())
        self.assertTrue(self.snapshots[-1]["nai_busy"])
        self.install()
        self.assertFalse(self.snapshots[-1]["nai_busy"])

    def test_connection_failure_publishes_idle_after_worker_cleanup(self):
        self.install()
        worker, release = self.failing_worker()
        self.assertTrue(self.snapshots[-1]["nai_busy"])
        release.set(); worker.join(3)
        self.assertIsNone(self.feature._nai_worker)
        self.assertFalse(self.snapshots[-1]["nai_busy"])
        self.assertIn("connection refused", self.feature._recent_logs[-1])

    def test_unexpected_error_clears_state_without_leaking_details(self):
        self.install()
        worker, release = self.failing_worker(unexpected=True)
        release.set(); worker.join(3)
        self.assertFalse(self.feature._planning)
        self.assertFalse(self.snapshots[-1]["nai_busy"])
        self.assertNotIn("secret-token", str(self.ctx.logs) + str(self.ctx.toasts) + str(self.feature._recent_logs))

    def test_stored_connection_failure_cleans_only_stored_worker(self):
        self.install()
        worker, release = self.failing_worker(stored=True)
        release.set(); worker.join(3)
        self.assertIsNone(self.feature._stored_worker)
        self.assertFalse(self.feature._stored_busy())

    def test_reset_does_not_permit_second_request_while_first_http_waits(self):
        self.install()
        worker, release = self.failing_worker()
        self.feature._reset_comic_state(source="nai")
        self.assertTrue(self.feature._nai_busy())
        self.feature._start_nai()
        self.assertIs(self.feature._nai_worker, worker)
        release.set(); worker.join(3)
        self.assertFalse(self.snapshots[-1]["nai_busy"])

    def test_success_preserves_active_image_work(self):
        self.install()
        self.feature._start_nai()
        # Capture worker under the same lock; it may already have completed.
        worker = self.feature._nai_worker
        if worker:
            worker.join(3)
        self.assertIsNotNone(self.feature._active_run)
        self.assertTrue(self.snapshots[-1]["nai_busy"])
        self.assertEqual(self.ctx.queue_starts, 1)

    def test_pending_plan_survives_stored_finalization(self):
        self.install()
        self.feature._start_stored()
        worker = self.feature._stored_worker
        if worker:
            worker.join(3)
        self.assertIsNotNone(self.feature._stored_pending)
        self.assertTrue(self.feature._stored_busy())
        self.assertEqual(self.ctx.enqueued, [])

    def test_already_running_worker_is_not_cleared_until_actual_exit(self):
        worker, release = self.failing_worker()
        self.install()
        self.assertIs(self.feature._nai_worker, worker)
        release.set(); worker.join(3)
        deadline = time.monotonic() + 2
        while self.feature._nai_worker is not None and time.monotonic() < deadline:
            self.handle._stop.wait(.02)
        self.assertIsNone(self.feature._nai_worker)
        self.assertFalse(self.snapshots[-1]["nai_busy"])

    def test_finalizer_does_not_clear_replacement_worker(self):
        self.install()
        original = threading.Thread()
        replacement = threading.Thread()
        self.feature._nai_worker = replacement
        self.feature._planning = True
        self.assertFalse(self.handle._finish_worker(self.feature, "nai", original))
        self.assertIs(self.feature._nai_worker, replacement)
        self.assertTrue(self.feature._planning)

    def test_monitor_rechecks_worker_liveness_under_workflow_lock(self):
        self.install()
        entered, release = threading.Event(), threading.Event()
        def wait():
            entered.set()
            release.wait(3)
        worker = threading.Thread(target=wait)
        self.feature._nai_worker = worker
        self.feature._planning = True
        worker.start()
        self.assertTrue(entered.wait(3))
        try:
            self.assertFalse(self.handle._finish_worker(
                self.feature, "nai", worker, require_finished=True))
            self.assertIs(self.feature._nai_worker, worker)
            self.assertTrue(self.feature._planning)
        finally:
            release.set()
            worker.join(3)

    def test_late_load_and_hot_reload_are_discovered_once(self):
        self.app.extension_manager.records = []
        self.install()
        self.assertEqual(self.handle.status()["patched_features"], 0)
        self.extension.refresh_panel()
        self.assertEqual(self.handle.status()["patched_features"], 1)
        wrapped = self.feature._prepare
        self.extension.refresh_panel()
        self.assertIs(self.feature._prepare, wrapped)
        replacement = ComicMakerFeature()
        replacement._attach(self.extension)
        self.extension.features.features["comic_maker"] = replacement
        self.ctx.register_panel([], on_action=self.handler_owner.action)
        self.assertEqual(self.handle.status()["patched_features"], 2)

    def test_unrelated_app_is_not_patched_and_close_restores_owned_only(self):
        original_register = self.context_class.register_panel
        self.install()
        other = ComicMakerFeature()
        other_ctx = self.context_class(SimpleNamespace(), "naia_exten")
        self.handler_owner.ctx = other_ctx
        self.handler_owner.features = SimpleNamespace(features={"comic_maker": other})
        other_ctx.register_panel([], on_action=self.handler_owner.action)
        self.assertNotIn("_prepare", vars(other))
        self.handle.close()
        self.assertIs(self.context_class.register_panel, original_register)
        self.assertNotIn("_prepare", vars(self.feature))
        self.assertFalse(self.handle.status()["installed"])


if __name__ == "__main__":
    unittest.main()
