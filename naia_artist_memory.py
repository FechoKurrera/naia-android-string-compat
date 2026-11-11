"""Bounded, disk-indexed artist packs for NAIA 2.0.48. Originals stay intact."""
from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import ItemsView, ValuesView
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import weakref

CHUNK_BYTES = 256 * 1024
MAX_RECORD_BYTES = 32 * 1024 * 1024
MAX_INDEX_BYTES = 32 * 1024 * 1024
MAX_ARTISTS = 200_000
PREPARING = "NAIA_INDEX_PREPARING"


class IndexPreparingError(RuntimeError):
    def __init__(self):
        super().__init__(PREPARING + ": 썸네일 목록을 준비하고 있습니다. 완료되면 자동으로 표시합니다.")


class SourceChangedError(RuntimeError):
    pass


def fingerprint(path):
    st = Path(path).stat()
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


class _Reader:
    """Find top-level JSON boundaries without reading the complete input."""
    special = re.compile(br'["\\{}\[\],:]')
    string_special = re.compile(br'["\\]')

    def __init__(self, file):
        self.file = file
        self.data = b""
        self.pos = 0
        self.base = 0
        self.eof = False

    @property
    def offset(self):
        return self.base + self.pos

    def fill(self):
        if self.eof:
            return False
        self.base += self.pos
        self.data = self.data[self.pos:]
        self.pos = 0
        chunk = self.file.read(CHUNK_BYTES)
        self.data += chunk
        if not chunk:
            self.eof = True
        return bool(chunk)

    def peek(self):
        while self.pos == len(self.data):
            if not self.fill():
                return None
        return self.data[self.pos]

    def whitespace(self):
        while self.peek() in (9, 10, 13, 32):
            self.pos += 1

    def expect(self, char):
        self.whitespace()
        if self.peek() != char:
            raise ValueError("썸네일 JSON 형식이 올바르지 않습니다.")
        self.pos += 1

    def value(self, delimiters, *, key=False):
        self.whitespace()
        start = self.offset
        pieces, size, depth = [], 0, 0
        quoted = escaped = False
        limit = 16384 if key else MAX_RECORD_BYTES
        while True:
            if self.peek() is None:
                raise ValueError("썸네일 JSON이 중간에서 끝났습니다.")
            begin = self.pos
            while self.pos < len(self.data):
                if escaped:
                    self.pos += 1
                    escaped = False
                    continue
                match = (self.string_special if quoted else self.special).search(self.data, self.pos)
                if match is None:
                    self.pos = len(self.data)
                    break
                self.pos = match.start()
                char = self.data[self.pos]
                if quoted:
                    if char == 92:
                        escaped = True
                    else:
                        quoted = False
                elif char == 34:
                    quoted = True
                elif depth == 0 and char in delimiters:
                    tail = self.data[begin:self.pos]
                    if size + len(tail) > limit:
                        raise ValueError("썸네일 항목 하나가 저메모리 처리 한도를 초과했습니다.")
                    raw = b"".join(pieces) + tail
                    value = json.loads(raw)
                    if key and not isinstance(value, str):
                        raise ValueError("썸네일 작가 이름은 문자열이어야 합니다.")
                    return start, len(raw), value
                elif char in (91, 123):
                    depth += 1
                elif char in (93, 125):
                    depth -= 1
                    if depth < 0:
                        raise ValueError("썸네일 JSON 괄호가 일치하지 않습니다.")
                self.pos += 1
            part = self.data[begin:self.pos]
            size += len(part)
            if size > limit:
                raise ValueError("썸네일 항목 하나가 저메모리 처리 한도를 초과했습니다.")
            pieces.append(part)


def build_index(path, sidecar, progress=lambda *_: None, cancelled=lambda: False):
    path, sidecar = Path(path), Path(sidecar)
    stamp = fingerprint(path)
    entries = {}
    estimated = 0
    with path.open("rb") as file:
        reader = _Reader(file)
        reader.expect(123)
        reader.whitespace()
        if reader.peek() != 125:
            while True:
                if cancelled():
                    raise InterruptedError("목록 준비가 취소되었습니다.")
                _, _, name = reader.value({58}, key=True)
                reader.expect(58)
                start, length, value = reader.value({44, 125})
                del value
                if name not in entries:
                    estimated += len(name.encode("utf-8")) + 80
                entries[name] = [start, length]
                if estimated > MAX_INDEX_BYTES or len(entries) > MAX_ARTISTS:
                    raise ValueError("썸네일 목록이 저메모리 색인 한도를 초과했습니다.")
                progress(min(99, int(reader.offset * 100 / max(1, stamp[2]))), len(entries))
                delimiter = reader.peek()
                if delimiter == 125:
                    break
                reader.expect(44)
        reader.expect(125)
        reader.whitespace()
        if reader.peek() is not None:
            raise ValueError("썸네일 JSON 끝에 잘못된 데이터가 있습니다.")
    if cancelled() or fingerprint(path) != stamp:
        raise SourceChangedError("썸네일 파일이 바뀌었습니다. 다시 목록을 준비합니다.")
    document = {"version": 1, "stamp": stamp, "entries": entries}
    raw = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_INDEX_BYTES:
        raise ValueError("썸네일 색인이 저메모리 처리 한도를 초과했습니다.")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=sidecar.name + ".", suffix=".tmp", dir=sidecar.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
        if cancelled() or fingerprint(path) != stamp:
            raise SourceChangedError("썸네일 파일이 바뀌었습니다. 다시 목록을 준비합니다.")
        temporary.replace(sidecar)
    finally:
        temporary.unlink(missing_ok=True)
    return DiskPack(path, stamp, entries)


class DiskPack(dict):
    """A dict-compatible roster; image arrays are read only on lookup."""
    def __init__(self, path, stamp, entries):
        super().__init__(entries)
        self.path, self.stamp = Path(path), list(stamp)

    @classmethod
    def open(cls, path, sidecar):
        path, sidecar = Path(path), Path(sidecar)
        if sidecar.stat().st_size > MAX_INDEX_BYTES:
            raise ValueError("색인 파일이 너무 큽니다.")
        data = json.loads(sidecar.read_bytes())
        stamp = fingerprint(path)
        entries = data.get("entries")
        if data.get("version") != 1 or data.get("stamp") != stamp or not isinstance(entries, dict) or len(entries) > MAX_ARTISTS:
            raise ValueError("썸네일 색인을 다시 준비해야 합니다.")
        for name, extent in entries.items():
            if (len(name.encode("utf-8")) > 16384 or not isinstance(extent, list) or len(extent) != 2
                    or any(type(n) is not int for n in extent) or extent[0] < 0 or not 0 < extent[1] <= MAX_RECORD_BYTES
                    or sum(extent) > stamp[2]):
                raise ValueError("썸네일 색인이 올바르지 않습니다.")
        return cls(path, stamp, entries)

    def valid(self):
        try:
            return fingerprint(self.path) == self.stamp
        except OSError:
            return False

    def __getitem__(self, key):
        start, size = dict.__getitem__(self, key)
        if not self.valid():
            raise SourceChangedError("썸네일 파일이 변경되었습니다. 목록을 다시 여세요.")
        with self.path.open("rb") as file:
            file.seek(start)
            raw = file.read(size)
        if len(raw) != size or not self.valid():
            raise SourceChangedError("썸네일 파일이 변경되었습니다. 목록을 다시 여세요.")
        return json.loads(raw)

    def get(self, key, default=None):
        return self[key] if key in self else default

    def items(self):
        return ItemsView(self)

    def values(self):
        return ValuesView(self)


class ByteLRU(OrderedDict):
    def __init__(self, budget):
        super().__init__()
        self.budget, self.bytes = budget, 0

    @staticmethod
    def size(value):
        return len(value[0]) if isinstance(value, tuple) and value and isinstance(value[0], bytes) else 0

    def __setitem__(self, key, value):
        if key in self:
            self.pop(key)
        size = self.size(value)
        if size > self.budget:
            return
        while self and self.bytes + size > self.budget:
            self.pop(next(iter(self)))
        super().__setitem__(key, value)
        self.bytes += size

    def get(self, key, default=None):
        if key not in self:
            return default
        self.move_to_end(key)
        return super().__getitem__(key)

    def pop(self, key, *args):
        if key in self:
            value = super().pop(key)
            self.bytes -= self.size(value)
            return value
        return super().pop(key, *args)

    def clear(self):
        super().clear()
        self.bytes = 0


class MemoryHandle:
    def __init__(self, settings_getter, state_root, service_class):
        self.settings_getter = settings_getter
        self.state_root = Path(state_root) if state_root is not None else None
        self.service_class = service_class
        self.lock = threading.RLock()
        self.services = weakref.WeakSet()
        self.limits = weakref.WeakKeyDictionary()
        self.jobs, self.queue = {}, deque()
        self.wake = threading.Event()
        self.closed = False
        self.revision = self.generation = 0
        self.options = self._options()
        names = ("__init__", "load_data", "_virtual_owner_map", "image_payload", "image_presence")
        self.original = {name: getattr(service_class, name) for name in names}
        handle = self

        def initialize(service, *args, **kwargs):
            handle.original["__init__"](service, *args, **kwargs)
            handle.attach(service)

        def load(service, mode):
            handle.attach(service)
            handle.refresh()
            if handle.closed or not handle._options()[0]:
                return handle.original["load_data"](service, mode)
            return handle.load(service, mode)

        def virtual(service, mode):
            if handle.closed or not handle._options()[0]:
                return handle.original["_virtual_owner_map"](service, mode)
            return handle.load_virtual(service, mode)

        def image(service, mode, artist):
            handle.attach(service)
            handle.refresh()
            if not handle.closed and handle._options()[0]:
                # Check replacement BEFORE upstream's decoded-image cache hit.
                handle.load(service, mode)
            return handle.original["image_payload"](service, mode, artist)

        def presence(service, mode=""):
            if not handle.closed and handle._options()[0] and mode:
                try:
                    load(service, mode)
                except (IndexPreparingError, SourceChangedError):
                    raise
                except Exception:
                    pass  # Preserve upstream fallback for absent/unknown packs.
            return handle.original["image_presence"](service, mode)

        self.wrappers = dict(zip(names, (initialize, load, virtual, image, presence)))
        for name, wrapper in self.wrappers.items():
            setattr(service_class, name, wrapper)
        self.worker = threading.Thread(target=self._worker, name="naia-artist-index", daemon=True)
        self.worker.start()

    def _options(self):
        settings = self.settings_getter() or {}
        try:
            mb = max(8, min(128, int(settings.get("cache_mb", 32))))
        except (TypeError, ValueError):
            mb = 32
        return settings.get("low_memory", True) is True and not self.closed, mb

    def attach(self, service):
        with self.lock:
            fresh = service not in self.services
            if fresh:
                self.services.add(service)
                self.limits[service] = service._data_cache_limit
        if fresh:
            self._configure(service)
        return service

    def _configure(self, service):
        enabled, mb = self._options()
        with service._lock:
            service._data_cache.clear()
            service._data_cache_size.clear()
            service._image_cache.clear()
            service._data_cache_limit = 1 if enabled else self.limits.get(service, 1)
            service._image_cache = ByteLRU(mb * 1024 * 1024) if enabled else {}

    def refresh(self):
        options = self._options()
        with self.lock:
            changed = options != self.options
            self.options = options
            services = list(self.services)
            if changed:
                self.generation += 1
                self.jobs.clear()
                self.queue.clear()
                self.revision += 1
        if changed:
            for service in services:
                self._configure(service)
        self.wake.set()
        return self.status()

    def _sidecar(self, service, path):
        root = self.state_root or Path(service.state_root)
        name = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:24]
        return root / "thumbnail-index" / (name + ".json")

    def _request(self, service, mode):
        path = Path(service._mode_path(mode)).resolve()
        stamp = fingerprint(path)
        key = str(path)
        with self.lock:
            job = self.jobs.get(key)
            if job is not None and job["stamp"] == stamp:
                if job["state"] == "ready":
                    return job["pack"]
                if job["state"] == "error":
                    raise RuntimeError(job["error"])
                raise IndexPreparingError()
            sidecar = self._sidecar(service, path)
            try:
                pack = DiskPack.open(path, sidecar)
            except (OSError, ValueError, TypeError, AttributeError, RecursionError):
                pack = None
            job = {"mode": mode, "path": key, "stamp": stamp, "sidecar": sidecar,
                   "state": "ready" if pack is not None else "queued", "percent": 100 if pack is not None else 0,
                   "count": len(pack) if pack is not None else 0, "error": "", "pack": pack,
                   "generation": self.generation}
            self.jobs[key] = job
            if pack is None:
                self.queue.append(job)
                self.wake.set()
                raise IndexPreparingError()
            self.revision += 1
            return pack

    def load(self, service, mode):
        key = str(mode or "").strip()
        if not key:
            return {}
        if service.is_virtual_mode(key):
            return self.load_virtual(service, key)
        generation = self.generation
        pack = self._request(service, key)
        fallback = False
        with service._lock:
            with self.lock:
                fallback = self.closed or not self._options()[0] or generation != self.generation
                if not fallback:
                    previous = service._data_cache.get(key)
                    if previous is not pack:
                        service._data_cache.pop(key, None)
                        service._data_cache_size.pop(key, None)
                        while len(service._data_cache) >= max(1, service._data_cache_limit):
                            oldest = next(iter(service._data_cache))
                            service._data_cache.pop(oldest, None)
                            service._data_cache_size.pop(oldest, None)
                        service._image_cache.clear()
                        service._data_cache[key] = pack
                        service._data_cache_size[key] = pack.stamp[2]
            if not fallback and key == "NAID4.5F-31000":
                service.sync_favorite_thumbnail_cache(key, pack)
        if fallback:
            self.refresh()
            return self.original["load_data"](service, mode)
        return pack

    def load_virtual(self, service, mode):
        self.attach(service)
        members = service.virtual_members(mode)
        service._data_cache_limit = max(1, len(members))
        owner, preparing = {}, False
        error = None
        for member in members:
            try:
                data = self.load(service, member)
            except IndexPreparingError:
                preparing = True
                continue
            except Exception as exc:
                error = exc
                continue
            for artist in data.keys():
                owner.setdefault(str(artist), member)
        if error:
            raise error
        if preparing:
            raise IndexPreparingError()
        return owner

    def _worker(self):
        while not self.closed:
            self.wake.wait(1)
            self.wake.clear()
            while not self.closed:
                with self.lock:
                    if not self.queue:
                        break
                    job = self.queue.popleft()
                    job["state"] = "indexing"

                def cancelled():
                    return self.closed or job["generation"] != self.generation or self.jobs.get(job["path"]) is not job

                def progress(percent, count):
                    with self.lock:
                        job.update(percent=percent, count=count)
                try:
                    pack = build_index(job["path"], job["sidecar"], progress, cancelled)
                    with self.lock:
                        if not cancelled():
                            job.update(state="ready", percent=100, count=len(pack), pack=pack)
                            self.revision += 1
                except Exception as error:
                    with self.lock:
                        if not cancelled():
                            job.update(state="error", error="썸네일 목록 준비 실패: " + str(error)[:240])
                            self.revision += 1

    def status(self):
        with self.lock:
            packs = [{key: job.get(key) for key in ("mode", "state", "percent", "count", "error")} for job in self.jobs.values()]
            return {"low_memory": self._options()[0], "cache_mb": self._options()[1],
                    "revision": self.revision, "busy": any(p["state"] in ("queued", "indexing") for p in packs), "packs": packs}

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.generation += 1
        self.wake.set()
        for name, wrapper in self.wrappers.items():
            if getattr(self.service_class, name) is wrapper:
                setattr(self.service_class, name, self.original[name])
        with self.lock:
            services = list(self.services)
            self.queue.clear()
            self.jobs.clear()
        for service in services:
            self._configure(service)


def install(settings_getter, state_root=None, service_class=None):
    if service_class is None:
        from core.artist_thumbnail_service import ArtistThumbnailService
        service_class = ArtistThumbnailService
    return MemoryHandle(settings_getter, state_root, service_class)


def patch_artist_asset(text):
    marker = "// naia-artist-memory-v1"
    if marker in text:
        return text
    anchor = "  async function loadPage(page = 0, options = {}) {"
    error_anchor = "      console.error('Artist Thumb list failed', error);"
    listener_anchor = "  bind();\n\n  return {"
    if any(text.count(s) != 1 for s in (anchor, error_anchor, listener_anchor)):
        raise RuntimeError("지원하지 않는 NAIA 아티스트 화면입니다.")
    text = text.replace(anchor, marker + "\n  let memoryRetry = null, memoryRevision = 0;\n" + anchor + "\n    memoryRetry = null;\n    const memoryAtStart = memoryRevision;", 1)
    text = text.replace(error_anchor, """      if (String(error.message || '').includes('NAIA_INDEX_PREPARING')) {
        memoryRetry = {page, options: {...options}};
        setStatus('썸네일 목록 준비 중… 완료되면 자동으로 표시합니다.', 'busy');
        if (gridEl) gridEl.textContent = '썸네일 목록을 준비하고 있습니다…';
        // The background worker can finish while this HTTP error is in flight.
        if (memoryRevision !== memoryAtStart) setTimeout(() => {
          if (memoryRetry && !selectionMode) void loadPage(page, options).catch(() => {});
        }, 0);
        return;
      }
""" + error_anchor, 1)
    text = text.replace(listener_anchor, """  window.addEventListener('naia-artist-index-ready', () => {
    memoryRevision += 1;
    if (!memoryRetry || selectionMode || !currentMode()) return;
    const retry = memoryRetry;
    void loadPage(retry.page, retry.options).catch(() => {});
  });
""" + listener_anchor, 1)
    return text
