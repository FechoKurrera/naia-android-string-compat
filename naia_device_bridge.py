"""Opt-in outbound device control for the audited NAIA 2.0.48 runtime.

No model-inference API is called. A private relay passes bounded commands;
the local extension remains the permission gate. All mutations run on the
headless event loop and every accepted command has a durable replay marker.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

RUNTIME_VERSION = "0.2.9"
MAX_AGE = 120
MAX_PROMPT = 16000
MAX_RESULT_BYTES = 60000
MAX_SCREEN_RESULT_BYTES = 900000
MAX_JOURNAL = 256
MAX_JOURNAL_BYTES = 16 * 1024 * 1024
PERMISSIONS = {
    "naia_get_state": "read", "naia_get_generation_status": "read",
    "naia_set_prompt": "edit", "naia_generate": "generate",
    "naia_cancel_generation": "generate",
    "naia_get_screen": "screen", "naia_ui_action": "control",
}
PARAM_KEYS = frozenset({"model", "sampler", "scheduler", "steps", "width", "height",
    "resolution", "seed", "seed_fixed", "cfg_scale", "cfg_rescale", "noise_schedule"})


class BridgeError(Exception):
    """An intentional non-sensitive error code."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise BridgeError("relay_redirect_refused")


def _origin(value):
    if not isinstance(value, str):
        raise BridgeError("invalid_relay_origin")
    try:
        parsed = urlsplit(value.strip())
        valid = (parsed.scheme == "https" and parsed.hostname and not parsed.username
                 and not parsed.password and not parsed.query and not parsed.fragment
                 and parsed.path in ("", "/") and parsed.port in (None, 443))
    except ValueError:
        valid = False
    if not valid:
        raise BridgeError("invalid_relay_origin")
    return "https://" + parsed.hostname.lower()


def _timestamp(value):
    if isinstance(value, bool):
        raise BridgeError("invalid_created_at")
    try:
        if isinstance(value, (int, float)):
            result = float(value)
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError()
            result = parsed.timestamp()
        else:
            raise ValueError()
        if not math.isfinite(result):
            raise ValueError()
        return result
    except ValueError:
        raise BridgeError("invalid_created_at") from None


def _text(value, nonempty=False):
    if not isinstance(value, str) or len(value) > MAX_PROMPT or (nonempty and not value.strip()):
        raise BridgeError("invalid_prompt")
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


class DeviceBridge:
    def __init__(self, ctx, settings_getter, *, state_root=None, allowed_origins=None, screen_service=None):
        self.ctx = ctx
        self.app = ctx._app_context
        self.settings_getter = settings_getter
        self.screen_service = screen_service
        self.allowed_origins = frozenset(_origin(x) for x in (allowed_origins or ()))
        self.state_root = Path(state_root if state_root is not None else ctx.ext_dir)
        self.journal_path = self.state_root / "device-bridge-journal.json"
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._opener = build_opener(_NoRedirect())
        self._state = "stopped"
        self._last_error = ""
        self._last_contact = None
        self._journal = {}
        self._journal_ok = True
        self._load_journal()

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="naia-device-plugin", daemon=True)
            self._thread.start()

    def close(self):
        self._stop.set()
        self._set_status("stopped")
        # Do not join on the backend loop while an in-flight call waits for it.

    def status(self):
        with self._lock:
            return {"state": self._state, "error": self._last_error, "last_contact": self._last_contact,
                    "running": bool(self._thread and self._thread.is_alive() and not self._stop.is_set())}

    def _set_status(self, state, error=""):
        with self._lock:
            self._state, self._last_error = state, error

    def _config(self):
        settings = self.settings_getter()
        if self._stop.is_set() or not isinstance(settings, dict) or settings.get("enabled") is not True:
            raise BridgeError("disabled")
        if not bool(getattr(getattr(self.ctx, "_record", None), "is_active", False)):
            raise BridgeError("extension_inactive")
        origin = _origin(settings.get("relay_url"))
        if origin not in self.allowed_origins:
            raise BridgeError("relay_origin_not_allowed")
        result = {"origin": origin}
        for key in ("device_id", "device_token", "relay_access_token"):
            value = settings.get(key)
            if not isinstance(value, str) or not value.strip() or len(value) > 8192 or any(c in value for c in "\r\n\0"):
                raise BridgeError("pairing_required")
            result[key] = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", result["device_id"]):
            raise BridgeError("invalid_device_id")
        permissions = settings.get("permissions")
        result["permissions"] = {k: isinstance(permissions, dict) and permissions.get(k) is True for k in ("read", "screen", "control", "edit", "generate")}
        result["pairing"] = hashlib.sha256(_json([result[k] for k in ("origin", "device_id", "device_token")]).encode()).hexdigest()
        return result

    def _require(self, config, name):
        # Waiting for the backend loop or a synchronous enqueue hook must not
        # turn a short-lived cloud command into a later surprise mutation.
        if "deadline" in config and time.time() > config["deadline"]:
            raise BridgeError("command_expired")
        current = self._config()
        if any(current[k] != config[k] for k in ("origin", "device_id", "device_token", "relay_access_token")):
            raise BridgeError("pairing_changed")
        permission = PERMISSIONS.get(name)
        if not permission:
            raise BridgeError("unknown_tool")
        if not current["permissions"][permission]:
            raise BridgeError("permission_denied_" + permission)
        if name == "naia_ui_action":
            if not current["permissions"]["screen"]:
                raise BridgeError("permission_denied_screen")
        return current

    def _delivery_result(self, config, name, result, error):
        if name in ("naia_get_screen", "naia_ui_action"):
            try:
                self._require(config, name)
            except BridgeError as exc:
                return {"error": str(exc), "retry": False}, True
        return result, error

    def _post(self, config, path, payload):
        if path not in ("/api/device/poll", "/api/device/result"):
            raise BridgeError("invalid_endpoint")
        request = Request(config["origin"] + path,
            data=_json({"device_id": config["device_id"], "device_token": config["device_token"], **payload}).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "OAI-Sites-Authorization": "Bearer " + config["relay_access_token"]}, method="POST")
        try:
            with self._opener.open(request, timeout=15) as response:
                if response.geturl() != request.full_url:
                    raise BridgeError("relay_redirect_refused")
                raw = response.read(256 * 1024 + 1)
        except HTTPError as error:
            raise BridgeError("relay_http_" + str(error.code)) from None
        if len(raw) > 256 * 1024:
            raise BridgeError("relay_response_too_large")
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise BridgeError("invalid_relay_response") from None
        if not isinstance(data, dict):
            raise BridgeError("invalid_relay_response")
        with self._lock:
            self._last_contact = int(time.time())
        return data

    def _run(self):
        failures = 0
        while not self._stop.is_set():
            try:
                config = self._config()
                self._set_status("connecting")
                self._flush_results(config)
                response = self._post(config, "/api/device/poll", {})
                commands = response.get("commands")
                if not isinstance(commands, list) or len(commands) > 16:
                    raise BridgeError("invalid_command_batch")
                self._set_status("connected")
                failures = 0
                for command in commands:
                    if self._stop.is_set():
                        break
                    result, error = self._handle(command, config)
                    command_id = command.get("id") if isinstance(command, dict) else None
                    if isinstance(command_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", command_id):
                        result, error = self._delivery_result(config, command.get("name"), result, error)
                        self._post(config, "/api/device/result", {"command_id": command_id, "result": result, "is_error": error})
                        self._ack(command_id, config)
            except BridgeError as error:
                code = str(error)
                self._set_status("disabled" if code in ("disabled", "extension_inactive") else "error", code)
                failures = min(failures + 1, 5)
            except Exception:
                self._set_status("error", "relay_unavailable")
                failures = min(failures + 1, 5)
            self._stop.wait(min(30, 3 * (2 ** failures)))

    def _load_journal(self):
        try:
            if not self.journal_path.exists():
                return
            if self.journal_path.stat().st_size > MAX_JOURNAL_BYTES:
                raise ValueError()
            data = json.loads(self.journal_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or len(data) > MAX_JOURNAL:
                raise ValueError()
            for key, item in data.items():
                if not isinstance(key, str) or not isinstance(item, dict) or item.get("state") not in ("started", "done") or not isinstance(item.get("created_at"), (int, float)):
                    raise ValueError()
            self._journal = data
        except (OSError, ValueError, TypeError):
            self._journal_ok = False
            self._last_error = "journal_unavailable"

    def _write_journal(self):
        if not self._journal_ok:
            raise BridgeError("journal_unavailable")
        temporary = self.journal_path.with_suffix(".tmp")
        try:
            self.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            raw = _json(self._journal).encode()
            if len(raw) > MAX_JOURNAL_BYTES:
                raise ValueError()
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as file:
                file.write(raw); file.flush(); os.fsync(file.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, self.journal_path)
        except (OSError, ValueError):
            self._journal_ok = False
            raise BridgeError("journal_unavailable") from None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _ack(self, command_id, config):
        with self._lock:
            item = self._journal.get(command_id)
            if item and item.get("pairing") == config["pairing"]:
                item["acked"] = True
                # Screens are transient. Keep a replay marker after relay ACK,
                # without retaining hundreds of screenshots on the tablet.
                if item.get("name") in ("naia_get_screen", "naia_ui_action") and item.get("state") == "done":
                    item.update(result={"error": "screen_result_already_delivered", "retry": False}, is_error=True)
                self._write_journal()

    def _flush_results(self, config):
        with self._lock:
            changed = False
            for item in self._journal.values():
                if item.get("name") in ("naia_get_screen", "naia_ui_action") and not item.get("acked") and (
                        item.get("pairing") != config["pairing"] or time.time() - item["created_at"] > MAX_AGE):
                    item.update(state="done", acked=True,
                        result={"error": "screen_result_discarded", "retry": False}, is_error=True)
                    changed = True
            if changed:
                self._write_journal()
            pending = [(key, dict(item)) for key, item in self._journal.items()
                       if item.get("pairing") == config["pairing"] and not item.get("acked")]
        for command_id, item in pending:
            if time.time() - item["created_at"] > MAX_AGE:
                self._ack(command_id, config)
                continue
            if item["state"] == "started":
                result, error = {"error": "previous_outcome_unknown", "retry": False}, True
            else:
                result, error = item["result"], item["is_error"]
            result, error = self._delivery_result(config, item.get("name"), result, error)
            try:
                self._post(config, "/api/device/result", {"command_id": command_id, "result": result, "is_error": error})
            except BridgeError as exc:
                if str(exc) not in ("relay_http_404", "relay_http_409"):
                    raise
            self._ack(command_id, config)

    def _validate(self, command):
        if not isinstance(command, dict) or set(command) - {"id", "name", "arguments", "created_at"}:
            raise BridgeError("invalid_command")
        command_id, name = command.get("id"), command.get("name")
        if not isinstance(command_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", command_id):
            raise BridgeError("invalid_command_id")
        if name not in PERMISSIONS:
            raise BridgeError("unknown_tool")
        stamp = _timestamp(command.get("created_at"))
        age = time.time() - stamp
        if age > MAX_AGE or age < -5:
            raise BridgeError("command_expired")
        args = command.get("arguments", {})
        if not isinstance(args, dict):
            raise BridgeError("invalid_arguments")
        allowed = {"naia_get_state": set(), "naia_get_generation_status": set(),
                   "naia_set_prompt": {"prompt", "negative_prompt", "expected_prompt"},
                   "naia_generate": {"prompt", "negative_prompt"},
                   "naia_cancel_generation": {"request_id"},
                   "naia_get_screen": {"capture"},
                   "naia_ui_action": {"snapshot_id", "action", "target", "value", "direction", "amount"}}[name]
        if set(args) - allowed:
            raise BridgeError("invalid_arguments")
        if name == "naia_set_prompt" and not ({"prompt", "negative_prompt"} & args.keys()):
            raise BridgeError("prompt_required")
        if name == "naia_generate" and "prompt" not in args:
            raise BridgeError("prompt_required")
        for key in ("prompt", "negative_prompt", "expected_prompt"):
            if key in args:
                _text(args[key], name == "naia_generate" and key == "prompt")
        if name == "naia_cancel_generation" and (not isinstance(args.get("request_id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", args["request_id"])):
            raise BridgeError("invalid_request_id")
        return command_id, name, args, stamp

    def _handle(self, command, config=None):
        marked = False
        command_id = None
        try:
            config = config or self._config()
            command_id, name, args, stamp = self._validate(command)
            config = {**config, "deadline": stamp + MAX_AGE}
            self._require(config, name)
            fingerprint = hashlib.sha256(_json([name, args, stamp]).encode()).hexdigest()
            with self._lock:
                existing = self._journal.get(command_id)
                if existing:
                    if existing.get("pairing") != config["pairing"] or existing.get("fingerprint") != fingerprint:
                        raise BridgeError("command_id_conflict")
                    if existing["state"] != "done":
                        return {"error": "previous_outcome_unknown", "retry": False}, True
                    return existing["result"], existing["is_error"]
                # Prune only expired replay windows; never evict a live marker.
                if len(self._journal) >= MAX_JOURNAL:
                    old = sorted(self._journal, key=lambda key: self._journal[key]["created_at"])
                    for key in old:
                        if len(self._journal) < MAX_JOURNAL:
                            break
                        if time.time() - self._journal[key]["created_at"] > MAX_AGE + 5:
                            del self._journal[key]
                    if len(self._journal) >= MAX_JOURNAL:
                        raise BridgeError("command_capacity_reached")
                self._journal[command_id] = {"pairing": config["pairing"], "fingerprint": fingerprint,
                    "state": "started", "created_at": stamp, "name": name, "acked": False}
                self._write_journal()
                marked = True
            result = self._on_loop(config, name, args)
            limit = MAX_SCREEN_RESULT_BYTES if name in ("naia_get_screen", "naia_ui_action") else MAX_RESULT_BYTES
            if len(_json(result).encode()) > limit:
                raise BridgeError("result_too_large")
            error = False
        except BridgeError as exc:
            result, error = {"error": str(exc), "retry": False}, True
        except Exception:
            result, error = {"error": "runtime_command_failed", "retry": False}, True
        if marked:
            with self._lock:
                self._journal[command_id].update(state="done", result=result, is_error=error)
                try:
                    self._write_journal()
                except BridgeError:
                    # The existing durable started marker prevents execution on restart.
                    return {"error": "outcome_persistence_failed", "retry": False}, True
        return result, error

    def _on_loop(self, config, name, args):
        loop = getattr(self.app, "headless_main_loop", None)
        if loop is None or loop.is_closed() or not loop.is_running():
            raise BridgeError("backend_loop_unavailable")
        future = asyncio.run_coroutine_threadsafe(self._dispatch(config, name, args), loop)
        try:
            return future.result(timeout=30)
        except FutureTimeout:
            future.cancel()
            raise BridgeError("runtime_outcome_unknown") from None

    async def _dispatch(self, config, name, args):
        self._require(config, name)
        if name in ("naia_get_screen", "naia_ui_action"):
            if self.screen_service is None:
                raise BridgeError("screen_service_unavailable")
            try:
                return await self.screen_service.request(name, args, lambda: self._require(config, name))
            except Exception as error:
                if type(error).__name__ == "ScreenError":
                    raise BridgeError(error.code) from None
                raise
        if name == "naia_get_state":
            params = getattr(self.app, "remote_params", {}) or {}
            selected = {key: value for key, value in params.items() if key in PARAM_KEYS and
                        (value is None or type(value) in (str, int, float, bool)) and
                        (not isinstance(value, str) or len(value) <= 200)}
            return {"prompt": str(getattr(self.app, "prompt_text", ""))[:MAX_PROMPT],
                    "negative_prompt": str(getattr(self.app, "negative_prompt_text", ""))[:MAX_PROMPT],
                    "api_mode": str(self.app.get_api_mode())[:40], "parameters": selected,
                    "generation": self._generation_status(config),
                    "diagnostics": {"runtime_version": RUNTIME_VERSION,
                        "screen": self.screen_service.status() if self.screen_service else {"ready": False}}}
        if name == "naia_get_generation_status":
            return self._generation_status(config)
        if name == "naia_set_prompt":
            if "expected_prompt" in args and args["expected_prompt"] != str(self.app.prompt_text or ""):
                raise BridgeError("prompt_changed")
            return await self._set_prompt(args)
        if name == "naia_generate":
            return self._generate(config, args)
        return self._cancel(config, args["request_id"])

    async def _set_prompt(self, args):
        from app.backend.server.session_commands import handle_session_command
        app = self.app
        class BroadcastSocket:
            async def send_text(self, text):
                payload = json.loads(text)
                if payload.get("type") == "prompt_sync":
                    payload["force"] = True
                app.publish("search_pool_broadcast", payload)
        async def broadcast(_clients, payload):
            app.publish("search_pool_broadcast", payload)
        command = {"type": "set_prompt", "prompt": args.get("prompt", str(app.prompt_text or "")),
                   "negative_prompt": args.get("negative_prompt", str(app.negative_prompt_text or ""))}
        accepted = await handle_session_command(BroadcastSocket(), app, set(), "127.0.0.1", command,
            broadcast_json=broadcast, run_in_thread=asyncio.to_thread)
        if accepted is not True:
            raise BridgeError("prompt_update_rejected")
        # Edit and read permissions are independent. In particular, a negative
        # prompt preserved by this update must not leak through an edit result.
        return {"updated": True, "fields": [key for key in ("prompt", "negative_prompt") if key in args]}

    def _busy(self):
        task = getattr(self.app, "headless_generation_runner_task", None)
        return bool(getattr(self.app, "is_generating", False) or
                    getattr(self.app, "headless_generation_runner_active", False) or
                    (task is not None and not task.done()))

    def _generation_status(self, config):
        queue = self.app.generation_queue_manager
        queued = {str(request.request_id) for request in queue.get_all_requests()}
        requests = {}
        with self._lock:
            for item in sorted(self._journal.values(), key=lambda entry: entry["created_at"]):
                if item.get("pairing") != config["pairing"] or item.get("state") != "done" or item.get("is_error"):
                    continue
                result = item.get("result", {})
                request_id = result.get("request_id") if isinstance(result, dict) else None
                if request_id and item.get("name") == "naia_generate":
                    requests[request_id] = "pending" if request_id in queued else "no_longer_pending"
                elif request_id and item.get("name") == "naia_cancel_generation" and result.get("cancelled"):
                    requests[request_id] = "cancelled"
        return {"is_generating": self._busy(), "queued": len(queued), "paused": bool(queue.is_paused()),
                "requests": [{"request_id": key, "status": value} for key, value in list(requests.items())[-32:]],
                "note": "no_longer_pending does not establish generation success"}

    def _generate(self, config, args):
        from core.headless_generation_service import HeadlessGenerationService
        queue = self.app.generation_queue_manager
        if self._busy() or queue.is_paused() or not queue.is_empty():
            raise BridgeError("generation_queue_busy")
        if not getattr(self.app, "headless_generation_execute_enabled", False):
            raise BridgeError("generation_execution_disabled")
        if self.app.get_api_mode() != "NAI":
            raise BridgeError("novelai_mode_required")
        options = self.app.get_options()
        if options.get("auto_generate") or getattr(self.app, "remote_params", {}).get("auto_generate"):
            raise BridgeError("continuous_generation_enabled")
        if HeadlessGenerationService.is_sequence_command_text(args["prompt"]):
            raise BridgeError("sequence_generation_not_supported")
        if not callable(getattr(self.app, "extension_queue_start_bridge", None)):
            raise BridgeError("generation_runner_unavailable")
        self._require(config, "naia_generate")
        result = self.ctx.enqueue_generation(prompt=args["prompt"],
            negative_prompt=args.get("negative_prompt", str(self.app.negative_prompt_text or "")),
            api_mode="NAI", allow_chain=False)
        request_id = str(result.get("request_id") or "") if isinstance(result, dict) else ""
        if not isinstance(result, dict) or not result.get("ok") or not request_id:
            raise BridgeError("generation_request_rejected")
        try:
            self._require(config, "naia_generate")
            current = queue.get_all_requests()
            # Hooks can enqueue other work synchronously. Never start that queue.
            if len(current) != 1 or str(current[0].request_id) != request_id or self._busy() or queue.is_paused():
                raise BridgeError("generation_queue_changed")
            params = getattr(current[0], "params", {}) or {}
            if any(params.get(key) for key in ("auto_generate", "sequence_run_id", "event_stream_run_id", "automation_run_id")):
                raise BridgeError("continuous_generation_enabled")
            started = self.ctx.start_generation_queue()
            if not isinstance(started, dict) or not started.get("ok"):
                raise BridgeError("generation_start_failed")
        except BaseException:
            queue.remove_request(request_id)
            raise
        return {"request_id": request_id, "status": "requested", "count": 1,
                "message": "Generation was requested; completion is not yet known."}

    def _cancel(self, config, request_id):
        with self._lock:
            owned = any(item.get("pairing") == config["pairing"] and item.get("name") == "naia_generate"
                        and not item.get("is_error") and item.get("result", {}).get("request_id") == request_id
                        for item in self._journal.values())
        if not owned:
            raise BridgeError("request_not_owned")
        self._require(config, "naia_cancel_generation")
        # No broad stop, no queue clear, no cancellation tombstone for in-flight jobs.
        removed = bool(self.app.generation_queue_manager.remove_request(request_id))
        return {"request_id": request_id, "cancelled": removed,
                "status": "cancelled" if removed else "no_longer_pending"}
