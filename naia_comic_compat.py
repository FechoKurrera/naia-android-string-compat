"""Repair EXten's cached Comic Maker busy state without changing its workflows.

The host stores panel definitions, so a panel registered by a still-running
worker must be registered again after that worker has relinquished ownership.
No server requests, generation, cancellation or settings writes happen here.
"""
from __future__ import annotations

import threading
import types


_MISSING = object()


class ComicCompatibility:
    def __init__(self, ctx, server_provider=None):
        self.ctx = ctx
        self.app = ctx._app_context
        self._entries = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._closed = False
        self._last_error = ""
        self._context_class = type(ctx)
        self._previous_register = self._context_class.register_panel
        previous = self._previous_register

        def register_panel(context, *args, **kwargs):
            result = previous(context, *args, **kwargs)
            if (not self._closed
                    and getattr(context, "_app_context", None) is self.app
                    and getattr(context, "ext_id", None) == "naia_exten"):
                self._discover_handler(kwargs.get("on_action"))
            return result

        self._register_wrapper = register_panel
        self._context_class.register_panel = register_panel
        manager = getattr(self.app, "extension_manager", None)
        for record in list(getattr(manager, "records", ()) or ()):
            if getattr(record, "ext_id", None) == "naia_exten":
                self._discover_handler(getattr(record, "action_handler", None))
        self._monitor = threading.Thread(target=self._watch, daemon=True,
                                         name="naia-comic-compat")
        self._monitor.start()

    def _discover_handler(self, handler):
        extension = getattr(handler, "__self__", None)
        context = getattr(extension, "ctx", None)
        if (getattr(context, "_app_context", None) is not self.app
                or getattr(context, "ext_id", None) != "naia_exten"):
            return
        registry = getattr(getattr(extension, "features", None), "features", None)
        feature = registry.get("comic_maker") if isinstance(registry, dict) else None
        if feature is None:
            return
        with self._lock:
            if self._closed or id(feature) in self._entries:
                return
            original = getattr(feature, "_prepare", None)
            if not callable(original) or not hasattr(feature, "_run_lock"):
                self._last_error = "unsupported_comic_runtime"
                return
            old_value = vars(feature).get("_prepare", _MISSING)

            def prepare(instance, *args, **kwargs):
                source = "stored" if kwargs.get("source") == "stored" else "nai"
                worker_attr, _ = self._attributes(source)
                worker = threading.current_thread()
                owns_worker = getattr(instance, worker_attr, None) is worker
                try:
                    return original(*args, **kwargs)
                except Exception:
                    if not owns_worker:
                        raise
                    # Exception messages can contain prompt or authentication data.
                    self._report_worker_failure(instance, source)
                    return None
                finally:
                    if owns_worker and not self._closed:
                        self._finish_worker(instance, source, worker)

            wrapper = types.MethodType(prepare, feature)
            self._entries[id(feature)] = (feature, wrapper, old_value)
            feature._prepare = wrapper
        # Existing finished workers may have registered a stale busy value.
        self._reap_finished(feature)
        self._refresh(feature)

    @staticmethod
    def _attributes(source):
        return (("_stored_worker", "_stored_waiting") if source == "stored"
                else ("_nai_worker", "_planning"))

    def _finish_worker(self, feature, source, worker, *, require_finished=False):
        worker_attr, flag_attr = self._attributes(source)
        with feature._run_lock:
            if getattr(feature, worker_attr, None) is not worker:
                return False
            # The monitor may have observed the narrow Thread.start interval
            # between assigning ident and setting the started event. Recheck
            # after acquiring the workflow lock before releasing ownership.
            if require_finished and worker.is_alive():
                return False
            setattr(feature, worker_attr, None)
            setattr(feature, flag_attr, False)
            # Pending plans and active image runs still own their busy state.
        self._refresh(feature)
        return True

    def _reap_finished(self, feature):
        for source in ("nai", "stored"):
            worker_attr, _ = self._attributes(source)
            worker = getattr(feature, worker_attr, None)
            if (isinstance(worker, threading.Thread) and worker.ident is not None
                    and not worker.is_alive()):
                self._finish_worker(feature, source, worker, require_finished=True)

    def _report_worker_failure(self, feature, source):
        message = "만화 준비 중 내부 오류가 발생했습니다. 작업 상태를 해제했습니다."
        try:
            feature._append_log(message, source=source)
            logs = feature._stored_logs if source == "stored" else feature._planning_logs
            feature._store_recent_logs(logs, source=source)
            feature._toast(message, "error")
        except Exception:
            self._last_error = "comic_worker_error"

    def _refresh(self, feature):
        if self._closed:
            return
        try:
            feature._refresh_panel()
        except Exception:
            self._last_error = "comic_panel_refresh_failed"

    def _watch(self):
        # Includes workers already running when this patch was installed. Their
        # original target cannot be replaced; wait for actual exit, then refresh.
        while not self._stop.wait(0.25):
            with self._lock:
                features = [entry[0] for entry in self._entries.values()]
            for feature in features:
                if self._closed:
                    return
                self._reap_finished(feature)

    def status(self):
        with self._lock:
            return {"installed": not self._closed,
                    "patched_features": len(self._entries),
                    "last_error": self._last_error}

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            if self._context_class.register_panel is self._register_wrapper:
                self._context_class.register_panel = self._previous_register
            for feature, wrapper, old_value in self._entries.values():
                if vars(feature).get("_prepare") is wrapper:
                    if old_value is _MISSING:
                        del feature._prepare
                    else:
                        feature._prepare = old_value
            self._entries.clear()
        if self._monitor is not threading.current_thread():
            self._monitor.join(timeout=1)


def install(ctx, server_provider=None):
    return ComicCompatibility(ctx, server_provider=server_provider)
