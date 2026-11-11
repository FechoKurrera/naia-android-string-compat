import http.client
import io
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import naia_local_photo_picker as picker
from naia_history_library import HistoryLibrary


class TestLocalPhotoPicker(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.gallery = HistoryLibrary(types.SimpleNamespace(), Path(self.temp.name))
        self.addCleanup(self.gallery.close)
        self.urls = []
        self.bridge = picker.LocalPhotoPicker(self.gallery.import_stream, launcher=self.urls.append)
        self.addCleanup(self.bridge.close)
        result = self.bridge.open()
        self.assertTrue(result["opened"])
        self.url = urlsplit(self.urls[-1])
        self.assertNotIn(self.url.fragment, str(result))
        self.assertEqual(self.url.hostname, "127.0.0.1")

    def http(self, method, path, body=None, headers=None, *, auth=True):
        c = http.client.HTTPConnection(self.url.hostname, self.url.port, timeout=2)
        h = {"Host":self.url.netloc}
        if auth:
            h.update({"Origin":"http://" + self.url.netloc, "X-NAIA-Picker":self.url.fragment, "Content-Type":"application/octet-stream"})
        if headers:
            h.update(headers)
        try:
            c.request(method, path, body=body, headers=h)
            resp = c.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            c.close()

    @staticmethod
    def png():
        from PIL import Image
        b = io.BytesIO()
        with Image.new("RGB", (3, 2), "green") as im:
            im.save(b, "PNG")
        return b.getvalue()

    def test_real_http_import_to_library_dedup_and_receipt(self):
        status, headers, page = self.http("GET", "/picker", auth=False)
        self.assertEqual(status, 200)
        self.assertNotIn(self.url.fragment.encode(), page)
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.http("POST", "/ready", b"")[0], 200)
        label = "갤럭시 사진.png"
        args = ("POST", "/file", self.png(), {"X-NAIA-Name":quote(label)})
        for expected in ({"imported":1,"duplicates":0}, {"imported":0,"duplicates":1}):
            code, _, raw = self.http(*args)
            self.assertEqual(code, 200, raw)
            self.assertEqual(json.loads(raw), expected)
        gallery = self.gallery.request("list", {})
        self.assertEqual(gallery["total"], 1)
        self.assertEqual(gallery["items"][0]["name"], label)
        self.assertEqual(self.http("POST", "/finish", b"")[0], 200)

    def test_origin_host_token_size_name_content_and_body_limits(self):
        headers = {"X-NAIA-Name":"one.png"}
        for override, code in [({"Origin":"null"},403),({"Origin":"https://example.test"},403),({"Host":"localhost"},403),
                               ({"X-NAIA-Picker":"0"*64},403),({"Content-Type":"text/plain"},413),({"Content-Length":str(picker.MAX_BYTES+1)},413),
                               ({"X-NAIA-Name":"outer%2Fone.png"},400),({"X-NAIA-Name":"%FE.png"},400),
                               ({"Content-Encoding":"gzip"},400),({"Content-Length":"-1"},411)]:
            with self.subTest(override=override):
                self.assertEqual(self.http("POST", "/file", b"X", {**headers, **override})[0], code)
        self.assertEqual(self.http("GET", "/picker", headers={"Host":"example.test"}, auth=False)[0],404)
        self.assertEqual(self.http("POST", "/file", b"this is not an image", headers)[0],422)
        self.assertEqual(self.gallery.request("list", {})["total"], 0)
        self.assertFalse(list(self.gallery.temporary.iterdir()))

    def test_short_upload_does_not_import_or_expose_path(self):
        body = self.png()
        with socket.create_connection((self.url.hostname, self.url.port),timeout=3) as sock:
            hdr = ("POST /file HTTP/1.1\r\nHost: " + self.url.netloc + "\r\nOrigin: http://" + self.url.netloc
                   + "\r\nX-NAIA-Picker: " + self.url.fragment + "\r\nX-NAIA-Name: photo.png\r\n"
                   + "Content-Type: application/octet-stream\r\nContent-Length: " + str(len(body)+2) + "\r\n\r\n")
            sock.sendall(hdr.encode() + body)
            sock.shutdown(socket.SHUT_WR)
            resp = b""
            while True:
                block = sock.recv(65536)
                if not block:
                    break
                resp += block
        self.assertIn(b" 422 ", resp)
        self.assertNotIn(self.temp.name.encode(), resp)
        self.assertEqual(self.gallery.request("list", {})["total"], 0)
        self.assertFalse(list(self.gallery.temporary.iterdir()))

    def test_duplicate_header_and_unauthorized_post_cannot_use_page(self):
        c = http.client.HTTPConnection(self.url.hostname,self.url.port,timeout=2)
        try:
            c.putrequest("POST","/ready"); c.putheader("Origin","http://"+self.url.netloc)
            c.putheader("X-NAIA-Picker", self.url.fragment); c.putheader("X-NAIA-Picker", self.url.fragment)
            c.putheader("Content-Length", "0"); c.endheaders()
            resp = c.getresponse(); self.assertEqual(resp.status,403);resp.read()
        finally:
            c.close()
        self.assertEqual(self.http("POST","/ready",b"",auth=False)[0],403)

    def test_second_import_blocked_active_socket_interrupted_by_close(self):
        waiting = threading.Event()
        original = self.gallery.import_stream
        def slow(name, stream, size):
            waiting.set()
            return original(name,stream,size)
        self.bridge._server.callback=slow
        sock=socket.create_connection((self.url.hostname,self.url.port),timeout=4)
        self.addCleanup(sock.close)
        hdr=("POST /file HTTP/1.1\r\nHost: "+self.url.netloc+"\r\nOrigin: http://"+self.url.netloc
             +"\r\nX-NAIA-Picker: "+self.url.fragment+"\r\nX-NAIA-Name: slow.png\r\nContent-Type: application/octet-stream\r\nContent-Length: 256\r\n\r\n")
        sock.sendall(hdr.encode()+b"1")
        self.assertTrue(waiting.wait(2))
        self.assertEqual(self.http("POST","/file",self.png(),{"X-NAIA-Name":"second.png"})[0],409)
        with self.assertRaisesRegex(ValueError,"복사하고"):
            self.bridge.open()
        self.assertEqual(self.http("POST","/finish",b"")[0],409)
        start=time.monotonic();self.bridge.close()
        self.assertLess(time.monotonic()-start,3)
        self.assertEqual(self.gallery.request("list",{})["total"],0)
        self.assertFalse(list(self.gallery.temporary.iterdir()))
        with self.assertRaises(ValueError):
            self.bridge.open()

    def test_expiry_reopen_and_failed_native_launch(self):
        previous = self.url
        self.bridge.open(); self.url=urlsplit(self.urls[-1])
        self.assertNotEqual(self.url.fragment,previous.fragment)
        self.assertEqual(self.http("POST","/ready",b"",{"X-NAIA-Picker":previous.fragment})[0],403)
        server=self.bridge._server
        with patch.object(picker,"_IDLE_SECONDS",0):
            server.thread.join(2)
        self.assertFalse(server.thread.is_alive())
        def fail(url):
            self.urls.append(url)
            raise OSError("private/secret")
        self.bridge._launcher=fail
        with self.assertRaisesRegex(ValueError,"기기 브라우저") as failed:
            self.bridge.open()
        self.assertNotIn("secret",str(failed.exception))
        self.assertIsNone(self.bridge._server)
        ended=urlsplit(self.urls[-1])
        with self.assertRaises(OSError):
            socket.create_connection((ended.hostname,ended.port),timeout=.5)

    @unittest.skipUnless(shutil.which("node"),"Node is needed to execute page JS")
    def test_browser_requires_explicit_copy_before_transmitting(self):
        js=picker._PAGE.decode().split("<script>",1)[1].split("</script>",1)[0]
        harness=r"""
const vm=require('node:vm'), a=require('node:assert/strict'), nodes={}, calls=[];
for(const key of ['controls','status','photos','directory','copy','stop','finish','dirLabel'])
 nodes[key]={disabled:false,hidden:true,files:[],addEventListener(type,fn){this[type]=fn;}};
nodes.directory.webkitdirectory=true;
const ctx={document:{getElementById:id=>nodes[id]},location:{hash:'#'+'a'.repeat(64),pathname:'/picker'},
 history:{replaceState(a,b,c){if(c!='/picker')throw Error()}}, setTimeout,clearTimeout,AbortController,Uint8Array,
 fetch:async(path,opts)=>{calls.push([path,opts]);return {ok:true,json:async()=>path==='/file'?{imported:1,duplicates:0}:{ok:true}}}};
vm.runInNewContext(JS,ctx);
(async()=>{await new Promise(r=>setImmediate(r));a.equal(nodes.controls.hidden,false);
 const file={name:'선택.png',size:4};nodes.photos.files=[file,{name:'one.mov',size:100}];
 nodes.photos.change({target:nodes.photos});a.equal(calls.length,1);a.equal(nodes.copy.disabled,false);
 a.match(nodes.status.textContent,/1개 제외/);await nodes.copy.onclick();
 a.equal(calls[1][0],'/file');a.equal(calls[1][1].body,file);a.equal(calls[1][1].headers['X-NAIA-Name'],encodeURIComponent(file.name));
 a.match(nodes.status.textContent,/복사 1장/);await nodes.finish.onclick();a.equal(calls[2][0],'/finish');
})().catch(e=>{console.error(e);process.exit(1)});
"""
        subprocess.run(["node","-e",harness.replace("JS,ctx",json.dumps(js)+",ctx")],capture_output=True,check=True,timeout=8)
