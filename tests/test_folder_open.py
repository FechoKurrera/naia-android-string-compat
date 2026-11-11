import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT.parent / "source" / "NAIA2.0"
spec = importlib.util.spec_from_file_location("folder_adapter_test", ROOT / "naia_folder_open.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class FolderOpenTests(unittest.TestCase):
    def test_canonical_primary_directory_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "primary"
            root.mkdir()
            images = root / "Images 한글"
            images.mkdir()
            value = module.document_uri(images, root)
            self.assertIn("primary%3AImages%20", value)
            self.assertNotIn("file://", value)
            self.assertIsNone(module.document_uri(root / "missing", root))
            self.assertFalse((root / "missing").exists())
            outside = Path(temporary) / "private"
            outside.mkdir()
            (root / "escape").symlink_to(outside, target_is_directory=True)
            self.assertIsNone(module.document_uri(root / "escape", root))
            (root / "Android" / "data").mkdir(parents=True)
            self.assertIsNone(module.document_uri(root / "Android" / "data", root))
            file = root / "file.png"
            file.write_bytes(b"not directory")
            self.assertIsNone(module.document_uri(file, root))

    def test_native_checks_provider_handler_and_closes_cursor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "images"
            folder.mkdir()
            events = []
            mime = [module.DIRECTORY_MIME]
            handler = [object()]
            cursor = types.SimpleNamespace(moveToFirst=lambda: True, getColumnIndex=lambda name: 0,
                getString=lambda index: mime[0], close=lambda: events.append("closed"))
            resolver = types.SimpleNamespace(query=lambda *args: cursor)
            app = types.SimpleNamespace(getContentResolver=lambda: resolver, getPackageManager=lambda: "pm",
                startActivity=lambda intent: events.append(("started", intent.uri, intent.flags)))
            class Intent:
                ACTION_VIEW = "view"
                FLAG_ACTIVITY_NEW_TASK = 0x10000000
                def __init__(self, action): self.action = action
                def setDataAndType(self, uri, mime): self.uri, self.mime = uri, mime
                def addFlags(self, flags): self.flags = flags
                def resolveActivity(self, manager): return handler[0]
            classes = {
                "com.chaquo.python.Python": types.SimpleNamespace(getPlatform=lambda: types.SimpleNamespace(getApplication=lambda: app)),
                "android.os.Environment": types.SimpleNamespace(getExternalStorageDirectory=lambda: types.SimpleNamespace(getCanonicalPath=lambda: str(root))),
                "android.net.Uri": types.SimpleNamespace(parse=lambda value: value),
                "android.content.Intent": Intent,
            }
            with patch.dict(sys.modules, {"java": types.SimpleNamespace(jclass=lambda key: classes[key])}):
                self.assertTrue(module.open_native_folder(folder)["opened"])
                self.assertEqual(events[-1], "closed")
                self.assertEqual(events[0][2], Intent.FLAG_ACTIVITY_NEW_TASK)
                events.clear(); mime[0] = "image/png"
                self.assertFalse(module.open_native_folder(folder)["opened"])
                self.assertEqual(events, ["closed"])
                events.clear(); mime[0] = module.DIRECTORY_MIME; handler[0] = None
                self.assertFalse(module.open_native_folder(folder)["opened"])
                self.assertEqual(events, ["closed"])
                events.clear(); app.startActivity = lambda intent: (_ for _ in ()).throw(RuntimeError("private detail"))
                handler[0] = object()
                result = module.open_native_folder(folder)
                self.assertFalse(result["opened"])
                self.assertNotIn("private detail", str(result))
                self.assertEqual(events, ["closed"])

    def test_exact_upstream_patch_and_syntax(self):
        for name in ("resultHistory.mjs", "artistThumbTab.mjs"):
            source = (SOURCE / "app/web/remote/js/features" / name).read_text()
            result = module.patch_asset("features/" + name, source)
            self.assertEqual(module.patch_asset("features/" + name, result), result)
            self.assertNotIn("fetch('/api/history/open-folder'", result)
            self.assertNotIn("fetch('/api/artist-thumb/open-folder'", result)
            with tempfile.TemporaryDirectory() as temporary:
                file = Path(temporary) / name
                file.write_text(result)
                subprocess.run(["node", "--check", str(file)], check=True, capture_output=True)
        with self.assertRaises(RuntimeError):
            module.patch_asset("features/resultHistory.mjs", "unsupported")

    def test_executed_javascript_native_and_readonly_fallback(self):
        script = r'''
const assert=require('node:assert/strict'), vm=require('node:vm');
const source=SOURCE;
class El {
 constructor(tag){this.tag=tag;this.children=[];this.style={};this.events={};this.hidden=false;this.open=false;this.attrs={};}
 append(...items){this.children.push(...items);for(const x of items)x.parent=this;}
 setAttribute(k,v){this.attrs[k]=v;}
 removeAttribute(k){delete this[k];delete this.attrs[k];}
 replaceChildren(...items){this.children=items;}
 addEventListener(k,cb){this.events[k]=cb;}
 showModal(){this.open=true;}
 close(){this.open=false;this.events.close?.();}
 remove(){this.removed=true;}
 focus(){}
}
function setup(native){
 const elements=[],pages=[],toast=[];
 const document={createElement(tag){const el=new El(tag);elements.push(el);return el;},body:new El('body')};
 const context={document,window:{naiaAndroidTools:{openFolderNative:native}},showToast:(...x)=>toast.push(x),
 URLSearchParams,AbortController,Promise,setTimeout,clearTimeout,encodeURIComponent,
 fetch:async url=>{pages.push(url);const page=Number(new URL(url,'http://localhost').searchParams.get('page'));return {ok:true,json:async()=>({total:25,images:Array.from({length:page?1:24},(_,i)=>({rel_path:'folder/'+(page*24+i)+'.png',filename:'image'}))})};}};
 vm.createContext(context);vm.runInContext(source,context);return {context,elements,pages,toast};
}
async function settle(){await new Promise(r=>setImmediate(r));}
(async()=>{
 const native=setup(async()=>({opened:true}));await vm.runInContext('openFolder()',native.context);assert.equal(native.pages.length,0);assert.equal(native.elements.length,0);assert.equal(native.toast.length,1);
 const fallback=setup(async()=>({opened:false}));await vm.runInContext('openFolder()',fallback.context);await settle();assert.equal(fallback.pages.length,1);assert.match(fallback.pages[0],/scope=disk/);assert.match(fallback.pages[0],/per_page=24/);
 const dialog=fallback.elements.find(x=>x.tag==='dialog');assert.equal(dialog.open,true);assert.equal(fallback.elements.filter(x=>x.tag==='img'&&x.loading==='lazy').length,24);
 const next=fallback.elements.find(x=>x.textContent==='다음');assert.equal(next.disabled,false);await next.onclick();assert.match(fallback.pages[1],/page=1/);assert.equal(next.disabled,true);
 const cell=fallback.elements.filter(x=>x.tag==='button'&&x.children[0]?.tag==='img').at(-1);cell.onclick();const preview=fallback.elements.find(x=>x.alt==='저장된 이미지 크게 보기');assert.equal(preview.src,'/api/viewer/image/folder/24.png');assert.equal(cell.parent.style.display,'none');assert.equal(next.parent.style.display,'none');
 fallback.elements.find(x=>x.textContent==='목록으로').onclick();assert.equal(cell.parent.style.display,'grid');assert.equal(next.parent.style.display,'flex');cell.onclick();
 fallback.elements.find(x=>x.textContent==='닫기').onclick();assert.equal(dialog.removed,true);assert.equal(preview.src,undefined);
 const thrown=setup(async()=>{throw new Error('native unavailable')});await vm.runInContext('openFolder()',thrown.context);await settle();assert.equal(thrown.pages.length,1);
 const artist={window:{naiaAndroidTools:{openFolderNative:async()=>false}},openingFolder:false,loadPage:async(...x)=>artist.loaded=x,showToast:(...x)=>artist.toast=x,Promise,setTimeout,clearTimeout};vm.createContext(artist);vm.runInContext(ARTIST,artist);await vm.runInContext('openThumbnailFolder()',artist);assert.equal(artist.loaded[0],0);assert.equal(artist.openingFolder,false);assert.match(artist.toast[0],/앱 안에서/);
})().catch(e=>{console.error(e);process.exitCode=1});
'''.replace("SOURCE", json.dumps(module._NATIVE + module._SAVED)).replace("ARTIST", json.dumps(module._NATIVE + module._ARTIST))
        result = subprocess.run(["node", "-e", script], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
