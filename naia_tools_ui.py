"""Private loopback settings and composition-preserving NAIA web adapters."""
from __future__ import annotations

import hmac
import importlib
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class PanelServer:
    def __init__(self, controller):
        self.controller = controller
        self.token = secrets.token_urlsafe(32)
        self.closed = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def log_message(self, *args):
                pass

            def reply(self, code, data, content_type="application/json"):
                body = (data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", content_type + "; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors http://127.0.0.1:* http://localhost:*")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def reply_asset(self, body, content_type):
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def valid(self, mutation=False):
                if self.headers.get("Host") != owner.authority:
                    self.reply(403, {"error": "Invalid host"})
                    return False
                origin = self.headers.get("Origin")
                if (mutation and origin != owner.origin) or (origin is not None and origin != owner.origin):
                    self.reply(403, {"error": "Invalid origin"})
                    return False
                token = self.headers.get("X-NAIA-Android-Tools", "")
                if owner.closed or not hmac.compare_digest(token.encode("utf-8"), owner.token.encode("ascii")):
                    self.reply(403, {"error": "NAIA 화면을 새로고침해 주세요."})
                    return False
                return True

            def do_OPTIONS(self):
                self.reply(403, {"error": "Cross-origin requests are not allowed"})

            def do_GET(self):
                if self.path == "/" and self.headers.get("Host") == owner.authority and not owner.closed:
                    self.reply(200, Path(__file__).with_name("android_tools_panel.html").read_text(encoding="utf-8"), "text/html")
                    return
                if not self.valid():
                    return
                if self.path != "/status":
                    self.reply(404, {"error": "Not found"})
                    return
                try:
                    self.reply(200, owner.controller.status())
                except Exception:
                    self.reply(502, {"error": "도구 상태를 확인하지 못했습니다."})

            def do_POST(self):
                if not self.valid(mutation=True):
                    return
                if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                    self.reply(415, {"error": "JSON required"})
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    maximum = (920000 if self.path == "/screen/result" else
                               2 * 1024 * 1024 if self.path in {"/history/import", "/history/delete_prepare"} else 65536)
                    if not 0 < size <= maximum or self.headers.get("Transfer-Encoding"):
                        raise ValueError()
                    data = json.loads(self.rfile.read(size))
                    if not isinstance(data, dict):
                        raise ValueError()
                except (ValueError, UnicodeError, RecursionError, TimeoutError):
                    self.reply(400, {"error": "잘못된 요청입니다."})
                    return
                try:
                    if self.path == "/settings":
                        result = owner.controller.update_settings(data)
                    elif self.path == "/pair":
                        result = owner.controller.import_pairing(data)
                    elif self.path == "/disconnect":
                        result = owner.controller.disconnect()
                    elif self.path == "/folder/open":
                        if data.get("target") not in {"save", "artist"}:
                            raise ValueError("지원하지 않는 폴더입니다.")
                        result = owner.controller.open_folder(data["target"])
                    elif self.path == "/open-relay":
                        result = owner.controller.open_relay()
                    elif self.path == "/comic/prepare":
                        if data:
                            raise ValueError("잘못된 Comic Maker 요청입니다.")
                        result = owner.controller.prepare_prompt_server()
                    elif self.path == "/comic/stop":
                        if data:
                            raise ValueError("잘못된 Comic Maker 요청입니다.")
                        result = owner.controller.stop_prompt_server()
                    elif self.path == "/history/asset":
                        if set(data) != {"id", "kind"}:
                            raise ValueError("잘못된 사진 요청입니다.")
                        body, mime = owner.controller.history_asset(data["id"], data["kind"])
                        self.reply_asset(body, mime)
                        return
                    elif self.path == "/history/source_asset":
                        if set(data) != {"root_id", "relative", "kind"}:
                            raise ValueError("잘못된 사진 미리보기 요청입니다.")
                        body, mime = owner.controller.history_source_asset(data["root_id"], data["relative"], data["kind"])
                        self.reply_asset(body, mime)
                        return
                    elif self.path.startswith("/history/"):
                        action = self.path[len("/history/"):]
                        if action not in {"state", "pick_folder", "open_photo_picker", "browse", "import", "job", "cancel", "list", "metadata", "delete_prepare", "delete"}:
                            self.reply(404, {"error": "Not found"})
                            return
                        result = owner.controller.history_request(action, data)
                    elif self.path in ("/screen/poll", "/screen/result"):
                        screen = owner.controller.screen
                        config = owner.controller.bridge_settings()
                        if screen is None or config.get("enabled") is not True or config["permissions"].get("screen") is not True:
                            raise ValueError("화면 보기 권한을 켜 주세요.")
                        if self.path == "/screen/poll":
                            if set(data) != {"client_id"}:
                                raise ValueError("잘못된 화면 요청입니다.")
                            result = screen.poll(data["client_id"])
                        else:
                            if set(data) != {"client_id", "id", "result", "is_error"}:
                                raise ValueError("잘못된 화면 응답입니다.")
                            result = screen.submit(data["client_id"], {k: v for k, v in data.items() if k != "client_id"})
                    else:
                        self.reply(404, {"error": "Not found"})
                        return
                    self.reply(200, result if isinstance(result, dict) else {"ok": True})
                except ValueError as error:
                    self.reply(400, {"error": str(error)[:300] or "설정을 확인해 주세요."})
                except Exception:
                    self.reply(502, {"error": "요청을 완료하지 못했습니다. 다시 시도해 주세요."})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.authority = f"127.0.0.1:{self.server.server_port}"
        self.origin = "http://" + self.authority
        self.panel_url = self.origin + "/#token=" + self.token
        self.thread = threading.Thread(target=self.server.serve_forever, name="naia-android-tools-panel", daemon=True)
        self.thread.start()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def patch_index(source):
    marker = '<div class="settings-page active" data-settings-page-content="global">'
    if 'id="naiaAndroidToolsSettings"' in source:
        return source
    if source.count(marker) != 1 or source.count("</body>") != 1:
        raise RuntimeError("지원하지 않는 NAIA Settings 화면입니다.")
    return source.replace(marker, marker + '''
          <div class="settings-group" id="naiaAndroidToolsSettings">
            <h3 class="settings-group-title">Android 도구</h3>
            <button type="button" class="mod-btn-secondary settings-action-btn" data-android-tools-open>Android 도구 · 플러그인</button>
            <p class="settings-toggle-desc">작가 썸네일 메모리와 기기 플러그인 연결을 설정합니다.</p>
          </div>''', 1)


def _injection(panel_url):
    directory = Path(__file__).resolve().parent
    scripts = [directory / "vendor/html2canvas-1.4.1.min.js", directory / "naia_screen_agent.js",
               directory / "naia_history_library.js"]
    libraries = "\n".join(path.read_text(encoding="utf-8") for path in scripts)
    # Inline only packaged static assets. Never insert device data in scripts.
    libraries = libraries.replace("</script", "<\\/script")
    return '<script>' + libraries + '</script>' + r'''<script>(()=>{
if(window.__naiaAndroidToolsPanel)return;window.__naiaAndroidToolsPanel=true;
const panelURL=new URL(PANEL_URL);const panelOrigin=panelURL.origin;
panelURL.hash += '&parent='+encodeURIComponent(location.origin);
const dialog=document.createElement('dialog');dialog.setAttribute('aria-label','Android 도구 · 플러그인');
dialog.style.cssText='padding:0;border:1px solid #475569;border-radius:16px;width:min(580px,96vw);max-height:94vh;background:#111827;color:#eee;z-index:2147483647';
const close=document.createElement('button');close.textContent='닫기 ×';close.style.cssText='display:block;margin:8px 12px 8px auto;min-height:44px;padding:8px 16px;background:#243244;color:#fff;border:0;border-radius:8px';close.onclick=()=>dialog.close();
// The command transport must not live inside a closed dialog. Keep it
// mounted for the whole page lifetime; settings use an independent iframe.
const transportURL=new URL(panelURL);transportURL.hash+='&mode=transport';
const frame=document.createElement('iframe');frame.title='NAIA 화면 연결';frame.referrerPolicy='no-referrer';frame.setAttribute('aria-hidden','true');frame.tabIndex=-1;frame.style.cssText='position:fixed;bottom:0;left:0;width:1px;height:1px;border:0;opacity:0.01;pointer-events:none';frame.src=transportURL.href;
const settingsFrame=document.createElement('iframe');settingsFrame.title='Android 도구 · 플러그인';settingsFrame.referrerPolicy='no-referrer';settingsFrame.style.cssText='width:100%;height:min(740px,78vh);border:0;display:block';
dialog.append(close,settingsFrame);document.body.append(frame,dialog);
window.__naiaInstallScreenAgent({frame,panelOrigin});
let serial=0,ready=false,revision;const pending=new Map();
const historyPending=new Map();
async function historyCall(action,data={}){
 for(let i=0;!ready&&i<40;i++)await new Promise(resolve=>setTimeout(resolve,250));
 if(!ready)throw new Error('앱 실행부와 연결되지 않았습니다. 화면을 새로고침해 주세요.');
 if(historyPending.size>=16)throw new Error('사진을 불러오는 중입니다. 잠시 후 다시 눌러 주세요.');
 const id=++serial;return new Promise((resolve,reject)=>{
  const timer=setTimeout(()=>{historyPending.delete(id);reject(new Error('응답을 기다리지 못했습니다. 가져오기 상태를 확인해 주세요.'));},120000);
  historyPending.set(id,{resolve,reject,timer});
  frame.contentWindow.postMessage({type:'naia-history-request',id,action,data},panelOrigin);
 });
}
window.naiaAndroidTools={openFolderNative(target){
 if(!ready||!['save','artist'].includes(target))return Promise.resolve({opened:false});
 const id=++serial;return new Promise(resolve=>{
  const timer=setTimeout(()=>{pending.delete(id);resolve({opened:false});},2200);
  pending.set(id,{resolve,timer});frame.contentWindow.postMessage({type:'naia-android-folder',id,target},panelOrigin);
 });
}};
window.addEventListener('message',event=>{
 if(event.source!==frame.contentWindow||event.origin!==panelOrigin)return;
 const data=event.data;if(!data||typeof data!=='object')return;
 if(data.type==='naia-android-ready'){ready=true;return;}
 if(data.type==='naia-history-result'){
  const item=historyPending.get(data.id);if(!item)return;
  historyPending.delete(data.id);clearTimeout(item.timer);
  if(data.error)item.reject(new Error(String(data.error)));else item.resolve(data.result);
  return;
 }
 if(data.type==='naia-android-folder-result'){
  const item=pending.get(data.id);if(!item)return;pending.delete(data.id);clearTimeout(item.timer);item.resolve({opened:data.opened===true});
 }
 if(data.type==='naia-android-memory'&&Number.isInteger(data.revision)&&data.revision!==revision){
  revision=data.revision;window.dispatchEvent(new CustomEvent('naia-artist-index-ready',{detail:{revision,force:true}}));
 }
});
// Foreground page messages wake the transport even if WebView throttles an
// iframe timer. Repeated handshake also recovers a missed initial ready event.
function wakeTransport(){frame.contentWindow.postMessage({type:'naia-transport-wake'},panelOrigin);}
frame.addEventListener('load',wakeTransport);
const wakeTimer=setInterval(wakeTransport,1000);
window.addEventListener('pageshow',wakeTransport);
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')wakeTransport();});
window.addEventListener('pagehide',event=>{if(!event.persisted)clearInterval(wakeTimer);});
document.addEventListener('click',event=>{if(event.target.closest?.('[data-android-tools-open]')){event.preventDefault();if(!settingsFrame.src)settingsFrame.src=panelURL.href;if(!dialog.open)dialog.showModal();}});
window.__naiaInstallHistoryLibrary?.({request:historyCall,asset:(id,kind)=>historyCall('asset',{id,kind}),sourceAsset:(root_id,relative,kind='thumb')=>historyCall('source_asset',{root_id,relative,kind})});
})();</script>'''.replace("PANEL_URL", json.dumps(panel_url))


def _response_text(response):
    if getattr(response, "status_code", 200) != 200:
        raise RuntimeError("NAIA 화면 파일을 읽지 못했습니다.")
    body = getattr(response, "body", None)
    if isinstance(body, bytes):
        return body.decode("utf-8")
    if isinstance(body, str):
        return body
    path = getattr(response, "path", None)
    if path is not None and Path(path).is_file():
        return Path(path).read_text(encoding="utf-8")
    raise RuntimeError("다른 확장의 화면 응답을 안전하게 읽을 수 없습니다.")


def install_ui(controller, folder_module, memory_module):
    from fastapi.responses import Response
    from app.web import resolve_remote_web_dir

    module = importlib.import_module("app.backend.server.web_shell_routes")
    previous_file, previous_asset = module._web_file, module._web_asset
    root = Path(resolve_remote_web_dir(controller.ctx._app_context.repo_root)).resolve()
    index = root / "index.html"
    asset_root = root / "js"
    assets = {"features/resultHistory.mjs", "features/artistThumbTab.mjs"}

    def adapt(asset, text):
        text = folder_module.patch_asset(asset, text)
        if asset == "features/artistThumbTab.mjs":
            text = memory_module.patch_artist_asset(text)
        return text

    patch_index(_response_text(previous_file(index, "text/html")))
    for asset in assets:
        adapt(asset, _response_text(previous_asset(asset_root, asset, default_media_type="text/javascript")))
    server = PanelServer(controller)
    active = True

    def wrap_response(previous, text, media_type):
        headers = {key: value for key, value in getattr(previous, "headers", {}).items()
                   if key.lower() not in {"content-length", "content-type", "content-encoding", "etag", "last-modified"}}
        headers["Cache-Control"] = "no-store"
        return Response(text, media_type=media_type, headers=headers,
                        background=getattr(previous, "background", None))

    def web_file(path, media_type):
        previous = previous_file(path, media_type)
        if active and Path(path).resolve() == index and media_type == "text/html":
            text = patch_index(_response_text(previous))
            return wrap_response(previous, text.replace("</body>", _injection(server.panel_url) + "</body>", 1), "text/html")
        return previous

    def web_asset(root, asset_path, *, default_media_type="application/octet-stream"):
        previous = previous_asset(root, asset_path, default_media_type=default_media_type)
        if active and Path(root).resolve() == asset_root and asset_path in assets:
            return wrap_response(previous, adapt(asset_path, _response_text(previous)), "text/javascript")
        return previous

    module._web_file, module._web_asset = web_file, web_asset

    def cleanup():
        nonlocal active
        if not active:
            return
        active = False
        if module._web_file is web_file:
            module._web_file = previous_file
        if module._web_asset is web_asset:
            module._web_asset = previous_asset
        server.close()

    cleanup.server = server
    cleanup.panel_url = server.panel_url
    return cleanup
