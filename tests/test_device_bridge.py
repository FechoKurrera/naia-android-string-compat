"""No-network bridge tests: real loop scheduling and durable replay records."""
import ast
import asyncio
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("device_bridge_test_target", ROOT / "naia_device_bridge.py")
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
ORIGIN = "https://naia-tablet-link.ugi8296.chatgpt.site"


class Queue:
    def __init__(self):
        self.items = []; self.paused = False
    def get_all_requests(self): return list(self.items)
    def is_paused(self): return self.paused
    def is_empty(self): return not self.items
    def remove_request(self, rid):
        before = len(self.items); self.items[:] = [x for x in self.items if x.request_id != rid]
        return len(self.items) != before


class Runtime:
    def __init__(self, loop):
        self.headless_main_loop = loop
        self.generation_queue_manager = Queue()
        self.prompt_text = "original"; self.negative_prompt_text = "negative"
        self.remote_params = {"model": "nai-diffusion-4-full", "steps": 28,
                              "credential": "NAI-SECRET", "other_private_value": "SECRET"}
        self.options = {"auto_generate": False}
        self.is_generating = False
        self.headless_generation_execute_enabled = True
        self.extension_queue_start_bridge = lambda payload: None
        self.events = []; self.saved = 0
    def get_api_mode(self): return "NAI"
    def get_options(self): return self.options
    def stale_prompt_edit(self, origin, stamp): return False
    def save_remote_ui_state(self): self.saved += 1
    def publish(self, event, data): self.events.append((event, data))


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()
        def run():
            asyncio.set_event_loop(self.loop); self.loop.call_soon(ready.set); self.loop.run_forever()
        self.thread = threading.Thread(target=run); self.thread.start(); ready.wait(3)
        self.addCleanup(self.stop_loop)
        self.runtime = Runtime(self.loop)
        self.record = types.SimpleNamespace(is_active=True)
        self.settings = {"enabled": True, "relay_url": ORIGIN, "relay_access_token": "ACCESS-SECRET",
                         "device_id": "device_example", "device_token": "DEVICE-SECRET",
                         "permissions": {"read": True, "edit": True, "generate": True}}
        self.count = 0; self.starts = 0; self.hook = None
        def enqueue(**kwargs):
            self.count += 1
            request = types.SimpleNamespace(request_id=f"request_{self.count}", params={"auto_generate": False})
            self.runtime.generation_queue_manager.items.append(request)
            if self.hook: self.hook(request)
            return {"ok": True, "request_id": request.request_id}
        def start(): self.starts += 1; return {"ok": True}
        self.ctx = types.SimpleNamespace(_app_context=self.runtime, _record=self.record,
            ext_dir=Path(self.temp.name), enqueue_generation=enqueue, start_generation_queue=start)
        self.bridge = self.new_bridge()
        self.addCleanup(self.bridge.close)
        fake = types.ModuleType("core.headless_generation_service")
        fake.HeadlessGenerationService = types.SimpleNamespace(is_sequence_command_text=lambda value: ":begin" in value or ":seq" in value)
        self.patcher = patch.dict(sys.modules, {"core.headless_generation_service": fake})
        self.patcher.start(); self.addCleanup(self.patcher.stop)

    def stop_loop(self):
        self.loop.call_soon_threadsafe(self.loop.stop); self.thread.join(3); self.loop.close()

    def new_bridge(self):
        return mod.DeviceBridge(self.ctx, lambda: copy.deepcopy(self.settings),
            state_root=self.temp.name, allowed_origins={ORIGIN})

    def command(self, name="naia_get_state", arguments=None, **changes):
        return {"id": "cmd_" + str(time.time_ns()), "name": name, "arguments": arguments or {},
                "created_at": time.time(), **changes}

    def call(self, name="naia_get_state", arguments=None, **changes):
        return self.bridge._handle(self.command(name, arguments, **changes))

    def test_only_allowlisted_state_is_returned(self):
        result, error = self.call()
        self.assertFalse(error); self.assertEqual(result["prompt"], "original")
        self.assertEqual(result["parameters"]["steps"], 28)
        self.assertEqual(result["diagnostics"]["runtime_version"], "0.2.9")
        self.assertNotIn("SECRET", json.dumps(result)); self.assertNotIn("SECRET", json.dumps(self.bridge.status()))

    def test_default_permissions_and_disarming_fail_closed(self):
        self.settings["permissions"] = {}
        self.assertTrue(self.call()[1])
        self.assertTrue(self.call("naia_set_prompt", {"prompt": "change"})[1])
        self.assertTrue(self.call("naia_generate", {"prompt": "one"})[1])
        self.assertEqual(self.count, 0)
        self.settings["permissions"]["generate"] = True; self.record.is_active = False
        self.assertTrue(self.call("naia_generate", {"prompt": "one"})[1])
        self.assertEqual(self.count, 0)

    def test_recheck_after_dispatch_wait_and_pair_change(self):
        original = self.bridge._on_loop
        def after_wait(config, name, args):
            self.settings["permissions"]["generate"] = False
            return original(config, name, args)
        self.bridge._on_loop = after_wait
        result, error = self.call("naia_generate", {"prompt": "one"})
        self.assertTrue(error); self.assertEqual(self.count, 0)
        self.bridge._on_loop = original
        old = self.bridge._config(); self.settings["device_token"] = "different"
        with self.assertRaisesRegex(mod.BridgeError, "pairing_changed"):
            self.bridge._require(old, "naia_get_state")

    def test_duplicate_generation_is_not_repeated_after_restart(self):
        command = self.command("naia_generate", {"prompt": "one"})
        result, error = self.bridge._handle(command)
        self.assertFalse(error); self.assertEqual(result["count"], 1)
        self.assertEqual(self.bridge._handle(command), (result, False))
        other = self.new_bridge(); self.addCleanup(other.close)
        self.assertEqual(other._handle(command), (result, False))
        self.assertEqual(self.count, 1); self.assertEqual(self.starts, 1)

    def test_durable_started_marker_refuses_unknown_retry(self):
        command = self.command("naia_generate", {"prompt": "one"})
        def interrupted(config, name, args): raise KeyboardInterrupt()
        self.bridge._on_loop = interrupted
        with self.assertRaises(KeyboardInterrupt): self.bridge._handle(command)
        other = self.new_bridge(); self.addCleanup(other.close)
        result, error = other._handle(command)
        self.assertTrue(error); self.assertEqual(result["error"], "previous_outcome_unknown")
        self.assertEqual(self.count, 0)

    def test_expired_future_malformed_commands_do_not_execute(self):
        for stamp in (time.time()-121, time.time()+60, float("nan"), True, "2026-01-01T00:00:00"):
            with self.subTest(stamp=stamp):
                self.assertTrue(self.call("naia_generate", {"prompt": "one"}, created_at=stamp)[1])
        self.assertTrue(self.call("naia_generate", {"prompt": "one", "overrides": {"steps": 999}})[1])
        self.assertTrue(self.call("shell", {"command": "ls"})[1]); self.assertEqual(self.count, 0)

    def test_deadline_rechecked_after_backend_loop_wait(self):
        original = self.bridge._on_loop
        command = self.command("naia_generate", {"prompt": "one"})
        def delayed(config, name, args):
            with patch.object(mod.time, "time", return_value=command["created_at"] + 121):
                return original(config, name, args)
        self.bridge._on_loop = delayed
        result, error = self.bridge._handle(command)
        self.assertTrue(error); self.assertEqual(result["error"], "command_expired")
        self.assertEqual(self.count, 0); self.assertEqual(self.starts, 0)

    def test_never_starts_pending_paused_or_running_work(self):
        queue = self.runtime.generation_queue_manager
        queue.items = [types.SimpleNamespace(request_id="user_job")]
        self.assertTrue(self.call("naia_generate", {"prompt": "one"})[1])
        queue.items = []; queue.paused = True
        self.assertTrue(self.call("naia_generate", {"prompt": "one"})[1])
        queue.paused = False; self.runtime.is_generating = True
        self.assertTrue(self.call("naia_generate", {"prompt": "one"})[1]); self.assertEqual(self.starts, 0)

    def test_continuous_generation_and_sequence_are_rejected(self):
        self.runtime.options["auto_generate"] = True
        self.assertTrue(self.call("naia_generate", {"prompt": "one"})[1])
        self.runtime.options["auto_generate"] = False
        self.assertTrue(self.call("naia_generate", {"prompt": ":begin\n:seq\n: end"})[1])
        self.assertEqual(self.count, 0)

    def test_hook_added_work_does_not_start_and_own_work_is_removed(self):
        self.hook = lambda request: self.runtime.generation_queue_manager.items.append(types.SimpleNamespace(request_id="user_work"))
        result, error = self.call("naia_generate", {"prompt": "one"})
        self.assertTrue(error); self.assertEqual(self.starts, 0)
        self.assertEqual([x.request_id for x in self.runtime.generation_queue_manager.items], ["user_work"])

    def test_disarming_during_enqueue_cleans_up_only_own_request(self):
        self.hook = lambda request: setattr(self.record, "is_active", False)
        self.assertTrue(self.call("naia_generate", {"prompt": "one"})[1])
        self.assertEqual(self.starts, 0); self.assertTrue(self.runtime.generation_queue_manager.is_empty())

    def test_cancel_only_own_pending_work_and_report_unknown_completion(self):
        generated, error = self.call("naia_generate", {"prompt": "one"}, id="cmd_z"); self.assertFalse(error)
        request_id = generated["request_id"]
        self.assertTrue(self.call("naia_cancel_generation", {"request_id": "foreign"})[1])
        status, _ = self.call("naia_get_generation_status")
        self.assertEqual(status["requests"][0]["status"], "pending")
        result, _ = self.call("naia_cancel_generation", {"request_id": request_id}, id="cmd_a")
        self.assertTrue(result["cancelled"])
        status, _ = self.call("naia_get_generation_status")
        self.assertEqual(status["requests"][0]["status"], "cancelled")
        other = self.new_bridge(); self.addCleanup(other.close)
        self.assertEqual(other._generation_status(other._config())["requests"][0]["status"], "cancelled")

    def test_journal_failure_blocks_execution(self):
        self.bridge._journal_ok = False
        self.assertTrue(self.call("naia_generate", {"prompt": "one"})[1]); self.assertEqual(self.count, 0)
        self.bridge.journal_path.write_text("invalid")
        other = self.new_bridge(); self.addCleanup(other.close)
        self.assertTrue(other._handle(self.command("naia_generate", {"prompt": "one"}))[1])
        self.assertEqual(self.count, 0)

    def test_origin_header_separation_and_redirect_refusal(self):
        self.settings["relay_url"] = "https://untrusted.example"
        with self.assertRaisesRegex(mod.BridgeError, "not_allowed"): self.bridge._config()
        self.settings["relay_url"] = ORIGIN
        captured = []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def geturl(self): return ORIGIN + "/api/device/poll"
            def read(self, size): return b'{"commands":[]}'
        def open_request(req, timeout): captured.append(req); return Response()
        self.bridge._opener = types.SimpleNamespace(open=open_request)
        self.bridge._post(self.bridge._config(), "/api/device/poll", {})
        self.assertEqual(captured[0].get_header("Oai-sites-authorization"), "Bearer ACCESS-SECRET")
        payload = json.loads(captured[0].data)
        self.assertEqual(payload["device_token"], "DEVICE-SECRET")
        self.assertNotIn("ACCESS-SECRET", captured[0].data.decode())
        with self.assertRaisesRegex(mod.BridgeError, "redirect_refused"):
            mod._NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere")

    def test_results_retry_without_reexecution(self):
        command = self.command("naia_generate", {"prompt": "one"})
        result, _ = self.bridge._handle(command)
        posts = []
        self.bridge._post = lambda config, path, body: posts.append((path, body)) or {"accepted": True}
        self.bridge._flush_results(self.bridge._config())
        self.bridge._flush_results(self.bridge._config())
        self.assertEqual(len(posts), 1); self.assertEqual(posts[0][1]["result"], result)
        self.assertEqual(self.count, 1)

    def test_canonical_prompt_handler_preserves_other_field_and_broadcasts(self):
        # Compile the exact audited handler, keeping its set_prompt implementation.
        source = ROOT.parent / "source/NAIA2.0/app/backend/server/session_commands.py"
        tree = ast.parse(source.read_text())
        function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_session_command")
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), function], type_ignores=[])
        namespace = {"json": json}; exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
        fake = types.ModuleType("app.backend.server.session_commands"); fake.handle_session_command = namespace["handle_session_command"]
        self.settings["permissions"]["read"] = False
        with patch.dict(sys.modules, {"app.backend.server.session_commands": fake}):
            result, error = self.call("naia_set_prompt", {"prompt": "new", "expected_prompt": "original"})
        self.assertFalse(error); self.assertEqual(result, {"updated": True, "fields": ["prompt"]})
        self.assertEqual(self.runtime.negative_prompt_text, "negative")
        self.assertNotIn("negative", json.dumps(result))
        self.assertEqual(self.runtime.saved, 1)
        event, payload = self.runtime.events[-1]
        self.assertEqual(event, "search_pool_broadcast"); self.assertEqual(payload["type"], "prompt_sync")
        self.assertEqual(payload["negative"], "negative"); self.assertTrue(payload["force"])


if __name__ == "__main__": unittest.main()

