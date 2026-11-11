"""No-network tests of the UI lease, one-shot commands, and result boundary."""
import asyncio
import base64
import importlib.util
import json
from pathlib import Path
import threading
import unittest


spec = importlib.util.spec_from_file_location("screen_service_test_target",
    Path(__file__).resolve().parents[1] / "naia_screen_service.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
CLIENT = "visible_client_0123456789"
OTHER = "other_client_01234567890"


def snapshot(**changes):
    result = {"snapshot_id": "screen_abc123", "title": "NAIA",
              "viewport": {"width": 900, "height": 700, "scroll_x": 0, "scroll_y": 0},
              "controls": [{"target": "t1", "role": "button", "label": "Artist", "enabled": True,
                            "actions": ["click"], "bounds": {"x": 0, "y": 0, "width": 100, "height": 40}}],
              "text": "Artist", "blocked": False}
    result.update(changes)
    return result


def tiny_jpeg():
    # SOI, SOF0 with 1x1 dimensions, EOI. Parsing here never decodes image data.
    raw = bytes.fromhex("ffd8 ffc0 000b 08 0001 0001 01 01 11 00 ffd9")
    return {"mimeType": "image/jpeg", "data": base64.b64encode(raw).decode(), "width": 1, "height": 1}


class ScreenServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 100.0
        self.service = mod.ScreenService(clock=lambda: self.now)
        self.permissions = {"screen": True, "control": True, "edit": True, "generate": False}
        self.guard_calls = 0
        self.addCleanup(self.service.close)

    def guard(self):
        self.guard_calls += 1
        return {"permissions": dict(self.permissions)}

    async def claimed(self, name="naia_get_screen", arguments=None):
        self.service.poll(CLIENT)
        task = asyncio.create_task(self.service.request(name, arguments or {}, self.guard))
        await asyncio.sleep(0)
        command = self.service.poll(CLIENT)["commands"][0]
        return task, command

    async def test_no_active_ui_means_no_request(self):
        with self.assertRaisesRegex(mod.ScreenError, "screen_not_ready"):
            await self.service.request("naia_get_screen", {}, self.guard)

    async def test_cross_thread_result_returns_sanitized_snapshot(self):
        task, command = await self.claimed()
        self.assertEqual(command["permissions"]["generate"], False)
        payload = snapshot(internal_secret="never_forward", agent_version="0.2.3")
        payload["controls"][0]["private_url"] = "must_not_forward"
        result = []
        thread = threading.Thread(target=lambda: result.append(self.service.submit(CLIENT,
            {"id": command["id"], "result": payload, "is_error": False})))
        thread.start(); thread.join(1)
        self.assertEqual(result, [{"accepted": True}])
        response = await task
        self.assertEqual(response["snapshot_id"], "screen_abc123")
        self.assertEqual(response["agent_version"], "0.2.3")
        self.assertNotIn("internal_secret", response)
        self.assertNotIn("private_url", response["controls"][0])
        self.assertGreaterEqual(self.guard_calls, 4)

    async def test_claim_is_not_redelivered_and_another_ui_cannot_steal(self):
        task, command = await self.claimed()
        self.assertEqual(self.service.poll(CLIENT)["commands"], [])
        self.assertFalse(self.service.poll(OTHER)["active"])
        with self.assertRaisesRegex(mod.ScreenError, "screen_request_not_active"):
            self.service.submit(OTHER, {"id": command["id"], "result": snapshot(), "is_error": False})
        self.service.submit(CLIENT, {"id": command["id"], "result": snapshot(), "is_error": False})
        await task
        with self.assertRaisesRegex(mod.ScreenError, "screen_request_not_active"):
            self.service.submit(CLIENT, {"id": command["id"], "result": snapshot(), "is_error": False})

    async def test_revocation_before_claim_prevents_delivery(self):
        self.service.poll(CLIENT)
        task = asyncio.create_task(self.service.request("naia_get_screen", {}, self.guard))
        await asyncio.sleep(0)
        self.permissions["screen"] = False
        self.assertEqual(self.service.poll(CLIENT)["commands"], [])
        with self.assertRaisesRegex(mod.ScreenError, "permission_denied_screen"):
            await task

    async def test_revocation_after_claim_discards_result(self):
        task, command = await self.claimed()
        self.permissions["screen"] = False
        self.service.submit(CLIENT, {"id": command["id"], "result": snapshot(), "is_error": False})
        with self.assertRaisesRegex(mod.ScreenError, "permission_denied_screen"):
            await task

    async def test_pairing_revocation_guard_checked_before_delivery(self):
        class BridgeError(Exception):
            pass
        def revoked():
            raise BridgeError("pairing_changed")
        with self.assertRaisesRegex(mod.ScreenError, "pairing_changed"):
            await self.service.request("naia_get_screen", {}, revoked)

    async def test_disconnected_client_cancels_pending_before_new_ui_takes_lease(self):
        task, command = await self.claimed()
        self.now += 7
        self.assertTrue(self.service.poll(OTHER)["active"])
        with self.assertRaisesRegex(mod.ScreenError, "screen_client_disconnected"):
            await task
        with self.assertRaisesRegex(mod.ScreenError, "screen_request_not_active"):
            self.service.submit(CLIENT, {"id": command["id"], "result": snapshot(), "is_error": False})

    async def test_expiry_never_delivers_late_ui_command(self):
        task, command = await self.claimed()
        self.now += 16
        self.assertEqual(self.service.poll(CLIENT)["commands"], [])
        with self.assertRaisesRegex(mod.ScreenError, "screen_request_expired"):
            await task

    async def test_cancelled_request_cannot_execute_later(self):
        self.service.poll(CLIENT)
        task = asyncio.create_task(self.service.request("naia_get_screen", {}, self.guard))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.service.poll(CLIENT)["commands"], [])

    async def test_ui_input_needs_control_but_not_direct_api_edit_permission(self):
        self.service.poll(CLIENT)
        args = {"snapshot_id": "screen_x", "action": "click", "target": "t1"}
        self.permissions["control"] = False
        with self.assertRaisesRegex(mod.ScreenError, "permission_denied_control"):
            await self.service.request("naia_ui_action", args, self.guard)
        self.permissions["control"] = True; self.permissions["edit"] = False
        task, command = await self.claimed()
        self.service.submit(CLIENT, {"id": command["id"], "result": snapshot(), "is_error": False})
        screen = await task
        task, command = await self.claimed("naia_ui_action", {**args, "snapshot_id": screen["snapshot_id"], "action": "fill", "value": "test"})
        self.service.submit(CLIENT, {"id": command["id"], "result": snapshot(image=tiny_jpeg()), "is_error": False})
        self.assertIn("image", await task)

    async def test_error_codes_only_no_untrusted_error_message(self):
        task, command = await self.claimed()
        self.service.submit(CLIENT, {"id": command["id"], "result": {"error": {"code": "protected_screen", "secret": "drop"}}, "is_error": True})
        with self.assertRaisesRegex(mod.ScreenError, "^protected_screen$"):
            await task

    async def test_snapshot_action_cannot_move_to_a_different_browser(self):
        task, command = await self.claimed()
        self.service.submit(CLIENT, {"id": command["id"], "result": snapshot(), "is_error": False})
        await task
        self.now += 7
        self.assertTrue(self.service.poll(OTHER)["active"])
        with self.assertRaisesRegex(mod.ScreenError, "screen_session_changed"):
            await self.service.request("naia_ui_action", {"snapshot_id": "screen_abc123", "action": "click", "target": "t1"}, self.guard)
        self.assertEqual(self.service.poll(OTHER)["commands"], [])

    async def test_read_snapshot_survives_another_read_but_action_consumes_both(self):
        for identifier in ("screen_first", "screen_second"):
            task, command = await self.claimed()
            self.service.submit(CLIENT, {"id": command["id"], "result": snapshot(snapshot_id=identifier), "is_error": False})
            await task
        args = {"snapshot_id": "screen_first", "action": "click", "target": "t1"}
        task, command = await self.claimed("naia_ui_action", args)
        self.service.submit(CLIENT, {"id": command["id"], "result": snapshot(snapshot_id="screen_after"), "is_error": False})
        await task
        for identifier in ("screen_first", "screen_second"):
            with self.assertRaisesRegex(mod.ScreenError, "stale_snapshot"):
                await self.service.request("naia_ui_action", {**args, "snapshot_id": identifier}, self.guard)

    async def test_expired_snapshot_never_dispatches_an_action(self):
        task, command = await self.claimed()
        self.service.submit(CLIENT, {"id": command["id"], "result": snapshot(), "is_error": False})
        await task
        self.now += 121
        self.service.poll(CLIENT)
        with self.assertRaisesRegex(mod.ScreenError, "stale_snapshot"):
            await self.service.request("naia_ui_action", {"snapshot_id": "screen_abc123", "action": "click", "target": "t1"}, self.guard)
        self.assertEqual(self.service.poll(CLIENT)["commands"], [])

    async def test_capture_defaults_on_and_explicit_false_remains_metadata_only(self):
        for arguments in ({}, {"capture": True}):
            task, command = await self.claimed(arguments=arguments)
            self.service.submit(CLIENT, {"id": command["id"], "result": snapshot(image=tiny_jpeg()), "is_error": False})
            self.assertEqual((await task)["image"]["mimeType"], "image/jpeg")
        task, command = await self.claimed(arguments={"capture": False})
        self.service.submit(CLIENT, {"id": command["id"], "result": snapshot(image=tiny_jpeg()), "is_error": False})
        with self.assertRaisesRegex(mod.ScreenError, "unexpected_screen_image"):
            await task

    async def test_capture_failure_is_reported_without_losing_completed_action(self):
        task, command = await self.claimed()
        self.service.submit(CLIENT, {"id": command["id"], "result": snapshot(), "is_error": False})
        await task
        task, command = await self.claimed("naia_ui_action", {"snapshot_id": "screen_abc123", "action": "click", "target": "t1"})
        self.service.submit(CLIENT, {"id": command["id"], "result": snapshot(capture_error="capture_unavailable"), "is_error": False})
        self.assertEqual((await task)["capture_error"], "capture_unavailable")
        for value in ("secret /tmp/path", {}, "", "x" * 81):
            with self.assertRaises(mod.ScreenError):
                mod.sanitize_result(snapshot(capture_error=value))
        with self.assertRaises(mod.ScreenError):
            mod.sanitize_result(snapshot(capture_error="capture_failed", image=tiny_jpeg()))

    async def test_invalid_or_oversized_result_settles_with_error(self):
        task, command = await self.claimed()
        self.service.submit(CLIENT, {"id": command["id"], "result": snapshot(extra="x" * 900001), "is_error": False})
        with self.assertRaisesRegex(mod.ScreenError, "screen_result_too_large"):
            await task

    async def test_concurrent_commands_do_not_queue_hidden_mutations(self):
        task, command = await self.claimed()
        with self.assertRaisesRegex(mod.ScreenError, "screen_busy"):
            await self.service.request("naia_get_screen", {}, self.guard)
        self.service.close()
        with self.assertRaisesRegex(mod.ScreenError, "screen_service_closed"):
            await task


class ResultBoundaryTests(unittest.TestCase):
    def test_metadata_budget_matches_compact_frontend_and_relay_json(self):
        result = snapshot()
        template = result["controls"][0]
        result["controls"] = [{**template, "target": "t" + str(i + 1),
            "options": [{"value": "", "label": ""} for _ in range(160)]} for i in range(64)]
        self.assertLess(len(json.dumps(result, separators=(",", ":")).encode()), mod.MAX_METADATA_BYTES)
        self.assertGreater(len(json.dumps(result).encode()), mod.MAX_METADATA_BYTES)
        self.assertEqual(len(mod.sanitize_result(result)["controls"]), 64)

    def test_mime_bytes_and_dimensions_must_agree(self):
        for change in ({"mimeType": "image/png"}, {"data": "not-base64"}, {"width": 2}, {"width": 1601}):
            with self.subTest(change=change), self.assertRaises(mod.ScreenError):
                mod.sanitize_result(snapshot(image={**tiny_jpeg(), **change}))

    def test_nonfinite_geometry_and_duplicate_targets_are_rejected(self):
        result = snapshot()
        result["controls"][0]["bounds"]["x"] = float("nan")
        with self.assertRaises(mod.ScreenError):
            mod.sanitize_result(result)
        result = snapshot()
        result["controls"][0]["bounds"]["x"] = 10 ** 400
        with self.assertRaises(mod.ScreenError):
            mod.sanitize_result(result)
        result = snapshot(); result["controls"] *= 2
        with self.assertRaises(mod.ScreenError):
            mod.sanitize_result(result)

    def test_protected_snapshot_is_never_forwarded(self):
        with self.assertRaisesRegex(mod.ScreenError, "protected_screen"):
            mod.sanitize_result(snapshot(blocked=True))

    def test_unsupported_argument_cannot_become_dom_code(self):
        invalid = [
            ("naia_get_screen", {"script": "alert(1)"}),
            ("naia_ui_action", {"snapshot_id": "screen_a", "action": "evaluate", "target": "t1"}),
            ("naia_ui_action", {"snapshot_id": "screen_a", "action": [], "target": "t1"}),
            ("naia_ui_action", {"snapshot_id": "screen_a", "action": "click", "target": "t161"}),
            ("naia_ui_action", {"snapshot_id": "screen_a", "action": "click", "target": "#settings"}),
            ("naia_ui_action", {"snapshot_id": "screen_a", "action": "click", "target": "t1", "value": "x"}),
            ("naia_ui_action", {"snapshot_id": "screen_a", "action": "scroll", "direction": "down", "amount": 100000}),
        ]
        for name, args in invalid:
            with self.subTest(args=args), self.assertRaises(mod.ScreenError):
                mod.validate_arguments(name, args)


if __name__ == "__main__":
    unittest.main()
