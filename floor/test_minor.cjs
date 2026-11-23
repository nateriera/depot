// Scenario checks for the shipped floor. Run separately from the turntable suite.
const assert = require('node:assert/strict');
const { randomUUID } = require('node:crypto');
const scenario = 'minor';
const check = process.argv[2];
class Element {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.listeners = {}; this.dataset = {}; this.attrs = {}; this.value = ''; this._text = ''; }
  append(...nodes) { for (const n of nodes) { n.parent = this; this.children.push(n); } }
  replaceChildren(...nodes) { this.children = []; this._text = ''; this.append(...nodes); }
  set textContent(v) { this._text = v; this.children = []; }
  get textContent() { return this._text + this.children.map(n => n.textContent).join(' '); }
  get childNodes() { return this.children; }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  async fire(type) { if (this.disabled && type === 'click') return; for (const fn of this.listeners[type] || []) await fn({ target: this, preventDefault() {} }); }
  focus() { document.activeElement = this; }
  scrollIntoView() {} // geometry is verified in Chrome, not by this stand-in
  closest(selector) { return (this.className || '').split(' ').includes(selector.slice(1)) ? this : this.parent?.closest(selector); }
  querySelectorAll(selector) { return all(this, n => selector.startsWith('.') ? (n.className || '').split(' ').includes(selector.slice(1)) : n.tagName === selector.toUpperCase()); }
  querySelector(selector) { return this.querySelectorAll(selector)[0]; }
}
function all(root, pred) { return root.children.flatMap(n => [...(pred(n) ? [n] : []), ...all(n, pred)]); }
const ids = Object.fromEntries(['board','banner','panel','announce','theme','readouts','archive','intake','new-work','subhead'].map(id => [id, new Element('div')]));
const historical = require('../turntable/fixtures/blocked-verification.json');
const details = {
  run_id: '20261123T000203Z', result: 'discrepancies', discrepancies: historical.discrepancies,
  cause: 'wrong-directory', explanation: 'Verification checked work/, but the files are in work/repo/.',
  recovery: { branch: 'feat/0021-poll-survives-divergence', commit: '89c2380', directory: 'work/repo' },
  links: { 'verify.json': '/records/0021-poll-survives-divergence/20261123T000203Z/verify.json', 'report.md': '/records/0021-poll-survives-divergence/20261123T000203Z/report.md' }
};
if (scenario === 'extra' || scenario === 'missing') {
  details.cause = scenario === 'extra' ? 'extra-files-only' : 'missing-files-only';
  details.discrepancies = historical.discrepancies.filter(d => d.kind === scenario);
  details.recovery = null;
} else if (!['minor', '0021', 'rerun', 'adjudicate', 'review-pass', 'review-fail', 'interrupted', 'contract', 'recovered', 'superseded'].includes(scenario)) throw Error('unknown scenario');
const card = {
  name: '0021-poll-survives-divergence', id: '0021', title: 'Poll survives divergence', track: 'active',
  stage: 'Building', hands: 'Turntable', truth: 'recorded', condition: 'blocked', condition_reason: 'verification: discrepancies',
  last_event: { text: 'verification: discrepancies', ts: '2026-11-23T00:07:53Z' }, trail: [], dwell_s: 0, receipts: [], attachments: [],
  contract: { brief: 'Full task brief.\nKeep this second line.', expected_files: ['src/result.py', 'test_result.py'], test_command: 'python3 -m unittest', repo: 'https://example.com/project', branch: 'review/task', fingerprint: 'a'.repeat(64) },
  blocked_details: details, posture: { next: { adapter: 'codex', sandbox: 'workspace-write' } },
  actions: Object.fromEntries(['answer','approve','park','unpark','attach','remove_attachment','adjudicate','recover'].map(k => [k, {ok: k !== 'recover' || !!details.recovery, why: ''}]))
};
if (scenario.startsWith('review-')) {
  card.stage = 'Review'; card.condition = null; card.blocked_details = null;
  card.review_commit = 'a'.repeat(40);
  card.actions.review = { ok: true, why: '' };
}
if (scenario === 'recovered' || scenario === 'superseded') {
  card.stage = 'Review'; card.condition = null; card.blocked_details = null;
  card.actions.review = { ok: scenario === 'superseded', why: 'Requires verified commit' };
  card.recovered_work = { commit: 'b'.repeat(40), directory: 'work/repo',
    superseded_by: scenario === 'superseded' ? 'a'.repeat(40) : null };
  card.review_commit = card.recovered_work.superseded_by;
}
if (scenario === 'interrupted') {
  card.stage = 'Interrupted'; card.hands = 'operator'; card.condition = 'interrupted';
  card.blocked_details = null;
  card.posture.live = { run_id: 'run1', running: false };
  for (const action of ['approve', 'park', 'recover', 'adjudicate']) card.actions[action] = { ok: false, why: 'Recover first' };
  card.actions.requeue_interrupted = { ok: true, why: '' };
}
const projection = {
  now: '2026-11-23T01:00:00Z', stages: ['Queued','Briefed','Building','Gating','Review','Awaiting operator','Interrupted','Done'],
  tracks: [{ id: 'active', name: 'active', state: 'active', card_count: 1, open_count: 1, done_count: 0, pipeline: [], receipts: [] }],
  cards: [card], board_receipts: []
};
const writes = [];
let refresh;
global.document = {
  getElementById: id => ids[id] || all(ids.panel, n => n.id === id)[0],
  createElement: tag => new Element(tag), createTextNode: text => { const e = new Element('text'); e.textContent = text; return e; },
  documentElement: { setAttribute() {}, removeAttribute() {} }, addEventListener() {}, activeElement: null
};
const memory = {};
global.window = { crypto: { randomUUID }, localStorage: { getItem(k) { return memory[k] || null; }, setItem(k,v) { memory[k] = v; }, removeItem(k) { delete memory[k]; } } };
global.setTimeout = fn => { refresh = fn; return 1; }; global.clearTimeout = () => {};
global.fetch = async (url, options) => {
  if (url === '/intents') {
    const intent = JSON.parse(options.body); writes.push(intent);
    card.receipts.push({ id: intent.id, kind: intent.kind, ts: projection.now, state: 'pending' });
    return { ok: true, json: async () => ({ id: intent.id }) };
  }
  assert.equal(url, '/cards.json'); return { ok: true, json: async () => projection };
};
const buttons = () => all(ids.panel, n => n.tagName === 'BUTTON');
const button = label => { const b = buttons().find(n => n.textContent.includes(label)); assert.ok(b, 'missing ' + label); return b; };
async function main() {
  card.actions.answer = { ok: false };
  card.condition_age_s = 18720;
  card.dwell_s = 28800;
  card.trail = [{ type: 'reviewed', text: 'reviewed: pass', truth: 'verified', ts: projection.now, checker: 'reviewer' }];
  if (check === 'approval') { card.actions.approve = { ok: false, why: 'Review the outcome first.' }; }
  if (check === 'readouts') {
    projection.tracks.push({ id: 'shelf', name: 'shelf', state: 'tabled', card_count: 1, receipts: [] });
    projection.cards.push({ ...card, name: 'shelved', track: 'shelf', stage: 'Awaiting operator' });
  }
  if (check === 'reload') {
    memory['depot.draft.' + card.name] = 'Saved note';
    memory['depot.decision.' + card.name] = JSON.stringify({id: 'saved', draft: 'Saved note'});
    card.receipts.push({id: 'saved', kind: 'adjudicate', state: 'landed'});
  }
  if (check === 'draft-storage') {
    window.localStorage.getItem = () => { throw Error('blocked'); };
    window.localStorage.setItem = () => { throw Error('blocked'); };
  }
  require('./floor.js');
  await new Promise(setImmediate);
  const tick = async () => { await refresh(); await new Promise(setImmediate); };
  const open = all(ids.board, n => n.className === 'open-panel')[0];
  await open.fire('click');
  if (check === 'hidden') {
    const copy = text => all(ids.panel, n => n.className === 'copy' && n.textContent.startsWith(text))[0];
    assert.equal(copy('unparked:').hidden, true);
    assert.equal(copy('parked:').hidden, false);
    card.parked = 'Hold'; await tick();
    assert.equal(copy('unparked:').hidden, false);
    assert.equal(copy('parked:').hidden, true);
  } else if (check === 'reload') {
    assert.equal(all(ids.panel, n => n.id === 'draft')[0].value, '');
    assert.equal(memory['depot.draft.' + card.name], undefined);
  } else if (check === 'announcements') {
    const pools = [card.receipts, projection.tracks[0].receipts, projection.board_receipts];
    pools.forEach((pool, i) => pool.push({id: String(i), kind: 'adjudicate', state: 'pending'}));
    await tick();
    pools.forEach((pool,i) => { pool[0].state = ['landed','refused','failed'][i]; pool[0].reason = i ? 'Reason ' + i : ''; });
    await tick();
    for (const word of ['landed','refused','failed','Reason 1','Reason 2']) assert.ok(ids.announce.textContent.includes(word));
    ids.announce.textContent = 'unchanged'; await tick(); assert.equal(ids.announce.textContent, 'unchanged');
  } else if (check === 'card') {
    const article = open.parent;
    assert.equal(article.attrs.role, undefined); assert.equal(article.attrs['aria-label'], undefined);
    assert.ok(article.textContent.includes('Building'));
    assert.ok(article.textContent.includes('blocked 5h 12m'));
    assert.ok(article.textContent.includes('8h 00m'));
    assert.ok(article.textContent.includes('verified'));
    assert.ok(article.textContent.includes(projection.now));
    assert.ok(article.textContent.includes('Checker: reviewer'));
    assert.equal(all(article, n => n.className === 'sr-only')[0].textContent, ' · verified');
  } else if (check === 'readouts') {
    const before = ids.readouts.textContent;
    assert.ok(before.includes('Awaiting operator 1'));
    await ids.archive.fire('click');
    assert.equal(ids.readouts.textContent, before);
    assert.ok(ids.board.textContent.includes('No ended tracks in the Archive.'));
  } else if (check === 'approval') {
    assert.equal(button('Re-run').disabled, true);
    assert.equal(button('Re-run').className, '');
    assert.ok(ids.panel.textContent.includes('Review the outcome first.'));
    assert.ok(!ids.panel.textContent.includes('brief_approved: authorizes'));
  } else if (check === 'polish') {
    const links = all(ids.panel, n => n.className === 'evidence-link');
    assert.equal(links.length, 2);
    assert.equal(all(ids.panel, n => n.tagName === 'LABEL' && n.textContent.includes('Adjudication'))[0].className, 'field-label');
    assert.ok(!ids.board.textContent.includes('folds 0 done cards'));
  } else if (check.startsWith('draft-')) {
    const kind = check === 'draft-storage' ? 'adjudicate' : check.slice(6);
    if (kind === 'review') { card.actions.review = { ok: true }; card.review_commit = 'a'.repeat(40); }
    if (kind === 'requeue_interrupted') { card.actions.requeue_interrupted = { ok: true }; card.posture.live = { run_id: 'run1' }; }
    await tick();
    const input = all(ids.panel, n => n.id === 'draft')[0];
    input.value = 'Inspected carefully'; await input.fire('input');
    const label = {review:'Accept — Done',adjudicate:'Mark adjudicated',recover:'Recover and review',requeue_interrupted:'Re-queue interrupted'}[kind];
    await button(label).fire('click');
    assert.equal(input.value, 'Inspected carefully', 'keep draft pending');
    card.receipts[0].state = 'refused'; await tick();
    assert.equal(input.value, 'Inspected carefully', 'keep draft on refusal');
    card.receipts[0].state = 'landed'; await tick();
    assert.equal(input.value, ''); assert.equal(button('Post note').attrs['aria-disabled'], 'true');
    // A newer edit is preserved when the next decision lands.
    input.value = 'Second decision'; await input.fire('input'); await button(label).fire('click');
    input.value = 'New annotation'; await input.fire('input');
    card.receipts.at(-1).state = 'landed'; await tick(); assert.equal(input.value, 'New annotation');
  } else throw Error('unknown check');
}
main().then(() => console.log('ok: ' + check)).catch(e => { console.error(e); process.exitCode = 1; });
