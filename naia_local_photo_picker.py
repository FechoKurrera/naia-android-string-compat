"""One temporary loopback page for importing explicitly selected Android documents."""
from __future__ import annotations

import hmac
import json
import re
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PurePath
from urllib.parse import unquote_to_bytes


MAX_BYTES = 64 * 1024 * 1024
_IDLE_SECONDS = 600
_SESSION_SECONDS = 3600
_TRANSFER_SECONDS = 180
_NAME = re.compile(r"[A-Za-z0-9._!~*'()%-]+\Z")
_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

_PAGE = r"""<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NAIA 사진 불러오기</title>
<style>body{font:16px/1.5 system-ui,sans-serif;color:#eef2fa;background:#141a23;margin:0}main{max-width:36rem;margin:5vh auto;padding:1.25rem}
label,button{display:inline-block;cursor:pointer;border:1px solid #667b99;border-radius:.6rem;color:inherit;background:#24344c;padding:.65rem 1rem;font:inherit;margin:.3rem .3rem .3rem 0}
label:focus-within{outline:2px solid #bbdcff} input{position:absolute;width:1px;height:1px;opacity:0}
button:disabled{opacity:.5;cursor:default}[hidden]{display:none!important}small{color:#c3d1df}
#status{white-space:pre-line;overflow-wrap:anywhere}</style>
<main><h1>NAIA 사진 불러오기</h1><p>선택한 사진을 이 기기의 NAIA 보관함에 복사합니다. 원본은 그대로 남습니다.</p>
<div id="controls" hidden><label>사진 선택<input id="photos" type="file" multiple accept="image/png,image/jpeg,image/webp,.png,.jpg,.jpeg,.webp"></label>
<label id="dirLabel" hidden>폴더 선택<input id="directory" type="file" webkitdirectory multiple></label>
<p><small>PNG, JPG, WebP · 사진당 64MiB 이하. 폴더를 고르면 하위 폴더의 사진도 포함됩니다.</small></p>
<button id="copy" disabled>선택한 사진 복사</button><button id="stop" hidden>이 사진까지만</button></div>
<p id="status" role="status" aria-live="polite"></p><button id="finish">마치기</button>
</main><script>
(() => {
'use strict';
const by = id => document.getElementById(id);
let token = location.hash.slice(1), pending = [], busy = false, stop = false;
history.replaceState(null, '', location.pathname);
const controls = by('controls'), status = by('status'), photos = by('photos'), directory = by('directory');
const copy = by('copy'), halt = by('stop'), finish = by('finish');
const say = s => { status.textContent = s; };
async function send(path, body, name, timeout) {
  const abort = new AbortController(), timer = setTimeout(() => abort.abort(), timeout);
  try {
    const headers = {'X-NAIA-Picker': token, 'Content-Type':'application/octet-stream'};
    if (name != null) headers['X-NAIA-Name'] = encodeURIComponent(name);
    const response = await fetch(path, {method:'POST', headers, body, signal:abort.signal, credentials:'omit', redirect:'error', cache:'no-store', referrerPolicy:'same-origin'});
    const data = await response.json();
    if (!response.ok) throw Error(data.error || '연결을 확인할 수 없습니다.');
    return data;
  } finally { clearTimeout(timer); }
}
function select(ev) {
  if (busy) return;
  const files = ev.target.files;
  if (!files || !files.length) return;
  pending = [];
  if (files.length > 10000) { copy.disabled = true; say('한 번에 10,000개 이하를 선택해 주세요.'); return; }
  for (const file of files)
    if (/\.(png|jpe?g|webp)$/i.test(file.name) && file.size > 0 && file.size <= 67108864) pending.push(file);
  copy.disabled = !pending.length;
  say('복사할 사진 '+pending.length+'장'+(pending.length < files.length ? ' · 형식 또는 크기 제한으로 '+(files.length-pending.length)+'개 제외' : '')+'\n아직 복사하지 않았습니다.');
}
photos.addEventListener('change', select); directory.addEventListener('change', select);
halt.onclick = () => { stop = true; halt.disabled = true; };
copy.onclick = async () => {
  if (busy || !pending.length) return;
  busy = true; stop = false; photos.disabled = directory.disabled = copy.disabled = finish.disabled = true;
  halt.hidden = false; halt.disabled = false;
  const group = pending; pending = [];
  let done = 0, duplicates = 0, n = 0, error = '';
  for (const f of group) {
    if (stop) break;
    say('복사 중 '+(n+1)+' / '+group.length);
    try {
      const data = await send('/file', f, f.name, 185000);
      done += data.imported; duplicates += data.duplicates; n++;
    } catch (e) {
      error = '전송 중단: '+(e.message || '연결을 확인하지 못했습니다.')+' 마지막 사진은 NAIA 보관함에서 확인해 주세요.';
      break;
    }
  }
  photos.value = directory.value = ''; busy = false; photos.disabled = directory.disabled = finish.disabled = false; halt.hidden = true;
  say('복사 '+done+'장 · 이미 보관함에 있음 '+duplicates+'장'+(error ? '\n'+error : n < group.length ? '\n남은 사진은 복사하지 않았습니다.' : '')+'\nNAIA로 돌아가 보관함을 새로고침하세요.');
};
finish.onclick = async () => {
  if (busy) return;
  finish.disabled = true; controls.hidden = true;
  try { if (token) await send('/finish', new Uint8Array(0), null, 3000); } catch (_) {}
  token = ''; pending = []; say('이 탭을 닫고 NAIA로 돌아가세요.');
};
if (!/^[a-f0-9]{64}$/.test(token)) { token=''; say('이 창은 만료됐습니다. NAIA에서 사진 선택을 다시 열어 주세요.'); return; }
send('/ready', new Uint8Array(0), null, 5000).then(() => {
  controls.hidden=false; by('dirLabel').hidden = !('webkitdirectory' in directory);
  say('사진을 고른 뒤 ‘선택한 사진 복사’를 눌러 주세요.');
}).catch(() => say('NAIA와 연결되지 않았습니다. 앱에서 사진 선택을 다시 열어 주세요.'));
})();
</script></html>""".encode("utf-8")


def _launch_browser(url):
    from java import jclass
    app = jclass("com.chaquo.python.Python").getPlatform().getApplication()
    cls = jclass("android.content.Intent")
    intent = cls(cls.ACTION_VIEW, jclass("android.net.Uri").parse(url))
    intent.addCategory(cls.CATEGORY_BROWSABLE)
    intent.addFlags(cls.FLAG_ACTIVITY_NEW_TASK)
    # resolveActivity can filter out installed handlers on Android 11+.
    # startActivity's result/exception is the authoritative dispatch check.
    app.startActivity(intent)


def _file_name(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 765 or not _NAME.fullmatch(value):
        raise ValueError
    if re.search(r"%(?![0-9a-fA-F]{2})", value):
        raise ValueError
    raw = unquote_to_bytes(value)
    name = raw.decode("utf-8")
    if (len(raw) > 255 or any(ch in name for ch in "/\\:") or
            any(ord(ch) < 32 or ord(ch) == 127 for ch in name) or
            PurePath(name).suffix.lower() not in _EXTENSIONS):
        raise ValueError
    return name


class _Body:
    """Expose exactly Content-Length bytes; premature EOF must abort the import."""
    def __init__(self, stream, size):
        self.stream, self.remaining = stream, size

    def read(self, size=-1):
        size = self.remaining if size is None or size < 0 else min(self.remaining, size)
        if not size:
            return b""
        chunk = self.stream.read(size)
        if not chunk:
            raise ValueError("파일 전송이 끝나기 전에 연결이 끊어졌습니다.")
        self.remaining -= len(chunk)
        return chunk


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, callback):
        self.callback = callback
        self.key = secrets.token_hex(32)
        self.transfer = threading.Lock()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.sockets = set()
        self.current = None
        self.started = self.last = time.monotonic()
        super().__init__(("127.0.0.1", 0), _Handler)
        self.timeout = .2
        self.host = "127.0.0.1:" + str(self.server_port)
        self.origin = "http://" + self.host
        self.thread = threading.Thread(target=self._run, daemon=True, name="NAIA-photo-picker")

    def process_request(self, request, address):
        with self.lock:
            reject = self.stop.is_set() or len(self.sockets) >= 6
            if not reject:
                self.sockets.add(request)
        if reject:
            self.close_request(request)
            return
        request.settimeout(15)
        super().process_request(request, address)

    def close_request(self, request):
        with self.lock:
            self.sockets.discard(request)
        super().close_request(request)

    def _run(self):
        try:
            while not self.stop.is_set():
                now = time.monotonic()
                with self.lock:
                    active = self.current
                    expired = now-self.started > _SESSION_SECONDS or (not active and now-self.last > _IDLE_SECONDS)
                if expired:
                    break
                if active and now > active[1]:
                    self._interrupt(active[0])
                self.handle_request()
        finally:
            self.stop.set()
            self.server_close()
            with self.lock:
                clients = list(self.sockets)
            for client in clients:
                self._interrupt(client)

    @staticmethod
    def _interrupt(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def handle_error(self, request, client_address):
        # A client closing a tab must not put its request or file info in logs.
        pass

    def finish(self):
        self.stop.set()
        self.thread.join()
        # When close returns, no callback from this listener may still run.
        with self.transfer:
            pass


class _Handler(BaseHTTPRequestHandler):
    server_version = "NAIA"
    sys_version = ""

    def log_message(self, *args):
        pass

    def _reply(self, code, data, mime="application/json; charset=utf-8"):
        self.close_connection = True
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _one(self, name):
        values = self.headers.get_all(name) or []
        return values[0] if len(values) == 1 else ""

    def _host(self):
        return not self.server.stop.is_set() and self._one("Host") == self.server.host

    def do_GET(self):
        if self.path != "/picker" or not self._host():
            self._reply(404, {})
        else:
            self._reply(200, _PAGE, "text/html; charset=utf-8")

    def do_POST(self):
        srv = self.server
        given = self._one("X-NAIA-Picker")
        if (not self._host() or self._one("Origin") != srv.origin or
                not re.fullmatch(r"[a-f0-9]{64}", given) or not hmac.compare_digest(given, srv.key)):
            return self._reply(403, {"error":"사진 선택 연결이 만료됐습니다."})
        if (self.headers.get("Transfer-Encoding") is not None or
                self.headers.get("Content-Encoding") is not None):
            return self._reply(400, {"error":"지원하지 않는 파일 전송 형식입니다."})
        raw = self._one("Content-Length")
        if not re.fullmatch(r"[0-9]{1,10}", raw):
            return self._reply(411, {"error":"파일 크기를 확인할 수 없습니다."})
        size = int(raw)
        if self.path in ("/ready", "/finish") and size == 0:
            with srv.lock:
                srv.last = time.monotonic()
            if self.path == "/finish" and srv.transfer.locked():
                return self._reply(409, {"error":"사진을 복사하고 있습니다."})
            self._reply(200, {"ok":True})
            if self.path == "/finish":
                srv.stop.set()
            return
        if self.path != "/file":
            return self._reply(404, {})
        if self._one("Content-Type") != "application/octet-stream" or not 0 < size <= MAX_BYTES:
            return self._reply(413, {"error":"64MiB 이하의 비어 있지 않은 사진을 선택해 주세요."})
        try:
            name = _file_name(self._one("X-NAIA-Name"))
        except (ValueError, UnicodeError):
            return self._reply(400, {"error":"PNG, JPG, WebP 사진 파일을 선택해 주세요."})
        if not srv.transfer.acquire(blocking=False):
            return self._reply(409, {"error":"다른 사진을 복사하고 있습니다."})
        try:
            if srv.stop.is_set():
                return self._reply(410, {"error":"사진 선택 연결이 종료됐습니다."})
            with srv.lock:
                srv.last = time.monotonic()
                srv.current = (self.connection, srv.last + _TRANSFER_SECONDS)
            body = _Body(self.rfile, size)
            try:
                result = srv.callback(name, body, size)
                pair = (result["imported"], result["duplicates"])
                if body.remaining or pair not in ((1, 0), (0, 1)):
                    raise RuntimeError("invalid import receipt")
            except ValueError as exc:
                # The history importer defines public Korean validation messages.
                # Never return arbitrary exception text, file names, or host paths.
                msg = str(exc)
                if "진행 중" in msg or "가져오는 중" in msg or "사용 중" in msg:
                    return self._reply(409, {"error":"다른 가져오기 작업이 끝나면 다시 시도하세요."})
                return self._reply(422, {"error":"사진을 복사하지 못했습니다. 파일 형식, 크기 또는 전송 상태를 확인해 주세요."})
            except Exception:
                return self._reply(500, {"error":"보관함에서 복사를 확인하지 못했습니다."})
            self._reply(200, {"imported":int(pair[0]), "duplicates":int(pair[1])})
        finally:
            with srv.lock:
                srv.current = None
                srv.last = time.monotonic()
            srv.transfer.release()


class LocalPhotoPicker:
    def __init__(self, callback, *, launcher=None):
        self._callback = callback
        self._launcher = launcher or _launch_browser
        self._lock = threading.Lock()
        self._closed = False
        self._server = None

    def open(self):
        with self._lock:
            if self._closed:
                raise ValueError("사진 선택이 종료되었습니다. NAIA 화면을 다시 열어 주세요.")
            if self._server is not None:
                if self._server.transfer.locked():
                    raise ValueError("사진을 복사하고 있습니다. 완료 후 다시 열어 주세요.")
                self._server.finish()
                self._server = None
            try:
                srv = _Server(self._callback)
                srv.thread.start()
            except (OSError, RuntimeError):
                raise ValueError("기기 안에서 사진 선택 화면을 열 수 없습니다.") from None
            self._server = srv
            try:
                self._launcher(srv.origin + "/picker#" + srv.key)
            except Exception:
                srv.finish()
                self._server = None
                raise ValueError("기기 브라우저를 열 수 없습니다. 사용 가능한 브라우저를 확인해 주세요.") from None
            return {"opened":True, "message":"기기 브라우저에서 사진을 고르고 ‘선택한 사진 복사’를 누르세요. 완료 후 NAIA 보관함을 새로고침하세요."}

    def close(self):
        with self._lock:
            self._closed = True
            if self._server is not None:
                self._server.finish()
                self._server = None
