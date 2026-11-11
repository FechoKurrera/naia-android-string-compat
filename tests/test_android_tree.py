"""Exercise persisted document grants via a fake Android documents provider."""
import importlib.util
import io
from pathlib import Path
import tempfile
import threading
import unittest
import sys
from types import SimpleNamespace as NS
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("naia_tree_tests", Path(__file__).resolve().parents[1] / "naia_android_tree.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

A = "content://com.android.externalstorage.documents/tree/primary%3AAlbum"
B = "content://com.android.externalstorage.documents/tree/primary%3ASecond"


def row(ident, name, directory=False, size=10):
    return {"id": ident, "name": name, "directory": directory, "size": None if directory else size}


class Documents:
    """A new tree can contain an identically named file with different bytes."""
    def __init__(self):
        self.grants = set()
        self.opened = []
        self.browsed = []
        self.launches = 0
        self.children_map = {
            (A, "a-root"): [row("z", "Z.jpg"), row("d", "Nested", True), row("not-image", "notes.txt"), row("a", "A.PNG")],
            (A, "d"): [row("b", "B.webp")],
            (B, "b-root"): [row("alien", "A.PNG")],
        }

    def setup(self, on_result, on_state):
        self.result, self.state = on_result, on_state
        self.state("ready", "")

    def choose(self):
        self.launches += 1

    def has_grant(self, uri):
        return uri in self.grants

    def remember(self, uri):
        self.grants.add(uri)

    def release(self, uri):
        self.grants.discard(uri)

    def descriptor(self, uri):
        if uri not in (A, B):
            raise ValueError("not a tree")
        return ("a-root", "Album") if uri == A else ("b-root", "Second")

    def children(self, uri, parent):
        self.browsed.append((uri, parent))
        if uri not in self.grants:
            raise PermissionError("revoked")
        return self.children_map.get((uri, parent), ())

    def open(self, uri, ident):
        self.opened.append((uri, ident))
        if uri not in self.grants:
            raise PermissionError("revoked")
        return io.BytesIO(ident.encode())

    def close(self):
        pass


class TreeGrantTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.storage = Path(tmp.name)
        self.platform = Documents()
        self.build()

    def build(self):
        self.tree = module.AndroidTree(self.storage, platform=self.platform)
        self.addCleanup(self.tree.close)

    def choose(self, uri=A):
        self.tree.choose()
        self.platform.result(uri)
        state = self.tree.status()
        self.assertTrue(state["selected"], state)
        self.assertTrue(state["root_id"])
        return state["root_id"]

    def test_does_not_gain_access_until_user_selects_and_can_cancel(self):
        self.assertFalse(self.tree.status()["selected"])
        self.tree.choose()
        self.assertFalse(self.tree.status()["selected"])
        self.platform.result(None)
        self.assertFalse(self.tree.status()["selected"])
        self.assertFalse(self.platform.grants)
        first = self.choose()
        self.tree.choose()
        self.platform.result(None)
        self.assertEqual(self.tree.status()["root_id"], first)
        self.assertIn(A, self.platform.grants)

    def test_persisted_selection_reopens_after_relaunch(self):
        rid = self.choose()
        self.tree.close()
        self.build()
        self.assertEqual(self.tree.status()["root_id"], rid)
        self.assertEqual(self.tree.status()["label"], "Album")
        with self.tree.open_file("Nested/B.webp", root_id=rid) as stream:
            self.assertEqual(stream.read(), b"b")
        self.assertEqual(self.platform.opened, [(A, "b")])

    def test_reselection_cannot_redirect_a_stale_request_to_same_filename(self):
        first, second = self.choose(), self.choose(B)
        self.assertNotEqual(first, second)
        with self.assertRaises(ValueError):
            with self.tree.open_file("A.PNG", root_id=first):
                pass
        with self.assertRaises(ValueError):
            self.tree.browse(root_id=first)
        with self.assertRaises(ValueError):
            list(self.tree.walk("", False, root_id=first))
        self.assertFalse(self.platform.opened)
        with self.tree.open_file("A.PNG", root_id=second) as stream:
            self.assertEqual(stream.read(), b"alien")

    def test_paged_directory_listing_and_subfolder_images(self):
        rid = self.choose()
        pages = [self.tree.browse("", i, 2, root_id=rid) for i in (1, 2)]
        self.assertEqual([p["pages"] for p in pages], [2, 2])
        self.assertEqual([p["total"] for p in pages], [3, 3])
        self.assertEqual([x["relative"] for x in pages[0]["dirs"]], ["Nested"])
        self.assertEqual({x["relative"] for p in pages for x in p["files"]}, {"A.PNG", "Z.jpg"})
        inside = self.tree.browse("Nested", root_id=rid)
        self.assertEqual(inside["parent"], "")
        self.assertEqual(inside["files"][0]["relative"], "Nested/B.webp")

    def test_walk_includes_only_supported_images_and_observes_cancel(self):
        rid = self.choose()
        self.assertEqual(set(self.tree.walk("", False, root_id=rid)), {"A.PNG", "Z.jpg"})
        self.assertEqual(set(self.tree.walk("", True, root_id=rid)), {"A.PNG", "Z.jpg", "Nested/B.webp"})
        event = threading.Event()
        event.set()
        self.assertEqual(list(self.tree.walk("", True, cancel=event, root_id=rid)), [])

    def test_refuses_raw_uris_traversal_and_ambiguous_filenames(self):
        rid = self.choose()
        for bad in ("../A.PNG", "/A.PNG", "Nested//B.webp", "content://unknown/tree/stranger", "a\x00b"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                with self.tree.open_file(bad, root_id=rid):
                    pass
        self.platform.children_map[A, "a-root"].append(row("other-png", "A.PNG"))
        with self.assertRaises(ValueError):
            with self.tree.open_file("A.PNG", root_id=rid):
                pass
        self.assertFalse(self.platform.opened)

    def test_unknown_size_pipe_is_seekable_but_capped_at_limit_plus_one(self):
        rid = self.choose()
        self.platform.children_map[A, "a-root"][1:1] = [row("pipe", "Pipe.png", size=None)]

        class Pipe(io.BytesIO):
            def seekable(self):
                return False

        for content in (b"abcdef", b"x" * 25):
            pipe = Pipe(content)
            self.platform.open = lambda *args: pipe
            with patch.object(module, "_LIMIT", 6):
                if len(content) == 6:
                    with self.tree.open_file("Pipe.png", root_id=rid) as stream:
                        self.assertTrue(stream.seekable())
                        self.assertEqual(stream.read(), content)
                        stream.seek(2)
                        self.assertEqual(stream.read(), b"cdef")
                else:
                    with self.assertRaises(ValueError):
                        with self.tree.open_file("Pipe.png", root_id=rid):
                            self.fail("oversize nonseekable stream reached decoder")
            self.assertTrue(pipe.closed)

    def test_closed_instance_cannot_replace_a_newer_grant(self):
        self.choose()
        self.tree.choose()
        old_result = self.platform.result
        self.tree.close()
        self.build()
        chosen = self.choose(B)
        old_result(A)
        self.assertEqual(self.tree.status()["root_id"], chosen)
        self.tree.close()
        self.build()
        self.assertEqual(self.tree.status()["root_id"], chosen)
        self.assertEqual(self.tree.status()["label"], "Second")

    def test_listing_failure_is_not_reported_as_empty_and_revoke_is_respected(self):
        rid = self.choose()
        def broken(*_args):
            raise PermissionError("provider stopped")
        self.platform.children = broken
        with self.assertRaises((OSError, ValueError)):
            self.tree.browse(root_id=rid)
        self.platform.grants.remove(A)
        self.tree.close()
        self.build()
        self.assertFalse(self.tree.status()["selected"])
        with self.assertRaises((OSError, ValueError)):
            with self.tree.open_file("A.PNG", root_id=rid):
                pass
        self.assertFalse(self.platform.opened)


class NativeActivityBridgeTests(unittest.TestCase):
    """Mock the Java API shape, not only the Python storage-service boundary."""
    def setUp(self):
        events, callbacks = [], []
        self.events, self.callbacks = events, callbacks
        class Owner:
            def getActivityResultRegistry(self):
                return self
            def register(self, key, contract, callback):
                events.append(("register", key))
                callbacks.append(callback)
                return NS(
                    launch=lambda intent: events.append(("intent", intent.action, intent.flags)),
                    unregister=lambda: events.append(("unregister", key)),
                )
        class Intent:
            ACTION_OPEN_DOCUMENT_TREE = "android.intent.action.OPEN_DOCUMENT_TREE"
            def __init__(self, action):
                self.action, self.flags = action, 0
            def addFlags(self, value):
                self.flags |= value
        class Handler:
            def __init__(self, looper):
                pass
            def post(self, runnable):
                runnable.run()
                return True
        self.owner = Owner()
        self.lifecycle = None
        def register_lifecycle(value):
            self.lifecycle = value
            events.append(("watch", value))
        app = NS(getContentResolver=lambda: object(),
                 registerActivityLifecycleCallbacks=register_lifecycle,
                 unregisterActivityLifecycleCallbacks=lambda value: events.append(("unwatch", value)))
        classes = {
            "com.chaquo.python.Python": NS(getPlatform=lambda: NS(getApplication=lambda: app)),
            "android.provider.DocumentsContract": object,
            "android.net.Uri": object,
            "android.content.Intent": Intent,
            "android.os.Handler": Handler,
            "android.os.Looper": NS(getMainLooper=lambda: object()),
            "androidx.activity.result.ActivityResultRegistryOwner": Owner,
            "androidx.activity.result.contract.ActivityResultContracts$StartActivityForResult": type("Contract", (), {}),
            "androidx.activity.result.ActivityResultCallback": object,
            "androidx.activity.result.ActivityResult": object,
            "android.app.Application$ActivityLifecycleCallbacks": object,
            "java.lang.Runnable": object,
        }
        with patch.dict(sys.modules, {"java": NS(jclass=classes.__getitem__, dynamic_proxy=lambda iface: iface,
                                                 cast=lambda cls, value: value)}):
            self.platform = module._AndroidPlatform()
        self.state = []
        self.results = []
        self.received = threading.Event()
        def receive(value):
            self.results.append(value)
            self.received.set()
        self.platform.setup(receive, lambda state, detail: self.state.append((state, detail)))
        self.addCleanup(self.platform.close)

    def select_folder(self):
        self.lifecycle.onActivityResumed(self.owner)
        self.assertEqual(self.state[-1][0], "ready")
        self.platform.choose()
        self.assertEqual(len(self.callbacks), 1)
        return self.callbacks[-1]

    def test_only_foreground_compatible_host_can_launch_readonly_intent(self):
        self.assertEqual(self.state[-1][0], "waiting_activity")
        self.lifecycle.onActivityResumed(object())
        self.assertEqual(self.state[-1][0], "unavailable")
        self.platform.choose()
        self.assertFalse(self.callbacks)
        callback = self.select_folder()
        self.assertIn(("intent", "android.intent.action.OPEN_DOCUMENT_TREE", 1 | 64 | 128), self.events)
        self.lifecycle.onActivityPaused(self.owner)
        self.assertEqual(self.state[-1][0], "waiting_activity")
        data = NS(getFlags=lambda: 1 | 2, getData=lambda: NS(toString=lambda: A))
        callback.onActivityResult(NS(getResultCode=lambda: -1, getData=lambda: data))
        self.assertTrue(self.received.wait(2))
        self.assertEqual(self.results, [A])
        self.assertEqual(sum(e[0] == "unregister" for e in self.events), 1)

    def test_cancelled_or_missing_read_permission_never_supplies_uri(self):
        callback = self.select_folder()
        callback.onActivityResult(NS(getResultCode=lambda: 0))
        self.assertEqual(self.results, [None])
        self.platform.choose()
        no_read = NS(getFlags=lambda: 2, getData=lambda: NS(toString=lambda: A))
        self.callbacks[-1].onActivityResult(NS(getResultCode=lambda: -1, getData=lambda: no_read))
        self.assertEqual(self.state[-1][0], "error")
        self.assertEqual(self.results, [None])
        self.assertEqual(sum(e[0] == "unregister" for e in self.events), 2)

    def test_destroying_host_unregisters_outstanding_request_and_close_stops_watching(self):
        self.select_folder()
        self.lifecycle.onActivityDestroyed(self.owner)
        self.assertEqual(self.state[-1][0], "error")
        self.assertEqual(sum(e[0] == "unregister" for e in self.events), 1)
        self.platform.close()
        self.assertIn(("unwatch", self.lifecycle), self.events)
        old_count = len(self.state)
        self.lifecycle.onActivityResumed(self.owner)
        self.assertEqual(len(self.state), old_count)
        self.assertFalse(self.results)


if __name__ == "__main__":
    unittest.main()
