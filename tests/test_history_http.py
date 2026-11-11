"""The actual private HTTP transport and gallery service, with synthetic PNGs."""
import http.client
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest

from PIL import Image, PngImagePlugin

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from naia_history_library import HistoryLibrary
from naia_tools_ui import PanelServer


class HistoryHTTPTests(unittest.TestCase):
    def test_folder_import_list_preview_metadata_and_restart_through_real_http(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "saved"
            source.mkdir()
            png = PngImagePlugin.PngInfo()
            png.add_text("Comment", json.dumps({"prompt": "synthetic green square", "seed": 7}))
            with Image.new("RGB", (48, 32), "green") as picture:
                picture.save(source / "test.png", pnginfo=png)
            original = (source / "test.png").read_bytes()
            (source / "renamed.png").write_bytes(original)
            (source / "broken.png").write_bytes(b"not a PNG")
            with Image.new("RGB", (48, 32), "red") as picture:
                picture.save(source / "red.png")
            context = SimpleNamespace(_output_root=lambda: source, _current_save_directory=lambda: source)
            service = HistoryLibrary(context, root / "state")
            controller = SimpleNamespace(history_request=service.request, history_asset=service.asset,
                                         history_source_asset=service.source_asset)
            server = PanelServer(controller)

            def post(action, data=None, auth=True):
                connection = http.client.HTTPConnection("127.0.0.1", server.server.server_port, timeout=10)
                headers = {"Host": server.authority, "Origin": server.origin, "Content-Type": "application/json"}
                if auth:
                    headers["X-NAIA-Android-Tools"] = server.token
                connection.request("POST", "/history/" + action, json.dumps(data or {}), headers)
                response = connection.getresponse()
                body = response.read()
                result = json.loads(body) if response.getheader("Content-Type", "").startswith("application/json") else body
                code = response.status
                connection.close()
                return code, result

            try:
                self.assertEqual(post("state", auth=False)[0], 403)
                code, state = post("state")
                self.assertEqual(code, 200)
                self.assertTrue(state["roots"][0]["available"])
                self.assertEqual(len(post("browse", {"root_id": "output"})[1]["files"]), 4)
                preview_request = {"root_id": "output", "relative": "test.png", "kind": "thumb"}
                self.assertEqual(post("source_asset", preview_request, auth=False)[0], 403)
                code, thumbnail = post("source_asset", preview_request)
                self.assertEqual(code, 200)
                with Image.open(io.BytesIO(thumbnail)) as picture:
                    self.assertEqual(picture.size, (48, 32))
                self.assertEqual(post("list")[1]["total"], 0)
                self.assertEqual(post("source_asset", {**preview_request, "relative": "../outside.png"})[0], 400)
                self.assertEqual(post("import", {"root_id": "output", "whole_folder": True})[0], 200)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    job = post("job")[1]["job"]
                    if job["state"] != "running":
                        break
                    time.sleep(.01)
                self.assertEqual((job["state"], job["imported"], job["duplicates"], job["failed"]), ("completed", 2, 1, 1))
                item = next(item for item in post("list")[1]["items"] if item["id"] == hashlib.sha256(original).hexdigest())
                self.assertEqual(post("asset", {"id": item["id"], "kind": "image"})[1], original)
                self.assertEqual(post("metadata", {"id": item["id"]})[1]["metadata"]["prompt"], "synthetic green square")
                preview = post("asset", {"id": item["id"], "kind": "preview"})[1]
                with Image.open(io.BytesIO(preview)) as picture:
                    self.assertEqual(picture.size, (48, 32))
                self.assertEqual((source / "test.png").read_bytes(), original)
                self.assertEqual(len(list(source.iterdir())), 4)
                code, prepared = post("delete_prepare", {"ids": [item["id"]]})
                self.assertEqual((code, prepared["count"]), (200, 1))
                self.assertEqual(post("list")[1]["total"], 2)
                self.assertEqual(post("delete", {"token": prepared["token"]}, auth=False)[0], 403)
                code, deleted = post("delete", {"token": prepared["token"]})
                self.assertEqual((code, deleted["deleted"], deleted["remaining"]), (200, 1, 1))
                self.assertEqual(post("delete", {"token": prepared["token"]})[0], 400)
                prepared = post("delete_prepare", {"all": True})[1]
                self.assertEqual(prepared["count"], 1)
                deleted = post("delete", {"token": prepared["token"]})[1]
                self.assertEqual((deleted["deleted"], deleted["remaining"]), (1, 0))
                self.assertEqual((source / "test.png").read_bytes(), original)
                self.assertTrue((source / "red.png").exists())
            finally:
                server.close()
                service.close()
            reopened = HistoryLibrary(context, root / "state")
            try:
                self.assertEqual(reopened.request("list")["total"], 0)
                self.assertEqual((source / "test.png").read_bytes(), original)
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()

