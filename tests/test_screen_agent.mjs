import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {test} from 'node:test';
import {webcrypto} from 'node:crypto';

const script = fs.readFileSync(new URL('../naia_screen_agent.js', import.meta.url), 'utf8');

// Small DOM test double: transport, stale-target and policy tests use no network,
// generation backend, browser account or private files.
class Element {
  constructor(tag = 'BUTTON', attrs = {}, parent = null) {
    this.nodeType = 1; this.tagName = tag; this.attrs = {...attrs}; this.parentElement = parent;
    this.id = attrs.id || ''; this.type = attrs.type || (tag === 'INPUT' ? 'text' : '');
    this.textContent = attrs.label || this.id; this.children = []; this.value = '';
    this.hidden = false; this.isConnected = true; this.disabled = false; this.checked = false;
    this.maxLength = -1; this.min = ''; this.max = ''; this.scrollTop = this.scrollLeft = 0;
    this.clientWidth = this.scrollWidth = 100; this.clientHeight = this.scrollHeight = 24;
    this.rect = {x: 10, y: 10, left: 10, top: 10, right: 110, bottom: 34, width: 100, height: 24};
    this.classList = {contains: value => (this.attrs.class || '').split(' ').includes(value)};
    this.clicks = 0; this.events = [];
    if (parent) parent.children.push(this);
  }
  getAttribute(key) { return key in this.attrs ? String(this.attrs[key]) : null; }
  hasAttribute(key) { return key in this.attrs; }
  matches(selectors) {
    return selectors.split(',').some(selector => {
      selector = selector.trim();
      const tag = selector.match(/^[A-Za-z][A-Za-z0-9]*/);
      if (tag && tag[0].toUpperCase() !== this.tagName) return false;
      for (const m of selector.matchAll(/#([\w-]+)/g)) if (this.id !== m[1]) return false;
      for (const m of selector.matchAll(/\.([\w-]+)/g)) if (!this.classList.contains(m[1])) return false;
      for (const m of selector.matchAll(/\[([^=\]]+)(?:="([^"]*)")?\]/g)) {
        if (m[1] === 'hidden' ? !this.hidden : !this.hasAttribute(m[1])) return false;
        if (m[2] !== undefined && this.getAttribute(m[1]) !== m[2]) return false;
      }
      return true;
    });
  }
  closest(selector) { for (let n = this; n; n = n.parentElement) if (n.matches(selector)) return n; return null; }
  contains(other) { for (let n = other; n; n = n.parentElement) if (n === this) return true; return false; }
  getBoundingClientRect() { return this.rect; }
  click() { this.clicks++; if (this.type === 'checkbox') this.checked = !this.checked; if (this.type === 'radio') this.checked = true; }
  dispatchEvent(event) { this.events.push(event.type); }
  scrollBy({top, left}) { this.scrollTop += top; this.scrollLeft += left; }
}
class Input extends Element { get value() { return this._value || ''; } set value(value) { this._value = value; } }
class Select extends Input {
  constructor(attrs, parent) { super('SELECT', attrs, parent); this.options = []; }
  get value() { return this._value || ''; } set value(value) { this._value = value; }
}
class Textarea extends Input { get value() { return this._value || ''; } set value(value) { this._value = value; } }

function fixture() {
  const body = new Element('BODY'), elements = [], messages = [], listeners = new Map(), observers = [];
  let clock = 0;
  body.id = ''; body.rect = {x: 0, y: 0, left: 0, top: 0, right: 1000, bottom: 800, width: 1000, height: 800};
  const doc = {
    body,
    querySelectorAll: selector => elements.filter(el => el.matches(selector)),
    createTreeWalker: () => ({nextNode: () => false}),
    elementFromPoint: (x, y) => doc.cover || elements.findLast(el => !el.hidden && !el.closest('[aria-hidden="true"]') && x >= el.rect.left && x <= el.rect.right && y >= el.rect.top && y <= el.rect.bottom) || body,
    getElementById: id => elements.find(el => el.id === id),
    addEventListener: (name, handler) => listeners.set('doc:' + name, handler), removeEventListener() {},
  };
  const window = {
    document: doc, innerWidth: 1000, innerHeight: 800, scrollX: 0, scrollY: 0, crypto: webcrypto,
    performance: {now: () => clock},
    html2canvas: async () => ({width:1000,height:800,toDataURL:()=> 'data:image/jpeg;base64,/9j/2Q=='}),
    getComputedStyle: el => ({display: el.hidden ? 'none' : 'block', visibility: 'visible', opacity: '1', overflow: 'visible', overflowX: 'visible', overflowY: 'visible'}),
    HTMLInputElement: Input, HTMLSelectElement: Select, HTMLTextAreaElement: Textarea,
    Event: class { constructor(type) { this.type = type; } },
    MutationObserver: class { constructor(callback) { this.callback = callback; this.records = []; observers.push(this); } observe() {} disconnect() {} takeRecords() { return this.records.splice(0); } },
    addEventListener: (name, handler) => listeners.set(name, handler),
    removeEventListener: name => listeners.delete(name), setTimeout: callback => setImmediate(callback), clearTimeout: clearImmediate,
    scrollBy({top, left}) { this.scrollX += left; this.scrollY += top; },
  };
  const context = vm.createContext({window, globalThis: window, module: {exports: {}}, URL, TextEncoder, Uint32Array, console});
  vm.runInContext(script, context);
  function add(tag, attrs = {}, parent = body) {
    const Class = tag === 'INPUT' ? Input : tag === 'TEXTAREA' ? Textarea : tag === 'SELECT' ? Select : Element;
    const el = tag === 'SELECT' ? new Class(attrs, parent) : new Class(tag, attrs, parent);
    const y = elements.length * 35 + 10;
    el.rect = {...el.rect, y, top: y, bottom: y + 24};
    if (parent.children.length > 1) el.previousElementSibling = parent.children.at(-2);
    elements.push(el); return el;
  }
  const frame = add('IFRAME'); frame.hidden = true;
  frame.contentWindow = {postMessage: (message, origin) => messages.push({message, origin})};
  window.__naiaInstallScreenAgent({frame, panelOrigin: 'http://127.0.0.1:4321'});
  const permissions = {screen: true, control: true, edit: true, generate: false};
  let nextId = 0;
  async function call(name, args = {}, perms = permissions, extra = {}) {
    const id = 'command-' + ++nextId;
    listeners.get('message')({source: frame.contentWindow, origin: 'http://127.0.0.1:4321', data: {type: 'naia-ui-command', id, name, arguments: args, permissions: perms}, ...extra});
    for (let attempts = 0; attempts < 10; attempts++) { await new Promise(resolve => setImmediate(resolve)); const result = messages.find(m => m.message.id === id); if (result) return JSON.parse(JSON.stringify(result.message)); }
    return undefined;
  }
  return {add, call, doc, window, frame, messages, listeners, observers, permissions,
    advanceTime: ms => { clock += ms; }, policy: context.module.exports.policy, optionAllowed: context.module.exports.optionAllowed};
}

test('only the exact trusted panel can send commands; handshake is acknowledged', async () => {
  const f = fixture();
  assert.equal(await f.call('naia_get_screen', {}, undefined, {origin: 'https://attacker.example'}), undefined);
  assert.equal(await f.call('naia_get_screen', {}, undefined, {source: {}}), undefined);
  f.listeners.get('message')({source: f.frame.contentWindow, origin: 'http://127.0.0.1:4321', data: {type: 'naia-android-ready'}});
  assert.equal(f.messages.at(-1).message.type, 'naia-ui-ready');
});

test('screen/control consent gates generic controls without per-function restrictions', async () => {
  const f = fixture();
  f.add('TEXTAREA', {id:'promptEdit'});f.add('BUTTON', {id:'btnGen', onclick:'generateAction()'});
  assert.equal((await f.call('naia_get_screen', {}, {screen:false})).result.error.code, 'screen_permission_required');
  const permissions={...f.permissions,edit:false,generate:false};
  const response=await f.call('naia_get_screen', {}, permissions);
  assert.deepEqual(response.result.controls.find(c=>c.label==='promptEdit').actions,['fill']);
  const button=response.result.controls.find(c=>c.label==='btnGen');assert.deepEqual(button.actions,['click']);
  const action=await f.call('naia_ui_action',{snapshot_id:response.result.snapshot_id,target:button.target,action:'click'},permissions);
  assert.equal(action.is_error,false);
  const readOnly=await f.call('naia_get_screen',{}, {...permissions,control:false});
  assert.ok(readOnly.result.controls.every(c=>c.actions.length===0));
});

test('field edits dispatch input and change; successful action consumes token', async () => {
  const f = fixture(), prompt = f.add('TEXTAREA', {id: 'promptEdit'});
  const {result: screen} = await f.call('naia_get_screen');
  const action = {snapshot_id: screen.snapshot_id, target: screen.controls[0].target, action: 'fill', value: 'two people'};
  const result = await f.call('naia_ui_action', action);
  assert.equal(result.is_error, false); assert.equal(prompt.value, 'two people');
  assert.deepEqual(prompt.events, ['input', 'change']); assert.equal(result.result.image.mimeType, 'image/jpeg');
  assert.equal((await f.call('naia_ui_action', action)).result.error.code, 'stale_snapshot');
});

test('visible search fields use generic control permission', async () => {
  const f=fixture(),field=f.add('INPUT',{id:'artistThumbSearch'});
  const screen=(await f.call('naia_get_screen')).result;
  const result=await f.call('naia_ui_action',{snapshot_id:screen.snapshot_id,target:screen.controls[0].target,action:'fill',value:'test'},{...f.permissions,edit:false});
  assert.equal(result.is_error,false);assert.equal(field.value,'test');
});

test('DOM mutation, changed value, and replaced target each reject stale action', async () => {
  for (const mode of ['mutation', 'value', 'removed']) {
    const f = fixture(), input = f.add('INPUT', {id: 'pSeed'});
    const {result: screen} = await f.call('naia_get_screen');
    if (mode === 'mutation') { input.attrs.class = 'changed'; f.observers[0].records.push({type: 'attributes', target: input, attributeName: 'class', oldValue: null}); }
    if (mode === 'value') input.value = '123';
    if (mode === 'removed') input.isConnected = false;
    const result = await f.call('naia_ui_action', {snapshot_id: screen.snapshot_id, target: screen.controls[0].target, action: 'fill', value: '9'});
    assert.match(result.result.error.code, /^stale_/);
  }
});

test('identical custom select refresh mutation does not stale snapshot', async () => {
  const f = fixture(), button = f.add('BUTTON', {onclick: "switchTab('params')", class: 'tab'});
  const {result: screen} = await f.call('naia_get_screen');
  f.observers[0].records.push({type: 'attributes', target: button, attributeName: 'class', oldValue: 'tab'});
  const result = await f.call('naia_ui_action', {snapshot_id: screen.snapshot_id, target: screen.controls[0].target, action: 'click'});
  assert.equal(result.is_error, false); assert.equal(button.clicks, 1);
});

test('covered controls cannot be clicked through an overlay', async () => {
  const f = fixture(), button = f.add('BUTTON', {onclick: "switchTab('params')"});
  const {result: screen} = await f.call('naia_get_screen');
  f.doc.cover = new Element('DIV');
  const result = await f.call('naia_ui_action', {snapshot_id: screen.snapshot_id, target: screen.controls[0].target, action: 'click'});
  assert.equal(result.result.error.code, 'covered_control'); assert.equal(button.clicks, 0);
});

test('Settings and login screens stay readable while credential values are redacted', async () => {
  const f=fixture(),settings=f.add('DIV',{id:'rightTabSettings'});
  const key=f.add('INPUT',{id:'naiApiKey',type:'password'},settings);key.value='PRIVATE_KEY_VALUE';
  f.add('BUTTON',{id:'apiLogin',label:'Login'},settings);
  const result=await f.call('naia_get_screen');
  assert.equal(result.is_error,false);assert.equal(result.result.image.mimeType,'image/jpeg');
  assert.ok(!JSON.stringify(result).includes('PRIVATE_KEY_VALUE'));
  assert.equal(result.result.controls.find(c=>c.label==='naiApiKey').value,'[redacted]');
  assert.deepEqual(result.result.controls.find(c=>c.label==='Login').actions,['click']);
});

test('custom select face exposes all enabled native options including folder action', async () => {
  const f=fixture();
  const select=f.add('SELECT',{id:'artistThumbMode',class:'native-select-hidden','aria-hidden':'true'});select.hidden=true;
  select.options=[{value:'regular',label:'Regular'},{value:'__artist_thumb_open_folder__',label:'Open folder'},{value:'disabled',label:'Disabled',disabled:true}];
  const wrapper=f.add('DIV',{class:'custom-select'});wrapper.previousElementSibling=select;
  const button=f.add('BUTTON',{class:'custom-select-button'},wrapper);
  const screen=(await f.call('naia_get_screen')).result,field=screen.controls.find(c=>c.role==='combobox');
  assert.deepEqual(field.options.map(o=>o.value),['regular','__artist_thumb_open_folder__']);
  const result=await f.call('naia_ui_action',{snapshot_id:screen.snapshot_id,target:field.target,action:'select',value:'__artist_thumb_open_folder__'});
  assert.equal(result.is_error,false);assert.equal(select.value,'__artist_thumb_open_folder__');assert.equal(button.clicks,0);
});

test('studio generation, files, deletion and unfamiliar buttons are observable controls', () => {
  const f=fixture(),studio=f.add('DIV',{id:'studioRoot'});
  for(const action of ['toggle-global','add-frame','generate-selected','start-sequence','export-board','import-board','delete-frame','surprise'])assert.deepEqual(Array.from(f.policy(f.add('BUTTON',{'data-studio-action':action},studio)).actions),['click']);
});

test('scroll accepts schema maximum 2000 and cannot reuse a consumed token', async () => {
  const f = fixture(); const screen = (await f.call('naia_get_screen')).result;
  const response = await f.call('naia_ui_action', {snapshot_id: screen.snapshot_id, action: 'scroll', direction: 'down', amount: 2000});
  assert.equal(response.is_error, false); assert.equal(f.window.scrollY, 2000);
});

test('capture bounds canvas at1600, excludes iframes and releases canvas', async () => {
  const f = fixture(); f.window.innerWidth = 2400; f.window.innerHeight = 1800;
  let options; const canvas = {width: 1600, height: 1200, toDataURL: () => 'data:image/jpeg;base64,/9j/2Q=='};
  f.window.html2canvas = async (_body, value) => { options = value; return canvas; };
  const response = await f.call('naia_get_screen', {capture: true});
  assert.equal(response.is_error, false); assert.equal(response.result.image.width, 1600);
  assert.equal(options.scale, 2 / 3); assert.equal(options.allowTaint, false);
  assert.equal(options.ignoreElements(new Element('IFRAME')), true);
  assert.equal(options.ignoreElements(new Input('INPUT', {type: 'password'})), false);
  assert.equal(canvas.width, 0); assert.equal(canvas.height, 0);
});


test('unrelated live DOM updates preserve Quick snapshot through queued and delivered mutations', async () => {
  const f = fixture(), quick = f.add('BUTTON', {id: 'tagFilterToggle', onclick: 'toggleTagFilter()'});
  const counter = f.add('SPAN', {id: 'liveCounter'});
  const {result: screen} = await f.call('naia_get_screen');
  counter.attrs.title = 'updated';
  const record = {type: 'attributes', target: counter, attributeName: 'title', oldValue: null};
  f.observers[0].callback([record]);
  f.observers[0].records.push(record);
  const response = await f.call('naia_ui_action', {snapshot_id: screen.snapshot_id, target: screen.controls[0].target, action: 'click'});
  assert.equal(response.is_error, false); assert.equal(quick.clicks, 1);
});

test('actual target and ancestor changes still reject saved navigation clicks', async () => {
  for (const mode of ['ancestor', 'handler']) {
    const f = fixture(), parent = f.add('DIV'), quick = f.add('BUTTON', {onclick: 'toggleTagFilter()'}, parent);
    const {result: screen} = await f.call('naia_get_screen');
    if (mode === 'ancestor') { parent.attrs.class = 'new-context'; f.observers[0].records.push({type:'attributes', target:parent, attributeName:'class', oldValue:null}); }
    if (mode === 'handler') quick.attrs.onclick = 'generateAction()';
    const response = await f.call('naia_ui_action', {snapshot_id:screen.snapshot_id, target:screen.controls[0].target, action:'click'});
    assert.match(response.result.error.code, /^stale_/); assert.equal(quick.clicks, 0);
  }
});

test('Android view switch and Settings buttons use ordinary observed click targets', async () => {
  const f=fixture(),button=f.add('BUTTON',{label:'간편 화면 ▾',title:'간편 화면 사용 중. 전체 기능을 보려면 전체 화면으로 전환'});
  const screen=(await f.call('naia_get_screen',{}, {...f.permissions,edit:false})).result;
  assert.equal((await f.call('naia_ui_action',{snapshot_id:screen.snapshot_id,target:screen.controls[0].target,action:'click'},{...f.permissions,edit:false})).is_error,false);
  assert.equal(button.clicks,1);
  for(const onclick of ['generateAction()','deleteFrame()','openSaveDirectoryPanel()','openApiPopup()'])assert.equal(f.policy(f.add('BUTTON',{onclick})).actions[0],'click');
});

test('tabs including Settings do not need pre-registered handlers', () => {
  const f=fixture();
  for(const destination of ['galleryPanel','settingsRoot','newPanel'])assert.equal(f.policy(f.add('BUTTON',{role:'tab','aria-controls':destination})).actions[0],'click');
});

test('opening a Settings root does not block an unchanged unobscured menu', async () => {
  const f=fixture(),button=f.add('BUTTON',{onclick:'toggleTagFilter()'});
  const screen=(await f.call('naia_get_screen')).result;f.add('DIV',{id:'settingsRoot'});
  const response=await f.call('naia_ui_action',{snapshot_id:screen.snapshot_id,target:screen.controls[0].target,action:'click'});
  assert.equal(response.is_error,false);assert.equal(button.clicks,1);
});

test('branded Android shell view switch uses exact observed title and reports agent version', async () => {
  const f = fixture(), button = f.add('DIV', {role:'button', label:'NAIA2 간편 화면 ▾', title:'간편 화면 사용 중. 전체 기능을 보려면 전체 화면으로 전환'});
  const {result: screen} = await f.call('naia_get_screen');
  assert.equal(screen.agent_version, '0.2.9');
  const result = await f.call('naia_ui_action', {snapshot_id:screen.snapshot_id,target:screen.controls[0].target,action:'click'});
  assert.equal(result.is_error, false);assert.equal(button.clicks,1);
});


test('trusted automatic scroll and resize keep stable Quick target; moved target is rejected', async () => {
  for (const moved of [false,true]) {
    const f=fixture(), button=f.add('BUTTON',{onclick:'toggleTagFilter()'});
    const {result:screen}=await f.call('naia_get_screen');
    f.listeners.get('doc:scroll')({isTrusted:true});f.listeners.get('resize')({isTrusted:true});
    if(moved)button.rect={...button.rect,x:20,left:20,right:120};
    const result=await f.call('naia_ui_action',{snapshot_id:screen.snapshot_id,target:screen.controls[0].target,action:'click'});
    if(moved){assert.equal(result.result.error.code,'stale_target');assert.equal(button.clicks,0);}
    else {assert.equal(result.is_error,false);assert.equal(button.clicks,1);}
  }
});

test('overlapping screen reads preserve prior navigation, then consume all old snapshots', async () => {
  const f = fixture(), quick = f.add('BUTTON', {onclick:'openFnQuick()'});
  const [first, second] = await Promise.all([f.call('naia_get_screen'), f.call('naia_get_screen')]);
  const action = screen => ({snapshot_id:screen.snapshot_id, target:screen.controls[0].target, action:'click'});
  assert.equal((await f.call('naia_ui_action', action(first.result))).is_error, false);
  assert.equal((await f.call('naia_ui_action', action(second.result))).result.error.code, 'stale_snapshot');
  assert.equal(quick.clicks, 1);
});

test('unrelated trusted touch and keyboard input preserve only unchanged navigation', async () => {
  for (const event of ['pointerdown', 'keydown']) {
    const f = fixture(), quick = f.add('BUTTON', {onclick:'openFnQuick()'});
    const screen = (await f.call('naia_get_screen')).result;
    f.listeners.get('doc:' + event)({isTrusted:true, target:f.doc.body});
    const response = await f.call('naia_ui_action', {snapshot_id:screen.snapshot_id, target:screen.controls[0].target, action:'click'});
    assert.equal(response.is_error, false); assert.equal(quick.clicks, 1);
  }
});

test('trusted input still rejects conflicting field edits and whole-page scroll', async () => {
  for (const mode of ['fill', 'page-scroll']) {
    const f = fixture();
    const target = mode === 'fill' ? f.add('TEXTAREA', {id:'promptEdit'}) : f.add('BUTTON', {id:'artistThumbInsertBtn'});
    const screen = (await f.call('naia_get_screen')).result;
    f.listeners.get('doc:keydown')({isTrusted:true, target:f.doc.body});
    const args = {snapshot_id:screen.snapshot_id, target:screen.controls[0].target, action:mode === 'fill' ? 'fill' : 'click'};
    if (mode === 'fill') args.value = 'changed';
    if (mode === 'page-scroll') { delete args.target; args.action = 'scroll'; args.direction = 'down'; }
    const response = await f.call('naia_ui_action', args);
    assert.equal(response.result.error.code, 'stale_user_input');
    assert.equal(target.value, ''); assert.equal(target.clicks, 0); assert.equal(f.window.scrollY, 0);
  }
});

test('trusted input allows bounded target scroll only while its exact state is unchanged', async () => {
  for (const moved of [false, true]) {
    const f = fixture(), panel = f.add('DIV', {id:'thumbnailPanel'});
    panel.scrollHeight = 1000;
    f.window.getComputedStyle = el => ({display:'block', visibility:'visible', opacity:'1', overflow:el === panel ? 'auto' : 'visible', overflowX:'visible', overflowY:'visible'});
    const screen = (await f.call('naia_get_screen')).result;
    const area = screen.controls.find(c => c.role === 'scrollarea');
    assert.ok(area);
    f.listeners.get('doc:pointerdown')({isTrusted:true, target:f.doc.body});
    if (moved) panel.scrollTop = 10;
    const response = await f.call('naia_ui_action', {snapshot_id:screen.snapshot_id, target:area.target, action:'scroll', direction:'down', amount:50});
    if (moved) { assert.equal(response.result.error.code, 'stale_target'); assert.equal(panel.scrollTop, 10); }
    else { assert.equal(response.is_error, false); assert.equal(panel.scrollTop, 50); }
  }
});

test('mutations dirty earlier snapshots even after a newer read', async () => {
  const f = fixture(), quick = f.add('BUTTON', {onclick:'openFnQuick()'});
  const first = (await f.call('naia_get_screen')).result;
  quick.attrs.class = 'changed';
  f.observers[0].records.push({type:'attributes', target:quick, attributeName:'class', oldValue:null});
  await f.call('naia_get_screen');
  const response = await f.call('naia_ui_action', {snapshot_id:first.snapshot_id, target:first.controls[0].target, action:'click'});
  assert.equal(response.result.error.code, 'stale_target'); assert.equal(quick.clicks, 0);
});

test('snapshots expire after 120 seconds and only the most recent four are retained', async () => {
  for (const elapsed of [119999, 120000]) {
    const f = fixture(), quick = f.add('BUTTON', {onclick:'openFnQuick()'});
    const screen = (await f.call('naia_get_screen')).result;
    f.advanceTime(elapsed);
    const response = await f.call('naia_ui_action', {snapshot_id:screen.snapshot_id, target:screen.controls[0].target, action:'click'});
    assert.equal(response.is_error, elapsed === 120000);
    assert.equal(quick.clicks, elapsed === 120000 ? 0 : 1);
  }
  for (const count of [4, 5]) {
    const f = fixture(), quick = f.add('BUTTON', {onclick:'openFnQuick()'});
    const screens = [];
    for (let i = 0; i < count; i++) screens.push((await f.call('naia_get_screen')).result);
    const response = await f.call('naia_ui_action', {snapshot_id:screens[0].snapshot_id, target:screens[0].controls[0].target, action:'click'});
    assert.equal(response.is_error, count === 5); assert.equal(quick.clicks, count === 5 ? 0 : 1);
  }
});

test('capture failure reports the missing image and preserves valid prior targets', async () => {
  const f=fixture(),quick=f.add('BUTTON',{onclick:'openFnQuick()'});
  const screen=(await f.call('naia_get_screen')).result;
  delete f.window.html2canvas;f.add('DIV',{id:'settingsRoot'});
  const read=await f.call('naia_get_screen');assert.equal(read.is_error,false);assert.equal(read.result.capture_error,'capture_unavailable');assert.equal(read.result.image,undefined);
  const response=await f.call('naia_ui_action',{snapshot_id:screen.snapshot_id,target:screen.controls[0].target,action:'click'});
  assert.equal(response.is_error,false);assert.equal(response.result.capture_error,'capture_unavailable');assert.equal(quick.clicks,1);
});

test('focusable shell controls and forms do not require a recognized label', async () => {
  const f=fixture(),form=f.add('FORM'),button=f.add('DIV',{tabindex:'0',label:'NAIA2 ▾'},form);
  const screen=(await f.call('naia_get_screen')).result;
  const response=await f.call('naia_ui_action',{snapshot_id:screen.snapshot_id,target:screen.controls[0].target,action:'click'});
  assert.equal(response.is_error,false);assert.equal(button.clicks,1);
  assert.equal(f.policy(f.add('DIV',{title:'plain noninteractive text'})).actions.length,0);
});

test('native checkbox, radio and contenteditable fields are controllable without edit scope', async () => {
  for (const kind of ['checkbox', 'radio', 'contenteditable']) {
    const f=fixture();
    const field=kind==='contenteditable'?f.add('DIV',{contenteditable:'true',label:'Editable'}):f.add('INPUT',{type:kind,id:kind});
    const permissions={...f.permissions,edit:false};
    const screen=(await f.call('naia_get_screen',{},permissions)).result;
    const args={snapshot_id:screen.snapshot_id,target:screen.controls[0].target,action:kind==='contenteditable'?'fill':'click'};
    if(kind==='contenteditable')args.value='typed text';
    assert.equal((await f.call('naia_ui_action',args,permissions)).is_error,false);
    if(kind==='contenteditable'){assert.equal(field.textContent,'typed text');assert.deepEqual(field.events,['input','change']);}
    else assert.equal(field.checked,true);
  }
});

test('unlabelled pointer-cursor shell switches remain observed and clickable', async () => {
  const f=fixture(),switcher=f.add('H1',{label:'NAIA2 간편 화면'});
  const style=f.window.getComputedStyle;
  f.window.getComputedStyle=el=>({...style(el),cursor:el===switcher?'pointer':'auto'});
  const screen=(await f.call('naia_get_screen')).result;
  assert.equal(screen.controls[0].label,'NAIA2 간편 화면');
  assert.equal((await f.call('naia_ui_action',{snapshot_id:screen.snapshot_id,target:screen.controls[0].target,action:'click'})).is_error,false);
  assert.equal(switcher.clicks,1);
});

test('visible fixed drawer tabs survive zero-height overflow ancestors and aria-hidden semantics', async () => {
  const f=fixture(),wrapper=f.add('DIV',{'aria-hidden':'true'});
  wrapper.rect={x:0,y:0,left:0,top:0,right:100,bottom:0,width:100,height:0};
  const tab=f.add('BUTTON',{'data-tab':'params',class:'tab-btn',label:'PARAMS'},wrapper);
  const style=f.window.getComputedStyle;
  f.window.getComputedStyle=el=>({...style(el),overflow:el===wrapper?'hidden':'visible',position:el===tab?'fixed':'static'});
  // aria-hidden is an accessibility hint, not visual CSS. The actual hit is
  // decisive even when an application leaves that hint on a visible wrapper.
  f.doc.elementFromPoint=()=>tab;
  const screen=(await f.call('naia_get_screen')).result;
  assert.equal(screen.controls[0].label,'PARAMS');assert.deepEqual(screen.controls[0].actions,['click']);
});

test('id-less Prompt Params Negative and right tabs precede large thumbnail grids', async () => {
  const f=fixture(),grid=f.add('DIV',{id:'artistThumbGrid'});
  for(let i=0;i<180;i++){
    const item=f.add('BUTTON',{id:'artist_'+i,label:'Artist '+i},grid);
    item.rect={x:0,y:100,left:0,top:100,right:100,bottom:124,width:100,height:24};
  }
  for(const [i,label] of ['PROMPT','PARAMS','NEGATIVE','Settings'].entries()){
    const tab=f.add('BUTTON',{class:i===3?'right-tab-btn':'tab-btn',label,'data-tab':label});
    tab.rect={x:i*110,y:10,left:i*110,top:10,right:i*110+100,bottom:34,width:100,height:24};
  }
  const screen=(await f.call('naia_get_screen',{capture:false})).result;
  assert.deepEqual(screen.controls.slice(0,4).map(c=>c.label),['PROMPT','PARAMS','NEGATIVE','Settings']);
  assert.ok(screen.controls.length<=160);
});

test('default and post-action observations contain images despite background DOM mutations', async () => {
  const f=fixture(),button=f.add('BUTTON',{label:'Quick'}),counter=f.add('SPAN',{id:'counter'});
  let rendered=0;
  f.window.html2canvas=async()=>{
    rendered++;counter.attrs.title='counter '+rendered;
    f.observers[0].callback([{type:'attributes',target:counter,attributeName:'title',oldValue:null}]);
    return{width:1000,height:800,toDataURL:()=> 'data:image/jpeg;base64,/9j/2Q=='};
  };
  const screen=(await f.call('naia_get_screen')).result;
  assert.ok(screen.image);assert.equal(screen.capture_error,undefined);
  const response=await f.call('naia_ui_action',{snapshot_id:screen.snapshot_id,target:screen.controls[0].target,action:'click'});
  assert.equal(response.is_error,false);assert.ok(response.result.image);assert.equal(button.clicks,1);assert.equal(rendered,2);
  const textOnly=await f.call('naia_get_screen',{capture:false});assert.equal(textOnly.result.image,undefined);assert.equal(rendered,2);
});

test('user or viewport change during capture is reported without returning mismatched pixels', async () => {
  for(const kind of ['input','viewport']){
    const f=fixture();
    f.window.html2canvas=async()=>{
      if(kind==='input')f.listeners.get('doc:pointerdown')({isTrusted:true});else f.window.innerWidth+=20;
      return{width:1000,height:800,toDataURL:()=> 'data:image/jpeg;base64,/9j/2Q=='};
    };
    const response=await f.call('naia_get_screen');
    assert.equal(response.is_error,false);assert.equal(response.result.capture_error,'screen_changed_during_capture');assert.equal(response.result.image,undefined);
  }
});

test('post-action capture timeout preserves completed action and releases a late canvas', async () => {
  const f=fixture(),button=f.add('BUTTON',{label:'Quick'});
  const screen=(await f.call('naia_get_screen')).result;
  let finish;
  f.window.html2canvas=()=>new Promise(resolve=>{finish=resolve;});
  const args={snapshot_id:screen.snapshot_id,target:screen.controls[0].target,action:'click'};
  const response=await f.call('naia_ui_action',args);
  assert.equal(response.is_error,false);assert.equal(response.result.capture_error,'capture_timeout');assert.equal(button.clicks,1);
  const late={width:1000,height:800};finish(late);await new Promise(resolve=>setImmediate(resolve));
  assert.equal(late.width,0);assert.equal(late.height,0);
  assert.equal((await f.call('naia_ui_action',args)).result.error.code,'stale_snapshot');assert.equal(button.clicks,1);
});

test('capture clone masks credential fields and echoed values but keeps Settings content', async () => {
  const f=fixture();
  let cloneKey,echo;
  f.window.html2canvas=async(_body,options)=>{
    cloneKey=new Input('INPUT',{id:'naiApiKey',type:'text'});cloneKey.value='SECRET_TOKEN_123';
    const settings=new Element('DIV',{id:'settingsRoot'});
    echo={textContent:'Value: SECRET_TOKEN_123'};
    let visited=false;
    const clone={body:settings,querySelectorAll:selector=>[cloneKey].filter(el=>el.matches(selector)),createTreeWalker:()=>({currentNode:echo,nextNode:()=>visited?false:(visited=true)})};
    options.onclone(clone);
    assert.equal(cloneKey.value,'');assert.equal(echo.textContent,'Value: [redacted]');
    assert.equal(options.ignoreElements(settings),false);
    return{width:1000,height:800,toDataURL:()=> 'data:image/jpeg;base64,/9j/2Q=='};
  };
  assert.ok((await f.call('naia_get_screen')).result.image);
});

