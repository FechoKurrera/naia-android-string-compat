/* NAIA screen bridge. Full Web UI control through observed, one-shot targets.
 * Screen/control consent gates access; credentials stay redacted in observations.
 * This is not a DOM-selector, script-evaluation, or Android system UI API.
 */
(function (global) {
  'use strict';
  const MAX_CONTROLS = 160;
  const MAX_SNAPSHOTS = 4, SNAPSHOT_TTL_MS = 120000;
  const SECRET_FIELDS = 'input[type="password"],[autocomplete="current-password"],' +
    '[autocomplete="new-password"],[data-naia-private]';
  const SECRET_NAME = /(?:api[_-]?key|token|password|passwd|secret|credential|bearer)/i;
  const INTERACTIVE = 'button,input,textarea,select,a,summary,label,[role="button"],[role="tab"],' +
    '[role="checkbox"],[role="radio"],[role="switch"],[role="menuitem"],[role="textbox"],' +
    '[onclick],[tabindex],[contenteditable],.tab-btn,.right-tab-btn';
  const norm = (v, max) => String(v == null ? '' : v).replace(/\s+/g, ' ').trim().slice(0, max);
  function normalizeHandler(value) { return String(value || '').replace(/\s+/g, '').replace(/;$/, ''); }
  function error(code) { const e = new Error(code); e.code = code; return e; }
  function attr(el, key) { return el.getAttribute(key) || ''; }
  function editable(el) { return el.isContentEditable === true || (el.hasAttribute('contenteditable') && attr(el, 'contenteditable') !== 'false'); }
  function sensitive(el) {
    if (!el || el.nodeType !== 1) return false;
    if (el.closest(SECRET_FIELDS)) return true;
    // Redact credential values, not whole Settings/account screens.
    return (['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName) || editable(el) || attr(el, 'role') === 'textbox') &&
      SECRET_NAME.test([el.id, attr(el, 'name'), attr(el, 'autocomplete')].join(' '));
  }
  function privateValues(doc) {
    return Array.from(doc.querySelectorAll('input,textarea,select,[contenteditable],[data-naia-private]'))
      .filter(sensitive).map(el => String(el.value || el.textContent || '')).filter(value => value.length >= 4);
  }
  function redact(text, values) {
    let output = String(text == null ? '' : text);
    for (const value of values) output = output.split(value).join('[redacted]');
    return output;
  }
  function policy(el) {
    if (el.tagName === 'SELECT') return {actions: ['select'], edit: true};
    if (el.tagName === 'TEXTAREA' || editable(el)) return {actions: ['fill'], edit: true};
    if (el.tagName === 'INPUT') {
      if (el.type === 'hidden') return {actions: [], reason: 'hidden_control'};
      if (['checkbox', 'radio', 'button', 'submit', 'reset', 'image', 'file', 'color'].includes(el.type)) return {actions: ['click'], edit: false};
      return {actions: ['fill'], edit: true};
    }
    if (el.matches(INTERACTIVE) || typeof el.onclick === 'function' || global.getComputedStyle(el).cursor === 'pointer') return {actions: ['click'], edit: false};
    return {actions: [], reason: 'unsupported_control'};
  }
  function optionAllowed(_el, option) {
    return !option.disabled && !(option.parentElement && option.parentElement.disabled);
  }
  // Upstream customSelects hides the native SELECT and inserts this exact
  // sibling button. Expose native selection against that visible face.
  function sourceElement(el) {
    if (!el.matches('.custom-select-button') || !el.parentElement.matches('.custom-select')) return el;
    const select = el.parentElement.previousElementSibling;
    return select && select.tagName === 'SELECT' && select.classList.contains('native-select-hidden') ? select : el;
  }
  function visible(el) {
    if (!el || !el.isConnected) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width < 1 || rect.height < 1 || rect.bottom <= 0 || rect.right <= 0 || rect.top >= global.innerHeight || rect.left >= global.innerWidth) return false;
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      const style = global.getComputedStyle(node);
      if (style.display === 'none' || (node === el && style.visibility === 'hidden') || Number(style.opacity) === 0) return false;
      // Fixed/absolute children can extend outside an overflow ancestor's
      // rectangle. Geometry-only clipping hid visible drawer tabs. Keep them
      // in the observation and let elementFromPoint decide whether actionable.
    }
    return true;
  }
  function bounds(el) {
    const r = el.getBoundingClientRect();
    return {x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height)};
  }
  function hittable(el) {
    const rect = el.getBoundingClientRect();
    const left = Math.max(0, rect.left), right = Math.min(global.innerWidth, rect.right);
    const top = Math.max(0, rect.top), bottom = Math.min(global.innerHeight, rect.bottom);
    if (right <= left || bottom <= top) return false;
    const points = [[0.5, 0.5], [0.15, 0.15], [0.85, 0.15], [0.15, 0.85], [0.85, 0.85]];
    return points.some(([x, y]) => {
      const hit = global.document.elementFromPoint(left + (right - left) * x, top + (bottom - top) * y);
      return hit === el || (hit && el.contains(hit));
    });
  }
  function label(el) {
    if (sensitive(el)) return norm(attr(el, 'aria-label') || attr(el, 'placeholder') || el.id || 'Private field', 300);
    const explicit = attr(el, 'aria-label') || attr(el, 'title');
    if (explicit) return norm(explicit, 300);
    if (el.labels && el.labels.length) return norm(el.labels[0].textContent, 300);
    return norm(el.textContent || attr(el, 'placeholder') || el.id || el.tagName.toLowerCase(), 300);
  }
  function role(el) {
    return norm(attr(el, 'role') || ({BUTTON: 'button', TEXTAREA: 'textbox', INPUT: el.type === 'checkbox' ? 'checkbox' : el.type === 'radio' ? 'radio' : 'textbox', SELECT: 'combobox', A: 'link'}[el.tagName]) || 'button', 40);
  }
  function disabled(el) { return !!el.disabled || !!el.closest('[inert]') || attr(el, 'aria-disabled') === 'true'; }
  function signature(el) {
    return JSON.stringify([el.id, el.tagName, attr(el, 'onclick'), el.value, el.checked, disabled(el), label(el), bounds(el),
      attr(el, 'data-studio-frame'), attr(el, 'data-studio-frame-id'), attr(el, 'data-studio-action'),
      attr(el, 'data-artist'), editable(el) ? el.textContent : null, el.scrollTop, el.scrollLeft]);
  }
  function scrollable(el) {
    if (!visible(el)) return false;
    const style = global.getComputedStyle(el);
    return /(auto|scroll)/.test(style.overflow + style.overflowX + style.overflowY) &&
      (el.scrollHeight > el.clientHeight + 4 || el.scrollWidth > el.clientWidth + 4);
  }
  function validateValue(el, value) {
    if (typeof value !== 'string' || value.length > 16000 || value.includes('\u0000')) throw error('invalid_value');
    if (el.maxLength >= 0 && value.length > el.maxLength) throw error('invalid_value');
    if (el.type === 'number' || el.type === 'range') {
      const n = Number(value);
      if (!value.trim() || !Number.isFinite(n) || (el.min !== '' && n < Number(el.min)) || (el.max !== '' && n > Number(el.max))) throw error('invalid_value');
    }
    return value;
  }
  function setValue(el, value) {
    const proto = el.tagName === 'TEXTAREA' ? global.HTMLTextAreaElement.prototype : el.tagName === 'SELECT' ? global.HTMLSelectElement.prototype : global.HTMLInputElement.prototype;
    if (editable(el)) el.textContent = value;
    else Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value);
    el.dispatchEvent(new global.Event('input', {bubbles: true}));
    // A studio input handler may re-render, replacing the original element. Do
    // not dispatch a second event through a detached element.
    if (el.isConnected) el.dispatchEvent(new global.Event('change', {bubbles: true}));
  }
  async function capture(doc) {
    if (typeof global.html2canvas !== 'function') throw error('capture_unavailable');
    let canvas, timer, timedOut = false;
    try {
      const scale = Math.min(1, 1600 / Math.max(global.innerWidth, global.innerHeight));
      const rendering = Promise.resolve().then(() => global.html2canvas(doc.body, {
        width: global.innerWidth, height: global.innerHeight, x: global.scrollX, y: global.scrollY,
        windowWidth: global.innerWidth, windowHeight: global.innerHeight, scale,
        logging: false, useCORS: false, allowTaint: false, backgroundColor: '#15171d',
        imageTimeout: 3000,
        ignoreElements: el => el.tagName === 'IFRAME' || el.tagName === 'SCRIPT',
        onclone: clone => {
          const values = privateValues(clone);
          for (const node of clone.querySelectorAll('iframe,script')) node.remove();
          for (const node of clone.querySelectorAll('input,textarea,select,[contenteditable],[data-naia-private]')) {
            if (sensitive(node)) { node.value = ''; node.textContent = ''; node.removeAttribute?.('value'); }
          }
          const walker = clone.createTreeWalker(clone.body, 4);
          while (walker.nextNode()) walker.currentNode.textContent = redact(walker.currentNode.textContent, values);
        },
      }));
      rendering.then(lateCanvas => { if (timedOut && lateCanvas) lateCanvas.width = lateCanvas.height = 0; }, () => {});
      canvas = await Promise.race([rendering, new Promise((_, reject) => {
        timer = global.setTimeout(() => { timedOut = true; reject(error('capture_timeout')); }, 8000);
      })]);
      for (let attempt = 0; attempt < 5; attempt++) {
        const data = canvas.toDataURL('image/jpeg', attempt ? 0.5 : 0.72).split(',')[1];
        if (data && data.length <= 600000) return {mimeType: 'image/jpeg', data, width: canvas.width, height: canvas.height};
        const reduced = doc.createElement('canvas');
        reduced.width = Math.max(1, Math.floor(canvas.width * 0.72));
        reduced.height = Math.max(1, Math.floor(canvas.height * 0.72));
        reduced.getContext('2d').drawImage(canvas, 0, 0, reduced.width, reduced.height);
        canvas.width = canvas.height = 0;
        canvas = reduced;
      }
      throw error('capture_too_large');
    } finally { if (timer !== undefined) global.clearTimeout?.(timer); if (canvas) canvas.width = canvas.height = 0; }
  }
  function install({frame, panelOrigin}) {
    const doc = global.document;
    if (!frame || frame.tagName !== 'IFRAME' || typeof panelOrigin !== 'string' || new URL(panelOrigin).origin !== panelOrigin) throw error('invalid_bridge');
    if (global.__naiaScreenAgentDispose) global.__naiaScreenAgentDispose();
    const snapshots = new Map();
    const now = () => global.performance?.now ? global.performance.now() : Date.now();
    const pruneSnapshots = () => {
      const time = now();
      for (const [id, saved] of snapshots) if (time - saved.created >= SNAPSHOT_TTL_MS) snapshots.delete(id);
      while (snapshots.size > MAX_SNAPSHOTS) snapshots.delete(snapshots.keys().next().value);
    };
    let inputEpoch = 0, disposed = false, pending = Promise.resolve();
    // customSelects refreshes identical attributes/labels every 750ms. Ignore
    // no-op mutations, otherwise no snapshot would survive a round trip. A
    // replaced target is independently rejected by its identity/isConnected.
    const relevant = record => {
      const host = record.target.nodeType === 1 ? record.target : record.target.parentElement;
      // These audited read-only status counters do not change action semantics.
      if (host && host.closest('#__gpuprobe,#poolLoadToast')) return false;
      if (record.type === 'childList' && [...record.addedNodes, ...record.removedNodes].every(node => node.nodeType === 1 && node.classList.contains('html2canvas-container'))) return false;
      if (record.type === 'attributes') return record.oldValue !== record.target.getAttribute(record.attributeName);
      if (record.type === 'characterData') return record.oldValue !== record.target.data;
      const describe = nodes => Array.from(nodes, node => node.outerHTML || node.textContent || '').join('');
      return describe(record.addedNodes) !== describe(record.removedNodes);
    };
    function observeChanges(records) {
      pruneSnapshots();
      for (const record of records) {
        if (!relevant(record)) continue;
        const host = record.target.nodeType === 1 ? record.target : record.target.parentElement;
        for (const saved of snapshots.values()) for (const entry of saved.entries.values()) {
          // Background counters, thumbnail loads and unrelated panels must not
          // expire a stable Quick/menu button. Changes to that exact control,
          // its contents, or ancestor attributes still invalidate that target.
          if (host && [entry.el, entry.source].some(node =>
              node === host || node.contains(host) ||
              (record.type === 'attributes' && host.contains(node)))) entry.dirty = true;
        }
      }
    }
    const observer = new global.MutationObserver(observeChanges);
    observer.observe(doc.body, {subtree: true, childList: true, characterData: true, characterDataOldValue: true, attributes: true, attributeOldValue: true});
    const flush = () => observeChanges(observer.takeRecords());
    const changed = event => {
      if (!event.isTrusted) return;
      inputEpoch++;
      pruneSnapshots();
      for (const saved of snapshots.values()) saved.userChanged = true;
    };
    // A browser-generated scroll/resize is also isTrusted (scroll anchoring,
    // WebView viewport updates). It is not proof of a new user action. Keep
    // the token and recheck exact target identity, bounds, value and overlays
    // when executing. Real input blocks edits and page-wide scrolling, while
    // an unchanged, audited navigation target can still be used.
    for (const name of ['pointerdown', 'keydown']) doc.addEventListener(name, changed, true);
    const layoutChanged = () => {};
    doc.addEventListener('scroll', layoutChanged, true);
    global.addEventListener('resize', layoutChanged);
    function snapshot(permissions) {
      flush();
      const entries = new Map(), controls = [], secrets = privateValues(doc);
      let remaining = 30000;
      const add = (el, scrolling = false) => {
        if (controls.length >= MAX_CONTROLS || !visible(el)) return;
        const source = scrolling ? el : sourceElement(el);
        const p = scrolling ? {actions: ['scroll'], edit: false} : policy(source);
        const covered = !hittable(el);
        const can = !disabled(source) && !covered && permissions.control === true;
        const target = 't' + (controls.length + 1);
        const item = {target, role: scrolling ? 'scrollarea' : role(source), label: redact(label(source), secrets), enabled: !disabled(source), actions: can ? p.actions : [], bounds: bounds(el)};
        if (p.reason || !can) item.blocked_reason = p.reason || (disabled(source) ? 'disabled' : covered ? 'covered_control' : 'control_permission_required');
        if (!scrolling && (['INPUT', 'TEXTAREA', 'SELECT'].includes(source.tagName) || editable(source))) {
          item.value = String(sensitive(source) ? '[redacted]' : source.type === 'checkbox' || source.type === 'radio' ? !!source.checked : editable(source) ? source.textContent : source.value || '').slice(0, Math.min(4000, remaining));
          item.value = redact(item.value, secrets);
          remaining -= item.value.length;
          if (source.tagName === 'SELECT' && !sensitive(source)) item.options = Array.from(source.options).filter(o => optionAllowed(source, o)).slice(0, 160).map(o => ({value: String(o.value).slice(0, 4000), label: redact(norm(o.label, 300), secrets)}));
        }
        controls.push(item); entries.set(target, {el, source, signature: signature(el), sourceSignature: signature(source), scrolling});
      };
      // Navigation and fields first, so a large thumbnail grid cannot starve
      // toolbar and editor controls from the bounded snapshot.
      const elements = Array.from(new Set([
        ...doc.querySelectorAll(INTERACTIVE),
        ...Array.from(doc.querySelectorAll('div,span,h1,h2,h3,li,td')).filter(el =>
          typeof el.onclick === 'function' || (global.getComputedStyle(el).cursor === 'pointer' && !el.parentElement?.closest(INTERACTIVE))),
      ])).filter(visible);
      const priority = el => el.matches('[role="tab"],[data-tab],[data-right-tab],.tab-btn,.right-tab-btn') || el.closest('header,nav,[role="tablist"],[role="toolbar"]') ? 0 :
        el.matches('input,textarea,select,[contenteditable]') ? 1 : el.closest('#artistThumbGrid,#characterViewerGrid,#characterViewerList') ? 3 : 2;
      elements.sort((a, b) => priority(a) - priority(b));
      for (const el of elements.slice(0, 140)) add(el);
      for (const el of doc.querySelectorAll('main,aside,section,div,textarea')) {
        if (controls.length >= MAX_CONTROLS) break;
        if (scrollable(el)) add(el, true);
      }
      let text = '';
      const walker = doc.createTreeWalker(doc.body, 4);
      while (walker.nextNode() && text.length < 12000) {
        const node = walker.currentNode, el = node.parentElement;
        if (!el || el.closest('script,style,iframe,textarea,select') || sensitive(el) || !visible(el)) continue;
        const part = redact(norm(node.textContent, 500), secrets);
        if (part) text += (text ? '\n' : '') + part;
      }
      const random = global.crypto.randomUUID ? global.crypto.randomUUID() : Array.from(global.crypto.getRandomValues(new Uint32Array(4)), n => n.toString(36)).join('_');
      const result = {agent_version: '0.2.9', snapshot_id: 'screen_' + random, title: 'NAIA', viewport: {width: global.innerWidth, height: global.innerHeight, scroll_x: global.scrollX, scroll_y: global.scrollY}, controls, text: text.slice(0, 12000), blocked: false};
      // Option dictionaries can be unusually large. Truncate options before
      // dropping controls, preserving all actionable target mappings.
      while (new TextEncoder().encode(JSON.stringify(result)).length > 260000) {
        const largest = controls.filter(c => c.options && c.options.length).sort((a, b) => b.options.length - a.options.length)[0];
        if (!largest) throw error('screen_too_large');
        largest.options.splice(Math.max(0, Math.floor(largest.options.length / 2)));
      }
      snapshots.set(result.snapshot_id, {created: now(), entries, userChanged: false});
      pruneSnapshots();
      return result;
    }
    async function observeScreen(permissions, withImage) {
      let image, captureError;
      if (withImage) {
        // html2canvas renders a detached clone. Live counters and image loads
        // can keep mutating forever and must not reject that completed frame.
        const before = [inputEpoch, global.innerWidth, global.innerHeight, global.scrollX, global.scrollY, global.location?.href];
        try {
          image = await capture(doc);
          const after = [inputEpoch, global.innerWidth, global.innerHeight, global.scrollX, global.scrollY, global.location?.href];
          if (JSON.stringify(before) !== JSON.stringify(after)) { image = null; captureError = 'screen_changed_during_capture'; }
        } catch (e) { captureError = typeof e.code === 'string' && /^[a-z0-9_]{1,100}$/.test(e.code) ? e.code : 'capture_failed'; }
      }
      const result = snapshot(permissions);
      if (image) result.image = image;
      if (captureError) result.capture_error = captureError;
      return result;
    }
    async function run(command) {
      const permissions = command.permissions || {}, args = command.arguments || {};
      if (permissions.screen !== true) throw error('screen_permission_required');
      if (command.name === 'naia_get_screen') {
        if (args.capture !== undefined && typeof args.capture !== 'boolean') throw error('invalid_arguments');
        return observeScreen(permissions, args.capture !== false);
      }
      if (command.name !== 'naia_ui_action') throw error('unknown_command');
      if (permissions.control !== true) throw error('control_permission_required');
      flush();
      const saved = typeof args.snapshot_id === 'string' ? snapshots.get(args.snapshot_id) : null;
      // Reads may overlap, but every attempted action consumes every old token.
      // This prevents another saved view from replaying the same action.
      snapshots.clear();
      if (!saved) throw error('stale_snapshot');
      const entry = saved.entries.get(args.target), action = args.action;
      if (!['click', 'fill', 'select', 'scroll'].includes(action)) throw error('invalid_action');
      if (action === 'scroll' && args.target === undefined) {
        if (saved.userChanged) throw error('stale_user_input');
        if (!['up', 'down', 'left', 'right'].includes(args.direction)) throw error('invalid_direction');
        const amount = args.amount === undefined ? 500 : args.amount;
        if (!Number.isInteger(amount) || amount < 1 || amount > 2000) throw error('invalid_amount');
        global.scrollBy({top: args.direction === 'down' ? amount : args.direction === 'up' ? -amount : 0, left: args.direction === 'right' ? amount : args.direction === 'left' ? -amount : 0, behavior: 'instant'});
      } else {
        if (!entry || entry.dirty || !visible(entry.el) || !entry.source.isConnected || signature(entry.el) !== entry.signature || signature(entry.source) !== entry.sourceSignature || (!entry.scrolling && sourceElement(entry.el) !== entry.source)) throw error('stale_target');
        if (!hittable(entry.el)) throw error('covered_control');
        const el = entry.source, p = entry.scrolling ? {actions: scrollable(el) ? ['scroll'] : [], edit: false} : policy(el);
        if (disabled(el) || !p.actions.includes(action)) throw error(p.reason || 'action_not_allowed');
        if (saved.userChanged && !(p.edit === false && (action === 'click' || (entry.scrolling && action === 'scroll')))) throw error('stale_user_input');
        if (action === 'click') {
          // Reject ancestors whose actual clickable child has another action.
          // The command clicks exactly the audited element, never coordinates.
          el.click();
        } else if (action === 'fill') setValue(el, validateValue(el, args.value));
        else if (action === 'select') {
          if (typeof args.value !== 'string' || args.value.length > 16000 || !Array.from(el.options).some(o => o.value === args.value && optionAllowed(el, o))) throw error('invalid_option');
          setValue(el, args.value);
        } else {
          if (!['up', 'down', 'left', 'right'].includes(args.direction)) throw error('invalid_direction');
          const amount = args.amount === undefined ? 500 : args.amount;
          if (!Number.isInteger(amount) || amount < 1 || amount > 2000) throw error('invalid_amount');
          el.scrollBy({top: args.direction === 'down' ? amount : args.direction === 'up' ? -amount : 0, left: args.direction === 'right' ? amount : args.direction === 'left' ? -amount : 0, behavior: 'instant'});
        }
      }
      await new Promise(resolve => global.setTimeout(resolve, 180));
      return observeScreen(permissions, true);
    }
    const receive = event => {
      if (disposed || event.source !== frame.contentWindow || event.origin !== panelOrigin) return;
      const command = event.data;
      if (command && command.type === 'naia-android-ready') {
        frame.contentWindow.postMessage({type: 'naia-ui-ready'}, panelOrigin);
        return;
      }
      if (!command || command.type !== 'naia-ui-command' || typeof command.id !== 'string' || command.id.length > 120) return;
      pending = pending.then(async () => {
        if (disposed) return;
        let result, is_error = false;
        try { result = await run(command); }
        catch (e) {
          if (command.name === 'naia_ui_action') snapshots.clear();
          result = {error: {code: typeof e.code === 'string' ? e.code : 'screen_action_failed'}}; is_error = true;
        }
        if (!disposed) frame.contentWindow.postMessage({type: 'naia-ui-result', id: command.id, result, is_error}, panelOrigin);
      });
    };
    global.addEventListener('message', receive);
    frame.contentWindow.postMessage({type: 'naia-ui-ready'}, panelOrigin);
    global.__naiaScreenAgentDispose = () => {
      disposed = true; snapshots.clear(); observer.disconnect();
      global.removeEventListener('message', receive); global.removeEventListener('resize', layoutChanged);
      for (const name of ['pointerdown', 'keydown']) doc.removeEventListener(name, changed, true);
      doc.removeEventListener('scroll', layoutChanged, true);
    };
  }
  global.__naiaInstallScreenAgent = install;
  // Pure policy exports are only available in Node tests, never over the bridge.
  if (typeof module === 'object' && module.exports) module.exports = {policy, optionAllowed, validateValue, normalizeHandler};
})(typeof window === 'object' ? window : globalThis);
