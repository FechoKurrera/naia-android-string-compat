"""Real loopback panel + backend-loop bridge integration; no relay network."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import http.client
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location("screen_integration_" + name, ROOT / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


entry, bridge_module, screen_module, ui = [load(name) for name in
    ("main", "naia_device_bridge", "naia_screen_service", "naia_tools_ui")]
CLIENT = "integration_visible_0123456789"


def snapshot(large=False):
    return {"snapshot_id": "screen_integration", "title": "NAIA",
            "viewport": {"width": 1000, "height": 800, "scroll_x": 0, "scroll_y": 0},
            "controls": [{"target": "t" + str(i + 1), "role": "textbox", "label": "Prompt",
                "value": "v" * (4000 if large else 4), "enabled": True, "actions": [],
                "bounds": {"x": 1, "y": i * 20, "width": 100, "height": 20}}
                for i in range(30 if large else 1)],
            "text": "SCREEN_PRIVATE_UNACKED", "blocked": False}


class ScreenIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saved = dict(entry.DEFAULTS)
        self.loop = asyncio.new_event_loop()
        started = threading.Event()
        def run_loop():
            asyncio.set_event_loop(self.loop)
            self.loop.call_soon(started.set)
            self.loop.run_forever()
        self.loop_thread = threading.Thread(target=run_loop, daemon=True)
        self.loop_thread.start()
        self.assertTrue(started.wait(3))
        self.record = NS(status="loaded", enabled=True, blocked=False, armed=True, is_active=True)
        self.ctx = NS(_record=self.record, _app_context=NS(headless_main_loop=self.loop),
                      ext_dir=Path(self.temp.name), load_settings=lambda defaults: {**defaults, **self.saved},
                      save_settings=self.save)
        self.controller = entry.Controller(self.ctx, self.temp.name, NS())
        self.screen = self.controller.screen = screen_module.ScreenService()
        self.bridge = self.controller.bridge = bridge_module.DeviceBridge(self.ctx,
            self.controller.bridge_settings, state_root=self.temp.name,
            allowed_origins={entry.RELAY_URL}, screen_service=self.screen)
        self.pair = {"relay_url": entry.RELAY_URL, "device_id": "integration_device",
                     "device_token": "PRIVATE_DEVICE", "relay_access_token": "PRIVATE_ACCESS"}
        self.controller.import_pairing(self.pair)
        self.controller.update_settings({"allow_read": False, "allow_screen": True})
        self.server = ui.PanelServer(self.controller)
        self.workers = ThreadPoolExecutor(max_workers=2)

    def tearDown(self):
        self.controller.close()
        self.server.close()
        self.workers.shutdown(wait=True)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.loop_thread.join(3)
        self.loop.close()
        self.temp.cleanup()

    def save(self, settings):
        self.saved = dict(settings)
        return True

    def post(self, path, data, headers=None):
        merged = {"Host": self.server.authority, "Origin": self.server.origin,
                  "X-NAIA-Android-Tools": self.server.token, "Content-Type": "application/json"}
        merged.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server.server_port, timeout=3)
        try:
            connection.request("POST", path, body=json.dumps(data), headers=merged)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def command(self, name="naia_get_screen", arguments=None):
        return {"id": "integration_" + str(time.time_ns()), "name": name,
                "arguments": arguments or {}, "created_at": time.time()}

    def begin(self, command):
        self.assertEqual(self.post("/screen/poll", {"client_id": CLIENT})[0], 200)
        future = self.workers.submit(self.bridge._handle, command)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            code, result = self.post("/screen/poll", {"client_id": CLIENT})
            self.assertEqual(code, 200)
            if result["commands"]:
                return future, result["commands"][0]
            if future.done():
                self.fail("Command failed before UI handoff: " + str(future.result()))
            threading.Event().wait(0.005)
        self.fail("Backend did not hand a command to the panel")

    def finish(self, future, command, result=None):
        code, response = self.post("/screen/result", {"client_id": CLIENT, "id": command["id"],
            "result": snapshot() if result is None else result, "is_error": False})
        self.assertEqual((code, response), (200, {"accepted": True}))
        return future.result(timeout=3)

    def completed(self, name="naia_get_screen", arguments=None, result=None):
        command = self.command(name, arguments)
        future, local = self.begin(command)
        response = self.finish(future, local, result)
        self.assertFalse(response[1], response)
        return command, response[0]

    def test_panel_capability_and_origin_required_before_ui_can_claim(self):
        for headers in ({"Origin": "https://untrusted.example"},
                        {"Origin": ""}, {"Host": "untrusted.example"},
                        {"X-NAIA-Android-Tools": "wrong"}):
            with self.subTest(headers=headers):
                code, _ = self.post("/screen/poll", {"client_id": CLIENT}, headers)
                self.assertEqual(code, 403)
                self.assertFalse(self.screen.status()["ready"])
        self.assertEqual(self.post("/screen/poll", {"client_id": CLIENT})[0], 200)
        self.assertTrue(self.screen.status()["ready"])

    def test_status_does_not_hold_controller_lock_while_entering_screen_service(self):
        # Screen poll/result holds the broker lock while its permission guard
        # reads Controller settings. Status must not acquire those in reverse.
        original = self.screen.status
        def checked_status():
            acquired = []
            def permission_guard_thread():
                locked = self.controller._lock.acquire(timeout=0.2)
                acquired.append(locked)
                if locked:
                    self.controller._lock.release()
            worker = threading.Thread(target=permission_guard_thread)
            worker.start()
            worker.join(1)
            self.assertEqual(acquired, [True], "status and screen permission guard can deadlock")
            return original()
        self.screen.status = checked_status
        self.controller.status()

    def test_screen_permission_independent_of_state_read_edit_and_generate(self):
        _, result = self.completed(result=snapshot(large=True))
        self.assertGreater(len(json.dumps(result).encode()), bridge_module.MAX_RESULT_BYTES)
        self.assertEqual(result["text"], "SCREEN_PRIVATE_UNACKED")
        self.assertFalse(self.controller.bridge_settings()["permissions"]["read"])
        for name, args, denied in (("naia_get_state", {}, "read"),
                                   ("naia_set_prompt", {"prompt": "x"}, "edit"),
                                   ("naia_generate", {"prompt": "x"}, "generate")):
            with self.subTest(name=name):
                result, failed = self.bridge._handle(self.command(name, args))
                self.assertTrue(failed)
                self.assertEqual(result["error"], "permission_denied_" + denied)

    def test_control_allows_ui_fill_without_direct_api_edit_but_requires_screen(self):
        self.controller.update_settings({"allow_control": True})
        _, screen = self.completed()
        args = {"snapshot_id": screen["snapshot_id"], "action": "fill", "target": "t1", "value": "new"}
        self.completed("naia_ui_action", args)
        self.controller.update_settings({"allow_screen": False, "allow_edit": True})
        result, failed = self.bridge._handle(self.command("naia_ui_action", {**args, "action": "click", "value": None}))
        self.assertTrue(failed)
        self.assertNotEqual(result.get("snapshot_id"), "screen_prior")
        args = {"snapshot_id": "screen_prior", "action": "click", "target": "t1"}
        result, failed = self.bridge._handle(self.command("naia_ui_action", args))
        self.assertTrue(failed)
        self.assertEqual(result["error"], "permission_denied_screen")
        self.assertEqual(self.post("/screen/poll", {"client_id": CLIENT})[0], 400)

    def test_pair_replaced_after_handoff_discards_old_result(self):
        future, local = self.begin(self.command())
        self.controller.import_pairing({**self.pair, "device_token": "REPLACED_DEVICE"})
        self.controller.update_settings({"allow_screen": True})
        result, failed = self.finish(future, local)
        self.assertTrue(failed)
        self.assertEqual(result["error"], "pairing_changed")
        self.assertNotIn("SCREEN_PRIVATE", json.dumps(result))

    def test_control_revoked_after_fill_handoff_discards_result(self):
        self.controller.update_settings({"allow_control": True, "allow_edit": True})
        _, screen = self.completed()
        future, local = self.begin(self.command("naia_ui_action",
            {"snapshot_id": screen["snapshot_id"], "action": "fill", "target": "t1", "value": "changed"}))
        self.controller.update_settings({"allow_control": False})
        result, failed = self.finish(future, local)
        self.assertTrue(failed)
        self.assertEqual(result["error"], "permission_denied_control")

    def test_ack_prunes_snapshot_but_keeps_durable_replay_marker(self):
        command, response = self.completed()
        self.assertIn("SCREEN_PRIVATE_UNACKED", self.bridge.journal_path.read_text())
        self.bridge._ack(command["id"], self.bridge._config())
        self.assertNotIn("SCREEN_PRIVATE_UNACKED", self.bridge.journal_path.read_text())
        result, failed = self.bridge._handle(command)
        self.assertTrue(failed)
        self.assertEqual(result["error"], "screen_result_already_delivered")
        restarted = bridge_module.DeviceBridge(self.ctx, self.controller.bridge_settings,
            state_root=self.temp.name, allowed_origins={entry.RELAY_URL}, screen_service=self.screen)
        try:
            self.assertEqual(restarted._handle(command), (result, True))
        finally:
            restarted.close()
        self.assertEqual(self.post("/screen/poll", {"client_id": CLIENT})[1]["commands"], [])

    def test_retry_sends_snapshot_once_then_ack_prunes_disk(self):
        command, response = self.completed()
        posts = []
        self.bridge._post = lambda config, path, payload: posts.append((path, payload)) or {"accepted": True}
        self.bridge._flush_results(self.bridge._config())
        self.bridge._flush_results(self.bridge._config())
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1]["result"], response)
        self.assertEqual(posts[0][1]["command_id"], command["id"])
        self.assertNotIn("SCREEN_PRIVATE_UNACKED", self.bridge.journal_path.read_text())

    def test_revoked_screen_not_resent_from_unacked_journal(self):
        self.completed()
        self.controller.update_settings({"allow_screen": False})
        posts = []
        self.bridge._post = lambda config, path, payload: posts.append(payload) or {"accepted": True}
        self.bridge._flush_results(self.bridge._config())
        self.assertNotIn("SCREEN_PRIVATE_UNACKED", json.dumps(posts))
        self.assertNotIn("SCREEN_PRIVATE_UNACKED", self.bridge.journal_path.read_text())

    def test_revoked_screen_not_replayed_from_completed_ui_action(self):
        self.controller.update_settings({"allow_control": True})
        _, screen = self.completed()
        command, _ = self.completed("naia_ui_action",
            {"snapshot_id": screen["snapshot_id"], "action": "click", "target": "t1"})
        self.controller.update_settings({"allow_screen": False})
        result, failed = self.bridge._handle(command)
        self.assertTrue(failed)
        self.assertNotIn("SCREEN_PRIVATE_UNACKED", json.dumps(result))
        self.assertEqual(result["error"], "permission_denied_screen")

    def test_repair_discards_old_pair_snapshot_without_erasing_replay_marker(self):
        command, _ = self.completed()
        self.controller.import_pairing({**self.pair, "device_token": "NEW_PAIR_DEVICE"})
        self.controller.update_settings({"allow_screen": True})
        posts = []
        self.bridge._post = lambda config, path, payload: posts.append(payload) or {"accepted": True}
        self.bridge._flush_results(self.bridge._config())
        self.assertEqual(posts, [])
        self.assertNotIn("SCREEN_PRIVATE_UNACKED", self.bridge.journal_path.read_text())
        self.assertIn(command["id"], self.bridge._journal)
        result, failed = self.bridge._handle(command)
        self.assertTrue(failed)
        self.assertEqual(result["error"], "command_id_conflict")


if __name__ == "__main__":
    unittest.main()
