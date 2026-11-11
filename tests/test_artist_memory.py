"""Offline parser, bounded-memory and actual NAIA service parity tests."""
import base64
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT.parent / "source" / "NAIA2.0"
sys.path[:0] = [str(ROOT), str(SOURCE)]
import naia_artist_memory as memory


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path, self.index = self.root / "pack.json", self.root / "index.json"

    def make(self, value):
        self.path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return memory.build_index(self.path, self.index)

    def test_escaped_unicode_nested_and_chunk_boundaries(self):
        value = {"a\\\"눈": ["a\\\"z\n" * 20, {"nested": [True, False, None, 5.2, "ñ"]}],
                 "empty": [], "scalar": 123, "오래": {"x": "\\\\"}, "null": None}
        for chunk in (1, 2, 3, 5, 7, 11, 31, 128, 256):
            with self.subTest(chunk=chunk), patch.object(memory, "CHUNK_BYTES", chunk):
                pack = self.make(value)
                self.assertEqual(list(pack), list(value))
                self.assertEqual(dict(pack.items()), value)
                self.assertEqual(list(pack.values()), list(value.values()))
                self.assertEqual(pack.get("missing", "default"), "default")
                self.assertEqual(dict(memory.DiskPack.open(self.path, self.index).items()), value)

    def test_empty_and_duplicate_keys_follow_json(self):
        self.assertEqual(len(self.make({})), 0)
        raw = '{"x":["first"],"y":1,"x":["last"]}'
        self.path.write_text(raw)
        pack = memory.build_index(self.path, self.index)
        self.assertEqual(list(pack), ["x", "y"])
        self.assertEqual(dict(pack.items()), json.loads(raw))

    def test_invalid_json_never_publishes_sidecar(self):
        for raw in ('[]', '{"x":1,}', '{"x":[1}', '{"x":"unterminated}', '{"x":1} extra', '{3:1}'):
            with self.subTest(raw=raw):
                self.path.write_text(raw)
                with self.assertRaises((ValueError, UnicodeError)):
                    memory.build_index(self.path, self.index)
                self.assertFalse(self.index.exists())

    def test_record_and_roster_limits_fail_before_unbounded_allocation(self):
        self.path.write_text(json.dumps({"a": ["x" * 5000]}))
        with patch.object(memory, "MAX_RECORD_BYTES", 128), patch.object(memory, "CHUNK_BYTES", 64), self.assertRaises(ValueError):
            memory.build_index(self.path, self.index)
        with patch.object(memory, "MAX_ARTISTS", 1), self.assertRaises(ValueError):
            self.make({"a": [], "b": []})
        with patch.object(memory, "MAX_INDEX_BYTES", 100), self.assertRaises(ValueError):
            self.make({"a" * 40: [], "b" * 40: []})

    def test_replacement_and_mutation_invalidate_offsets(self):
        pack = self.make({"a": ["old"]})
        replacement = self.root / "replacement"
        replacement.write_text(json.dumps({"a": ["new"]}))
        replacement.replace(self.path)
        with self.assertRaises(memory.SourceChangedError):
            pack["a"]
        with self.assertRaises(ValueError):
            memory.DiskPack.open(self.path, self.index)

    def test_source_change_during_build_does_not_publish(self):
        self.path.write_text(json.dumps({"a": ["old"], "b": ["more"]}))
        changed = False
        def change(*_):
            nonlocal changed
            if not changed:
                changed = True
                replacement = self.root / "new"
                replacement.write_text('{"x":[]}')
                replacement.replace(self.path)
        with self.assertRaises(memory.SourceChangedError):
            memory.build_index(self.path, self.index, change)
        self.assertFalse(self.index.exists())

    def test_sidecar_offset_validation(self):
        self.make({"a": ["x"]})
        data = json.loads(self.index.read_text())
        data["entries"]["a"] = [0, 10 ** 12]
        self.index.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            memory.DiskPack.open(self.path, self.index)

    def test_128mb_pack_indexing_uses_bounded_extra_rss(self):
        program = r'''
import json,pathlib,resource,sys,tempfile
sys.path.insert(0,sys.argv[1]);import naia_artist_memory as m
with tempfile.TemporaryDirectory() as d:
 p=pathlib.Path(d)/'pack.json';index=pathlib.Path(d)/'index.json'
 chunk='A'*(1024*1024)
 with p.open('w') as f:
  f.write('{')
  for n in range(128):
   if n:f.write(',')
   f.write(json.dumps(str(n))+':["'+chunk+'"]')
  f.write('}')
 before=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
 pack=m.build_index(p,index)
 added=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss-before
 assert len(pack)==128 and pack['127'][0]==chunk
 assert added<32768,(added,'KiB')
 assert index.stat().st_size<100000
 print(json.dumps({'extra_rss_kib':added,'pack_mib':p.stat().st_size//(1024*1024)}))
'''
        result = subprocess.run([sys.executable, "-c", program, str(ROOT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


class CacheTests(unittest.TestCase):
    def test_byte_lru_evicts_by_bytes_and_access(self):
        cache = memory.ByteLRU(8)
        cache["a"] = (b"123", "image/png")
        cache["b"] = (b"456", "image/png")
        cache.get("a")
        cache["c"] = (b"789", "image/png")
        self.assertEqual(list(cache), ["a", "c"])
        self.assertEqual(cache.bytes, 6)
        cache["big"] = (b"x" * 9, "image/png")
        self.assertNotIn("big", cache)
        cache.pop("a")
        self.assertEqual(cache.bytes, 3)
        cache.clear()
        self.assertEqual(cache.bytes, 0)

    def test_frontend_patch_has_scoped_retry_and_valid_syntax(self):
        path = SOURCE / "app/web/remote/js/features/artistThumbTab.mjs"
        original = path.read_text(encoding="utf-8")
        patched = memory.patch_artist_asset(original)
        self.assertEqual(memory.patch_artist_asset(patched), patched)
        self.assertIn("memoryRetry = {page, options: {...options}}", patched)
        self.assertIn("!memoryRetry || selectionMode", patched)
        if shutil.which("node"):
            with tempfile.TemporaryDirectory() as temp:
                out = Path(temp) / "artist.mjs"
                out.write_text(patched)
                checked = subprocess.run(["node", "--check", str(out)], capture_output=True, text=True)
                self.assertEqual(checked.returncode, 0, checked.stderr)


class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from core.artist_thumbnail_service import ArtistThumbnailService
        cls.base = ArtistThumbnailService

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.packdir = self.root / "packs"
        self.packdir.mkdir()
        encoded = base64.b64encode(b"thumbnail bytes").decode()
        self.data = {"artist_" + str(i): [encoded] for i in range(64)}
        self.second = {"artist_70": [encoded], "artist_1": [encoded]}
        (self.packdir / "a.json").write_text(json.dumps(self.data))
        (self.packdir / "b.json").write_text(json.dumps(self.second))
        (self.packdir / "f.json").write_text(json.dumps(self.data))
        modes = {"PACK-A": {"path": Path("data/a.json"), "label": "A", "expected_size": 0},
                 "PACK-B": {"path": Path("data/b.json"), "label": "B", "expected_size": 0},
                 "NAID4.5F-31000": {"path": Path("data/f.json"), "label": "F", "expected_size": 0}}
        virtual = {"ALL": {"prefix": "PACK-"}}
        self.original_type = type("OriginalService", (self.base,), {"ARTIST_THUMB_MODES": modes, "ARTIST_THUMB_VIRTUAL_MODES": virtual})
        self.patched_type = type("PatchedService", (self.base,), {"ARTIST_THUMB_MODES": modes, "ARTIST_THUMB_VIRTUAL_MODES": virtual})
        self.original = self.original_type(self.root, mode_data_root=self.packdir, state_root=self.root / "original")
        self.settings = {"low_memory": True, "cache_mb": 16}
        self.handle = memory.install(lambda: self.settings, self.root / "indexes", self.patched_type)
        self.addCleanup(self.handle.close)
        self.service = self.patched_type(self.root, mode_data_root=self.packdir, state_root=self.root / "patched")

    def ready(self, mode):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                return self.service.load_data(mode)
            except memory.IndexPreparingError:
                time.sleep(.01)
        self.fail("index did not finish")

    def test_original_list_search_page_virtual_and_images_match(self):
        self.ready("ALL")
        for mode in ("PACK-A", "PACK-B", "ALL"):
            for query, page in (("", 0), ("artist_1", 0), ("", 1)):
                kwargs = dict(mode=mode, query=query, page=page, per_page=12)
                self.assertEqual(self.service.build_list(**kwargs), self.original.build_list(**kwargs))
        self.assertEqual(self.service.load_data("ALL"), self.original.load_data("ALL"))
        self.assertEqual(self.service.image_payload("PACK-A", "artist_1"), self.original.image_payload("PACK-A", "artist_1"))
        self.assertTrue(self.service.image_presence("PACK-A")("artist_1"))
        self.assertIsInstance(self.service._data_cache["PACK-A"], memory.DiskPack)

    def test_favorites_and_groups_use_lazy_dict_values(self):
        self.ready("NAID4.5F-31000")
        for service in (self.original, self.service):
            service.load_data("NAID4.5F-31000")
            service.set_favorite("artist_1", True, "NAID4.5F-31000")
            service.sync_group_thumbnail_cache(["artist_1"])
        self.assertEqual(self.service.favorite_image_payload("artist_1"), self.original.favorite_image_payload("artist_1"))
        self.assertEqual(self.service.group_image_payload("artist_1"), self.original.group_image_payload("artist_1"))
        self.assertEqual(self.service.build_list(mode="NAID4.5F-31000", filter_key="favorites"), self.original.build_list(mode="NAID4.5F-31000", filter_key="favorites"))

    def test_preparing_returns_fast_and_virtual_never_partial(self):
        self.ready("PACK-A")
        original_builder = memory.build_index
        from threading import Event
        entered, release = Event(), Event()
        def blocked(*args, **kwargs):
            entered.set()
            release.wait(2)
            return original_builder(*args, **kwargs)
        with patch.object(memory, "build_index", blocked):
            start = time.monotonic()
            with self.assertRaises(memory.IndexPreparingError):
                self.service.load_data("ALL")
            self.assertLess(time.monotonic() - start, .5)
            self.assertTrue(entered.wait(1))
            with self.assertRaises(memory.IndexPreparingError):
                self.service.load_data("ALL")
            release.set()
            self.ready("ALL")

    def test_replacement_invalidates_image_cache_and_sidecar(self):
        self.ready("PACK-A")
        old = self.service.image_payload("PACK-A", "artist_1")
        data = dict(self.data)
        data["artist_1"] = [base64.b64encode(b"replacement data").decode()]
        new = self.packdir / "new"
        new.write_text(json.dumps(data))
        new.replace(self.packdir / "a.json")
        with self.assertRaises(memory.IndexPreparingError):
            self.service.image_payload("PACK-A", "artist_1")
        self.ready("PACK-A")
        self.assertNotEqual(self.service.image_payload("PACK-A", "artist_1"), old)

    def test_toggle_off_clears_lazy_data_and_restore(self):
        self.ready("PACK-A")
        self.assertIsInstance(self.service._image_cache, memory.ByteLRU)
        self.settings["low_memory"] = False
        self.handle.refresh()
        self.assertEqual(self.service._data_cache, {})
        self.assertNotIsInstance(self.service.load_data("PACK-A"), memory.DiskPack)
        self.assertEqual(self.service.load_data("PACK-A"), self.data)
        self.settings["low_memory"] = True
        self.handle.refresh()
        self.assertIsInstance(self.ready("PACK-A"), memory.DiskPack)
        self.handle.close()
        self.assertEqual(self.patched_type.load_data, self.original_type.load_data)
        self.assertEqual(self.service._data_cache, {})

    def test_switch_off_during_descriptor_lookup_cannot_restore_lazy_cache(self):
        self.ready("PACK-A")
        request = self.handle._request
        def disable(*args):
            result = request(*args)
            self.settings["low_memory"] = False
            return result
        with patch.object(self.handle, "_request", disable):
            result = self.service.load_data("PACK-A")
        self.assertNotIsInstance(result, memory.DiskPack)
        self.assertNotIsInstance(self.service._data_cache["PACK-A"], memory.DiskPack)

    def test_existing_sidecar_reused_without_reindexing(self):
        self.ready("PACK-A")
        self.settings["low_memory"] = False
        self.handle.refresh()
        self.settings["low_memory"] = True
        self.handle.refresh()
        with patch.object(memory, "build_index", side_effect=AssertionError("unexpected rebuild")):
            self.assertIsInstance(self.service.load_data("PACK-A"), memory.DiskPack)


if __name__ == "__main__":
    unittest.main()
