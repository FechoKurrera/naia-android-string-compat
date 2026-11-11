"""Android folder opening with read-only in-app fallbacks. No new permissions."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

DIRECTORY_MIME = "vnd.android.document/directory"


def document_uri(directory, primary_root):
    """Resolve symlinks before mapping an existing primary-storage directory."""
    try:
        path = Path(directory).resolve(strict=True)
        root = Path(primary_root).resolve(strict=True)
        if not path.is_dir() or not root.is_dir():
            return None
        relative = path.relative_to(root)
        if relative.parts[:2] in (("Android", "data"), ("Android", "obb")):
            return None
        name = "primary:" + (relative.as_posix() if relative.parts else "")
        return "content://com.android.externalstorage.documents/document/" + quote(name, safe="")
    except (OSError, ValueError, TypeError):
        return None


def open_native_folder(directory):
    """Only launch an already-readable document directory with an ACTION_VIEW handler."""
    cursor = None
    try:
        from java import jclass
        application = jclass("com.chaquo.python.Python").getPlatform().getApplication()
        primary = jclass("android.os.Environment").getExternalStorageDirectory().getCanonicalPath()
        value = document_uri(directory, str(primary))
        if value is None:
            return {"opened": False, "reason": "not_external_directory"}
        uri = jclass("android.net.Uri").parse(value)
        resolver = application.getContentResolver()
        cursor = resolver.query(uri, ["mime_type"], None, None, None)
        if cursor is None or not cursor.moveToFirst():
            return {"opened": False, "reason": "directory_unavailable"}
        index = cursor.getColumnIndex("mime_type")
        if index < 0 or str(cursor.getString(index)) != DIRECTORY_MIME:
            return {"opened": False, "reason": "not_document_directory"}
        intent_type = jclass("android.content.Intent")
        intent = intent_type(intent_type.ACTION_VIEW)
        intent.setDataAndType(uri, DIRECTORY_MIME)
        intent.addFlags(intent_type.FLAG_ACTIVITY_NEW_TASK)
        if intent.resolveActivity(application.getPackageManager()) is None:
            return {"opened": False, "reason": "no_folder_handler"}
        application.startActivity(intent)
        return {"opened": True}
    except Exception:
        return {"opened": False, "reason": "native_open_unavailable"}
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass


_NATIVE = r'''
  async function naiaAndroidOpenNativeFolder(target) {
    const open = window.naiaAndroidTools?.openFolderNative;
    if (typeof open !== 'function') return false;
    let timer;
    try {
      const result = await Promise.race([
        Promise.resolve().then(() => open(target)),
        new Promise(resolve => { timer = setTimeout(() => resolve(false), 2500); }),
      ]);
      return result === true || result?.opened === true;
    } catch (_) { return false; }
    finally { clearTimeout(timer); }
  }
'''

_SAVED = r'''
  // naia-android-folder-v1: independent, read-only disk browser.
  let naiaSavedDialog = null;
  let naiaFolderBusy = false;
  function naiaBrowseSavedImages() {
    if (naiaSavedDialog?.open) { naiaSavedDialog.focus(); return; }
    const dialog = document.createElement('dialog');
    naiaSavedDialog = dialog;
    dialog.setAttribute('aria-label', '저장된 이미지');
    dialog.style.cssText = 'box-sizing:border-box;width:min(900px,96vw);max-height:94vh;padding:18px;border:1px solid #607089;border-radius:14px;background:#131b29;color:#eef2f7;overflow:auto';
    const title = document.createElement('h2'); title.textContent = '저장된 이미지'; title.style.cssText = 'font-size:20px;margin:0 0 10px';
    const note = document.createElement('p'); note.textContent = '파일 앱에서 열 수 없어 앱 안에서 보여 줍니다. 이미지를 누르면 크게 볼 수 있습니다.'; note.style.cssText = 'font-size:13px;line-height:1.5';
    const status = document.createElement('p'); status.setAttribute('role', 'status');
    const grid = document.createElement('div'); grid.style.cssText = 'display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:10px';
    const preview = document.createElement('div'); preview.hidden = true;
    const previewImage = document.createElement('img'); previewImage.alt = '저장된 이미지 크게 보기'; previewImage.style.cssText = 'display:block;max-width:100%;max-height:70vh;object-fit:contain;margin:auto';
    const button = text => { const el = document.createElement('button'); el.type = 'button'; el.textContent = text; el.style.cssText = 'min-height:44px;padding:9px 15px;background:#26364e;color:#fff;border:1px solid #607089;border-radius:8px;cursor:pointer'; return el; };
    const back = button('목록으로'); back.onclick = () => { preview.hidden = true; previewImage.removeAttribute('src'); grid.hidden = false; grid.style.display = 'grid'; navigation.hidden = false; navigation.style.display = 'flex'; };
    preview.append(back, previewImage);
    const navigation = document.createElement('div'); navigation.style.cssText = 'display:flex;align-items:center;gap:12px;justify-content:center;margin:16px 0';
    const previous = button('이전'); const next = button('다음'); const counter = document.createElement('span'); navigation.append(previous, counter, next);
    const close = button('닫기'); close.style.float = 'right'; close.onclick = () => dialog.close();
    dialog.append(close, title, note, status, grid, preview, navigation); document.body.append(dialog);
    let page = 0, total = 0, serial = 0, request = null;
    const encodedPath = path => String(path).split('/').map(encodeURIComponent).join('/');
    async function load(wanted) {
      const current = ++serial; request?.abort(); request = new AbortController();
      page = Math.max(0, wanted); previous.disabled = next.disabled = true; status.textContent = '저장된 이미지를 불러오는 중…'; grid.replaceChildren();
      try {
        const params = new URLSearchParams({scope:'disk', page:String(page), per_page:'24'});
        const response = await fetch('/api/viewer/list?' + params, {signal:request.signal, cache:'no-store'});
        const data = await response.json();
        if (!response.ok || !Array.isArray(data.images)) throw new Error('저장된 이미지를 불러오지 못했습니다.');
        if (current !== serial || !dialog.open) return;
        total = Math.max(0, Number(data.total) || 0);
        const pages = Math.max(1, Math.ceil(total / 24));
        if (page >= pages) { await load(pages - 1); return; }
        status.textContent = total ? `저장된 이미지 ${total}장` : '현재 저장 폴더에 이미지가 없습니다.';
        counter.textContent = `${page + 1} / ${pages}`;
        for (const item of data.images.slice(0, 24)) {
          if (!item || typeof item.rel_path !== 'string' || !item.rel_path) continue;
          const cell = button(''); cell.style.padding = '4px';
          const img = document.createElement('img'); img.loading = 'lazy'; img.decoding = 'async'; img.alt = String(item.filename || item.rel_path); img.style.cssText = 'display:block;width:100%;height:120px;object-fit:contain';
          const path = encodedPath(item.rel_path); img.src = '/api/viewer/thumb/' + path + '?size=256';
          cell.setAttribute('aria-label', img.alt); cell.append(img);
          cell.onclick = () => { previewImage.src = '/api/viewer/image/' + path; preview.hidden = false; grid.hidden = true; grid.style.display = 'none'; navigation.hidden = true; navigation.style.display = 'none'; };
          grid.append(cell);
        }
        previous.disabled = page <= 0; next.disabled = page + 1 >= pages;
      } catch (error) {
        if (current !== serial || error.name === 'AbortError') return;
        status.textContent = error.message || '저장된 이미지를 불러오지 못했습니다.';
      }
    }
    previous.onclick = () => load(page - 1); next.onclick = () => load(page + 1);
    dialog.addEventListener('close', () => { ++serial; request?.abort(); previewImage.removeAttribute('src'); grid.replaceChildren(); dialog.remove(); if (naiaSavedDialog === dialog) naiaSavedDialog = null; }, {once:true});
    dialog.showModal(); void load(0);
  }

  async function openFolder() {
    if (naiaFolderBusy) return;
    naiaFolderBusy = true;
    try {
      if (await naiaAndroidOpenNativeFolder('save')) {
        showToast('파일 앱으로 폴더 열기를 요청했습니다.', 'success');
        return;
      }
      naiaBrowseSavedImages();
    } finally { naiaFolderBusy = false; }
  }
'''

_ARTIST = r'''
  // naia-android-folder-v1: keep the existing selected-mode artist grid.
  async function openThumbnailFolder() {
    if (openingFolder) return;
    openingFolder = true;
    try {
      if (await naiaAndroidOpenNativeFolder('artist')) {
        showToast?.('파일 앱으로 폴더 열기를 요청했습니다.', 'success');
      } else {
        showToast?.('파일 앱에서 열 수 없어 현재 아티스트 목록을 앱 안에서 표시합니다.', 'info');
        await loadPage(0, {anchor: 'top'});
      }
    } catch (error) {
      showToast?.(error.message || '아티스트 목록을 열지 못했습니다.', 'error');
    } finally { openingFolder = false; }
  }
'''


def _replace_function(source, start, end, replacement):
    if source.count(start) != 1 or source.count(end) != 1:
        raise RuntimeError("지원하지 않는 NAIA 폴더 열기 화면입니다.")
    left = source.index(start)
    right = source.index(end, left)
    return source[:left] + replacement + source[right:]


def patch_asset(path, text, native_url=None):
    """Patch pinned functions only; native_url is intentionally never embedded."""
    if "// naia-android-folder-v1:" in text:
        return text
    name = str(path).replace("\\", "/")
    if name.endswith("features/resultHistory.mjs"):
        return _replace_function(text, "  async function openFolder() {", "  function init() {", _NATIVE + _SAVED)
    if name.endswith("features/artistThumbTab.mjs"):
        return _replace_function(text, "  async function openThumbnailFolder() {", "  function bind() {", _NATIVE + _ARTIST)
    return text
