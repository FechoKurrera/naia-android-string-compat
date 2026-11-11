"""The gallery connects **the same grant** to list, preview and original imports."""
import hashlib
import importlib.util
import io
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from contextlib import contextmanager

from PIL import Image, PngImagePlugin

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('picker_history_library', ROOT / 'naia_history_library.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def png(color):
    out = io.BytesIO()
    info = PngImagePlugin.PngInfo(); info.add_text('Description', 'original metadata')
    with Image.new('RGB', (11, 8), color) as img:
        img.save(out, format='PNG', pnginfo=info)
    return out.getvalue()


class Tree:
    token = 'android_tree_a0123456789abcde'
    def __init__(self):
        self.selected = self.closed = False
        self.pick_count = 0
        self.paths = {'one.png': png('red'), 'sub/two.png': png('blue')}
        self.opens = []
    def status(self):
        return {'selected': self.selected, 'root_id': self.token if self.selected else '', 'label': '선택한 폴더',
                'state': 'selected' if self.selected else 'ready', 'available': True, 'detail': ''}
    def choose(self):
        self.pick_count += 1; self.selected = True
        return self.status()
    def check(self, root_id):
        if not self.selected or self.token != root_id:
            raise ValueError('선택한 폴더가 바뀌었습니다.')
    def browse(self, relative, page, per_page, *, root_id):
        self.check(root_id)
        return {'root_id': root_id, 'relative': relative, 'page': 1, 'pages': 1, 'parent': None,
                'total': 2, 'dirs': [{'name': 'sub', 'relative': 'sub'}],
                'files': [{'name': 'one.png', 'relative': 'one.png', 'size': len(self.paths['one.png'])}]}
    def open_file(self, relative, *, root_id):
        self.check(root_id); self.opens.append(relative)
        return io.BytesIO(self.paths[relative])
    def walk(self, relative, recursive, cancel, *, root_id):
        self.check(root_id)
        for path in self.paths:
            if cancel(): return
            if recursive or '/' not in path: yield path
    def close(self): self.closed = True


class GrantIntegration(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        base = Path(temporary.name); self.dir = base / 'out'; self.dir.mkdir()
        self.tree = Tree()
        self.lib = mod.HistoryLibrary(SimpleNamespace(_output_root=lambda:self.dir), base / 'state',
                                      tree_factory=lambda _state: self.tree)
        self.addCleanup(self.lib.close)
    def import_files(self, **kwargs):
        self.lib.request('import', {'root_id': self.tree.token, **kwargs})
        self.lib._worker.join(5); self.assertFalse(self.lib._worker.is_alive())
        return self.lib.request('job')['job']
    def test_grant_chosen_only_on_click_then_used_for_list_preview_selected_and_recursive_import(self):
        state = self.lib.request('state'); self.assertFalse(state['picker']['selected']); self.assertEqual(self.tree.pick_count, 0)
        with self.assertRaises(ValueError): self.lib.request('pick_folder', {'uri': 'content://arbitrary'})
        self.assertEqual(self.tree.pick_count, 0)
        root_id = self.lib.request('pick_folder')['picker']['root_id']
        self.assertEqual(self.tree.pick_count, 1)
        self.assertIn(root_id, [r['id'] for r in self.lib.request('state')['roots']])
        self.assertEqual(self.lib.request('browse', {'root_id': root_id})['files'][0]['relative'], 'one.png')
        thumb, mime = self.lib.source_asset(root_id, 'one.png')
        self.assertEqual(mime, 'image/webp'); self.assertGreater(len(thumb), 8)
        self.assertEqual(self.lib.request('list')['total'], 0)
        self.assertEqual(self.import_files(relative='sub', files=['sub/two.png'])['imported'], 1)
        self.assertEqual(self.import_files(whole_folder=True)['imported'], 1)
        self.assertEqual(self.import_files(whole_folder=True, recursive=True)['duplicates'], 2)
        items = self.lib.request('list')['items']; self.assertEqual(len(items), 2)
        for name, raw in self.tree.paths.items():
            id_ = hashlib.sha256(raw).hexdigest(); self.assertEqual(self.lib.asset(id_, 'image'), (raw,'image/png'))
            self.assertEqual(self.lib.request('metadata', {'id': id_})['metadata']['prompt'], 'original metadata')
        self.lib.close(); self.assertTrue(self.tree.closed)
    def test_stale_grants_traversal_uris_and_outside_selection_blocked_before_read(self):
        old = self.lib.request('pick_folder')['picker']['root_id']
        for s in ['../one.png', 'content://hidden/p.png', '/one.png']:
            with self.subTest(s=s), self.assertRaises(ValueError): self.lib.source_asset(old, s)
        with self.assertRaises(ValueError): self.import_files(relative='sub', files=['one.png'])
        self.tree.token = 'android_tree_10123456789abcdf'
        with self.assertRaises(ValueError): self.lib.source_asset(old, 'one.png')
        with self.assertRaises(ValueError): self.lib.request('browse', {'root_id': old})
        with self.assertRaises(ValueError): self.lib.request('import', {'root_id':old, 'files':['one.png']})
        self.assertFalse(self.tree.opens)
    def test_browser_stream_exact_length_originals_dedup_and_invalidate_pending_deletion(self):
        red, blue = self.tree.paths.values()
        # An HTTP body must finish at Content-Length; probing for another byte
        # could block a persistent connection, or consume the next request.
        body = io.BytesIO(red+b'NEXT'); self.assertEqual(self.lib.import_stream('갤러리.png', body, len(red))['imported'], 1)
        self.assertEqual(body.read(), b'NEXT'); token = self.lib.request('delete_prepare', {'all':True})['token']
        self.assertEqual(self.lib.import_stream('두 번째.png', io.BytesIO(blue), len(blue))['imported'], 1)
        with self.assertRaises(ValueError): self.lib.request('delete', {'token':token})
        self.lib.request('pick_folder')
        self.assertEqual(self.import_files(whole_folder=True, recursive=True)['duplicates'], 2)
        self.assertEqual(self.lib.asset(hashlib.sha256(red).hexdigest(), 'image'), (red,'image/png'))
        self.assertEqual(self.lib.request('list')['total'], 2)
    def test_invalid_uploads_leave_no_photos_or_temporary_files(self):
        red = self.tree.paths['one.png']
        for name, raw, size in [('a.png', red[:-4], len(red)), ('x.png', b'notaimage', 9), ('a.png', red, -1),
                                ('a.png', red, True), ('a.png', red, mod.MAX_FILE_BYTES + 1), ('x.exe', red, len(red)),
                                ('sub/x.png', red, len(red)), ('../x.png', red, len(red)), ('x\n.png', red, len(red))]:
            with self.subTest(name=name, size=size), self.assertRaises(ValueError):
                self.lib.import_stream(name, io.BytesIO(raw), size)
        self.assertEqual(self.lib.request('list')['total'], 0)
        self.assertEqual(list(self.lib.temporary.iterdir()), [])
    def test_stream_excludes_competing_imports_deletes_and_picker_until_finished(self):
        red = self.tree.paths['one.png']; started, proceed = threading.Event(), threading.Event(); errors=[]
        class Slow(io.BytesIO):
            def read(self, n):
                started.set()
                if not proceed.wait(4): raise AssertionError('no test release')
                return super().read(n)
        def run():
            try: self.lib.import_stream('red.png',Slow(red),len(red))
            except Exception as exc: errors.append(exc)
        upload=threading.Thread(target=run); upload.start()
        try:
            self.assertTrue(started.wait(2))
            for action, args in [('import',{'root_id':'output','whole_folder':True}),('pick_folder',{}),('delete_prepare',{'all':True})]:
                with self.subTest(action=action), self.assertRaises(ValueError): self.lib.request(action,args)
            with self.assertRaises(ValueError): self.lib.import_stream('again.png',io.BytesIO(red),len(red))
        finally:
            proceed.set(); upload.join(5)
        self.assertFalse(upload.is_alive()); self.assertFalse(errors); self.assertEqual(self.lib.request('list')['total'], 1)
    def test_listing_reports_stat_denied_and_unsupported_type_without_bogus_total(self):
        for name in ['a.png','hidden.png','old.HEIC','note.txt']: (self.dir/name).touch()
        real = mod.os.scandir
        class DenyStat:
            name='hidden.png'
            def is_symlink(self): return False
            def is_dir(self, **unused): return False
            def is_file(self, **unused): return True
            def stat(self, **unused): raise PermissionError()
        @contextmanager
        def scan(fd):
            with real(fd) as iterator: yield (DenyStat() if e.name=='hidden.png' else e for e in iterator)
        with patch.object(mod.os,'scandir',side_effect=scan):
            listed=self.lib.request('browse', {'root_id':'output'})
        self.assertEqual([x['name'] for x in listed['files']], ['a.png']); self.assertEqual(listed['total'],1)
        self.assertIn('읽지 못', listed['warning']); self.assertIn('표시하지', listed['warning'])
        with patch.object(self.lib, '_open_directory', side_effect=PermissionError('secret pathname')), self.assertRaisesRegex(ValueError,'권한'):
            self.lib.request('browse', {'root_id':'output'})
        with patch.object(self.lib,'_roots',return_value={'shared':('공유',self.dir)}):
            (self.dir/'hidden.png').unlink(); (self.dir/'a.png').unlink()
            self.assertIn('Android',self.lib.request('browse',{'root_id':'shared'})['warning'])
    def test_unavailable_picker_does_not_break_existing_library(self):
        def failed(_state): raise RuntimeError('Android API is unavailable')
        separate=mod.HistoryLibrary(SimpleNamespace(_output_root=lambda:self.dir), self.dir.parent/'missing', tree_factory=failed)
        self.addCleanup(separate.close)
        self.assertEqual(separate.request('pick_folder')['picker']['state'],'unavailable')
        self.assertEqual(separate.request('browse', {'root_id':'output'})['total'],0)

if __name__ == '__main__':
    unittest.main()
