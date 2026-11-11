import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {test} from 'node:test';

const source = fs.readFileSync(new URL('../naia_history_library.js', import.meta.url), 'utf8');
const pure = {module: {exports: {}}};
vm.runInNewContext(source, pure);
const {jobSummary, metadataText, selectGalleryItem, createTaskQueue, isHistoryOpener} = pure.module.exports;
const flush = async () => { for (let i = 0; i < 3; i++) await new Promise(resolve => setImmediate(resolve)); };

// A focused DOM double exercises real user flows without a phone, native picker,
// network connection, credentials or existing generated history.
function fixture(options = {}) {
  const observers = [], calls = [], assets = [], sourceAssets = [], posts = [], urls = new Set(), revoked = [], timers = new Map();
  let document, timerId = 0, urlId = 0;
  class Element {
    constructor(tag) {
      this.nodeType = 1; this.tagName = tag.toUpperCase(); this.children = []; this.parentElement = null;
      this.attributes = {}; this.events = new Map(); this._text = ''; this.className = ''; this.disabled = false;
      this.hidden = false; this.checked = false; this.open = false; this.onclick = null; this.style = {};
      this.classList = {
        contains: name => this.className.split(' ').includes(name),
        add: name => { if (!this.classList.contains(name)) this.className = (this.className + ' ' + name).trim(); },
        remove: name => { this.className = this.className.split(' ').filter(value => value !== name).join(' '); },
        toggle: (name, enabled) => { const on = enabled == null ? !this.classList.contains(name) : enabled; on ? this.classList.add(name) : this.classList.remove(name); return on; },
      };
    }
    get isConnected() { for (let node = this; node; node = node.parentElement) if (node === document) return true; return false; }
    get childElementCount() { return this.children.length; }
    get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
    set textContent(value) { this._text = String(value ?? ''); for (const child of this.children) child.parentElement = null; this.children = []; }
    append(...values) { for (const value of values) { value.remove(); value.parentElement = this; this.children.push(value); } }
    replaceChildren(...values) { this.textContent = ''; this.append(...values); }
    remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(child => child !== this); this.parentElement = null; }
    setAttribute(key, value) { this.attributes[key] = String(value); if (key === 'class') this.className = String(value); if (key === 'open') this.open = true; }
    getAttribute(key) { return key in this.attributes ? this.attributes[key] : null; }
    hasAttribute(key) { return key in this.attributes; }
    removeAttribute(key) { delete this.attributes[key]; }
    addEventListener(type, action, capture) { if (!this.events.has(type)) this.events.set(type, []); this.events.get(type).push({action, capture: capture === true}); }
    removeEventListener(type, action) { this.events.set(type, (this.events.get(type) || []).filter(value => value.action !== action)); }
    matches(selector) {
      return selector.split(',').some(part => {
        part = part.trim(); const tag = part.match(/^[a-z]+/i); if (tag && this.tagName !== tag[0].toUpperCase()) return false;
        for (const match of part.matchAll(/\.([\w-]+)/g)) if (!this.classList.contains(match[1])) return false;
        for (const match of part.matchAll(/\[([^\]*=]+)(\*=|=)?(?:"([^"]*)")?\]/g)) {
          const value = this.getAttribute(match[1]); if (value == null) return false;
          if (match[2] === '=' && value !== match[3]) return false;
          if (match[2] === '*=' && !value.includes(match[3])) return false;
        }
        return true;
      });
    }
    querySelectorAll(selector) { const result = []; for (const child of this.children) { if (child.matches(selector)) result.push(child); result.push(...child.querySelectorAll(selector)); } return result; }
    closest(selector) { for (let value = this; value && value !== document; value = value.parentElement) if (value.matches(selector)) return value; return null; }
    focus() { document.activeElement = this; }
    showModal() { this.open = true; this.focus(); }
    close() { this.open = false; }
    dispatch(type) {
      const event = {target: this, stopped: false, preventDefault() {}, stopPropagation() { this.stopped = true; }, stopImmediatePropagation() { this.stopped = true; }};
      for (const entry of document.events.get(type) || []) if (entry.capture) entry.action(event);
      if (!event.stopped) for (const entry of this.events.get(type) || []) entry.action(event);
      return event;
    }
    click() { if (this.disabled) return; if (this.type === 'checkbox') { this.checked = !this.checked; this.dispatch('change'); } else this.dispatch('click'); }
  }
  document = new Element('document'); document.nodeType = 9;
  document.body = new Element('body'); document.head = new Element('head'); document.append(document.head, document.body);
  document.createElement = tag => new Element(tag); document.activeElement = document.body;
  const original = new Element('button'); original.className = 'viewer-folder-btn'; original.textContent = 'Open Folder'; original.setAttribute('onclick', 'openResultFolder()');
  const artist = new Element('button'); artist.className = 'viewer-folder-btn'; artist.textContent = 'Artist folder'; artist.setAttribute('onclick', 'openThumbnailFolder()'); document.body.append(original, artist);
  const items = options.items || [{id: 'a', name: 'first.png', width: 1024, height: 1024}, {id: 'b', name: 'second.png', width: 832, height: 1216}, {id: 'c', name: 'third.png', width: 512, height: 512}];
  let currentJob = null, deleteToken = 0;
  const preparedDeletes = new Map();
  const roots = [{id: 'output', label: '앱 출력 폴더', available: true}, {id: 'shared', label: '내장 저장공간', available: false}];
  async function request(action, data) {
    calls.push({action, data});
    if (options.request) { const result = await options.request(action, data); if (result !== undefined) return result; }
    if (action === 'state') return {roots, total: items.length, job: currentJob};
    if (action === 'list') return {items, page: 1, pages: 1, total: items.length};
    if (action === 'metadata') return {metadata: {prompt: '<script>alert(1)</script>', parameters: {steps: 28}}};
    if (action === 'browse') return {root_id: data.root_id, relative: data.relative, parent: data.relative ? '' : null, dirs: [{name: 'nested', relative: 'nested'}], files: [{name: 'one.png', relative: data.relative ? `${data.relative}/one.png` : 'one.png', size: 2048}, {name: 'two.png', relative: data.relative ? `${data.relative}/two.png` : 'two.png', size: 4096}], page: 1, pages: 1, total: 2};
    if (action === 'import') { currentJob = {id: 'job-1', state: 'running', processed: 0, imported: 0, duplicates: 0, failed: 0, total: data.files?.length ?? null}; return {job: currentJob}; }
    if (action === 'job') return {job: currentJob};
    if (action === 'cancel') { currentJob = {...currentJob, state: 'cancelled'}; return {job: currentJob}; }
    if (action === 'delete_prepare') {
      const ids = items.filter(item => data.all === true || data.ids.includes(item.id)).map(item => item.id);
      const token = 'delete-' + ++deleteToken; preparedDeletes.set(token, ids); return {token, count: ids.length, expires_in: 120};
    }
    if (action === 'delete') {
      const ids = preparedDeletes.get(data.token); assert.ok(ids); preparedDeletes.delete(data.token);
      for (let i = items.length - 1; i >= 0; i--) if (ids.includes(items[i].id)) items.splice(i, 1);
      return {deleted: ids.length, failed: 0, total: ids.length, remaining: items.length};
    }
    throw new Error('Unexpected action: ' + action);
  }
  const window = {
    AbortController,
    document, URL: {createObjectURL() { const value = 'blob:fixture-' + ++urlId; urls.add(value); return value; }, revokeObjectURL(value) { urls.delete(value); revoked.push(value); }},
    setTimeout(fn) { const id = ++timerId; timers.set(id, fn); return id; }, clearTimeout(id) { timers.delete(id); },
    MutationObserver: class { constructor(callback) { this.callback = callback; observers.push(this); } observe() {} disconnect() { this.closed = true; } },
    async fetch(url, init) { posts.push({url, init}); if (options.fetch) return options.fetch(url, init); return {ok: true, async json() { return {ok: true, history_id: 'inserted'}; }}; },
  };
  vm.runInNewContext(source, {window, module: {exports: {}}, console});
  const controller = window.__naiaInstallHistoryLibrary({request,
    async asset(id, kind) { assets.push({id, kind}); return options.asset ? options.asset(id, kind) : new Blob(['image'], {type: 'image/png'}); },
    async sourceAsset(root, relative, kind) { sourceAssets.push({root, relative, kind}); return options.sourceAsset ? options.sourceAsset(root, relative, kind) : new Blob(['source-image'], {type: 'image/webp'}); },
  });
  const buttons = () => document.querySelectorAll('button');
  const byText = text => { const target = buttons().find(value => value.textContent === text); assert.ok(target, `Missing button ${text}`); return target; };
  const checks = () => document.querySelectorAll('input').filter(value => value.type === 'checkbox');
  const byAria = label => { const target = document.querySelectorAll('[aria-label]').find(value => value.getAttribute('aria-label') === label); assert.ok(target, `Missing aria ${label}`); return target; };
  async function tick() { const first = timers.entries().next().value; if (first) { timers.delete(first[0]); await first[1](); await flush(); } }
  return {controller, document, window, original, artist, calls, assets, sourceAssets, posts, urls, revoked, timers, observers, byText, byAria, checks, tick, items, Element};
}

test('job summary distinguishes imported, duplicate, failed and cancelled work', () => {
  const summary = jobSummary({state: 'completed', processed: 8, total: 8, imported: 3, duplicates: 2, failed: 3, skipped: 1});
  assert.match(summary, /추가 3/); assert.match(summary, /중복 2/); assert.match(summary, /실패 3/); assert.match(summary, /건너뜀 1/);
  assert.match(jobSummary({state: 'cancelled', processed: 1, imported: 1}), /취소됨/);
  assert.ok(!jobSummary({state: 'running', total: null}).includes(' / 0'));
});

test('gallery selection accepts many photos and metadata stays text', () => {
  const selection = new Map(); assert.ok(selectGalleryItem(selection, {id: 'one'}, true)); assert.ok(selectGalleryItem(selection, {id: 'two'}, true));
  assert.equal(selectGalleryItem(selection, {id: 'three'}, true), true); assert.equal(selection.size, 3);
  selectGalleryItem(selection, {id: 'one'}, false); assert.equal(selection.size, 2);
  assert.match(metadataText({prompt: '<script>bad()</script>'}), /<script>bad/);
  assert.match(metadataText({prompt: 'x'.repeat(500)}, 20), /일부만/);
  const formatted = metadataText({prompt: 'portrait', negative: 'blur', width: 832, height: 1216, parameters: {seed: 0, steps: 28, sampler: 'k_euler', cfg_scale: 5, model: 'nai'}, info: {duplicate: 'DO_NOT_DUMP'}, extracted: {raw: 'DO_NOT_DUMP'}});
  assert.match(formatted, /프롬프트\nportrait/); assert.match(formatted, /네거티브 프롬프트\nblur/); assert.match(formatted, /시드: 0/); assert.match(formatted, /크기: 832 × 1216/); assert.ok(!formatted.includes('DO_NOT_DUMP'));
  assert.equal(isHistoryOpener({getAttribute: () => 'openThumbnailFolder()'}), false);
  assert.equal(isHistoryOpener({getAttribute: () => 'openResultFolder()'}), true);
});

test('work queue caps concurrent requests and clears pending work after view change', async () => {
  const queue = createTaskQueue(9); let active = 0, max = 0; const finish = [], started = [];
  const pending = [0, 1, 2, 3].map(index => queue.run(async () => { started.push(index); active++; max = Math.max(max, active); await new Promise(resolve => finish.push(resolve)); active--; }));
  await flush(); assert.equal(max, 2); assert.deepEqual(started, [0, 1]); queue.clear(); finish.splice(0).forEach(resolve => resolve()); await Promise.all(pending);
  assert.deepEqual(started, [0, 1]);
  await assert.rejects(queue.run(() => { throw new Error('failed'); }), /failed/); assert.equal(await queue.run(() => 42), 42); queue.close();
});

test('install replaces only history buttons, delegates clicks and restores on cleanup', async () => {
  const f = fixture(); assert.equal(f.original.textContent, '사진·폴더'); assert.equal(f.original.getAttribute('onclick'), null);
  assert.equal(f.artist.textContent, 'Artist folder'); assert.equal(f.artist.getAttribute('onclick'), 'openThumbnailFolder()');
  f.original.click(); await flush(); assert.equal(f.calls[0].action, 'state'); assert.ok(f.byText('사진 불러오기'));
  const next = new f.Element('button'); next.setAttribute('onclick', 'openResultFolder()'); next.textContent = 'folder'; f.document.body.append(next);
  f.observers[0].callback([{addedNodes: [next]}]); assert.equal(next.textContent, '사진·폴더');
  f.controller.destroy(); assert.equal(f.original.textContent, 'Open Folder'); assert.equal(f.original.getAttribute('onclick'), 'openResultFolder()'); assert.equal(f.urls.size, 0);
});

test('internal browser selects single or multiple files without a native file input', async () => {
  const f = fixture(); await f.controller.open(); f.byText('사진 불러오기').click(); f.byText('앱 출력 폴더열기').click(); await flush();
  assert.equal(f.document.querySelectorAll('input').some(value => value.type === 'file'), false);
  f.byAria('one.png 불러오기 선택').click(); assert.ok(!f.byText('선택한 사진 불러오기 (1)').disabled);
  f.byAria('two.png 불러오기 선택').click(); f.byText('선택한 사진 불러오기 (2)').click(); await flush();
  const submitted = f.calls.find(value => value.action === 'import'); assert.equal(submitted.data.root_id, 'output'); assert.deepEqual(Array.from(submitted.data.files), ['one.png', 'two.png']);
  assert.ok(f.byText('불러오기 취소')); assert.equal(f.timers.size, 1); f.controller.destroy();
});

test('whole-folder import defaults nonrecursive and explicitly accepts subfolders', async () => {
  for (const recursive of [false, true]) {
    const f = fixture(); await f.controller.open(); f.byText('사진 불러오기').click(); f.byText('앱 출력 폴더열기').click(); await flush();
    const subfolders = f.checks().find(value => value.parentElement.textContent === '하위 폴더 포함'); assert.equal(subfolders.checked, false);
    if (recursive) subfolders.click(); f.byText('이 폴더 전체').click(); await flush();
    const payload = f.calls.find(value => value.action === 'import').data; assert.equal(payload.whole_folder, true); assert.equal(payload.recursive, recursive); assert.equal(payload.files, undefined); f.controller.destroy();
  }
});

test('inaccessible roots are disabled and browse failures leave a working back button', async () => {
  const f = fixture({request: async action => action === 'browse' ? {ok: false, error: {code: 'permission_denied', message: '이 폴더에 접근할 수 없습니다.'}} : undefined});
  await f.controller.open(); f.byText('사진 불러오기').click(); assert.equal(f.byText('내장 저장공간현재 앱에서 접근할 수 없음').disabled, true);
  f.byText('앱 출력 폴더열기').click(); await flush(); assert.match(f.document.body.textContent, /이 폴더에 접근할 수 없습니다/);
  f.byText('폴더 목록으로').click(); assert.ok(f.byText('앱 출력 폴더열기')); f.controller.destroy();
});

test('closing stops polling without cancelling import and reopening resumes progress', async () => {
  const f = fixture(); await f.controller.open(); f.byText('사진 불러오기').click(); f.byText('앱 출력 폴더열기').click(); await flush(); f.byText('이 폴더 전체').click(); await flush();
  assert.equal(f.timers.size, 1); await f.tick(); assert.equal(f.calls.filter(value => value.action === 'job').length, 1);
  f.controller.close(); assert.equal(f.timers.size, 0); assert.equal(f.calls.filter(value => value.action === 'cancel').length, 0);
  await f.controller.open(); assert.equal(f.timers.size, 1); assert.match(f.document.body.textContent, /불러오는 중/);
  f.byText('불러오기 취소').click(); await flush(); assert.equal(f.calls.filter(value => value.action === 'cancel').length, 1); assert.match(f.document.body.textContent, /취소됨/); f.controller.destroy();
});

test('completion reports partial failures and refreshes the persistent gallery', async () => {
  const f = fixture({request: async action => action === 'job' ? {job: {id: 'job-1', state: 'completed', total: 5, processed: 5, imported: 2, duplicates: 1, failed: 2}} : undefined});
  await f.controller.open(); f.byText('사진 불러오기').click(); f.byText('앱 출력 폴더열기').click(); await flush(); f.byText('이 폴더 전체').click(); await flush(); await f.tick();
  assert.match(f.document.body.textContent, /추가 2 · 중복 1 · 실패 2/); assert.equal(f.timers.size, 0); assert.ok(f.calls.filter(value => value.action === 'list').length >= 3); f.controller.destroy();
});

test('two-photo comparison uses previews and never inserts into generated history', async () => {
  const f = fixture(); await f.controller.open(); await flush();
  f.byAria('first.png 선택').click(); f.byAria('second.png 선택').click(); f.byAria('third.png 선택').click(); assert.equal(f.byAria('third.png 선택').checked, true);
  assert.equal(f.byText('선택한 두 장 비교').disabled, true); f.byAria('third.png 선택').click();
  f.byText('선택한 두 장 비교').click(); await flush(); assert.equal(f.assets.filter(value => value.kind === 'preview').length, 2); assert.equal(f.assets.filter(value => value.kind === 'image').length, 0); assert.equal(f.posts.length, 0);
  assert.ok(f.document.querySelectorAll('dialog')[0].classList.contains('is-compare')); assert.equal(f.document.querySelectorAll('script').length, 0); assert.match(f.document.querySelectorAll('pre')[0].textContent, /<script>alert/); f.controller.destroy();
});

test('single detail posts the original exactly once after explicit history action', async () => {
  const f = fixture(); await f.controller.open(); f.byAria('first.png 크게 보기').click(); await flush(); assert.equal(f.posts.length, 0);
  const insert = f.byText('히스토리에 추가'); insert.click(); insert.click(); await flush();
  assert.equal(f.posts.length, 1); assert.equal(f.posts[0].init.method, 'POST'); assert.equal(f.posts[0].url, '/api/image/insert-history?label=first.png');
  assert.equal(f.assets.filter(value => value.kind === 'image').length, 1); assert.ok(f.byText('히스토리에 추가됨').disabled); f.controller.destroy();
});

test('uncertain POST outcome is not automatically retried', async () => {
  const f = fixture({fetch: async () => { throw new Error('connection lost'); }}); await f.controller.open(); f.byAria('first.png 크게 보기').click(); await flush(); f.byText('히스토리에 추가').click(); await flush();
  assert.equal(f.posts.length, 1); assert.ok(f.byText('히스토리에 추가').disabled); assert.match(f.document.body.textContent, /기존 히스토리를 확인/); f.controller.destroy();
});

test('history upload times out once and leaves an uncertain outcome visible', async () => {
  const f = fixture({fetch: async (_url, init) => new Promise((_resolve, reject) => { init.signal.addEventListener('abort', () => reject(new Error('aborted'))); })});
  await f.controller.open(); f.byAria('first.png 크게 보기').click(); await flush(); f.byText('히스토리에 추가').click(); await flush();
  assert.equal(f.posts.length, 1); assert.equal(f.timers.size, 1); await f.tick();
  assert.equal(f.posts.length, 1); assert.equal(f.timers.size, 0); assert.ok(f.byText('히스토리에 추가').disabled); assert.match(f.document.body.textContent, /기존 히스토리를 확인/); f.controller.destroy();
});

test('folder actions stay visible and become available when a background import finishes', async () => {
  const f = fixture({request: async action => action === 'job' ? {job: {id: 'job-1', state: 'completed', processed: 1, imported: 1}} : undefined});
  await f.controller.open(); f.byText('사진 불러오기').click(); f.byText('앱 출력 폴더열기').click(); await flush(); f.byText('이 폴더 전체').click(); await flush();
  f.byText('사진 불러오기').click(); f.byText('앱 출력 폴더열기').click(); await flush();
  assert.ok(f.byText('이 폴더 전체').disabled); assert.ok(f.byText('이 폴더 전체').parentElement.classList.contains('naia-hl-sticky-actions')); await f.tick();
  assert.equal(f.byText('이 폴더 전체').disabled, false); f.controller.destroy();
});

test('closing a view prevents late asset creation and releases existing object URLs', async () => {
  const finish = []; const f = fixture({asset: () => new Promise(resolve => finish.push(resolve))});
  await f.controller.open(); await flush(); assert.equal(finish.length, 2); f.controller.close(); finish.splice(0).forEach(resolve => resolve(new Blob(['image']))); await flush();
  assert.equal(f.urls.size, 0); assert.equal(f.revoked.length, 0); f.controller.destroy();
  const live = fixture(); await live.controller.open(); await flush(); assert.ok(live.urls.size > 0); live.controller.close(); assert.equal(live.urls.size, 0); assert.ok(live.revoked.length > 0); live.controller.destroy();
});

test('gallery page selection allows deleting more than two and clear resets actions', async () => {
  const f = fixture(); await f.controller.open(); f.byText('이 페이지 모두 선택').click();
  assert.ok(!f.byText('선택 삭제 (3)').disabled); assert.ok(f.byText('선택한 두 장 비교').disabled);
  assert.equal(f.checks().filter(item => item.checked).length, 3);
  f.byText('선택 해제').click(); await flush(); assert.ok(f.byText('선택 삭제 (0)').disabled);
  assert.equal(f.checks().filter(item => item.checked).length, 0); f.controller.destroy();
});

test('selected deletion requires confirmation, cancellation writes nothing, and originals are explicitly retained', async () => {
  const f = fixture(); await f.controller.open(); f.byAria('first.png 선택').click(); f.byAria('third.png 선택').click();
  f.byText('선택 삭제 (2)').click(); await flush();
  assert.deepEqual(Array.from(f.calls.find(item => item.action === 'delete_prepare').data.ids), ['a', 'c']);
  assert.match(f.document.body.textContent, /선택한 사진 2장/); assert.match(f.document.body.textContent, /보관함 복사본만 삭제되며 원본 파일은 그대로 남습니다/);
  assert.equal(f.calls.filter(item => item.action === 'delete').length, 0); f.byText('취소').click(); await flush();
  assert.equal(f.items.length, 3); assert.equal(f.calls.filter(item => item.action === 'delete').length, 0);
  f.byText('선택 삭제 (2)').click(); await flush(); const confirm = f.byText('2장 삭제'); confirm.click(); confirm.click(); await flush();
  assert.equal(f.calls.filter(item => item.action === 'delete').length, 1); assert.deepEqual(f.items.map(item => item.id), ['b']);
  assert.match(f.document.body.textContent, /2장 삭제/); assert.ok(f.byText('전체 삭제 (1)')); f.controller.destroy();
});

test('whole-library deletion confirms exact current count and single detail has its own delete action', async () => {
  const f = fixture(); await f.controller.open(); f.byText('전체 삭제 (3)').click(); await flush();
  assert.equal(f.calls.find(item => item.action === 'delete_prepare').data.all, true);
  assert.match(f.document.body.textContent, /보관함 전체 3장/); f.byText('3장 삭제').click(); await flush();
  assert.equal(f.items.length, 0); assert.ok(f.byText('전체 삭제 (0)').disabled); assert.match(f.document.body.textContent, /아직 보관함에 사진이 없습니다/); f.controller.destroy();
  const detail = fixture(); await detail.controller.open(); detail.byAria('second.png 크게 보기').click(); await flush();
  detail.byText('이 사진 삭제').click(); await flush();
  assert.deepEqual(Array.from(detail.calls.find(item => item.action === 'delete_prepare').data.ids), ['b']);
  detail.byText('1장 삭제').click(); await flush(); assert.deepEqual(detail.items.map(item => item.id), ['a', 'c']); detail.controller.destroy();
});

test('an uncertain delete response cannot be replayed and users can inspect the list', async () => {
  const f = fixture({request: async action => { if (action === 'delete') throw new Error('connection lost'); }});
  await f.controller.open(); f.byText('전체 삭제 (3)').click(); await flush(); f.byText('3장 삭제').click(); await flush();
  assert.equal(f.calls.filter(item => item.action === 'delete').length, 1);
  assert.ok(f.byText('목록에서 삭제 결과 확인').disabled); assert.match(f.document.body.textContent, /다시 삭제하기 전에 보관함 목록을 확인/);
  f.byText('보관함 확인').click(); await flush(); assert.equal(f.calls.filter(item => item.action === 'delete').length, 1); f.controller.destroy();
});

test('delete confirmation expiration prevents a late destructive action', async () => {
  const f = fixture(); await f.controller.open(); f.byText('전체 삭제 (3)').click(); await flush();
  assert.equal(f.timers.size, 1); await f.tick(); const confirm = f.byText('3장 삭제'); assert.ok(confirm.disabled); confirm.click();
  assert.equal(f.calls.filter(item => item.action === 'delete').length, 0); assert.match(f.document.body.textContent, /삭제 확인 시간이 지났습니다/); f.controller.destroy();
});

test('partial deletion reports retained failures and deferred private cleanup without losing counts', async () => {
  const f = fixture({request: async action => action === 'delete' ? {deleted: 2, failed: 1, total: 3, remaining: 1, cleanup_pending: 1, warning: '저장공간 정리를 다음 실행 때 이어갑니다.'} : undefined});
  await f.controller.open(); f.byText('전체 삭제 (3)').click(); await flush(); f.byText('3장 삭제').click(); await flush();
  assert.match(f.document.body.textContent, /2장 삭제 · 1장 삭제 실패/); assert.match(f.document.body.textContent, /저장공간 정리/);
  assert.equal(f.calls.filter(item => item.action === 'delete').length, 1); f.controller.destroy();
});

test('deletion is unavailable during import and becomes available after cancellation', async () => {
  const f = fixture(); await f.controller.open(); f.byText('사진 불러오기').click(); f.byText('앱 출력 폴더열기').click(); await flush();
  f.byText('이 폴더 전체').click(); await flush(); assert.ok(f.byText('전체 삭제 (3)').disabled);
  f.byText('전체 삭제 (3)').click(); assert.equal(f.calls.filter(item => item.action === 'delete_prepare').length, 0);
  f.byText('불러오기 취소').click(); await flush(); assert.equal(f.byText('전체 삭제 (3)').disabled, false); f.controller.destroy();
});

test('source thumbnails and large preview appear before any photo is imported', async () => {
  const f = fixture(); await f.controller.open(); f.byText('사진 불러오기').click(); f.byText('앱 출력 폴더열기').click(); await flush();
  assert.deepEqual(f.sourceAssets.map(item => [item.root, item.relative, item.kind]), [['output', 'one.png', 'thumb'], ['output', 'two.png', 'thumb']]);
  assert.equal(f.document.querySelectorAll('img').length, 2); assert.ok(f.document.querySelectorAll('img').every(item => item.src.startsWith('blob:')));
  f.byAria('one.png 미리보기').click(); await flush();
  assert.equal(f.sourceAssets.at(-1).kind, 'preview'); assert.equal(f.sourceAssets.at(-1).relative, 'one.png');
  assert.equal(f.calls.filter(item => item.action === 'import').length, 0);
  f.byAria('one.png 불러오기 선택').click(); f.byText('사진 선택으로').click(); await flush();
  assert.ok(f.byAria('one.png 불러오기 선택').checked); f.byAria('two.png 불러오기 선택').click();
  f.byText('선택한 사진 불러오기 (2)').click(); await flush();
  assert.deepEqual(Array.from(f.calls.find(item => item.action === 'import').data.files), ['one.png', 'two.png']); f.controller.destroy();
});

test('source selections survive page changes and submit exact relative paths', async () => {
  const f = fixture({request: async (action, data) => action === 'browse' ? {relative: 'nested', parent: '', dirs: [], files: [{name: `page${data.page}.png`, relative: `nested/page${data.page}.png`, size: 40}], page: data.page, pages: 2, total: 2} : undefined});
  await f.controller.open(); f.byText('사진 불러오기').click(); f.byText('앱 출력 폴더열기').click(); await flush();
  f.byAria('page1.png 불러오기 선택').click(); f.byText('다음').click(); await flush();
  assert.ok(f.byText('선택한 사진 불러오기 (1)')); f.byAria('page2.png 불러오기 선택').click(); f.byText('이전').click(); await flush();
  assert.ok(f.byAria('page1.png 불러오기 선택').checked); f.byText('선택한 사진 불러오기 (2)').click(); await flush();
  const data = f.calls.find(item => item.action === 'import').data; assert.equal(data.relative, 'nested'); assert.deepEqual(Array.from(data.files), ['nested/page1.png', 'nested/page2.png']); f.controller.destroy();
});

test('source thumbnail failures remain visible and late source responses cannot leak object URLs', async () => {
  const bad = fixture({sourceAsset: async () => { throw new Error('읽기 권한 없음'); }});
  await bad.controller.open(); bad.byText('사진 불러오기').click(); bad.byText('앱 출력 폴더열기').click(); await flush();
  assert.match(bad.document.body.textContent, /읽기 권한 없음/); assert.equal(bad.calls.filter(item => item.action === 'import').length, 0); bad.controller.destroy();
  const complete = []; const late = fixture({sourceAsset: () => new Promise(resolve => complete.push(resolve))});
  await late.controller.open(); late.byText('사진 불러오기').click(); late.byText('앱 출력 폴더열기').click(); await flush();
  assert.equal(complete.length, 2); late.controller.close(); complete.forEach(resolve => resolve(new Blob(['photo']))); await flush();
  assert.equal(late.urls.size, 0); late.controller.destroy();
});

test('closing and reopening during deletion never resends it and refreshes the resulting gallery', async () => {
  let complete;
  const f = fixture({request: async action => action === 'delete' ? new Promise(resolve => { complete = resolve; }) : undefined});
  await f.controller.open(); f.byText('전체 삭제 (3)').click(); await flush(); f.byText('3장 삭제').click(); await flush();
  assert.equal(typeof complete, 'function'); f.controller.close(); await f.controller.open();
  assert.ok(f.byText('전체 삭제 (3)').disabled); f.items.splice(0); complete({deleted: 3, failed: 0, total: 3, remaining: 0}); await flush();
  assert.equal(f.calls.filter(item => item.action === 'delete').length, 1); assert.ok(f.byText('전체 삭제 (0)').disabled); assert.match(f.document.body.textContent, /3장 삭제/); f.controller.destroy();
});


const treeRoot = {id: 'android_tree_0023422300234223', label: '고른 폴더', available: true};
const treeState = (state, selected = false, detail = '') => ({picker:{state, selected, detail, root_id: selected ? treeRoot.id : ''}, roots:selected ? [treeRoot] : [], job:null});

test('only an explicit folder launch can open the system picker; refresh polls state and opens the granted root once', async () => {
  let started = false;
  const f = fixture({request: async action => {
    if (action === 'pick_folder') { started = true; return treeState('picking'); }
    if (action === 'state') return started ? treeState('selected', true) : treeState('ready');
  }});
  await f.controller.open(); f.byText('사진 불러오기').click();
  assert.ok(f.byText('안드로이드 폴더 선택')); assert.equal(f.calls.some(x => x.action === 'pick_folder'), false);
  f.byText('안드로이드 폴더 선택').click(); await flush(); assert.equal(f.timers.size, 1);
  await f.tick(); assert.equal(f.timers.size, 0);
  assert.equal(f.calls.filter(x => x.action === 'pick_folder').length, 1);
  assert.deepEqual(f.calls.filter(x => x.action === 'browse').map(x => x.data.root_id), [treeRoot.id]);
  assert.ok(f.byAria('one.png 미리보기')); assert.equal(f.calls.some(x => x.action === 'import'), false);
  f.byAria('one.png 불러오기 선택').click(); f.byText('선택한 사진 불러오기 (1)').click(); await flush();
  assert.equal(f.calls.find(x => x.action === 'import').data.root_id, treeRoot.id); f.controller.destroy();
});

test('cancelling a replacement picker keeps the prior grant and does not report it as the new selection', async () => {
  const f = fixture({request: async action => action === 'pick_folder' ? treeState('cancelled', true) : action === 'state' ? treeState('selected', true) : undefined});
  await f.controller.open(); f.byText('사진 불러오기').click(); f.byText('안드로이드 폴더 선택').click(); await flush();
  assert.match(f.document.body.textContent, /폴더 선택을 취소/); assert.equal(f.calls.some(x => x.action === 'browse'), false); assert.equal(f.timers.size, 0);
  f.byText('폴더 목록').click(); f.byText('고른 폴더열기').click(); await flush(); assert.equal(f.calls.find(x => x.action === 'browse').data.root_id, treeRoot.id); f.controller.destroy();
});

test('folder launch requiring activity resume leaves a manual retry and never relaunches itself', async () => {
  let ready = false;
  const f = fixture({request: async action => action === 'pick_folder' ? treeState('waiting_activity', false, '홈으로 갔다가 NAIA로 돌아와 다시 눌러 주세요.') : action === 'state' ? treeState(ready ? 'ready' : 'waiting_activity') : undefined});
  await f.controller.open(); f.byText('사진 불러오기').click(); f.byText('안드로이드 폴더 선택').click(); await flush();
  assert.match(f.document.body.textContent, /홈으로 갔다가/); assert.equal(f.timers.size, 0); ready = true;
  f.document.dispatch('visibilitychange'); await flush(); assert.match(f.document.body.textContent, /다시 폴더 선택/);
  assert.equal(f.calls.filter(x => x.action === 'pick_folder').length, 1); assert.equal(f.timers.size, 0); f.controller.destroy();
});

test('after closing a pending picker, a late response and visibility changes cannot move the gallery', async () => {
  let finish; const f = fixture({request: async action => action === 'pick_folder' ? new Promise(resolve => { finish = resolve; }) : undefined});
  await f.controller.open(); f.byText('사진 불러오기').click(); f.byText('안드로이드 폴더 선택').click(); await flush();
  f.controller.close(); await f.controller.open(); finish(treeState('selected', true)); await flush();
  f.document.dispatch('visibilitychange'); await flush(); assert.equal(f.timers.size, 0); assert.equal(f.calls.some(x => x.action === 'browse'), false); f.controller.destroy();
});

test('device photo fallback refreshes only on return and never infers successful imports from opening the browser', async () => {
  const f = fixture({request: async action => action === 'pick_folder' ? treeState('unavailable') : action === 'open_photo_picker' ? {opened:true} : undefined});
  await f.controller.open(); f.byText('사진 불러오기').click(); f.byText('안드로이드 폴더 선택').click(); await flush();
  assert.match(f.document.body.textContent, /이 앱에서는 폴더 선택창을 열 수 없습니다/);
  f.byText('기기에서 사진 선택').click(); await flush(); assert.match(f.document.body.textContent, /브라우저에서 사진을 골라/);
  assert.equal(f.calls.filter(x => x.action === 'open_photo_picker').length, 1); assert.equal(f.calls.some(x => x.action === 'import'), false);
  const before = f.calls.filter(x => x.action === 'list').length; f.items.push({id:'via-picker',name:'picked.png'}); f.byText('보관함 새로고침').click(); await flush();
  assert.equal(f.calls.filter(x => x.action === 'list').length, before + 1); assert.ok(f.byAria('picked.png 크게 보기')); f.controller.destroy();
});

test('shared folder diagnostics are preserved and open a working recovery route without claiming an empty folder', async () => {
  const f = fixture({request: async action => action === 'state' ? {roots:[{id:'shared',label:'내장 저장공간',available:true}]} : action === 'browse' ? {relative:'Pictures/PhotosEditor',dirs:[],files:[],warning:'파일 2개의 읽기 권한을 확인하세요.'} : action === 'open_photo_picker' ? {opened:false} : undefined});
  await f.controller.open(); f.byText('사진 불러오기').click(); f.byText('내장 저장공간열기').click(); await flush();
  assert.match(f.document.body.textContent, /파일 2개의 읽기 권한/); assert.doesNotMatch(f.document.body.textContent, /불러올 사진을 찾지 못했습니다/);
  f.byText('기기에서 사진 선택').click(); await flush(); assert.match(f.document.body.textContent, /열렸는지 확인할 수 없습니다/); f.controller.destroy();
});
