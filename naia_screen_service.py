"""Bounded, opt-in handoff between the device bridge and an active NAIA UI.

The containing loopback PanelServer authenticates requests. This broker does
not expose HTTP, credentials, arbitrary JavaScript, or arbitrary selectors.
Only a ready frontend may poll; a command is claimed once and is never replayed.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math
import re
import secrets
import threading
import time


MAX_RESULT_BYTES = 900000
MAX_IMAGE_BASE64 = 600000
MAX_IMAGE_DIMENSION = 1600
MAX_CONTROLS = 160
MAX_METADATA_BYTES = 280000
_CLIENT = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_SCREEN = re.compile(r"^screen_[A-Za-z0-9_-]{1,120}$")
_TARGET = re.compile(r"^t[1-9][0-9]{0,5}$")
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_ACTIONS = frozenset(("click", "fill", "select", "scroll"))


class ScreenError(Exception):
    def __init__(self, code):
        self.code = code if isinstance(code, str) and _CODE.fullmatch(code) else "screen_request_failed"
        super().__init__(self.code)


def _text(value, maximum, code="invalid_screen_result"):
    if not isinstance(value, str) or len(value) > maximum:
        raise ScreenError(code)
    return value


def _number(value, minimum, maximum):
    if type(value) not in (int, float) or not minimum <= value <= maximum or not math.isfinite(value):
        raise ScreenError("invalid_screen_result")
    return value


def _dimension(value, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ScreenError("invalid_screen_result")
    return value


def _jpeg_dimensions(data):
    """Read the SOF dimensions without decoding untrusted pixels."""
    if not data.startswith(b"\xff\xd8\xff") or not data.endswith(b"\xff\xd9"):
        raise ScreenError("invalid_screen_image")
    position = 2
    sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while position + 4 <= len(data):
        if data[position] != 0xFF:
            raise ScreenError("invalid_screen_image")
        while position < len(data) and data[position] == 0xFF:
            position += 1
        if position >= len(data):
            break
        marker = data[position]
        position += 1
        if marker in (0xDA, 0xD9):
            break
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            continue
        if position + 2 > len(data):
            break
        size = int.from_bytes(data[position:position + 2], "big")
        if size < 2 or position + size > len(data):
            break
        if marker in sof:
            if size < 8:
                break
            height = int.from_bytes(data[position + 3:position + 5], "big")
            width = int.from_bytes(data[position + 5:position + 7], "big")
            return width, height
        position += size
    raise ScreenError("invalid_screen_image")


def _image(value):
    if not isinstance(value, dict) or value.get("mimeType") != "image/jpeg":
        raise ScreenError("invalid_screen_image")
    data = _text(value.get("data"), MAX_IMAGE_BASE64, "invalid_screen_image")
    try:
        pixels = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error):
        raise ScreenError("invalid_screen_image") from None
    width = _dimension(value.get("width"), MAX_IMAGE_DIMENSION)
    height = _dimension(value.get("height"), MAX_IMAGE_DIMENSION)
    if _jpeg_dimensions(pixels) != (width, height):
        raise ScreenError("invalid_screen_image")
    return {"mimeType": "image/jpeg", "data": data, "width": width, "height": height}


def sanitize_result(result):
    """Copy only the documented screen fields; never relay ad-hoc UI data."""
    if not isinstance(result, dict):
        raise ScreenError("invalid_screen_result")
    try:
        if len(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")) > MAX_RESULT_BYTES:
            raise ScreenError("screen_result_too_large")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ScreenError("invalid_screen_result") from None
    if result.get("blocked") is True:
        raise ScreenError("protected_screen")
    snapshot_id = result.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not _SCREEN.fullmatch(snapshot_id):
        raise ScreenError("invalid_screen_result")
    viewport = result.get("viewport")
    if not isinstance(viewport, dict):
        raise ScreenError("invalid_screen_result")
    output = {
        "snapshot_id": snapshot_id,
        "title": _text(result.get("title"), 200),
        "viewport": {
            "width": _dimension(viewport.get("width"), 100000),
            "height": _dimension(viewport.get("height"), 100000),
            "scroll_x": _number(viewport.get("scroll_x"), -10000000, 10000000),
            "scroll_y": _number(viewport.get("scroll_y"), -10000000, 10000000),
        },
        "controls": [],
        "text": _text(result.get("text", ""), 12000),
        "blocked": False,
    }
    agent_version = result.get("agent_version")
    if isinstance(agent_version, str) and re.fullmatch(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}", agent_version):
        output["agent_version"] = agent_version
    if "capture_error" in result:
        code = result["capture_error"]
        if not isinstance(code, str) or not _CODE.fullmatch(code) or "image" in result:
            raise ScreenError("invalid_screen_result")
        output["capture_error"] = code
    controls = result.get("controls")
    if not isinstance(controls, list) or len(controls) > MAX_CONTROLS:
        raise ScreenError("invalid_screen_result")
    seen = set()
    for control in controls:
        if not isinstance(control, dict):
            raise ScreenError("invalid_screen_result")
        target, bounds, actions = control.get("target"), control.get("bounds"), control.get("actions")
        if (not isinstance(target, str) or not _TARGET.fullmatch(target) or int(target[1:]) > MAX_CONTROLS or target in seen
                or not isinstance(bounds, dict) or type(control.get("enabled")) is not bool
                or not isinstance(actions, list) or len(actions) > len(_ACTIONS)
                or any(not isinstance(action, str) or action not in _ACTIONS for action in actions)
                or len(set(actions)) != len(actions)):
            raise ScreenError("invalid_screen_result")
        seen.add(target)
        item = {
            "target": target, "role": _text(control.get("role"), 40),
            "label": _text(control.get("label"), 300),
            "enabled": control["enabled"], "actions": list(actions),
            "bounds": {key: _number(bounds.get(key), -10000000 if key in ("x", "y") else 0, 10000000)
                       for key in ("x", "y", "width", "height")},
        }
        if "value" in control:
            item["value"] = _text(control["value"], 4000)
        if "blocked_reason" in control:
            item["blocked_reason"] = _text(control["blocked_reason"], 100)
        if "options" in control:
            options = control["options"]
            if not isinstance(options, list) or len(options) > MAX_CONTROLS:
                raise ScreenError("invalid_screen_result")
            item["options"] = []
            for option in options:
                if not isinstance(option, dict):
                    raise ScreenError("invalid_screen_result")
                item["options"].append({"value": _text(option.get("value"), 4000),
                                        "label": _text(option.get("label"), 300)})
        output["controls"].append(item)
    if len(json.dumps(output, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")) > MAX_METADATA_BYTES:
        raise ScreenError("screen_result_too_large")
    if "image" in result:
        output["image"] = _image(result["image"])
    return output


def validate_arguments(name, arguments):
    if not isinstance(arguments, dict):
        raise ScreenError("invalid_screen_arguments")
    if name == "naia_get_screen":
        if set(arguments) - {"capture"} or ("capture" in arguments and type(arguments["capture"]) is not bool):
            raise ScreenError("invalid_screen_arguments")
    elif name == "naia_ui_action":
        if set(arguments) - {"snapshot_id", "action", "target", "value", "direction", "amount"}:
            raise ScreenError("invalid_screen_arguments")
        snapshot_id, action = arguments.get("snapshot_id"), arguments.get("action")
        if (not isinstance(snapshot_id, str) or not _SCREEN.fullmatch(snapshot_id)
                or not isinstance(action, str) or action not in _ACTIONS):
            raise ScreenError("invalid_screen_arguments")
        target = arguments.get("target")
        if target is not None and (not isinstance(target, str) or not _TARGET.fullmatch(target)
                                   or int(target[1:]) > MAX_CONTROLS):
            raise ScreenError("invalid_screen_arguments")
        if action != "scroll" and target is None:
            raise ScreenError("invalid_screen_arguments")
        if action in ("fill", "select"):
            _text(arguments.get("value"), 16000, "invalid_screen_arguments")
        elif "value" in arguments:
            raise ScreenError("invalid_screen_arguments")
        if action == "scroll":
            if arguments.get("direction") not in ("up", "down", "left", "right"):
                raise ScreenError("invalid_screen_arguments")
            amount = arguments.get("amount", 500)
            if type(amount) is not int or not 1 <= amount <= 2000:
                raise ScreenError("invalid_screen_arguments")
        elif "direction" in arguments or "amount" in arguments:
            raise ScreenError("invalid_screen_arguments")
    else:
        raise ScreenError("unknown_screen_tool")
    return dict(arguments)


class _Pending:
    def __init__(self, identifier, name, arguments, client, guard, loop, future, deadline):
        self.id, self.name, self.arguments, self.client = identifier, name, arguments, client
        self.guard, self.loop, self.future, self.deadline = guard, loop, future, deadline
        self.claimed = False


class ScreenService:
    def __init__(self, *, timeout=15.0, lease=6.0, clock=time.monotonic):
        if not 0 < timeout <= 25 or not 0 < lease <= timeout:
            raise ValueError("Invalid screen service timeout")
        self.timeout, self.lease, self.clock = timeout, lease, clock
        self._lock = threading.RLock()
        self._client, self._seen, self._pending, self._closed = None, 0.0, None, False
        self._snapshots = {}  # Bounded snapshot -> (browser client, expiry).

    @staticmethod
    def _client_id(value):
        if not isinstance(value, str) or not _CLIENT.fullmatch(value):
            raise ScreenError("invalid_screen_client")
        return value

    @staticmethod
    def _check(pending):
        try:
            current = pending.guard()
        except Exception as error:
            code = str(error) if type(error).__name__ in ("BridgeError", "ScreenError") else "permission_check_failed"
            raise ScreenError(code) from None
        permissions = current.get("permissions") if isinstance(current, dict) else None
        if not isinstance(permissions, dict) or permissions.get("screen") is not True:
            raise ScreenError("permission_denied_screen")
        if pending.name == "naia_ui_action":
            if permissions.get("control") is not True:
                raise ScreenError("permission_denied_control")
        return {key: permissions.get(key) is True for key in ("screen", "control", "edit", "generate")}

    def _finish_locked(self, pending, *, result=None, error=None):
        if self._pending is pending:
            self._pending = None
        def settle():
            if pending.future.done():
                return
            if error is not None:
                pending.future.set_exception(error)
            else:
                pending.future.set_result(result)
        try:
            pending.loop.call_soon_threadsafe(settle)
        except RuntimeError:
            pass  # The owning application loop has already stopped.

    def _expire_locked(self):
        now = self.clock()
        self._snapshots = {key: value for key, value in self._snapshots.items() if value[1] > now}
        pending = self._pending
        if pending and now >= pending.deadline:
            self._finish_locked(pending, error=ScreenError("screen_request_expired"))
        if self._client is not None and now - self._seen > self.lease:
            if self._pending:
                self._finish_locked(self._pending, error=ScreenError("screen_client_disconnected"))
            self._client = None

    def status(self):
        with self._lock:
            self._expire_locked()
            return {"ready": self._client is not None and not self._closed,
                    "busy": self._pending is not None}

    async def request(self, name, arguments, guard):
        arguments = validate_arguments(name, arguments)
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        pending = _Pending("ui_" + secrets.token_urlsafe(24), name, arguments, None, guard,
                           loop, future, self.clock() + self.timeout)
        self._check(pending)
        with self._lock:
            self._expire_locked()
            if self._closed:
                raise ScreenError("screen_service_closed")
            if self._client is None:
                raise ScreenError("screen_not_ready")
            if self._pending is not None:
                raise ScreenError("screen_busy")
            if name == "naia_ui_action":
                snapshot = self._snapshots.get(arguments["snapshot_id"])
                if snapshot is None:
                    raise ScreenError("stale_snapshot")
                if snapshot[0] != self._client:
                    raise ScreenError("screen_session_changed")
                # A command must never migrate to another browser after a lease
                # handoff, or be replayed through a second recent snapshot.
                self._snapshots.clear()
            pending.client = self._client
            self._pending = pending
        try:
            result = await asyncio.wait_for(future, self.timeout)
            self._check(pending)
            return result
        except asyncio.TimeoutError:
            raise ScreenError("screen_request_expired") from None
        finally:
            with self._lock:
                if self._pending is pending:
                    self._pending = None

    def poll(self, client_id):
        client_id = self._client_id(client_id)
        with self._lock:
            self._expire_locked()
            if self._closed:
                raise ScreenError("screen_service_closed")
            if self._client is not None and self._client != client_id:
                return {"commands": [], "active": False, "reason": "another_screen_active"}
            self._client, self._seen = client_id, self.clock()
            pending = self._pending
            if pending is None or pending.claimed:
                return {"commands": [], "active": True}
            try:
                permissions = self._check(pending)
            except ScreenError as error:
                self._finish_locked(pending, error=error)
                return {"commands": [], "active": True}
            pending.claimed = True
            return {"active": True, "commands": [{"id": pending.id, "name": pending.name,
                "arguments": dict(pending.arguments), "permissions": permissions}]}

    def submit(self, client_id, payload):
        client_id = self._client_id(client_id)
        if (not isinstance(payload, dict) or set(payload) != {"id", "result", "is_error"}
                or type(payload.get("is_error")) is not bool):
            raise ScreenError("invalid_screen_response")
        with self._lock:
            self._expire_locked()
            pending = self._pending
            if (self._closed or self._client != client_id or pending is None
                    or pending.client != client_id or pending.id != payload.get("id") or not pending.claimed):
                raise ScreenError("screen_request_not_active")
            try:
                self._check(pending)
                if payload["is_error"]:
                    result = payload["result"]
                    error = result.get("error") if isinstance(result, dict) else None
                    code = error.get("code") if isinstance(error, dict) else error
                    raise ScreenError(code)
                result = sanitize_result(payload["result"])
                if pending.name == "naia_get_screen" and pending.arguments.get("capture") is False and "image" in result:
                    raise ScreenError("unexpected_screen_image")
            except ScreenError as error:
                self._finish_locked(pending, error=error)
            else:
                self._snapshots[result["snapshot_id"]] = (pending.client, self.clock() + 120)
                while len(self._snapshots) > 4:
                    del self._snapshots[next(iter(self._snapshots))]
                self._finish_locked(pending, result=result)
            return {"accepted": True}

    def close(self):
        with self._lock:
            self._closed, self._client = True, None
            self._snapshots.clear()
            if self._pending:
                self._finish_locked(self._pending, error=ScreenError("screen_service_closed"))

