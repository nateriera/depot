// A small DOM stand-in exercises the shipped page and its sole POST call site without a server or live yard.
const assert = require('node:assert/strict');
const { randomUUID } = require('node:crypto');
const f = require('./floor.js');
const scenario = process.argv[2];

class Element {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.listeners = {}; this.dataset = {}; this.attrs = {}; this.value = ''; this._text = ''; }
  append(...nodes) { for (const n of nodes) { n.parent = this; n.parentNode = this; this.children.push(n); } }
  replaceChildren(...nodes) { this.children = []; this._text = ''; this.append(...nodes); }
  set textContent(v) { this._text = v; this.children = []; }
  get textContent() { return this._text + this.children.map(n => n.textContent).join(' '); }
  get childNodes() { return this.children; }
  get disabled() { return this._disabled || this.attrs['aria-disabled'] === 'true'; }
  set disabled(value) { this._disabled = value; }
  scrollIntoView() {} // geometry is verified in Chrome, not by this stand-in
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  async fire(type) { if (this.disabled && type === 'click') return; for (const fn of this.listeners[type] || []) await fn({ target: this, preventDefault() {} }); }
  focus() { document.activeElement = this; }
  querySelectorAll(selector) { return all(this, n => selector === "[data-focus-key]" ? !!n.dataset.focusKey : selector.startsWith('.') ? (n.className || '').split(' ').includes(selector.slice(1)) : n.tagName === selector.toUpperCase()); }
  querySelector(selector) { return this.querySelectorAll(selector)[0]; }
}
function all(root, pred) { return root.children.flatMap(n => [...(pred(n) ? [n] : []), ...all(n, pred)]); }
const ids = Object.fromEntries(['board','banner','panel','announce','theme','readouts','archive','intake','new-work','subhead'].map(id => [id, new Element('div')]));
const memory = {};
const store = { get: k => memory[k] || null, set: (k, v) => { memory[k] = v; } };
const writes = [];
let timer;
function card(name, track, stage) { return { name, id: name, track, stage, title: name, hands: '—', truth: 'recorded', last_event: { text: stage, ts: '2026-11-22T00:00:00Z' }, trail: [], dwell_s: 0, receipts: [] }; }
const projection = {
  now: '2026-11-22T12:00:00Z', stages: ['Queued','Briefed','Building','Gating','Review','Awaiting operator','Done'],
  tracks: [
    { id: 'active', name: 'active', state: 'active', card_count: 2, open_count: 1, done_count: 1, pipeline: [], receipts: [] },
    { id: 'shelf', name: 'shelf', state: 'tabled', card_count: 1, open_count: 1, done_count: 0, pipeline: [], receipts: [] },
    { id: 'archive', name: 'archive', state: 'ended', card_count: 2, open_count: 1, done_count: 1, pipeline: [], receipts: [] },
    { id: 'paused', name: 'paused', state: 'paused', card_count: 0, open_count: 0, done_count: 0, pipeline: [], receipts: [] }
  ],
  cards: [card('a-open','active','Queued'), card('a-done','active','Done'), card('s-open','shelf','Queued'), card('e-open','archive','Queued'), card('e-done','archive','Done')], board_receipts: []
};
const board = ids.board;
const nodes = (tag, root = board) => all(root, n => n.tagName === tag.toUpperCase());
const button = (text, root = board) => { const b = nodes('button', root).find(n => n.textContent.includes(text)); assert.ok(b, 'missing button: ' + text); return b; };
const lanes = () => all(board, n => n.className === 'lane');
const cards = () => all(board, n => (n.className || '').split(' ').includes('card'));
async function refresh() { await timer(); await new Promise(setImmediate); }

async function main() {
  if (scenario === 'prefs') {
    const p = f.donePrefs(store); assert.equal(p.hidden('a'), false); p.set('a', true);
    assert.equal(f.donePrefs(store).hidden('a'), true); assert.equal(f.donePrefs(store).hidden('b'), false);
    p.set('a', false); assert.equal(f.donePrefs(store).hidden('a'), false);
    const blocked = { get() { throw Error('blocked'); }, set() { throw Error('blocked'); } };
    const b = f.donePrefs(blocked); b.set('a', true); assert.equal(b.hidden('a'), true); assert.equal(f.donePrefs(blocked).hidden('a'), false);
    memory['depot.hideDone'] = '{bad'; assert.equal(f.donePrefs(store).hidden('a'), false);
    return;
  }
  global.document = { getElementById: id => ids[id], createElement: tag => new Element(tag), documentElement: { setAttribute() {}, removeAttribute() {} }, addEventListener() {}, activeElement: null };
  global.window = { crypto: { randomUUID }, localStorage: { getItem: store.get, setItem: store.set, removeItem: k => delete memory[k] } };
  global.setTimeout = fn => { timer = fn; return 1; }; global.clearTimeout = () => {};
  global.fetch = async (url, options) => {
    if (url === '/intents') {
      const intent = JSON.parse(options.body); writes.push(intent);
      const t = projection.tracks.find(t => t.id === intent.data.track);
      if (t) t.receipts.push({ id: intent.id, kind: intent.kind, ts: projection.now, state: 'pending' });
      return { ok: true, json: async () => ({ id: intent.id }) };
    }
    assert.equal(url, '/cards.json'); return { ok: true, json: async () => projection };
  };
  delete require.cache[require.resolve('./floor.js')]; require('./floor.js');
  await new Promise(setImmediate);
  if (scenario === 'pending-reasons') {
    for (const track of projection.tracks) track.receipts = [{ id: track.id, kind: 'track_pause', state: 'pending' }];
    await refresh();
    const acts = all(board, n => n.tagName === 'BUTTON' && /^(Pause|Resume|Table|End track)/.test(n.textContent));
    assert.ok(acts.length >= 4);
    for (const b of acts) {
      assert.equal(b.attrs['aria-disabled'], 'true');
      assert.ok(b.parent.textContent.includes('A track request is already pending'));
    }
  } else if (scenario === 'menu') {
    assert.ok(board.textContent.includes('PAUSED'));
    await button('Pause —').fire('click');
    assert.equal(writes.length, 1);
    assert.deepEqual(writes[0], f.trackIntent('track_pause', 'active', writes[0].id));
    assert.ok(board.textContent.includes('Pending · not in log'));
    const r = projection.tracks[0].receipts[0]; r.state = 'landed'; r.event = { type: 'track_paused', seq: 1 }; projection.tracks[0].state = 'paused';
    await refresh();
    assert.ok(board.textContent.includes('Landed · recorded · track_paused #1'));
    assert.ok(button('Resume — releases'));
    assert.ok(board.textContent.includes('1 open items will be parked (reason: track ended)'));
    await button('Move later').fire('click');
    assert.equal(writes.length, 1); assert.ok(memory['depot.trackorder']);
    for (const [state, text, kind] of [['paused', 'Resume — releases', 'track_resume'], ['active', 'Table —', 'track_table'], ['active', 'End track — 1', 'track_end']]) {
      projection.tracks[0].state = state; projection.tracks[0].receipts = [];
      await refresh(); await button(text).fire('click');
      assert.deepEqual(writes.at(-1), f.trackIntent(kind, 'active', writes.at(-1).id));
    }
  } else if (scenario === 'shelf') {
    assert.deepEqual(lanes().map(n => n.attrs['aria-label']), ['active', 'paused']);
    assert.ok(board.textContent.includes('Tabled: shelf — 1 cards'));
    await button('Resume — returns the lane').fire('click');
    assert.equal(writes[0].kind, 'track_untable'); assert.equal(writes[0].data.track, 'shelf');
  } else if (scenario === 'archive') {
    f.donePrefs(store).set('archive', true);
    await ids.archive.fire('click');
    assert.deepEqual(lanes().map(n => n.attrs['aria-label']), ['archive']);
    assert.deepEqual(cards().map(n => n.dataset.name || n.textContent.split(' ')[0]), ['e-open', 'e-done']);
    assert.equal(ids['new-work'].disabled, true);
    assert.equal(nodes('button').length, 1);
    for (const c of cards()) { assert.equal(c.attrs.role, undefined); await c.fire('click'); }
    assert.equal(writes.length, 0);
    await button('Reopen —').fire('click');
    assert.equal(writes[0].kind, 'track_reopen');
    await ids.archive.fire('click'); assert.equal(ids['new-work'].disabled, false);
  } else if (scenario === 'fold') {
    assert.equal(cards().length, 2);
    await button('Hide done cards').fire('click');
    assert.equal(cards().length, 1); assert.ok(button('1 done — show'));
    const station = all(board, n => (n.className || '').split(' ').includes('station') && n.textContent.startsWith('Done'))[0];
    assert.equal(station.textContent, 'Done 0');
    assert.equal(f.donePrefs(store).hidden('active'), true);
    await refresh(); assert.equal(cards().length, 1);
    await button('1 done — show').fire('click'); assert.equal(cards().length, 2);
    assert.equal(writes.length, 0);
  } else if (scenario === 'intake') {
    await ids['new-work'].fire('click');
    const fields = all(ids.intake, n => n.id && n.id.startsWith('intake-'));
    const sel = fields.find(n => n.id === 'intake-track_select');
    for (const key of ['title', 'adapter', 'body', 'expected', 'test']) {
      const n = fields.find(n => n.id === 'intake-' + key); n.value = key === 'adapter' ? 'codex' : 'filled'; await n.fire('input');
    }
    for (const id of ['shelf', 'paused', 'archive']) {
      sel.value = id; await sel.fire('change');
      assert.ok(ids.intake.textContent.includes(f.intakeConsequence(projection.tracks.find(t => t.id === id))));
      assert.equal(!!button('File as new work', ids.intake).disabled, id === 'archive');
    }
    assert.equal(button('File as new work', ids.intake).disabled, true);
    assert.equal(writes.length, 0);
  } else throw Error('unknown scenario');
}
main().then(() => console.log('ok')).catch(e => { console.error(e); process.exitCode = 1; });
