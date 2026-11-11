"""NAIA v2.0.48 Android tools. Credentials stay in private app storage."""
from __future__ import annotations
import atexit
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import types

RELAY_URL = "https://naia-tablet-link.ugi8296.chatgpt.site"
DEFAULTS = {"low_memory": True, "cache_mb": 32, "plugin_enabled": False,
            "allow_read": True, "allow_screen": False, "allow_control": False,
            "allow_edit": False, "allow_generate": False, "comic_server_enabled": False}
_PACKAGE_PREFIX = "_naia_android_tools_runtime_"
_CONTROLLER_ATTRIBUTE = "_naia_android_tools_controller"
_MODULE_NAMES = ("naia_folder_open", "naia_artist_memory", "naia_device_bridge",
                 "naia_tools_ui", "naia_screen_service", "naia_comic_compat", "naia_prompt_server",
                 "naia_history_library", "naia_android_tree", "naia_local_photo_picker")
_RUNTIME_FILES = ("main.py", *(name + ".py" for name in _MODULE_NAMES),
                  "android_tools_panel.html", "naia_screen_agent.js", "naia_history_library.js",
                  "vendor/html2canvas-1.4.1.min.js", "promptserver-source-manifest.json")
_controller = None

def _runtime_package():
    """Bind helpers to the bytes on disk, not a process-global version label.

    NAIA retries replace the entry module but do not clear helper modules in
    sys.modules. Android can also retain the Python process when its UI closes.
    Read one complete bundle and give each changed bundle its own namespace.
    """
    directory = Path(__file__).resolve().parent
    before = directory.stat()
    digest = hashlib.sha256(str(directory).encode("utf-8"))
    sources = {}
    for filename in _RUNTIME_FILES:
        content = (directory / filename).read_bytes()
        digest.update(filename.encode("utf-8") + b"\0")
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
        if filename.endswith(".py") and filename != "main.py":
            sources[Path(filename).stem] = content
    after = directory.stat()
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise RuntimeError("Android 도구 파일 교체가 진행 중입니다. 완료 후 다시 활성화하세요.")
    package_name = _PACKAGE_PREFIX + digest.hexdigest()
    package = sys.modules.get(package_name)
    if package is None:
        package = types.ModuleType(package_name)
        package.__path__ = [str(directory)]
        package.__package__ = package_name
        package.__spec__ = importlib.util.spec_from_loader(package_name, loader=None, is_package=True)
        package._naia_sources = sources
        package._naia_load_lock = threading.RLock()
        sys.modules[package_name] = package
    return package

def _module(name, package=None):
    if name not in _MODULE_NAMES:
        raise ValueError("지원하지 않는 Android 도구 모듈입니다.")
    package = package or _runtime_package()
    fullname = package.__name__ + "." + name
    with package._naia_load_lock:
        module = sys.modules.get(fullname)
        if module is not None:
            return module
        path = Path(package.__path__[0]) / (name + ".py")
        spec = importlib.util.spec_from_file_location(fullname, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[fullname] = module
        try:
            # Explicit compilation bypasses stale timestamp/size based .pyc
            # caches as well as cached modules from a previous extension load.
            exec(compile(package._naia_sources[name], str(path), "exec"), module.__dict__)
        except BaseException:
            if sys.modules.get(fullname) is module:
                del sys.modules[fullname]
            raise
        setattr(package, name, module)
        return module

def _retire_previous(ctx):
    """Stop our old owner even when a pre-0.2.1 entry has been overwritten.

    Old releases did not store a controller on the app. Their live web wrapper
    still owns the PanelServer, which owns that controller. Follow only the
    audited serving-chain links and verify the app, installation and types;
    never inspect or close unrelated extension resources.
    """
    directory = Path(__file__).resolve().parent
    app = ctx._app_context
    candidates = [_controller, getattr(app, _CONTROLLER_ATTRIBUTE, None)]
    shell = sys.modules.get("app.backend.server.web_shell_routes")
    pending = [getattr(shell, name, None) for name in ("_web_file", "_web_asset")]
    visited = set()
    owned_ui = re.compile(r"(?:_naia_android_tools_v1|_naia_android_tools_runtime_[0-9a-f]{64})\.naia_tools_ui\Z")
    while pending and len(visited) < 128:
        function = pending.pop()
        if not isinstance(function, types.FunctionType) or id(function) in visited:
            continue
        visited.add(id(function))
        values = {}
        for name, cell in zip(function.__code__.co_freevars, function.__closure__ or ()):
            try:
                values[name] = cell.cell_contents
            except ValueError:
                pass
        pending.extend(values.get(name) for name in ("previous_file", "previous_asset"))
        if (function.__name__ not in ("web_file", "web_asset")
                or not isinstance(function.__module__, str)
                or not owned_ui.fullmatch(function.__module__)
                or Path(function.__code__.co_filename).resolve() != directory / "naia_tools_ui.py"):
            continue
        server = values.get("server")
        if type(server).__name__ == "PanelServer" and type(server).__module__ == function.__module__:
            candidates.append(getattr(server, "controller", None))
    closed = set()
    for controller in candidates:
        if controller is None or id(controller) in closed:
            continue
        context = getattr(controller, "ctx", None)
        if context is None or getattr(context, "_app_context", None) is not app:
            continue
        ext_dir = getattr(context, "ext_dir", None)
        if ext_dir is None or Path(ext_dir).resolve() != directory:
            raise RuntimeError("이전 Android 도구의 설치 위치가 달라 안전하게 중지하지 못했습니다.")
        close = getattr(controller, "close", None)
        if not callable(close):
            raise RuntimeError("이전 Android 도구 실행부를 안전하게 중지하지 못했습니다.")
        close()
        atexit.unregister(close)
        closed.add(id(controller))

def _private_directory(ctx):
    config = getattr(getattr(ctx._app_context, "runtime_paths", None), "config_dir", None)
    if config is None:
        raise RuntimeError("앱 전용 설정 폴더를 확인할 수 없습니다.")
    directory = (Path(config) / "android-tools").resolve()
    if hasattr(sys, "getandroidapilevel") and not re.match(
        r"^/data/(?:user(?:_de)?/\d+|data)/[^/]+/(?:files|no_backup)/", str(directory)):
        raise RuntimeError("연결 정보를 저장할 Android 앱 전용 폴더가 필요합니다.")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    return directory

def _pairing(data):
    if not isinstance(data, dict) or not isinstance(data.get("relay_url"), str) or data["relay_url"].rstrip("/") != RELAY_URL:
        raise ValueError("NAIA 연결 페이지에서 받은 연결 파일을 선택하세요.")
    result = {"relay_url": RELAY_URL}
    for key in ("relay_access_token", "device_id", "device_token"):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > 8192 or any(c in value for c in "\r\n\0"):
            raise ValueError("연결 파일 형식이 올바르지 않습니다.")
        result[key] = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", result["device_id"]):
        raise ValueError("연결 파일의 기기 정보가 올바르지 않습니다.")
    return result

class Controller:
    def __init__(self, ctx, state_root, folder_module):
        self.ctx, self.state_root, self.folder_module = ctx, Path(state_root), folder_module
        self.pair_path = self.state_root / "pairing.json"
        self._lock, self._closed, self._cleanup = threading.RLock(), False, []
        self.memory, self.bridge, self.screen, self._pair = None, None, None, None
        self.comic, self.prompt_server, self.history_library = None, None, None
        self._history_factory = None
        self._photo_picker_factory, self._photo_picker = None, None
        try:
            if self.pair_path.stat().st_size <= 32768:
                self._pair = _pairing(json.loads(self.pair_path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            pass

    def available(self):
        r = self.ctx._record
        return bool(not self._closed and r.status == "loaded" and r.enabled and not r.blocked)

    def active(self):
        return bool(self.available() and self.ctx._record.armed)

    def require_available(self):
        if not self.available():
            raise ValueError("Settings → Extension에서 Android 도구를 활성화하세요.")

    def settings(self):
        raw = self.ctx.load_settings(dict(DEFAULTS))
        result = {k: raw.get(k, v) is True for k, v in DEFAULTS.items() if k != "cache_mb"}
        result["cache_mb"] = raw.get("cache_mb") if type(raw.get("cache_mb")) is int and raw["cache_mb"] in (16, 32, 64) else 32
        return result

    def memory_settings(self):
        settings = self.settings()
        return {"low_memory": settings["low_memory"] and self.active(), "cache_mb": settings["cache_mb"]}

    def bridge_settings(self):
        with self._lock:
            settings = self.settings()
            return {**(self._pair or {}), "enabled": settings["plugin_enabled"] and bool(self._pair) and self.active(),
                    "permissions": {k: settings["allow_" + k] for k in ("read", "screen", "control", "edit", "generate")}}

    def status(self):
        memory = self.memory.refresh() if self.memory else {}
        with self._lock:
            result = {"settings": self.settings(), "memory": memory,
                "paired": bool(self._pair), "extension_active": self.available(),
                "armed": bool(self.ctx._record.armed), "relay_url": RELAY_URL}
        # Broker permission guards acquire this controller's lock. Do not hold
        # it while entering the broker (or simultaneous status/poll deadlocks).
        result["bridge"] = self.bridge.status() if self.bridge else {"state": "stopped"}
        result["screen"] = self.screen.status() if self.screen else {"ready": False}
        result["comic"] = self.comic.status() if self.comic else {"state": "unavailable"}
        result["prompt_server"] = self.prompt_server.status() if self.prompt_server else {"state": "unavailable"}
        return result

    def prepare_prompt_server(self):
        self.require_available()
        if not self.active() or self.prompt_server is None:
            raise ValueError("Android 도구의 Activate This Script를 켜 주세요.")
        with self._lock:
            previous = self.settings()
            self._save({**previous, "comic_server_enabled": True})
        try:
            self.prompt_server.prepare()
        except Exception:
            with self._lock:
                self._save({**self.settings(), "comic_server_enabled": previous["comic_server_enabled"]})
            raise
        return self.status()

    def history_request(self, action, data):
        history = self._history()
        if action == "open_photo_picker":
            if data != {}:
                raise ValueError("사진 선택 요청이 올바르지 않습니다.")
            with self._lock:
                if self._closed or self._photo_picker_factory is None:
                    raise ValueError("사진 선택기를 준비하지 못했습니다.")
                if self._photo_picker is None:
                    self._photo_picker = self._photo_picker_factory(self._import_selected_photo)
                picker = self._photo_picker
            return picker.open()
        return history.request(action, data)

    def _import_selected_photo(self, name, stream, size):
        # Recheck extension state per file, including after switching apps.
        return self._history().import_stream(name, stream, size)

    def history_asset(self, item_id, kind):
        return self._history().asset(item_id, kind)

    def history_source_asset(self, root_id, relative, kind):
        return self._history().source_asset(root_id, relative, kind)

    def _history(self):
        self.require_available()
        if not self.active():
            raise ValueError("Android 도구의 Activate This Script를 켜 주세요.")
        with self._lock:
            if self.history_library is None:
                if self._history_factory is None:
                    raise ValueError("사진 보관함을 준비하지 못했습니다. 화면을 새로고침해 주세요.")
                # Gallery disk failures must not prevent the existing tools,
                # settings or screen connection from loading.
                self.history_library = self._history_factory()
            return self.history_library

    def stop_prompt_server(self):
        self.require_available()
        with self._lock:
            self._save({**self.settings(), "comic_server_enabled": False})
        if self.prompt_server:
            self.prompt_server.stop()
        return self.status()

    @staticmethod
    def _comic_status_message(server):
        labels = {"not_installed": "실행부 준비 필요", "stopped": "실행부 꺼짐",
                  "preparing": "실행부 준비 중", "downloading": "실행 파일 받는 중",
                  "starting": "실행부 시작 중", "running": "실행부 연결 준비 완료",
                  "ready": "실행부 연결 준비 완료", "external": "기존 PromptServer 연결",
                  "stopping": "실행부 종료 중", "error": "실행부 확인 필요"}
        state = labels.get(server.get("state"), server.get("state", "준비 필요"))
        return state + (" · " + server["error"] if server.get("error") else "")

    def register_panel(self):
        message = self._comic_status_message(self.prompt_server.status() if self.prompt_server else {})
        self.ctx.register_panel([
            {"key": "help", "type": "action", "label": "Android 도구 설정 안내",
             "help": "Settings → Global → Android 도구 · 플러그인에서 설정합니다."},
            {"key": "comic_prepare", "type": "action", "label": "Comic Maker 실행부 준비",
             "help": "원작자 GitHub에서 검증된 실행 파일을 받아 이 기기 안에서 시작합니다. 생성은 실행하지 않습니다."},
            {"key": "comic_status", "type": "action", "label": "Comic Maker 연결 상태 확인",
             "help": message},
        ], title="Android 도구 0.2.9", on_action=self.panel_action)

    def panel_action(self, key):
        if key == "help":
            self.ctx.show_toast("Settings → Global → Android 도구 · 플러그인을 여세요.")
        elif key in ("comic_prepare", "comic_status"):
            if key == "comic_prepare":
                self.prepare_prompt_server()
            self.register_panel()
            server = self.prompt_server.status()
            self.ctx.show_toast("Comic Maker: " + self._comic_status_message(server))

    def _comic_token(self):
        # Read only when the owned server actually needs NAI authentication.
        # Never copy it to environment variables, status, logs or server settings.
        if not self.active() or not self.settings()["comic_server_enabled"]:
            return ""
        manager = getattr(self.ctx._app_context, "secure_token_manager", None)
        return manager.get_token("nai_token") if manager is not None else ""

    def _save(self, settings):
        if not self.ctx.save_settings(settings):
            raise ValueError("설정을 저장하지 못했습니다. 다시 시도하세요.")

    def update_settings(self, changes):
        self.require_available()
        if not isinstance(changes, dict) or not changes or set(changes) - DEFAULTS.keys():
            raise ValueError("설정 형식이 올바르지 않습니다.")
        if "comic_server_enabled" in changes:
            raise ValueError("Comic Maker 실행부 준비·끄기 버튼을 사용하세요.")
        for key, value in changes.items():
            if key == "cache_mb":
                if type(value) is not int or value not in (16, 32, 64):
                    raise ValueError("캐시 크기는 16, 32, 64MB 중 선택하세요.")
            elif type(value) is not bool:
                raise ValueError("설정은 켜기 또는 끄기여야 합니다.")
        with self._lock:
            new = {**self.settings(), **changes}
            if new["plugin_enabled"] and not self._pair:
                raise ValueError("먼저 연결 파일을 불러오세요.")
            self._save(new)
        return self.status()

    def import_pairing(self, data):
        self.require_available()
        pair = _pairing(data)
        with self._lock:
            # Persist revocation first: a failed import never inherits old writes.
            disabled = {**self.settings(), "plugin_enabled": False, "allow_read": True,
                        "allow_screen": False, "allow_control": False, "allow_edit": False, "allow_generate": False}
            self._save(disabled)
            temporary = self.pair_path.with_suffix(".tmp")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as file:
                    json.dump(pair, file, ensure_ascii=False)
                    file.flush()
                    os.fsync(file.fileno())
                temporary.chmod(0o600)
                os.replace(temporary, self.pair_path)
            finally:
                temporary.unlink(missing_ok=True)
            self._pair = pair
            self._save({**disabled, "plugin_enabled": True})
        return self.status()

    def disconnect(self):
        self.require_available()
        with self._lock:
            self._save({**self.settings(), "plugin_enabled": False, "allow_screen": False,
                        "allow_control": False, "allow_edit": False, "allow_generate": False})
            self._pair = None
            self.pair_path.unlink(missing_ok=True)
        return self.status()

    def open_folder(self, target):
        self.require_available()
        if not self.active():
            return {"opened": False, "reason": "extension_inactive"}
        app = self.ctx._app_context
        if target == "save":
            path = app._save_service().current_save_directory()
        elif target == "artist":
            from app.backend.server.artist_thumbnail_routes import artist_thumbnail_service
            path = artist_thumbnail_service(app).mode_data_root
        else:
            raise ValueError("열 수 있는 폴더가 아닙니다.")
        return self.folder_module.open_native_folder(path)

    def open_relay(self):
        self.require_available()
        opened = False
        if sys.platform == "android" or hasattr(sys, "getandroidapilevel"):
            try:
                r = subprocess.run(["/system/bin/am", "start", "-a", "android.intent.action.VIEW",
                    "-c", "android.intent.category.BROWSABLE", "-d", RELAY_URL],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=10, check=False)
                opened = r.returncode == 0
            except (OSError, subprocess.SubprocessError):
                pass
        return {"opened": opened, "url": RELAY_URL}

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        if self.bridge:
            self.bridge.close()
        if self.screen:
            self.screen.close()
        if self.prompt_server:
            self.prompt_server.close()
        if self.comic:
            self.comic.close()
        if self._photo_picker:
            self._photo_picker.close()
        if self.history_library:
            self.history_library.close()
        for cleanup in reversed(self._cleanup):
            try:
                cleanup()
            except Exception:
                pass
        if self.memory:
            self.memory.close()
        app = self.ctx._app_context
        if getattr(app, _CONTROLLER_ATTRIBUTE, None) is self:
            delattr(app, _CONTROLLER_ATTRIBUTE)
        atexit.unregister(self.close)

def register(ctx):
    global _controller
    # The host creates a fresh main.py module on retry, so its _controller global
    # alone cannot find the previous owner. Keep one owner on the live app too.
    _retire_previous(ctx)
    _controller = None
    package = _runtime_package()
    (folder, memory, bridge, ui, screen, comic, prompt_server, history_library,
     android_tree, local_picker) = [_module(n, package) for n in _MODULE_NAMES]
    if "screen_service" not in inspect.signature(bridge.DeviceBridge).parameters:
        raise RuntimeError("Android 도구의 실행 파일 버전이 일치하지 않습니다. 복구 업데이트가 필요합니다.")
    controller = Controller(ctx, _private_directory(ctx), folder)
    try:
        controller._history_factory = lambda: history_library.HistoryLibrary(
            ctx._app_context, controller.state_root, tree_factory=android_tree.AndroidTree)
        controller._photo_picker_factory = local_picker.LocalPhotoPicker
        controller.comic = comic.install(ctx)
        controller.prompt_server = prompt_server.PromptServer(controller.state_root, controller._comic_token)
        if controller.settings()["comic_server_enabled"]:
            controller.prompt_server.start_installed()
        controller.memory = memory.install(controller.memory_settings, state_root=controller.state_root)
        existing = getattr(ctx._app_context, "artist_thumbnail_service", None)
        if existing is not None:
            controller.memory.attach(existing)
        controller.screen = screen.ScreenService()
        controller.bridge = bridge.DeviceBridge(ctx, controller.bridge_settings,
            state_root=controller.state_root, allowed_origins={RELAY_URL}, screen_service=controller.screen)
        handle = ui.install_ui(controller, folder, memory)
        controller._cleanup.append(handle if callable(handle) else handle.close)
        controller.register_panel()
        controller.bridge.start()
    except BaseException:
        controller.close()
        raise
    _controller = controller
    setattr(ctx._app_context, _CONTROLLER_ATTRIBUTE, controller)
    atexit.register(controller.close)
    ctx.log("Android 도구가 준비되었습니다. 화면을 새로고침하세요. 플러그인 연결은 설정에서 시작합니다.")
