// Scenario checks for the shipped floor. Run separately from the turntable suite.
const assert = require('node:assert/strict');
const { randomUUID } = require('node:crypto');
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
  async fire(type) {
    if (this._disabled && type === 'click') return;
    let stopped = false;
    for (const fn of this.listeners[type] || []) {
      await fn({ target: this, preventDefault() {}, stopImmediatePropagation() { stopped = true; } });
      if (stopped) break;
    }
  }
  focus() { document.activeElement = this; }
  closest(selector) { return (this.className || '').split(' ').includes(selector.slice(1)) ? this : this.parent?.closest(selector); }
  querySelectorAll(selector) { return all(this, n => selector === "[data-focus-key]" ? !!n.dataset.focusKey : selector.startsWith('.') ? (n.className || '').split(' ').includes(selector.slice(1)) : n.tagName === selector.toUpperCase()); }
  querySelector(selector) { return this.querySelectorAll(selector)[0]; }
}
function all(root, pred) { return root.children.flatMap(n => [...(pred(n) ? [n] : []), ...all(n, pred)]); }
const ids = Object.fromEntries(['board','banner','panel','announce','theme','readouts','archive','intake','new-work','subhead'].map(id => [id, new Element('div')]));
const fixture = require('../turntable/fixtures/blocked-verification.json');
const details = {
  run_id: 'demo-run-7001', result: 'discrepancies', discrepancies: fixture.discrepancies,
  cause: 'wrong-directory', explanation: 'Verification checked work/, but the files are in work/repo/.',
  recovery: { branch: 'demo/existing-work', commit: '0000001', directory: 'work/repo' },
  links: { 'verify.json': '/records/7001-example-work/demo-run-7001/verify.json', 'report.md': '/records/7001-example-work/demo-run-7001/report.md' }
};
if (scenario === 'extra' || scenario === 'missing') {
  details.cause = scenario === 'extra' ? 'extra-files-only' : 'missing-files-only';
  details.discrepancies = fixture.discrepancies.filter(d => d.kind === scenario);
  details.recovery = null;
} else if (!['0021', 'rerun', 'adjudicate', 'review-pass', 'review-fail', 'interrupted', 'contract', 'recovered', 'superseded', 'truth', 'usability'].includes(scenario)) throw Error('unknown scenario');
const card = {
  name: '7001-example-work', id: '7001', title: 'Example work', track: 'active',
  stage: 'Building', hands: 'Turntable', truth: 'recorded', condition: 'blocked', condition_reason: 'verification: discrepancies',
  last_event: { text: 'verification: discrepancies', ts: '2040-06-15T11:00:00Z' }, trail: [], dwell_s: 0, receipts: [], attachments: [],
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
  now: '2040-06-15T12:00:00Z', stages: ['Queued','Briefed','Building','Gating','Review','Awaiting operator','Interrupted','Done'],
  tracks: [{ id: 'active', name: 'active', state: 'active', card_count: 1, open_count: 1, done_count: 0, pipeline: [], receipts: [] }],
  cards: [card], board_receipts: []
};
const writes = [];
let refresh;
const documentListeners = {};
global.document = {
  getElementById: id => ids[id] || all(ids.panel, n => n.id === id)[0],
  createElement: tag => new Element(tag), createTextNode: text => { const e = new Element('text'); e.textContent = text; return e; },
  documentElement: { setAttribute() {}, removeAttribute() {} }, addEventListener(type, fn) { documentListeners[type] = fn; }, activeElement: null
};
const storage = new Map();
global.window = { crypto: { randomUUID }, localStorage: { getItem(k) { return storage.get(k) || null; }, setItem(k, v) { storage.set(k, v); }, removeItem(k) { storage.delete(k); } } };
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
  let expected;
  if (scenario === 'truth') {
    const fixture = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
    projection.cards = [fixture.card];
    projection.tracks[0].id = fixture.card.track;
    expected = fixture.expected;
  }
  require('./floor.js');
  await new Promise(setImmediate);
  if (scenario === 'truth') {
    const event = all(ids.board, n => (n.className || '').split(' ').includes('event'))[0];
    const head = event.querySelector('.event-head');
    assert.ok(head.textContent.includes(expected.glyph));
    assert.ok(head.textContent.toLowerCase().includes(expected.label.toLowerCase()));
    assert.ok(event.textContent.includes('Checker: ' + expected.checker));
    assert.equal(head.textContent.includes('✓'), expected.glyph === '✓');
    const trail = all(ids.board, n => n.tagName === 'LI').at(-1);
    assert.ok(trail.textContent.includes(expected.glyph));
    assert.ok(trail.textContent.includes(expected.checker));
    if (expected.glyph === '✕') {
      assert.ok(trail.textContent.includes('Checked · failed'));
      assert.ok(!trail.textContent.includes('✓'));
    }
    return;
  }
  await all(ids.board, n => (n.className || '').split(' ').includes('card'))[0].fire('click');
  await all(ids.board, n => n.className === 'open-panel')[0].fire('click');
  if (scenario === 'usability') {
    const check = process.argv[3];
    const tick = async () => { await refresh(); await new Promise(setImmediate); };
    const why = b => all(b.parent, n => n.className === 'why').map(n => n.textContent).join(' ');
    const unavailable = (label, reason) => {
      const b = button(label);
      assert.equal(b.attrs['aria-disabled'], 'true', label);
      assert.ok(why(b).includes(reason), label + ' omits reason: ' + reason);
      assert.equal(b.attrs['aria-describedby'], all(b.parent, n => n.className === 'why')[0].id);
      return b;
    };
    const draft = all(ids.panel, n => n.id === 'draft')[0];
    if (check === 'review-reasons') {
      card.blocked_details = null; card.actions.review = { ok: true }; card.review_commit = 'a'.repeat(40);
      await tick();
      for (const label of ['Accept — Done', 'Reject — blocked']) {
        const b = unavailable(label, 'Type a review note first'); await b.fire('click');
      }
      assert.equal(writes.length, 0, 'unavailable actions must not submit');
      draft.value = 'Inspected'; await draft.fire('input');
      button('Accept — Done').focus();
      await button('Accept — Done').fire('click');
      assert.equal(document.activeElement, button('Accept — Done'));
      unavailable('Accept — Done', 'already pending'); unavailable('Reject — blocked', 'already pending');
      await button('Reject — blocked').fire('click'); assert.equal(writes.length, 1);
    } else if (check === 'recovery-reasons') {
      unavailable('Mark adjudicated', 'Type an adjudication note first');
      card.actions.requeue_interrupted = { ok: true }; card.posture.live = { run_id: 'run1' };
      await tick(); unavailable('Re-queue interrupted', 'Type a recovery note first');
      draft.value = 'Note'; await draft.fire('input');
      for (const kind of ['recover', 'adjudicate', 'requeue_interrupted']) card.receipts.push({ id: kind, kind, state: 'pending' });
      await tick();
      for (const label of ['Recover and review', 'Mark adjudicated', 'Re-queue interrupted']) {
        const b = unavailable(label, 'already pending'); await b.fire('click');
      }
      assert.equal(writes.length, 0);
    } else if (check === 'context-order') {
      card.question_text = 'Which directory?'; await tick();
      const text = all(ids.panel, n => n.attrs['aria-label'] === 'Selected card context')[0].textContent;
      const brief = text.indexOf('Task contract');
      for (const textBefore of [card.condition_reason, details.cause, details.explanation, 'Which directory?', ...details.discrepancies.map(d => d.path + ' · ' + d.kind)]) {
        assert.ok(text.indexOf(textBefore) >= 0 && text.indexOf(textBefore) < brief, textBefore + ' must precede the brief');
      }
    } else if (check === 'focus') {
      assert.equal(document.activeElement.tagName, 'H2');
      assert.ok(document.activeElement.textContent.includes(card.title));
      documentListeners.keydown({ key: 'Escape' });
      assert.equal(document.activeElement.dataset.name, card.name);
      const path = all(ids.board, n => n.tagName === 'SUMMARY' && n.textContent.startsWith('path'))[0];
      path.focus(); await tick();
      assert.notEqual(document.activeElement, path);
      assert.equal(document.activeElement.textContent, path.textContent);
      const menuButton = all(ids.board, n => n.tagName === 'BUTTON' && n.textContent.startsWith('Pause'))[0];
      menuButton.focus(); await tick();
      assert.equal(document.activeElement.dataset.focusKey, menuButton.dataset.focusKey);
      await all(ids.board, n => n.className === 'open-panel' && n.dataset.name === card.name)[0].fire('click');
      const link = all(ids.panel, n => n.tagName === 'A')[0]; link.focus(); await tick();
      assert.notEqual(document.activeElement, link); assert.equal(document.activeElement.href, link.href);
      const recover = button('Recover and review'); recover.focus(); await tick();
      assert.notEqual(document.activeElement, recover); assert.equal(document.activeElement, button('Recover and review'));
    } else throw Error('unknown check: ' + check);
    return;
  }
  if (scenario.startsWith('review-')) {
    assert.ok(ids.panel.textContent.includes(card.review_commit));
    assert.equal(button('Accept — Done').disabled, true);
    assert.equal(button('Reject — blocked').disabled, true);
    const draft = all(ids.panel, n => n.id === 'draft')[0];
    draft.value = 'Reviewed against brief'; await draft.fire('input');
    const result = scenario.slice('review-'.length);
    await button(result === 'pass' ? 'Accept — Done' : 'Reject — blocked').fire('click');
    assert.equal(writes.length, 1);
    assert.equal(writes[0].kind, 'review');
    assert.deepEqual(writes[0].data, { result, note: draft.value, commit: card.review_commit });
    assert.equal(button('Accept — Done').disabled, true);
    assert.equal(button('Reject — blocked').disabled, true);
    assert.ok(ids.panel.textContent.includes('Card stays in Review, blocked, with your note. Next: Re-run (approve) or adjudicate.'));
    // Submission alone keeps the note; refusal keeps it available to correct.
    card.receipts[0].state = 'refused'; await refresh();
    assert.equal(draft.value, 'Reviewed against brief');
    await button(result === 'pass' ? 'Accept — Done' : 'Reject — blocked').fire('click');
    card.receipts[1].state = 'landed';
    card.actions.review.ok = false;
    card.stage = result === 'pass' ? 'Done' : 'Review';
    card.condition = result === 'pass' ? null : 'blocked';
    card.condition_reason = 'review: fail · Reviewed against brief';
    card.rejected_review = result === 'fail' ? { note: 'Reviewed against brief', commit: card.review_commit, run_id: 'run1' } : null;
    await refresh();
    assert.equal(draft.value, '');
    assert.ok(!storage.has('depot.draft.' + card.name));
    if (result === 'fail') {
      assert.equal(button('Mark adjudicated').disabled, true);
      assert.equal(button('Re-run').disabled, false);
      assert.ok(ids.panel.textContent.includes('Reviewed against brief'));
      assert.ok(!ids.panel.textContent.includes('Verification:'));
      draft.value = 'Separate adjudication'; await draft.fire('input');
      await button('Mark adjudicated').fire('click');
      assert.deepEqual(writes[2].data, { run_id: 'run1', note: 'Separate adjudication' });
    }
    return;
  }
  if (scenario === 'recovered') {
    assert.ok(ids.panel.textContent.includes('cannot be accepted'));
    assert.ok(ids.panel.textContent.includes('starts fresh from the base'));
    assert.ok(!buttons().some(n => /Accept|Reject/.test(n.textContent)));
    assert.equal(button('Re-run to verify').disabled, false);
    await button('Re-run to verify').fire('click');
    assert.equal(writes.length, 1);
    assert.equal(writes[0].kind, 'approve');
    assert.deepEqual(writes[0].data, { contract_fingerprint: card.contract.fingerprint });
    return;
  }
  if (scenario === 'superseded') {
    assert.ok(ids.panel.textContent.includes('Superseded by verified commit ' + card.review_commit));
    assert.ok(ids.panel.textContent.includes(card.recovered_work.commit));
    assert.ok(button('Accept — Done'));
    return;
  }
  if (scenario === 'interrupted') {
    assert.equal(button('Approve & dispatch').disabled, true);
    assert.equal(button('Park').disabled, true);
    assert.equal(button('Re-queue interrupted run').disabled, true);
    const draft = all(ids.panel, n => n.id === 'draft')[0];
    draft.value = 'Old worker stopped; inspected artifacts'; await draft.fire('input');
    await button('Re-queue interrupted run').fire('click');
    assert.equal(writes.length, 1);
    assert.equal(writes[0].kind, 'requeue_interrupted');
    assert.deepEqual(writes[0].data, { run_id: 'run1', note: draft.value });
    assert.ok(ids.panel.textContent.includes('Pending'));
    return;
  }
  assert.ok(ids.panel.textContent.includes(details.cause));
  const list = all(ids.panel, n => n.tagName === 'LI').map(n => n.textContent);
  assert.deepEqual(list, details.discrepancies.map(d => d.path + ' · ' + d.kind));
  const links = all(ids.panel, n => n.tagName === 'A');
  for (const [label, href] of Object.entries(details.links)) assert.ok(links.some(n => n.textContent === label && n.href === href));
  assert.equal(button('Re-run').disabled, false);
  for (const text of [card.contract.brief, ...card.contract.expected_files, card.contract.test_command, card.contract.repo, card.contract.branch]) {
    assert.ok(ids.panel.textContent.includes(text), 'contract omits ' + text);
  }
  if (scenario === 'extra' || scenario === 'missing') {
    assert.ok(!buttons().some(n => n.textContent.includes('Recover and review')));
  } else if (scenario === '0021') {
    for (const text of ['move the card to Review', 'cannot be accepted', 'Re-run to verify requires separate approval', 'starts fresh from the base']) {
      assert.ok(ids.panel.textContent.includes(text), 'Recover copy omits ' + text);
    }
    await button('Recover and review — demo/existing-work @ 0000001').fire('click');
    assert.equal(writes[0].kind, 'recover');
    assert.deepEqual(writes[0].data, { run_id: details.run_id, commit: '0000001' });
  } else if (scenario === 'rerun' || scenario === 'contract') {
    await button('Re-run').fire('click');
    assert.equal(writes[0].kind, 'approve');
    assert.deepEqual(writes[0].data, { contract_fingerprint: card.contract.fingerprint });
  } else if (scenario === 'adjudicate') {
    assert.equal(button('Mark adjudicated').disabled, true);
    const draft = all(ids.panel, n => n.id === 'draft')[0];
    draft.value = 'Verifier used wrong directory'; await draft.fire('input');
    await button('Mark adjudicated').fire('click');
    assert.equal(writes[0].kind, 'adjudicate');
    assert.deepEqual(writes[0].data, { run_id: details.run_id, note: draft.value });
  }
  if (writes.length) assert.ok(ids.panel.textContent.includes('Pending'));
}
main().then(() => console.log('ok: ' + scenario)).catch(e => { console.error(e); process.exitCode = 1; });
