"""Read an explicitly selected Android document tree using a persisted SAF read grant.

The activity is obtained only via public lifecycle callbacks. A host without
AndroidX activity results remains usable for existing grants, but cannot pick.
No provider URI or document ID is accepted from the web UI.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile, SpooledTemporaryFile

_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
_LIMIT = 64 * 1024 * 1024
_MAX_ENTRIES = 50_000
_MAX_DEPTH = 64
_MAX_PATH = 4096
_WAIT = "홈 화면에 잠깐 나갔다가 NAIA로 돌아와 폴더 선택을 다시 눌러 주세요."
_HOST = "이 앱은 Android 폴더 선택 결과를 받을 수 없습니다. ‘기기에서 사진 선택’을 이용해 주세요."
_REVOKED = "지정한 폴더를 읽을 수 없습니다. Android 폴더 선택에서 다시 허용해 주세요."
_READ = "지정한 폴더의 목록을 읽지 못했습니다. 폴더가 있는지 확인하고 다시 허용해 주세요."
_DIR = "vnd.android.document/directory"


def _parts(relative):
    if not isinstance(relative, str) or len(relative) > _MAX_PATH or any(c in relative for c in (":", "\\", "\0")):
        raise ValueError("목록에서 폴더나 사진을 선택하세요.")
    parts = tuple(relative.split("/")) if relative else ()
    if len(parts) > _MAX_DEPTH or any(s in ("", ".", "..") for s in parts):
        raise ValueError("목록에서 폴더나 사진을 선택하세요.")
    return parts


def _number(value, limit):
    if isinstance(value, bool):
        raise ValueError("페이지 번호가 올바르지 않습니다.")
    try:
        return max(1, min(limit, int(value)))
    except (TypeError, OverflowError, ValueError):
        raise ValueError("페이지 번호가 올바르지 않습니다.") from None


def _valid_uri(value):
    return isinstance(value, str) and 0 < len(value) <= 8192 and value.startswith("content://") and "/tree/" in value and "\0" not in value


class AndroidTree:
    def __init__(self, state_root, platform=None):
        self._state_dir = Path(state_root)
        self._file = self._state_dir / "android-tree-selection.json"
        self._lock = threading.RLock()
        self._closed = self._ready = False
        self._state, self._detail = "unavailable", _HOST
        self._selected = None
        self._request = 0
        self._cache = OrderedDict()
        self._platform = platform
        try:
            if platform is None:
                self._platform = _AndroidPlatform()
            self._platform.setup(self._on_result, self._on_state)
        except Exception:
            self._on_state("unavailable", _HOST)
        self._restore()

    def _restore(self):
        if self._platform is None or self._file.is_symlink() or not self._file.is_file():
            return
        try:
            if self._file.stat().st_size > 16384:
                return
            saved = json.loads(self._file.read_text("utf-8")); uri = saved["uri"]
            if not _valid_uri(uri) or not self._platform.has_grant(uri):
                self._state, self._detail = "error", _REVOKED
                return
            self._selected = self._record(uri, saved.get("owned") is True)
            self._state, self._detail = "selected", ""
        except Exception:
            self._state, self._detail = "error", _REVOKED

    def _record(self, uri, owned):
        doc, label = self._platform.descriptor(uri)
        if not doc or not _valid_uri(uri):
            raise ValueError(_READ)
        token = "android_tree_" + hashlib.sha256(uri.encode()).hexdigest()[:16]
        return (uri, str(doc), str(label or "지정한 폴더")[:256], token, owned)

    def _save(self, selected):
        self._state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._file.is_symlink():
            raise ValueError(_READ)
        tmp = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=self._state_dir, delete=False) as out:
                tmp = out.name
                json.dump({"uri": selected[0], "owned": selected[4]}, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, self._file)
        finally:
            if tmp:
                Path(tmp).unlink(missing_ok=True)

    def _on_state(self, state, detail=""):
        with self._lock:
            if self._closed:
                return
            if state in ("ready", "waiting_activity", "unavailable"):
                self._ready = state == "ready"
                self._capability = state
                if self._state == "picking" and state != "unavailable":
                    return
                if self._selected and self._state in ("selected", "cancelled", "error") and state == "ready":
                    return
            self._state, self._detail = state, str(detail or "")[:350]

    def _on_result(self, uri):
        # The real success callback runs in a background thread. Do not hold
        # the UI-visible state lock while calling a potentially remote provider.
        with self._lock:
            if self._closed or self._state != "picking":
                return
            if uri is None:
                self._state, self._detail = "cancelled", "폴더 선택을 취소했습니다."
                return
            old, request = self._selected, self._request
        acquired = False
        try:
            if not _valid_uri(uri):
                raise ValueError(_READ)
            existed = self._platform.has_grant(uri)
            # Take the read bit only, regardless of what the provider offered.
            self._platform.remember(uri)
            acquired = not existed
            if not self._platform.has_grant(uri):
                raise ValueError(_REVOKED)
            record = self._record(uri, acquired or bool(old and old[0] == uri and old[4]))
            with self._lock:
                if self._closed or self._state != "picking" or self._request != request:
                    raise ValueError(_READ)
                self._save(record)
                self._selected, self._state, self._detail = record, "selected", ""
                self._cache.clear()
        except Exception:
            with self._lock:
                if not self._closed and self._state == "picking" and self._request == request:
                    self._state, self._detail = "error", "폴더의 읽기 권한을 저장하지 못했습니다. 폴더를 다시 선택해 주세요."
                # Never revoke a successful newer selection, including a grant
                # already handed to a freshly reloaded extension instance.
                release = acquired and not self._closed and not (self._selected and self._selected[0] == uri)
            if release:
                self._release(uri)
            return
        # Never release a grant that already belonged to another app feature.
        if old and old[0] != uri and old[4]:
            self._release(old[0])

    def _release(self, uri):
        try:
            self._platform.release(uri)
        except Exception:
            pass

    def status(self):
        with self._lock:
            s = self._selected
            return {"state": self._state, "detail": self._detail, "available": bool(s) or
                    getattr(self, "_capability", "unavailable") != "unavailable",
                    "picker_ready": self._ready, "selected": bool(s), "label": s[2] if s else "",
                    "root_id": s[3] if s else ""}

    def choose(self):
        with self._lock:
            if self._closed:
                raise ValueError("폴더 선택 기능이 종료됐습니다.")
            if self._state == "picking":
                return self.status()
            if not self._ready:
                cap = getattr(self, "_capability", "unavailable")
                self._state, self._detail = cap, (_WAIT if cap == "waiting_activity" else _HOST)
                return self.status()
            self._request += 1
            self._state, self._detail = "picking", "Android 화면에서 사진이 있는 폴더를 선택해 주세요."
            try:
                self._platform.choose()
            except Exception:
                self._state, self._detail = "error", "Android 폴더 선택창을 열지 못했습니다."
            return self.status()

    def _snapshot(self, root_id):
        with self._lock:
            s = self._selected
            if self._closed or not s or (root_id is not None and root_id != s[3]):
                raise ValueError("지정한 폴더가 바뀌었거나 사용할 수 없습니다. 폴더 목록을 다시 열어 주세요.")
            return s

    def _children(self, s, parent, *, refresh=False):
        key = (s[0], parent)
        with self._lock:
            hit = self._cache.get(key)
            if not refresh and hit and time.monotonic() - hit[0] < 3:
                return hit[1]
        try:
            rows = {}
            for i, row in enumerate(self._platform.children(s[0], parent)):
                if i >= _MAX_ENTRIES:
                    raise ValueError("폴더에 항목이 너무 많습니다. 더 작은 하위 폴더를 지정해 주세요.")
                name = row.get("name")
                if not isinstance(name, str) or not name or any(c in name for c in ("\0", "/", "\\", ":")) or name in (".", ".."):
                    continue
                ident = row.get("id")
                if not isinstance(ident, str) or not ident or len(ident) > 8192:
                    continue
                directory = bool(row.get("directory"))
                if not directory and Path(name).suffix.lower() not in _EXTS:
                    continue
                if name in rows:
                    raise ValueError("같은 이름의 항목이 여러 개 있습니다. 이름을 구분하거나 다른 폴더를 선택해 주세요.")
                size = row.get("size")
                rows[name] = (ident, directory, size if type(size) is int and size >= 0 else None)
        except ValueError:
            raise
        except Exception:
            raise ValueError(_READ) from None
        # Keep only small, short-lived metadata indexes. File opens always go
        # through ContentResolver; the cache cannot bestow a URI permission.
        if len(rows) <= 8192:
            with self._lock:
                if not self._closed and self._selected == s:
                    self._cache[key] = (time.monotonic(), rows)
                    self._cache.move_to_end(key)
                    while len(self._cache) > 4:
                        self._cache.popitem(last=False)
        return rows

    def _resolve(self, s, parts, directory):
        item = (s[1], True, None)
        for name in parts:
            if not item[1]:
                raise ValueError("사진 폴더를 찾지 못했습니다.")
            item = self._children(s, item[0]).get(name)
            if item is None:
                raise ValueError("선택한 파일이나 폴더를 찾지 못했습니다. 목록을 다시 열어 주세요.")
        if item[1] != directory:
            raise ValueError("목록에서 사진 파일이나 폴더를 다시 선택해 주세요.")
        return item

    def browse(self, relative="", page=1, per_page=60, root_id=None):
        s, parts = self._snapshot(root_id), _parts(relative)
        size, page = _number(per_page, 100), _number(page, 10000)
        items = self._children(s, self._resolve(s, parts, True)[0], refresh=True)
        ordered = sorted(items, key=lambda name: (not items[name][1], name.casefold(), name))
        total = len(ordered); pages = max(1, (total + size - 1) // size); page = min(page, pages)
        dirs, files = [], []
        for name in ordered[(page-1)*size:page*size]:
            _, directory, length = items[name]
            row = {"name": name, "relative": "/".join((*parts, name))}
            if not directory:
                row["size"] = length
            (dirs if directory else files).append(row)
        return {"root_id": s[3], "relative": relative, "parent": "/".join(parts[:-1]) if parts else None,
                "dirs": dirs, "files": files, "total": total, "page": page, "pages": pages}

    @contextmanager
    def open_file(self, relative, root_id=None):
        s, parts = self._snapshot(root_id), _parts(relative)
        ident, _, length = self._resolve(s, parts, False)
        if length is not None and length > _LIMIT:
            raise ValueError("64MB 이하 이미지 파일만 가져올 수 있습니다.")
        try:
            stream = self._platform.open(s[0], ident)
        except Exception:
            raise ValueError("선택한 사진을 읽지 못했습니다. 폴더 접근 권한을 확인해 주세요.") from None
        with stream:
            if stream.seekable():
                stream.seek(0, os.SEEK_END)
                if stream.tell() > _LIMIT:
                    raise ValueError("64MB 이하 이미지 파일만 가져올 수 있습니다.")
                stream.seek(0)
                yield stream
            else:
                # Cloud document providers can return a pipe. Bound decoded
                # input before presenting Pillow with a seekable object.
                with SpooledTemporaryFile(max_size=512*1024, dir=self._state_dir) as local:
                    total = 0
                    while True:
                        block = stream.read(min(256*1024, _LIMIT+1-total))
                        if not block:
                            break
                        total += len(block)
                        if total > _LIMIT:
                            raise ValueError("64MB 이하 이미지 파일만 가져올 수 있습니다.")
                        local.write(block)
                    local.seek(0)
                    yield local

    def walk(self, relative, recursive, cancel=None, root_id=None):
        s, parts = self._snapshot(root_id), _parts(relative)
        stop = getattr(cancel, "is_set", cancel) or (lambda: False)
        if stop():
            return
        root = self._resolve(s, parts, True)[0]
        # One directory iterator per level, bounded depth; stop document cycles.
        def visit(parent, path, ancestors):
            for name, (ident, directory, _) in self._children(s, parent, refresh=True).items():
                if stop():
                    return
                child = (*path, name)
                result = "/".join(child)
                if len(child) > _MAX_DEPTH or len(result) > _MAX_PATH:
                    raise ValueError("폴더 경로가 너무 깊거나 깁니다.")
                if not directory:
                    yield result
                elif recursive and ident not in ancestors:
                    yield from visit(ident, child, ancestors | {ident})
        yield from visit(root, parts, {root})

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._cache.clear()
        if self._platform is not None:
            try:
                self._platform.close()
            except Exception:
                pass


class _AndroidPlatform:
    def __init__(self):
        from java import jclass, dynamic_proxy, cast
        self.J, self.proxy, self.cast = jclass, dynamic_proxy, cast
        self.app = jclass("com.chaquo.python.Python").getPlatform().getApplication()
        self.resolver = self.app.getContentResolver()
        self.D = jclass("android.provider.DocumentsContract")
        self.Uri = jclass("android.net.Uri")
        self.Intent = jclass("android.content.Intent")
        self.handler = jclass("android.os.Handler")(jclass("android.os.Looper").getMainLooper())
        self.activity = self.launcher = self.lifecycle = None
        self.pending_host = None
        self.closed = False

    def setup(self, on_result, on_state):
        self.result, self.state = on_result, on_state
        try:
            self.Owner = self.J("androidx.activity.result.ActivityResultRegistryOwner")
            self.Contract = self.J("androidx.activity.result.contract.ActivityResultContracts$StartActivityForResult")
            self.Callback = self.J("androidx.activity.result.ActivityResultCallback")
            self.Result = self.J("androidx.activity.result.ActivityResult")
        except Exception:
            self.state("unavailable", _HOST)
            return
        platform = self
        class Lifecycle(self.proxy(self.J("android.app.Application$ActivityLifecycleCallbacks"))):
            def onActivityResumed(self, activity):
                if platform.closed:
                    return
                if isinstance(activity, platform.Owner):
                    platform.activity = activity
                    platform.state("ready", "")
                else:
                    platform.activity = None
                    platform.state("unavailable", _HOST)
            def onActivityPaused(self, activity):
                if platform.activity == activity:
                    platform.activity = None
                    platform.state("waiting_activity", _WAIT)
            def onActivityDestroyed(self, activity):
                if platform.pending_host == activity:
                    platform._unregister()
                    platform.state("error", "앱 화면이 다시 열렸습니다. 폴더 선택을 다시 눌러 주세요.")
            def onActivityCreated(self, *args): pass
            def onActivityStarted(self, *args): pass
            def onActivityStopped(self, *args): pass
            def onActivitySaveInstanceState(self, *args): pass
            # API 29+ dispatches these additional (Java default) callbacks.
            def onActivityPreCreated(self, *args): pass
            def onActivityPostCreated(self, *args): pass
            def onActivityPreStarted(self, *args): pass
            def onActivityPostStarted(self, *args): pass
            def onActivityPreResumed(self, *args): pass
            def onActivityPostResumed(self, *args): pass
            def onActivityPrePaused(self, *args): pass
            def onActivityPostPaused(self, *args): pass
            def onActivityPreStopped(self, *args): pass
            def onActivityPostStopped(self, *args): pass
            def onActivityPreSaveInstanceState(self, *args): pass
            def onActivityPostSaveInstanceState(self, *args): pass
            def onActivityPreDestroyed(self, *args): pass
            def onActivityPostDestroyed(self, *args): pass
        self.lifecycle = Lifecycle()
        self.state("waiting_activity", _WAIT)
        self.app.registerActivityLifecycleCallbacks(self.lifecycle)

    def _run_main(self, action):
        class Task(self.proxy(self.J("java.lang.Runnable"))):
            def run(self): action()
        if not self.handler.post(Task()):
            raise RuntimeError("main handler unavailable")

    def _unregister(self):
        launcher = self.launcher
        self.launcher, self.pending_host = None, None
        if launcher is not None:
            launcher.unregister()

    def choose(self):
        platform = self
        def launch():
            if self.closed:
                return
            if self.activity is None:
                self.state("error", _WAIT)
                return
            try:
                self._unregister()
                registry = self.cast(self.Owner, self.activity).getActivityResultRegistry()
                class Callback(self.proxy(self.Callback)):
                    def onActivityResult(self, value):
                        try:
                            result = platform.cast(platform.Result, value)
                            if result.getResultCode() != -1:
                                platform.result(None)
                            else:
                                data = result.getData()
                                if data is None or data.getData() is None or not int(data.getFlags()) & 1:
                                    platform.state("error", "폴더 읽기 권한이 전달되지 않았습니다.")
                                else:
                                    uri = str(data.getData().toString())
                                    # File-provider queries and fsync must not run on the main thread.
                                    threading.Thread(target=platform.result, args=(uri,), daemon=True,
                                                     name="naia-android-tree-result").start()
                        except Exception:
                            platform.state("error", "폴더 선택 결과를 읽지 못했습니다.")
                        finally:
                            platform._unregister()
                self.pending_host = self.activity
                self.launcher = registry.register("naia.tree." + uuid.uuid4().hex, self.Contract(), Callback())
                intent = self.Intent(self.Intent.ACTION_OPEN_DOCUMENT_TREE)
                intent.addFlags(1 | 64 | 128)  # READ, PERSISTABLE, PREFIX; no WRITE
                self.launcher.launch(intent)
            except Exception:
                self._unregister()
                self.state("error", "Android 폴더 선택창을 열지 못했습니다. 앱 화면으로 돌아와 다시 선택해 주세요.")
        self._run_main(launch)

    def has_grant(self, uri):
        grants = self.resolver.getPersistedUriPermissions()
        return any(str(grants.get(i).getUri().toString()) == uri and grants.get(i).isReadPermission()
                   for i in range(int(grants.size())))

    def remember(self, uri):
        parsed = self.Uri.parse(uri)
        if not self.D.isTreeUri(parsed):
            raise ValueError(_READ)
        self.resolver.takePersistableUriPermission(parsed, 1)

    def release(self, uri):
        self.resolver.releasePersistableUriPermission(self.Uri.parse(uri), 1)

    def descriptor(self, uri):
        parsed = self.Uri.parse(uri)
        ident = str(self.D.getTreeDocumentId(parsed))
        rows = self.resolver.query(self.D.buildDocumentUriUsingTree(parsed, ident), ["_display_name", "mime_type"], None, None, None)
        if rows is None:
            raise ValueError(_READ)
        try:
            if not rows.moveToFirst() or str(rows.getString(1)) != _DIR:
                raise ValueError(_READ)
            return ident, str(rows.getString(0) or "지정한 폴더")
        finally:
            rows.close()

    def children(self, uri, ident):
        target = self.D.buildChildDocumentsUriUsingTree(self.Uri.parse(uri), ident)
        rows = self.resolver.query(target, ["document_id", "_display_name", "mime_type", "_size"], None, None, None)
        if rows is None:
            raise ValueError(_READ)
        try:
            extras = rows.getExtras()
            if extras is not None and extras.getBoolean("loading", False):
                raise ValueError("기기가 폴더의 사진 목록을 준비 중입니다. 잠시 후 다시 열어 주세요.")
            if extras is not None and extras.getString("error"):
                raise ValueError(_READ)
            while rows.moveToNext():
                if rows.isNull(0) or rows.isNull(1):
                    continue
                yield {"id": str(rows.getString(0)), "name": str(rows.getString(1)),
                       "directory": str(rows.getString(2)) == _DIR,
                       "size": None if rows.isNull(3) else int(rows.getLong(3))}
        finally:
            rows.close()

    def open(self, uri, ident):
        target = self.D.buildDocumentUriUsingTree(self.Uri.parse(uri), ident)
        pfd = self.resolver.openFileDescriptor(target, "r")
        if pfd is None:
            raise ValueError(_READ)
        try:
            return os.fdopen(os.dup(int(pfd.getFd())), "rb")
        finally:
            pfd.close()

    def close(self):
        self.closed = True
        if self.lifecycle is not None:
            self.app.unregisterActivityLifecycleCallbacks(self.lifecycle)
        self._run_main(self._unregister)
        self.activity = self.lifecycle = None
