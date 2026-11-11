"""Opt-in, verified upstream PromptServer in NAIA's existing Python runtime.

Only prepare() downloads source. The upstream source is not redistributed here;
the manifest pins its Git object hashes. No generation or token test is performed
during installation/startup. User data is outside the verified source directory.
"""
from __future__ import annotations

import builtins
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import socket
import sys
import tempfile
import threading
import time
import types
import urllib.request
import urllib.error
import urllib.parse
import uuid
import zipfile


URL = "http://127.0.0.1:8765"
_MAX_ARCHIVE = 8 * 1024 * 1024
_MANIFEST = Path(__file__).with_name("promptserver-source-manifest.json")


class PromptServerError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "Redirect refused", headers, fp)


def _novelai_urlopen(request, **kwargs):
    parsed = urllib.parse.urlsplit(request.full_url)
    if (parsed.scheme != "https" or parsed.netloc != "text.novelai.net"
            or parsed.path not in ("/oa/v1/models", "/oa/v1/completions", "/oa/v1/chat/completions")):
        raise OSError("NovelAI endpoint is not allowed")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    return opener.open(request, **kwargs)


def _blob_hash(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


class _PrivateModules:
    """Private builtins import routing; never mutates sys.path or host modules."""
    def __init__(self, source, data, manifest, token_provider):
        self.source, self.data = Path(source), Path(data)
        self.names = {Path(p).stem for p in manifest["files"] if "/" not in p and p.endswith(".py")}
        self.prefix = "_naia_promptserver_" + uuid.uuid4().hex
        self.modules = {}
        self.lock = threading.RLock()
        self.token_provider = token_provider
        self.closed = threading.Event()
        package = types.ModuleType(self.prefix)
        package.__path__ = []
        sys.modules[self.prefix] = package

    def _import(self, name, globals=None, locals=None, fromlist=(), level=0):
        if not level and name in self.names:
            return self.load(name)
        return builtins.__import__(name, globals, locals, fromlist, level)

    def load(self, name):
        with self.lock:
            if self.closed.is_set():
                raise ImportError("PromptServer stopped")
            if name in self.modules:
                return self.modules[name]
            full = self.prefix + "." + name
            path = self.source / (name + ".py")
            spec = importlib.util.spec_from_file_location(full, path)
            module = importlib.util.module_from_spec(spec)
            module.__dict__["__builtins__"] = dict(vars(builtins), __import__=self._import)
            self.modules[name] = module
            sys.modules[full] = module
            try:
                exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
                if name == "app_paths":
                    module.BUNDLE_ROOT = self.source
                    module.BUNDLED_DATA_DIR = self.source / "data"
                    module.APP_ROOT = self.source
                    module.DATA_DIR = self.data
                elif name == "novelai_settings":
                    self._adapt_settings(module)
                elif name == "novelai_client":
                    # A private facade avoids globally monkey-patching urllib.
                    def checked_urlopen(request, **kwargs):
                        if self.closed.is_set():
                            raise OSError("PromptServer stopped")
                        return _novelai_urlopen(request, **kwargs)
                    module.urllib = types.SimpleNamespace(
                        error=urllib.error,
                        request=types.SimpleNamespace(Request=urllib.request.Request, urlopen=checked_urlopen),
                    )
                elif name == "comic_auto_service":
                    # Resolve the host token only for an explicit model operation,
                    # not app startup, status, OpenAPI, or settings observation.
                    settings = self.load("novelai_settings")
                    def generation_settings():
                        if self.closed.is_set():
                            raise OSError("PromptServer stopped")
                        result = settings.load_settings()
                        result["token"] = str(self.token_provider() or "").strip()
                        return result
                    module.load_settings = generation_settings
            except BaseException:
                self.modules.pop(name, None)
                sys.modules.pop(full, None)
                raise
            return module

    @staticmethod
    def _adapt_settings(module):
        def load_settings():
            result = module._normalize(module._read_disk())
            # Do not consume a legacy plaintext token or process environment.
            result["token"] = ""
            result["base_url"] = "https://text.novelai.net"
            return result
        def public_settings():
            result = load_settings()
            result.pop("token", None)
            result.update(configured=None, token_masked="", token_storage="NAIA 앱의 저장된 계정 · 요청할 때 확인")
            return result
        def save_settings(update):
            if str((update or {}).get("token") or "").strip() not in ("", "********", "••••••••"):
                raise ValueError("NAI 인증은 NAIA 앱의 API 설정에서 관리하세요.")
            current = load_settings()
            current.update({k: v for k, v in dict(update or {}).items() if k != "token"})
            current = module._normalize(current)
            module._write_settings(current)
            return current
        module.load_settings = load_settings
        module.public_settings = public_settings
        module.save_settings = save_settings
        module.clear_token = lambda: None  # Never deletes the host credential.

    def close(self):
        self.closed.set()
        with self.lock:
            for name in tuple(self.modules):
                sys.modules.pop(self.prefix + "." + name, None)
            sys.modules.pop(self.prefix, None)
            self.modules.clear()


class PromptServer:
    def __init__(self, state_root, token_provider):
        self.root = Path(state_root) / "promptserver"
        self.manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
        self.source = self.root / ("source-" + self.manifest["commit"])
        self.data = self.root / "data"
        self.token_provider = token_provider
        self._lock = threading.RLock()
        self._state, self._error = "stopped" if self.source.is_dir() else "not_installed", ""
        self._worker = self._server_thread = self._server = self._modules = None
        self._cancel = threading.Event()
        self._closed = False

    def status(self):
        with self._lock:
            return {"state": self._state, "error": self._error,
                    "installed": self.source.is_dir()}

    def prepare(self):
        return self._begin(download=True)

    def start_installed(self):
        return self._begin(download=False)

    def _begin(self, *, download):
        with self._lock:
            if self._closed:
                return self.status()
            if self._worker is not None and self._worker.is_alive():
                if self._cancel.is_set():
                    raise PromptServerError("PromptServer 준비 작업 종료 중입니다. 잠시 뒤 ‘Comic Maker 실행부 준비’를 다시 눌러주세요.")
                return self.status()
            if self._server_thread is not None and self._server_thread.is_alive():
                if self._cancel.is_set():
                    raise PromptServerError("PromptServer 종료 중입니다. 잠시 뒤 ‘Comic Maker 실행부 준비’를 다시 눌러주세요.")
                return self.status()
            self._cancel = threading.Event()
            self._state, self._error = "preparing" if download else "starting", ""
            self._worker = threading.Thread(target=self._setup, args=(download, self._cancel),
                                            daemon=True, name="naia-promptserver-setup")
            self._worker.start()
            return self.status()

    def _set(self, state, error=""):
        with self._lock:
            if not self._closed and not self._cancel.is_set():
                self._state, self._error = state, error

    def _verified(self):
        try:
            if self.source.is_symlink():
                return False
            for relative, expected in self.manifest["files"].items():
                path = self.source / relative
                if path.is_symlink() or not path.is_file():
                    return False
                if any(parent.is_symlink() for parent in path.parents if parent != self.source and self.source in parent.parents):
                    return False
                if path.stat().st_size != expected["size"]:
                    return False
                data = path.read_bytes()
                if len(data) != expected["size"] or _blob_hash(data) != expected["sha"]:
                    return False
            return True
        except OSError:
            return False

    def _download(self):
        url = "https://codeload.github.com/" + self.manifest["repository"] + "/zip/" + self.manifest["commit"]
        request = urllib.request.Request(url, headers={"User-Agent": "NAIA-Android-PromptServer-Installer"})
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=30) as response:
            raw = response.read(_MAX_ARCHIVE + 1)
        if len(raw) > _MAX_ARCHIVE:
            raise PromptServerError("PromptServer 원본 파일이 허용 크기를 넘었습니다.")
        return raw

    def _install(self, raw, cancel):
        self.root.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix="source-install-", dir=self.root))
        backup = None
        try:
            prefix = self.manifest["repository"].split("/")[1] + "-" + self.manifest["commit"] + "/"
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                entries = archive.infolist()
                for relative, expected in self.manifest["files"].items():
                    if cancel.is_set():
                        return
                    if PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts:
                        raise PromptServerError("PromptServer 파일 목록 경로가 잘못되었습니다.")
                    found = [item for item in entries if item.filename == prefix + relative]
                    if len(found) != 1 or found[0].file_size != expected["size"]:
                        raise PromptServerError("PromptServer 원본 파일 목록 검증에 실패했습니다.")
                    if (found[0].external_attr >> 16) & 0o170000 == 0o120000:
                        raise PromptServerError("PromptServer 원본에 허용하지 않는 링크가 있습니다.")
                    content = archive.read(found[0])
                    if _blob_hash(content) != expected["sha"]:
                        raise PromptServerError("PromptServer 원본 해시 검증에 실패했습니다.")
                    target = stage / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
            with self._lock:
                if cancel.is_set() or self._closed:
                    return
                if self.source.exists():
                    backup = self.root / ("old-source-" + uuid.uuid4().hex)
                    self.source.rename(backup)
                try:
                    stage.rename(self.source)
                except BaseException:
                    if backup is not None:
                        backup.rename(self.source)
                        backup = None
                    raise
        finally:
            if stage.exists():
                shutil.rmtree(stage)
            if backup is not None and backup.exists():
                shutil.rmtree(backup)

    @staticmethod
    def _port_open():
        try:
            with socket.create_connection(("127.0.0.1", 8765), timeout=0.4):
                return True
        except OSError:
            return False

    @staticmethod
    def _probe():
        # Disable environment proxies for this private loopback request.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(URL + "/openapi.json", timeout=2) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            return False
        data = json.loads(raw)
        paths = data.get("paths", {})
        return all(path in paths for path in ("/api/comics/generate/stream", "/api/comics/random", "/api/comics/{comic_id}/use"))

    def _setup(self, download, cancel):
        modules = None
        stage = "connection"
        try:
            if self._port_open():
                if self._probe():
                    self._set("external", "이미 실행 중인 PromptServer를 사용합니다. 인증 설정은 해당 서버에서 관리합니다.")
                else:
                    raise PromptServerError("8765 포트를 다른 프로그램이 사용 중입니다. 기존 프로그램을 확인하세요.")
                return
            if not self._verified():
                if not download:
                    raise PromptServerError("검증된 PromptServer 파일이 없습니다. ‘Comic Maker 실행부 준비’를 눌러 원본을 받아주세요.")
                stage = "download"
                self._install(self._download(), cancel)
            if cancel.is_set():
                return
            if not self._verified():
                raise PromptServerError("PromptServer 설치 검증에 실패했습니다. ‘Comic Maker 실행부 준비’를 다시 눌러주세요.")
            stage = "runtime"
            import pydantic
            import uvicorn
            if tuple(int(x) for x in str(pydantic.__version__).split(".")[:2]) < (2, 8):
                raise PromptServerError("앱의 Pydantic 2 실행환경이 필요합니다. 앱 실행부 업데이트가 필요합니다.")
            self._set("starting")
            modules = _PrivateModules(self.source, self.data, self.manifest, self.token_provider)
            # app_paths must be adapted before any dependent import takes DATA_DIR.
            modules.load("app_paths")
            app = modules.load("app").app
            self._restrict_app(app, modules.load("api_routes").router.routes)
            config = uvicorn.Config(app, host="127.0.0.1", port=8765, loop="asyncio", http="h11",
                                    ws="none", lifespan="on", access_log=False, log_config=None,
                                    timeout_graceful_shutdown=2)
            server = uvicorn.Server(config)
            with self._lock:
                if cancel.is_set() or self._closed:
                    modules.close()
                    return
                self._modules, self._server = modules, server
                thread = threading.Thread(target=self._serve, args=(server, modules, cancel),
                                          daemon=True, name="naia-promptserver")
                self._server_thread = thread
                thread.start()
            deadline = time.monotonic() + 20
            while thread.is_alive() and time.monotonic() < deadline and not cancel.is_set():
                if server.started:
                    if self._probe():
                        self._set("ready")
                        return
                cancel.wait(0.1)
            if cancel.is_set():
                server.should_exit = True
                return
            server.should_exit = True
            raise PromptServerError("PromptServer가 시작되지 않았습니다. 8765 포트와 앱 실행환경을 확인하세요.")
        except PromptServerError as exc:
            self._set("error", str(exc))
        except (ImportError, ModuleNotFoundError):
            self._set("error", "PromptServer 실행에 필요한 Python 구성요소가 없습니다. 앱 실행부 업데이트가 필요합니다.")
        except Exception:
            # Never echo upstream exceptions: settings or response text may contain secrets.
            errors = {
                "connection": "8765 포트의 프로그램을 PromptServer로 확인하지 못했습니다. 해당 포트의 실행 프로그램을 확인하세요.",
                "download": "PromptServer 원본 다운로드·검증에 실패했습니다. 인터넷 연결을 확인하고 ‘Comic Maker 실행부 준비’를 다시 눌러주세요.",
                "runtime": "PromptServer 실행환경 초기화에 실패했습니다. 앱 실행부와 PromptServer 준비 상태를 확인하세요.",
            }
            self._set("error", errors[stage])
        finally:
            with self._lock:
                if modules is not None and self._modules is not modules:
                    modules.close()

    def _serve(self, server, modules, cancel):
        try:
            server.run()
        except BaseException:
            if not cancel.is_set():
                self._set("error", "PromptServer 실행이 종료되었습니다. 앱 실행환경과 8765 포트를 확인하세요.")
        finally:
            modules.close()
            with self._lock:
                if self._modules is modules:
                    self._modules = self._server = None
                    if self._state in ("ready", "stopping"):
                        self._state = "stopped"

    @staticmethod
    def _restrict_app(app, api_routes=()):
        """Only routes used by Comic Maker; no settings/token management UI."""
        allowed = {"/openapi.json", "/api/comics/generate/stream", "/api/comics/generate",
                   "/api/comics/random", "/api/comics/{comic_id}/use", "/api/comics/progress"}
        selected = [route for route in app.router.routes if getattr(route, "path", "") in allowed]
        present = {getattr(route, "path", "") for route in selected}
        # FastAPI >= 0.161 includes APIRouter lazily rather than flattening it.
        # Select the original known API routes explicitly in either version.
        selected.extend(route for route in api_routes if getattr(route, "path", "") in allowed - present)
        app.router.routes[:] = selected
        app.openapi_schema = None
        app.user_middleware.clear()
        from starlette.responses import JSONResponse

        @app.middleware("http")
        async def local_client_only(request, call_next):
            if (request.headers.get("origin") is not None
                    or request.headers.get("host", "").lower() not in ("127.0.0.1:8765", "localhost:8765")):
                return JSONResponse({"detail": "Local Comic Maker requests only"}, status_code=403)
            return await call_next(request)

    def stop(self):
        with self._lock:
            self._cancel.set()
            if self._server is not None:
                self._server.should_exit = True
            if self._modules is not None:
                self._modules.closed.set()
            if not self._closed:
                alive = self._server_thread is not None and self._server_thread.is_alive()
                self._state, self._error = "stopping" if alive else "stopped", ""
        return self.status()

    def close(self):
        self.stop()
        with self._lock:
            self._closed = True
            self._state = "closed"
            thread = self._server_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(3)
