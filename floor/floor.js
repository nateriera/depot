// The floor page: fetch the projection, render it, fetch again; and the write panel.
// The panel never edits the board. Every control builds an intent and hands it to the daemon through POST /intents;
// what a card looks like afterwards is whatever the daemon landed in the log, read back through /cards.json.
// ---- pure helpers (no DOM): track order and the intake draft. Exported for the tests; the page below uses them. ----

// Display order of track lanes: the saved list first (names no longer present are dropped), then any track not in
// the list in projection order. With nothing saved this is exactly the projection order.
function orderTracks(ids, saved) {
  const out = [];
  if (Array.isArray(saved)) for (const id of saved) if (ids.includes(id) && !out.includes(id)) out.push(id);
  for (const id of ids) if (!out.includes(id)) out.push(id);
  return out;
}

function moveTrack(order, id, dir) {
  const i = order.indexOf(id), j = i + dir;
  const out = order.slice();
  if (i < 0 || j < 0 || j >= order.length) return out;
  [out[i], out[j]] = [out[j], out[i]];
  return out;
}

// A per-browser display preference, never a record: it lives only in the guarded store. If storage throws, the
// order still works for this session (kept in memory) and a fresh page load shows the default order.
function trackOrderPrefs(store) {
  const KEY = "depot.trackorder";
  let session = null;
  function read() {
    try {
      const raw = store.get(KEY);
      const v = raw ? JSON.parse(raw) : null;
      return Array.isArray(v) ? v : null;
    } catch (e) { return null; }
  }
  return {
    order(ids) { return orderTracks(ids, session !== null ? session : read()); },
    move(ids, id, dir) {
      const next = moveTrack(this.order(ids), id, dir);
      session = next;
      try { store.set(KEY, JSON.stringify(next)); } catch (e) { /* storage is optional */ }
      return next;
    }
  };
}

// The "New work" form's draft: saved as you type, kept through a refresh, cleared only when the daemon reports the
// create landed (a refusal keeps it, so you can fix it). `resolve` reads the board receipts for the submitted id.
function intakeDraft(store) {
  const DKEY = "depot.intake.draft", PKEY = "depot.intake.pending";
  let mem = null, pend = null;
  const get = k => { try { return store.get(k); } catch (e) { return null; } };
  const set = (k, v) => { try { store.set(k, v); } catch (e) { /* storage is optional */ } };
  const del = k => { try { store.del(k); } catch (e) { /* storage is optional */ } };
  return {
    load() {
      if (mem) return mem;
      try { const raw = get(DKEY); return raw ? JSON.parse(raw) : null; } catch (e) { return null; }
    },
    save(v) { mem = v; set(DKEY, JSON.stringify(v)); },
    clear() { mem = null; del(DKEY); },
    submitted(id) { pend = id; set(PKEY, id); },
    pendingId() { return pend || get(PKEY) || null; },
    resolve(receipts) {
      const id = this.pendingId();
      const r = id ? receipts.find(x => x.id === id) : null;
      if (!r) return null;
      if (r.state === "landed") { this.clear(); pend = null; del(PKEY); return { state: "landed", filed: r.filed }; }
      if (r.state === "refused" || r.state === "failed") return { state: r.state, reason: r.reason };
      return null;
    }
  };
}

// Done-card display shares the same guarded browser store as track order. A failed store still has session state.
function donePrefs(store) {
  const key = "depot.hideDone";
  let saved = {};
  try { const v = JSON.parse(store.get(key)); if (v && typeof v === "object" && !Array.isArray(v)) saved = v; } catch (e) {}
  const hidden = new Set(Object.keys(saved).filter(k => saved[k] === true));
  return {
    hidden(id) { return hidden.has(id); },
    set(id, value) {
      if (value) hidden.add(id); else hidden.delete(id);
      try { store.set(key, JSON.stringify(Object.fromEntries([...hidden].map(k => [k, true])))); } catch (e) {}
    }
  };
}

function trackIntent(kind, track, id) { return { id, kind, item: null, data: { track } }; }

function intakeConsequence(track) {
  if (!track) return "";
  if (track.state === "ended") return "Track ended — reopen it from the Archive to file new work. Nothing will be filed.";
  if (track.state === "tabled") return "Files the item and returns " + track.name + " to the board.";
  if (track.state === "paused") return "Files in Queued on a paused track; it will not dispatch until the track resumes.";
  return "";
}

function trackActions(track) {
  const state = track.state || "active";
  if (state === "ended") return [["track_reopen", "Reopen — returns the lane to the board as active; swept items stay parked and budgets persist."]];
  const actions = state === "tabled"
    ? [["track_untable", "Resume — returns the lane to its arranged position on the board; approved items may dispatch next cycle."]]
    : state === "paused"
      ? [["track_resume", "Resume — releases the dispatch hold; approved items may dispatch next cycle."]]
      : [["track_pause", "Pause — holds the next dispatch; in-flight work completes and records still land."]];
  if (state !== "tabled") actions.push(["track_table", "Table — shelves the lane and holds new dispatches; in-flight work completes and records still land."]);
  actions.push(["track_end", "End track — " + track.open_count + " open items will be parked (reason: track ended); the lane moves to the Archive. Nothing is deleted." +
    (track.pipeline && track.pipeline.length ? " Refused while these pipeline items remain: " + track.pipeline.join(", ") + "." : "")]);
  return actions;
}

if (typeof module !== "undefined" && module.exports) module.exports = { orderTracks, moveTrack, trackOrderPrefs, intakeDraft, donePrefs, trackIntent, intakeConsequence, trackActions };

function boot() {
  "use strict";
  const REFRESH_MS = 15000;
  const FAST_MS = 3000; // while any intent is pending or reconciling
  const MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024;
  const marks = { claimed: "◇", recorded: "■", verified: "✓", checked_failed: "✕" };
  const KIND_LABEL = { review: "Review decision", requeue_interrupted: "Re-queue interrupted run", adjudicate: "Mark adjudicated", recover: "Recover and review", message: "Message", approve: "Approve & dispatch", park: "Park", unpark: "Unpark",
                       attach: "Attach", remove_attachment: "Remove attachment", create: "New work",
                       track_pause: "Pause track", track_resume: "Resume track", track_table: "Table track",
                       track_untable: "Resume tabled track", track_end: "End track", track_reopen: "Reopen track" };
  const board = document.getElementById("board");
  const banner = document.getElementById("banner");
  const panelEl = document.getElementById("panel");
  const openPaths = new Set(); // card names whose path is expanded, kept across refreshes

  // Per-viewer conveniences only (theme, drafts). The page works with storage blocked.
  const store = {
    get(k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { window.localStorage.setItem(k, v); } catch (e) { /* storage is optional */ } },
    del(k) { try { window.localStorage.removeItem(k); } catch (e) { /* storage is optional */ } }
  };
  const prefs = trackOrderPrefs(store);
  const draft = intakeDraft(store);
  const done = donePrefs(store);
  let archive = false;
  const openMenus = new Set();
  const trackErrors = new Map();
  const archiveBtn = document.getElementById("archive");
  archiveBtn.addEventListener("click", () => {
    archive = !archive;
    lowerPanel();
    document.getElementById("intake").hidden = true;
    document.getElementById("new-work").disabled = archive;
    archiveBtn.textContent = archive ? "Back to board · display only" : "Archive · ended tracks, read-only except Reopen";
    archiveBtn.setAttribute("aria-pressed", String(archive));
    if (data) render(data);
  });

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    if (tag === "button") node.addEventListener("click", ev => {
      if (node.dataset.unavailable) { ev.preventDefault(); ev.stopImmediatePropagation(); }
    });
    return node;
  }
  function say(text) { document.getElementById("announce").textContent = text; }

  // Keys describe the entity and act, never the node's position on the board.
  function key(node, value) { node.dataset.focusKey = value; return node; }
  function focusKey() { return document.activeElement && document.activeElement.dataset.focusKey; }
  function restoreFocus(value) {
    if (!value) return;
    const find = root => Array.from(root.querySelectorAll("[data-focus-key]")).find(n => n.dataset.focusKey === value);
    const node = find(board) || find(panelEl);
    if (node) node.focus({ preventScroll: true });
  }
  // aria-disabled keeps a pending action in the keyboard sequence, including after a refresh.
  // The guard applies to mouse, keyboard and programmatic activation alike.
  function unavailable(button, reason) {
    button.dataset.unavailable = reason || "";
    button.setAttribute("aria-disabled", String(!!reason));
  }
  function action(parent, button, reason, identity) {
    key(button, identity);
    unavailable(button, reason);
    const box = el("div", "action");
    const why = el("span", "why", reason || "");
    why.id = "why-" + identity;
    button.setAttribute("aria-describedby", why.id);
    box.append(button, why);
    parent.append(box);
    return box;
  }

  // ---- theme: a persisted choice, else the system preference ----
  const themeBtn = document.getElementById("theme");
  const THEMES = ["auto", "dark", "light"];
  function applyTheme() {
    const t = store.get("depot.theme");
    const root = document.documentElement;
    if (t === "light" || t === "dark") root.setAttribute("data-theme", t); else root.removeAttribute("data-theme");
    themeBtn.textContent = "Theme: " + (t === "light" || t === "dark" ? t : "auto (system)");
    themeBtn.title = "Cycles auto, dark, light. Stored in this browser only; creates no event.";
  }
  themeBtn.addEventListener("click", () => {
    const t = store.get("depot.theme");
    const cur = t === "light" || t === "dark" ? t : "auto";
    const next = THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length];
    if (next === "auto") store.del("depot.theme"); else store.set("depot.theme", next);
    applyTheme();
  });
  applyTheme();

  // ---- time ----
  function dwell(s) {
    if (s < 60) return "<1m";
    if (s < 3600) return Math.floor(s / 60) + "m";
    if (s < 86400) return Math.floor(s / 3600) + "h " + String(Math.floor((s % 3600) / 60)).padStart(2, "0") + "m";
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
    return h ? d + "d " + h + "h" : d + "d";
  }
  function closed(s) {
    if (s < 3600) return "closed " + Math.max(1, Math.floor(s / 60)) + "m ago";
    if (s < 86400) return "closed " + Math.floor(s / 3600) + "h ago";
    return "closed " + Math.floor(s / 86400) + "d ago";
  }
  // Exact durations for the supervision line: 45s, 3m 12s, 10m, 1h 05m.
  function exact(s) {
    s = Math.floor(s);
    if (s < 60) return s + "s";
    if (s < 3600) return Math.floor(s / 60) + "m" + (s % 60 ? " " + String(s % 60).padStart(2, "0") + "s" : "");
    return Math.floor(s / 3600) + "h " + String(Math.floor((s % 3600) / 60)).padStart(2, "0") + "m";
  }
  function seconds(from, to) { return Math.max(0, (Date.parse(to) - Date.parse(from)) / 1000); }

  // ---- state ----
  let data = null;
  let selected = null;              // card name bound to the panel; null when the panel is lowered
  const fileKeys = new WeakMap();
  const staged = new Map();         // card name -> File[] chosen but not yet submitted
  const pendingReviews = new Map();
  const decisionDrafts = new Map();   // session fallback when browser storage is blocked
  const receiptStates = new Map();   // snapshots: projection objects may be reused between ticks
  let receiptsSeen = false;
  const payloads = new Map();       // intent id -> the body sent, so a failed one can be retried with the same id
  let panelBuilt = false;
  const P = {};                     // panel element refs

  function cardByName(name) { return data && data.cards.find(c => c.name === name); }
  function draftKey(name) { return "depot.draft." + name; }
  function reasonKey(name) { return "depot.reason." + name; }
  function uuid() {
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    const h = n => Array.from({ length: n }, () => Math.floor(Math.random() * 16).toString(16)).join("");
    return h(8) + "-" + h(4) + "-4" + h(3) + "-a" + h(3) + "-" + h(12);
  }
  function pendingOf(card) { return (card.receipts || []).filter(r => r.state === "pending" || r.state === "reconciling"); }

  // ---- the board ----
  function render(d) {
    const now = d.now;
    const members = d.tracks.filter(t => archive ? t.state === "ended" : t.state !== "ended" && t.state !== "tabled");
    const cards = d.cards.filter(c => members.some(t => t.id === c.track));
    const focused = focusKey();
    board.replaceChildren();

    const readouts = [
      ["Awaiting operator", d.cards.filter(c => c.stage === "Awaiting operator").length, "operator"],
      ["Stuck", d.cards.filter(c => c.condition === "blocked" || c.condition === "stalled?" || c.condition === "interrupted").length, "stuck"],
      ["Building / gating", d.cards.filter(c => c.stage === "Building" || c.stage === "Gating").length, ""]
    ];
    const box = document.getElementById("readouts");
    box.replaceChildren();
    for (const [label, count, kind] of readouts) {
      const r = el("div", "readout " + kind);
      r.append(el("span", "label", label), el("strong", "", String(count)));
      box.append(r);
    }

    // One axis, drawn once: the stations on the route.
    const axis = el("div", "axis");
    axis.append(el("div", "axis-gutter eyebrow", "Tracks / stages →"));
    for (const stage of d.stages) {
      const st = el("div", "station" + (stage === "Awaiting operator" ? " awaiting" : ""));
      st.append(el("span", "", stage), el("small", "", String(cards.filter(c => c.stage === stage && (archive || stage !== "Done" || !done.hidden(c.track))).length)));
      axis.append(st);
    }
    board.append(axis);

    if (!members.length) board.append(el("p", "empty-state", archive ? "No ended tracks in the Archive." : "No active tracks. New work can start a track; tabled tracks are below."));

    // Lanes: whitespace between them; empty stage zones stay empty.
    const ids = d.tracks.map(t => t.id);
    const shown = prefs.order(ids);
    for (const id of shown) {
      const track = members.find(t => t.id === id);
      if (!track) continue;
      const lane = el("section", "lane");
      lane.setAttribute("aria-label", track.name);
      const mine = cards.filter(c => c.track === track.id);
      const head = el("div", "lane-head");
      head.append(el("div", "eyebrow", "Track"), el("div", "track-name", track.name),
                  el("div", "track-count", mine.length + " work item" + (mine.length === 1 ? "" : "s")));
      if (track.state === "paused") head.append(el("div", "track-state", "PAUSED · new dispatches held"));
      if (archive) head.append(el("div", "track-state", "ENDED · Archive"));
      const menu = el("details", "track-menu");
      menu.open = openMenus.has(id);
      menu.addEventListener("toggle", () => { if (menu.open) openMenus.add(id); else openMenus.delete(id); });
      menu.append(key(el("summary", "", "… Track menu · " + track.name), "track:" + id + ":menu"));
      const menuBody = el("div", "track-menu-body");
      const err = el("div", "why", trackErrors.get(id) || "");
      const busy = (track.receipts || []).some(r => r.state === "pending" || r.state === "reconciling");
      for (const [kind, copy] of trackActions(track)) {
        const b = el("button", kind === "track_end" ? "danger" : "", copy);
        b.type = "button";
        const why = busy ? "A track request is already pending." : kind === "track_end" && (track.pipeline || []).length ? "Pipeline items must finish before ending this track." : "";
        b.addEventListener("click", async () => {
          unavailable(b, "A track request is already pending.");
          b.parentNode.querySelector(".why").textContent = b.dataset.unavailable;
          const ok = await send(trackIntent(kind, id, uuid()), err, kind);
          trackErrors.set(id, err.textContent);
          if (!ok) { unavailable(b, why); b.parentNode.querySelector(".why").textContent = why; }
        });
        action(menuBody, b, why, "track:" + id + ":" + kind);
      }
      menuBody.append(err);
      // Lanes are chrome, not state: moving one changes this browser's display order only and never touches a card.
      const arrange = el("div", "arrange");
      for (const [dir, word] of [[-1, "earlier"], [1, "later"]]) {
        const b = el("button", "arrow", "Move " + word + " — display order in this browser only; creates no event.");
        b.type = "button";
        b.title = "Move track " + word + ": changes the display order in this browser only; creates no event.";
        b.setAttribute("aria-label", "Move track " + track.name + " " + word);
        const edge = (dir < 0 && shown.indexOf(id) === 0) || (dir > 0 && shown.indexOf(id) === shown.length - 1);
        b.addEventListener("click", () => { prefs.move(ids, id, dir); render(data); say("Track " + track.name + " moved " + word + "."); });
        action(arrange, b, edge ? "Already the " + (dir < 0 ? "first" : "last") + " track." : "", "track:" + id + ":move:" + dir);
      }
      if (!archive) {
        menuBody.append(arrange);
      }
      if (!archive && track.done_count > 0) {
        const toggle = el("button", "", (done.hidden(id) ? "☑ " : "☐ ") + "Hide done cards — " + (done.hidden(id) ? "shows " : "folds ") + track.done_count + " done cards in this browser only; creates no event.");
        key(toggle, "track:" + id + ":done");
        toggle.type = "button";
        toggle.setAttribute("role", "menuitemcheckbox");
        toggle.setAttribute("aria-checked", String(done.hidden(id)));
        toggle.addEventListener("click", () => { done.set(id, !done.hidden(id)); render(data); });
        menuBody.append(toggle);
      }
      menu.append(menuBody);
      head.append(menu);
      if (!archive && done.hidden(id) && track.done_count > 0) {
        const chip = el("button", "done-chip", track.done_count + " done — show");
        key(chip, "track:" + id + ":show-done");
        chip.type = "button";
        chip.addEventListener("click", () => { done.set(id, false); render(data); });
        head.append(chip);
      }
      for (const r of (track.receipts || []).slice().reverse().slice(0, 6)) head.append(receiptRow(r));
      lane.append(head);
      for (const stage of d.stages) {
        const zone = el("div", "zone");
        zone.dataset.stage = stage;
        for (const card of mine.filter(c => c.stage === stage && (archive || stage !== "Done" || !done.hidden(id)))) zone.append(renderCard(card, now, archive));
        lane.append(zone);
      }
      board.append(lane);
    }
    if (!archive) {
      const shelf = el("section", "track-shelf");
      shelf.setAttribute("aria-label", "Tabled tracks");
      for (const id of shown) {
        const track = d.tracks.find(t => t.id === id && t.state === "tabled");
        if (!track) continue;
        const row = el("div", "shelf-row");
        row.append(el("span", "", "Tabled: " + track.name + " — " + track.card_count + " cards"));
        const err = el("span", "why", trackErrors.get(id) || "");
        const menu = el("details", "track-menu shelf-menu");
        menu.open = openMenus.has(id);
        menu.addEventListener("toggle", () => { if (menu.open) openMenus.add(id); else openMenus.delete(id); });
        menu.append(key(el("summary", "", "… Track menu · " + track.name), "track:" + id + ":menu"));
        const menuBody = el("div", "track-menu-body");
        for (const [kind, copy] of trackActions(track)) {
          const b = el("button", "", copy); b.type = "button";
          const why = pendingOf(track).length ? "A track request is already pending." : kind === "track_end" && (track.pipeline || []).length ? "Pipeline items must finish before ending this track." : "";
          b.addEventListener("click", async () => {
            unavailable(b, "A track request is already pending.");
            b.parentNode.querySelector(".why").textContent = b.dataset.unavailable;
            const ok = await send(trackIntent(kind, id, uuid()), err, kind);
            trackErrors.set(id, err.textContent);
            if (!ok) { unavailable(b, why); b.parentNode.querySelector(".why").textContent = why; }
          });
          action(kind === "track_untable" ? row : menuBody, b, why, "track:" + id + ":" + kind);
        }
        menu.append(menuBody);
        row.append(menu);
        row.append(err);
        for (const r of (track.receipts || []).slice().reverse().slice(0, 6)) row.append(receiptRow(r));
        shelf.append(row);
      }
      board.append(shelf);
    }
    restoreFocus(focused);
  }

  function renderCard(card, now, readOnly = false) {
    const cls = (card.condition === "blocked" || card.condition === "interrupted") ? "blocked" : card.condition === "parked" ? "parked"
      : card.condition === "stalled?" ? "stalled" : "";
    const article = el("article", "card " + cls + (card.name === selected ? " selected" : ""));
    key(article, "card:" + card.name);
    if (readOnly) article.className += " archived";
    let open;
    if (!readOnly) {
      article.dataset.name = card.name;
      open = el("button", "open-panel", "Open panel");
      open.type = "button";
      open.dataset.name = card.name;
      open.setAttribute("aria-label", "Open panel for " + card.id + " " + card.title);
      open.setAttribute("aria-pressed", String(card.name === selected));
      open.addEventListener("click", () => select(card.name));
    }
    const top = el("div", "card-top");
    top.append(el("span", "card-id", card.id + (card.kind === "gate" ? " · gate" : "")));
    if (card.condition) top.append(el("span", "condition " + cls, card.condition + (card.condition_age_s != null ? " " + dwell(card.condition_age_s) : "")));
    const meta = el("div", "meta");
    const hands = el("span");
    hands.append(el("b", "", card.hands));
    meta.append(el("span", "card-stage", card.stage), hands, el("span", "dwell", card.stage === "Done" ? closed(card.dwell_s) : dwell(card.dwell_s)));

    const ev = el("div", "event " + card.truth);
    const evHead = el("div", "event-head");
    evHead.append(el("span", "truth-mark", marks[card.truth]), el("span", "", card.truth === "checked_failed" ? "Checked · failed" : card.truth));
    const evText = el("div", "", card.last_event.text);
    evText.append(el("span", "event-age", " · " + dwell(seconds(card.last_event.ts, now)) + " ago"));
    ev.append(evHead, evText);
    if (card.truth === "verified" || card.truth === "checked_failed") {
      const missing = card.checker === "Checker: not supplied";
      ev.append(el("div", "checker" + (missing ? " missing" : ""), missing ? card.checker : "Checker: " + card.checker));
    }

    article.append(top, el("h2", "title", card.title), meta, ev);
    // A live run always shows how long since its adapter last spoke and how much of its budget is spent.
    if (card.last_adapter_event_age_s !== null && card.last_adapter_event_age_s !== undefined) {
      article.append(el("div", "live",
        "last event " + exact(card.last_adapter_event_age_s) + " ago · " +
        exact(card.elapsed_s) + " of " + exact(card.timeout_s) + " budget"));
    }
    // What is being asked of operator, and the newest message or reopening note. Neither changes the truth mark above.
    if (card.question) article.append(el("div", "question", "Asks: " + card.question));
    if (card.annotation) {
      const a = card.annotation;
      const note = el("div", "annotation");
      note.append(el("span", "who", "✎ " + (a.type === "reopened" ? "reopened · " : "") + a.author),
                  el("span", "event-age", " · " + dwell(seconds(a.ts, now)) + " ago"),
                  el("div", "note-text", a.text));
      article.append(note);
    }
    if (card.condition_reason) article.append(el("div", "condition-reason", card.condition_reason));
    const pend = pendingOf(card);
    if (pend.length) article.append(el("div", "card-pending", "… " + pend.length + " pending · not in log"));

    // The path: the trail the card took, newest last. Disclosure only; it changes nothing.
    const details = el("details", "path");
    details.open = openPaths.has(card.name);
    details.addEventListener("toggle", () => {
      if (details.open) openPaths.add(card.name); else openPaths.delete(card.name);
    });
    details.append(key(el("summary", "", "path · " + card.trail.length + " events" +
      (card.heartbeats ? " · " + card.heartbeats + " heartbeat" + (card.heartbeats === 1 ? "" : "s") : "")), "card:" + card.name + ":path"));
    const list = el("ol");
    for (const t of card.trail) {
      const li = el("li");
      const glyph = el("span", "t", marks[t.truth]);
      glyph.setAttribute("aria-hidden", "true");
      const text = el("span", "", (t.truth === "checked_failed" ? "Checked · failed · " : "") + t.type + " · " + t.text + " · " + t.ts +
        (t.checker ? " · " + (t.checker === "Checker: not supplied" ? t.checker : "Checker: " + t.checker) : ""));
      text.append(el("span", "sr-only", " · " + t.truth));
      li.append(glyph, text);
      list.append(li);
    }
    details.append(list);
    article.append(details);
    if (open) article.append(open);
    return article;
  }

  // ---- intents: the only thing the page can send ----
  async function send(intent, errEl, kind) {
    if (kind === "review") {
      const pending = { id: intent.id, note: intent.data.note };
      pendingReviews.set(intent.item, pending);
      store.set("depot.review.pending." + intent.item, JSON.stringify(pending));
    }
    if (kind !== "attach") payloads.set(intent.id, intent); // attachments are not kept for retry: re-attach instead
    errEl.textContent = "";
    try {
      const res = await fetch("/intents", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(intent)
      });
      const reply = await res.json().catch(() => ({}));
      if (!res.ok) {
        errEl.textContent = "Not handed to the daemon (HTTP " + res.status + "): " + (reply.error || "refused at the door") +
          ". Nothing was submitted.";
        return false;
      }
      say("Handed to the daemon: " + KIND_LABEL[kind] + ". Pending until it lands in the log.");
      await tick();
      return true;
    } catch (err) {
      errEl.textContent = "Not handed to the daemon: " + err.message + ". Nothing was submitted.";
      return false;
    }
  }

  async function post(kind, payload, body) {
    const card = cardByName(selected);
    if (!card || archive) return false;
    const intent = body || { id: uuid(), kind: kind, item: card.name, data: payload };
    if (["review", "adjudicate", "recover", "requeue_interrupted"].includes(kind)) {
      const decision = { id: intent.id, draft: P.draft.value };
      decisionDrafts.set(card.name, decision);
      store.set("depot.decision." + card.name, JSON.stringify(decision));
    }
    return send(intent, P.err, kind);
  }

  function bytesToBase64(buf) {
    const bytes = new Uint8Array(buf);
    let bin = "";
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return window.btoa(bin);
  }

  // ---- the panel: built once, bound to one card at a time ----
  function copyLine(parent, text) { const c = el("span", "copy", text); parent.append(c); return c; }

  function buildPanel() {
    const head = el("header", "panel-head");
    P.binding = key(el("h2", "binding"), "panel:heading");
    P.binding.tabIndex = -1;
    const dismiss = el("div", "dismiss");
    const lower = el("button", "", "Lower panel · Esc");
    lower.type = "button";
    lower.addEventListener("click", lowerPanel);
    dismiss.append(el("span", "copy", "Lowers this panel; keeps drafts and pending intents; creates no event."), lower);
    head.append(P.binding, dismiss);

    const body = el("div", "panel-body");
    P.ctx = el("section", "pz");
    P.ctx.setAttribute("aria-label", "Selected card context");
    P.resp = el("section", "pz");
    P.resp.setAttribute("aria-label", "Response and decision");
    P.post = el("section", "pz");
    P.post.setAttribute("aria-label", "Next-dispatch posture");
    body.append(P.ctx, P.resp, P.post);

    // response + decision zone (static: the draft survives refreshes)
    P.resp.append(el("h3", "", "Response and decision"));
    P.blocked = el("div");
    P.resp.append(P.blocked);
    const lbl = el("label", "field-label", "Your response");
    lbl.htmlFor = "draft";
    P.draft = el("textarea");
    P.draft.id = "draft";
    P.draft.addEventListener("input", () => {
      if (selected) { if (P.draft.value) store.set(draftKey(selected), P.draft.value); else store.del(draftKey(selected)); }
      refreshPanel();
    });
    P.draftCopy = el("span", "copy", "Draft only: typing creates no event and starts no run.");
    P.send = el("button", "", "Send answer");
    P.send.type = "button";
    P.send.addEventListener("click", async () => {
      const text = P.draft.value;
      if (await post("message", { text: text })) { P.draft.value = ""; store.del(draftKey(selected)); refreshPanel(); }
    });
    P.sendCopy = el("span", "copy");
    P.sendWhy = el("span", "why");
    const sendBox = el("div");
    sendBox.append(P.send, P.sendCopy, P.sendWhy);

    P.approve = el("button", "primary", "Approve & dispatch");
    P.approve.type = "button";
    P.approve.addEventListener("click", () => post("approve", { contract_fingerprint: cardByName(selected).contract.fingerprint }));
    P.approveCopy = el("span", "copy");
    P.approveWarn = el("div", "callout warn");
    P.approveWhy = el("span", "why");
    const approveBox = el("div");
    approveBox.append(P.approve, P.approveCopy, P.approveWarn, P.approveWhy);
    const row = el("div", "act-row");
    row.append(sendBox, approveBox);

    // park / unpark
    P.parkBox = el("div", "act");
    const rl = el("label", "field-label", "Reason to park (required)");
    rl.htmlFor = "reason";
    P.reason = el("input");
    P.reason.type = "text";
    P.reason.id = "reason";
    P.reason.autocomplete = "off";
    P.reason.addEventListener("input", () => {
      if (selected) { if (P.reason.value) store.set(reasonKey(selected), P.reason.value); else store.del(reasonKey(selected)); }
      refreshPanel();
    });
    P.park = el("button", "danger", "Park with reason");
    P.park.type = "button";
    P.park.addEventListener("click", async () => {
      if (await post("park", { reason: P.reason.value.trim() })) { P.reason.value = ""; store.del(reasonKey(selected)); refreshPanel(); }
    });
    P.parkCopy = el("span", "copy", "parked: records your reason and holds the card (decided, not dispatched); does not cancel a live run.");
    P.parkWhy = el("span", "why");
    P.unpark = el("button", "", "Unpark");
    P.unpark.type = "button";
    P.unpark.addEventListener("click", () => post("unpark", {}));
    P.unparkCopy = el("span", "copy", "unparked: releases the hold and dispatches nothing. Approving is a separate act.");
    P.unparkWhy = el("span", "why");
    P.parkBox.append(rl, P.reason, P.park, P.parkCopy, P.parkWhy, P.unpark, P.unparkCopy, P.unparkWhy);

    // attachments
    P.attachBox = el("div", "act");
    P.attachBox.append(el("div", "field-label", "Attachments"));
    P.file = el("input");
    P.file.type = "file";
    P.file.multiple = true;
    P.file.setAttribute("aria-label", "Choose files to attach");
    P.file.addEventListener("change", () => {
      if (!selected) return;
      const list = staged.get(selected) || [];
      for (const f of P.file.files) list.push(f);
      staged.set(selected, list);
      P.file.value = "";
      refreshPanel();
    });
    P.attachCopy = el("span", "copy", "attached: lands the file for this card's next dispatch; never enters a live run. Choosing a file submits nothing until you press Attach.");
    P.files = el("div", "files");
    P.attachBox.append(P.file, P.attachCopy, P.files);

    P.err = el("div", "why");
    P.resp.append(lbl, P.draft, P.draftCopy, row, P.parkBox, P.attachBox, P.err);

    P.rail = el("div", "rail");
    P.rail.setAttribute("aria-label", "Receipts");
    panelEl.append(head, body, P.rail);
    for (const [name, node] of Object.entries(P)) {
      if (["button", "textarea", "input"].includes(node.tagName.toLowerCase())) key(node, "panel:" + name);
    }
    key(lower, "panel:lower");
    panelBuilt = true;
  }

  function select(name) {
    if (!panelBuilt) buildPanel();
    selected = name;
    const card = cardByName(name);
    if (!card) return;
    panelEl.hidden = false;
    P.draft.value = store.get(draftKey(name)) || "";
    P.reason.value = store.get(reasonKey(name)) || "";
    P.err.textContent = "";
    if (data) render(data);
    updatePanel(card);
    const chosen = Array.from(board.querySelectorAll(".card")).find(n => n.dataset.name === name);
    if (chosen) chosen.scrollIntoView({ block: "nearest" });
    P.binding.focus({ preventScroll: true });
    say("Panel open for " + card.id + " " + card.title);
  }

  function lowerPanel() {
    if (!selected) return;
    const previous = selected;
    selected = null;
    panelEl.hidden = true;
    if (data) render(data);
    restoreFocus("card:" + previous);
    say("Panel lowered; drafts and pending intents kept.");
  }
  document.addEventListener("keydown", ev => { if (ev.key === "Escape" && selected) lowerPanel(); });

  function refreshPanel() { const c = cardByName(selected); if (c) updatePanel(c); }

  function grantText(next) {
    if (!next) return null;
    const who = next.adapter ? next.adapter.charAt(0).toUpperCase() + next.adapter.slice(1) : "adapter";
    const model = next.model ? ", model " + next.model : "";
    if (next.tools) {
      return who + " with shell access (" + next.tools.join(", ") + "); everything else is denied. A prompt gate, not a filesystem sandbox" + model;
    }
    return who + " in its " + next.sandbox + " sandbox" + model;
  }

  function updatePanel(card) {
    const focused = focusKey();
    const a = card.actions;
    const pend = pendingOf(card);
    const pendingKinds = new Set(pend.map(r => r.kind));
    const next = card.posture && card.posture.next;

    P.binding.replaceChildren(el("strong", "", card.id + " · " + card.title),
      el("span", "subtle", card.stage + " · " + card.hands + " · " + card.track +
        (card.parked ? " · Decided · parked" : "")));

    // zone 1: context
    P.ctx.replaceChildren(el("h3", "", "Context"));
    const contract = card.contract;
    if (card.parked) P.ctx.append(el("div", "callout park", "Decided · parked: " + card.parked));
    if (card.condition && card.condition !== "parked" && card.condition_reason) {
      P.ctx.append(el("div", "callout", card.condition + ": " + card.condition_reason));
    }
    P.blocked.replaceChildren();
    if (a.review && a.review.ok) {
      P.blocked.append(el("p", "copy", "Review verified commit " + card.review_commit + ". Add a review note in Your response below."));
      P.blocked.append(el("p", "copy", "Reject — blocked: Card stays in Review, blocked, with your note. Next: Re-run (approve) or adjudicate."));
      for (const [result, label] of [["pass", "Accept — Done"], ["fail", "Reject — blocked"]]) {
        const button = el("button", "", label);
        button.type = "button";
        const why = pendingKinds.has("review") ? "A review decision is already pending." : !P.draft.value.trim() ? "Type a review note first." : "";
        button.addEventListener("click", () => post("review", { result, note: P.draft.value, commit: card.review_commit }));
        action(P.blocked, button, why, "panel:review:" + result);
      }
    }
    if (a.requeue_interrupted && a.requeue_interrupted.ok) {
      const requeue = el("button", "", "Re-queue interrupted run with note");
      requeue.type = "button";
      const requeueWhy = pendingKinds.has("requeue_interrupted") ? "A recovery request is already pending." : !P.draft.value.trim() ? "Type a recovery note first." : "";
      requeue.addEventListener("click", () => post("requeue_interrupted", {
        run_id: card.posture.live.run_id, note: P.draft.value
      }));
      action(P.blocked, requeue, requeueWhy, "panel:requeue_interrupted");
      P.blocked.append(el("p", "copy", "Use Your response below for the required recovery note. Returns to Queued; separate approval is required before dispatch. Confirm the old worker has stopped before approving."));
    }
    const recovery = card.recovered_work;
    const needsRerun = recovery && !recovery.superseded_by;
    if (recovery) {
      P.ctx.append(el("p", "copy", "Recovered commit " + recovery.commit + " · " + recovery.directory +
        (needsRerun
          ? ". Unverified: it cannot be accepted. Re-run to verify starts fresh from the base; it does not resume this commit."
          : ". Superseded by verified commit " + recovery.superseded_by + ".")));
    }
    const detail = card.blocked_details;
    if (detail) {
      P.ctx.append(el("h3", "", "Verification: " + detail.cause), el("p", "", detail.explanation));
      const list = el("ul");
      detail.discrepancies.forEach(d => list.append(el("li", "", d.path + " · " + d.kind)));
      P.ctx.append(list);
      Object.entries(detail.links).forEach(([label, href]) => {
        const link = key(el("a", "evidence-link", label), "panel:record:" + label); link.href = href; P.ctx.append(link);
      });
      if (a.recover && a.recover.ok && detail.recovery) {
        const r = detail.recovery;
        const recover = el("button", "", "Recover and review — " + (r.branch || "detached HEAD") + " @ " + r.commit.slice(0, 7));
        recover.type = "button";
        const recoverWhy = pendingKinds.has("recover") ? "A recovery request is already pending." : "";
        recover.addEventListener("click", () => post("recover", { run_id: detail.run_id, commit: r.commit }));
        action(P.blocked, recover, recoverWhy, "panel:recover");
        P.blocked.append(el("p", "copy", "Record the existing commit in " + r.directory + " and move the card to Review. This unverified commit cannot be accepted; Re-run to verify requires separate approval and starts fresh from the base. Recovery does not dispatch."));
      }
    }
    if (card.rejected_review) {
      P.ctx.append(el("h3", "", "Rejected review"), el("div", "ptext", card.rejected_review.note || "No review note recorded."));
    }
    const decision = card.rejected_review || detail;
    if (decision) {
      if (a.adjudicate && a.adjudicate.ok) {
        const label = el("label", "field-label", "Adjudication note (required; use Your response below)");
        label.htmlFor = "draft";
        const adjudicate = el("button", "", "Mark adjudicated with note");
        adjudicate.type = "button";
        const adjudicateWhy = pendingKinds.has("adjudicate") ? "An adjudication request is already pending." : !P.draft.value.trim() ? "Type an adjudication note first." : "";
        adjudicate.addEventListener("click", () => post("adjudicate", { run_id: decision.run_id, note: P.draft.value }));
        P.blocked.append(label);
        action(P.blocked, adjudicate, adjudicateWhy, "panel:adjudicate");
        P.blocked.append(el("p", "copy", card.rejected_review
          ? "Records your adjudication note and clears the block. The rejection remains recorded; fresh verification and a commit are required before acceptance. Nothing dispatches."
          : "Human override: records your note and sends the item to review. Verification evidence remains available; nothing dispatches."));
      }
    }
    if (card.question_text) {
      P.ctx.append(el("div", "callout ask", "Open question — the agent's words:"), el("div", "ptext", card.question_text),
        el("div", "note-line", "Claimed: the text is the agent's. Recorded: that it asked."));
    } else {
      P.ctx.append(el("div", "ptext", "No open question. Latest: " + card.last_event.text));
    }
    if (contract) {
      P.ctx.append(el("h3", "", "Task contract"), el("div", "ptext", contract.brief),
        el("h3", "", "Expected files"), el("div", "ptext", contract.expected_files.join("\n") || "(none)"),
        el("h3", "", "Test command"), el("div", "ptext", contract.test_command || "(none)"),
        el("h3", "", "Repository destination"), el("div", "ptext", contract.repo
          ? contract.repo + " · branch: " + contract.branch : "Local work repository (no remote destination)"));
    } else {
      P.ctx.append(el("div", "callout warn", "Task contract unavailable; refresh after correcting the brief."));
    }
    P.approve.textContent = needsRerun && card.stage === "Review" ? "Re-run to verify" : decision ? "Re-run — approve & dispatch" : "Approve & dispatch";
    if (card.annotation) P.ctx.append(el("div", "copy", "✎ " + card.annotation.author + ": " + card.annotation.text));

    // zone 2: response + decision
    const hasQuestion = a.answer.ok;
    P.send.textContent = hasQuestion ? "Send answer" : "Post note (annotation)";
    P.sendCopy.textContent = hasQuestion
      ? "message (answers the question): saves these words as the answer; does not dispatch."
      : "message (annotation): adds these words to the card; answers nothing and dispatches nothing.";
    const blankDraft = !P.draft.value.trim();
    unavailable(P.send, blankDraft ? "Nothing to send: the draft is empty." : "");
    P.sendWhy.textContent = blankDraft ? "Nothing to send: the draft is empty." : "";

    const grant = grantText(next);
    P.approveCopy.textContent = grant
      ? "brief_approved: authorizes one dispatch to " + grant + ". Uses landed inputs only; sends no draft."
      : "";
    const unsent = hasQuestion && !blankDraft;
    const msgPending = pend.some(r => r.kind === "message");
    const unanswered = hasQuestion && blankDraft && !msgPending;   // open question, no draft, nothing pending
    P.approveWarn.hidden = !(unsent || msgPending || unanswered);
    P.approveWarn.textContent = msgPending
      ? "Your message has not landed yet. The daemon lands it first, in order; if it is refused or fails, approval is refused too."
      : unsent
        ? "Your answer has not landed: it is only a draft. Approval will dispatch without it."
        : "No answer has landed for this question. Approval will dispatch the brief unanswered — the agent will likely ask the same question again.";
    let approveWhy = "";
    if (!a.approve.ok) approveWhy = a.approve.why;
    else if (!contract) approveWhy = "Task contract unavailable.";
    else if (!next) approveWhy = "Next-run posture is unavailable; dispatch stays disabled rather than guessing.";
    else if (pendingKinds.has("approve")) approveWhy = "An approval for this card is already pending.";
    unavailable(P.approve, approveWhy);
    P.approveWhy.textContent = approveWhy;
    P.approve.disabled = !!approveWhy;
    P.approve.className = approveWhy ? "" : "primary";
    if (approveWhy) P.approveCopy.textContent = approveWhy;
    if (approveWhy) P.approveWarn.hidden = true;

    // park / unpark: a parked card offers only Unpark; any other card offers Park
    const parked = !!card.parked;
    P.reason.hidden = P.park.hidden = P.parkCopy.hidden = P.parkWhy.hidden = parked;
    P.parkBox.querySelector("label").hidden = parked;
    P.unpark.hidden = P.unparkCopy.hidden = P.unparkWhy.hidden = !parked;
    if (!parked) {
      let why = "";
      if (!a.park.ok) why = a.park.why;
      else if (!P.reason.value.trim()) why = "A reason is required; nothing is parked without one.";
      else if (pendingKinds.has("park")) why = "A park request for this card is already pending.";
      unavailable(P.park, why);
      P.parkWhy.textContent = why;
      P.reason.readOnly = !a.park.ok;
      P.reason.setAttribute("aria-disabled", String(!a.park.ok));
    } else {
      unavailable(P.unpark, pendingKinds.has("unpark") ? "An unpark for this card is already pending." : "");
      P.unparkWhy.textContent = pendingKinds.has("unpark") ? "An unpark for this card is already pending." : "";
    }
    renderFiles(card, pend);

    // zone 3: posture (display only; the grant is communicated on the approve control)
    P.post.replaceChildren(el("h3", "", "Run permissions"));
    const live = card.posture && card.posture.live;
    const snap = el("div", "snap");
    if (live) {
      snap.append(el("b", "", live.running ? "Live run" : "Last run"), document.createTextNode(
        (live.run_id ? " (" + live.run_id + ")" : "") + ": " + live.adapter + (live.model ? ", model " + live.model : "") + ", " + live.sandbox));
    } else {
      snap.append(el("b", "", "No run yet"), document.createTextNode(": nothing has been dispatched for this card."));
    }
    P.post.append(snap);
    const nx = el("div", "snap next");
    if (next) {
      nx.append(el("b", "", "Next dispatch"), document.createTextNode(": " + grantText(next) + "."));
    } else {
      nx.append(el("b", "", "Next dispatch"), document.createTextNode(": posture unavailable; dispatch stays disabled rather than guessing."));
    }
    P.post.append(nx, el("div", "copy",
      "Display only. Approving authorizes exactly the next-dispatch grant above; it never changes a live run. Per-item posture toggles are not part of this panel."));

    renderRail(card);
    for (const name of ["send", "approve", "park", "unpark"]) {
      P[name + "Why"].id = "why-panel-" + name;
      P[name].setAttribute("aria-describedby", P[name + "Why"].id);
    }
    P.reason.setAttribute("aria-describedby", P.parkWhy.id);
    restoreFocus(focused);
  }

  function renderFiles(card, pend) {
    P.files.replaceChildren();
    const have = new Set(card.attachments.map(x => x.file));
    for (const f of (staged.get(card.name) || [])) {
      if (!fileKeys.has(f)) fileKeys.set(f, uuid());
      const row = el("div", "file-row staged");
      const over = f.size > MAX_ATTACHMENT_BYTES;
      row.append(el("span", "", f.name + " (" + f.size + " bytes)"), el("span", "tag", over ? "too large · 5 MiB limit" : "staged · not submitted"));
      const go = el("button", "", "Attach");
      go.type = "button";
      const why = over ? "File exceeds the 5 MiB attachment limit." : "";
      go.addEventListener("click", async () => {
        const buf = await f.arrayBuffer();
        const ok = await post("attach", { name: f.name, content_b64: bytesToBase64(buf) });
        if (ok) { const l = (staged.get(card.name) || []).filter(x => x !== f); staged.set(card.name, l); refreshPanel(); }
      });
      const drop = el("button", "", "Discard");
      drop.type = "button";
      drop.addEventListener("click", () => { staged.set(card.name, (staged.get(card.name) || []).filter(x => x !== f)); refreshPanel(); });
      action(row, go, why, "file:" + card.name + ":" + fileKeys.get(f) + ":attach");
      key(drop, go.dataset.focusKey + ":discard");
      row.append(drop);
      P.files.append(row);
    }
    for (const r of pend.filter(x => x.kind === "attach" && !have.has(x.summary))) {
      const row = el("div", "file-row pending");
      row.append(el("span", "", r.summary), el("span", "tag", "pending · not in log"));
      P.files.append(row);
    }
    for (const at of card.attachments) {
      const delivered = at.state === "delivered";
      const row = el("div", "file-row" + (delivered ? " delivered" : ""));
      row.append(el("span", "", at.file + " (" + at.bytes + " bytes)"),
        el("span", "tag", delivered ? "delivered · in a run's workspace" : "attached · rides the next dispatch"));
      if (!delivered) {
        const rm = el("button", "", "Remove");
        rm.type = "button";
        rm.title = "attachment_removed: removes it before delivery; a delivered file cannot be recalled.";
        const why = pend.some(x => x.kind === "remove_attachment" && x.summary === at.file) ? "Removal of this attachment is already pending." : "";
        rm.addEventListener("click", () => post("remove_attachment", { name: at.file }));
        action(row, rm, why, "file:" + card.name + ":" + at.file + ":remove");
      }
      P.files.append(row);
    }
    if (!P.files.childNodes.length) P.files.append(el("span", "copy", "No attachments on this card."));
  }

  function renderRail(card) {
    P.rail.replaceChildren(el("div", "rail-title", "Receipts · " + card.id + " · state comes from the daemon's outcome and the log, never from the click"));
    const list = (card.receipts || []).slice().reverse();
    if (!list.length) { P.rail.append(el("div", "copy", "No intents yet for this card.")); return; }
    for (const r of list) P.rail.append(receiptRow(r));
  }

  function receiptRow(r) {
    const row = el("div", "receipt " + r.state);
    const when = r.ts ? r.ts.slice(11, 19) : "";
    let label;
    if (r.state === "pending") label = "… Pending · not in log";
    else if (r.state === "reconciling") label = "… Commit status unknown — reconciling";
    else if (r.state === "landed") label = "■ Landed · recorded" + (r.event ? " · " + r.event.type + " #" + r.event.seq : "");
    else if (r.state === "refused") label = "✕ Refused · no event appended";
    else label = "! Failed · retryable";
    const what = r.kind === "create" && r.filed ? "Filed " + r.filed : (KIND_LABEL[r.kind] || r.kind) + (r.summary ? " — " + r.summary : "");
    row.append(el("span", "state", label), el("span", "rtext", what + " · " + when));
    if (r.reason) row.append(el("span", "rtext", "Reason: " + r.reason));
    if (r.state === "failed" && payloads.has(r.id) && (!archive || r.kind === "track_reopen")) {
      const retry = el("button", "", "Retry same intent");
      key(retry, "receipt:" + r.id + ":retry");
      retry.type = "button";
      retry.title = "Resends this failed intent with the same id; creates no duplicate event.";
      const err = el("span", "why");
      retry.addEventListener("click", () => send(payloads.get(r.id), r.kind === "create" ? I.err : r.kind.startsWith("track_") ? err : P.err, r.kind));
      row.append(retry, err);
    }
    return row;
  }

  // ---- intake: the "New work" form (board-level; it binds to no card) ----
  const FIELDS = ["title", "track_select", "track_new", "adapter", "body", "expected", "test", "repo", "branch",
                  "timeout", "quiet", "links", "context"];
  const I = { built: false, f: {} };
  let lastResult = null;

  function lines(v) { return v.split("\n").map(x => x.trim()).filter(Boolean); }
  function formValues() { const v = {}; for (const k of FIELDS) v[k] = I.f[k].value; return v; }
  function effectiveTrack(v) { return (v.track_new || "").trim() || v.track_select; }

  function formIntent(v) {
    const data = { title: v.title, track: effectiveTrack(v), adapter: v.adapter, body: v.body,
                   expected_files: lines(v.expected), test_command: v.test };
    if (v.repo.trim()) data.repo = v.repo.trim();
    if (v.branch.trim()) data.branch = v.branch.trim();
    if (v.timeout.trim() && !isNaN(parseFloat(v.timeout))) data.timeout_s = parseFloat(v.timeout);
    if (v.quiet.trim() && !isNaN(parseFloat(v.quiet))) data.quiet_after_s = parseFloat(v.quiet);
    if (lines(v.links).length) data.links = lines(v.links);
    if (lines(v.context).length) data.context = lines(v.context);
    return { id: uuid(), kind: "create", data: data };
  }

  function field(label, node, copy, wide) {
    const box = el("div", "ifield" + (wide ? " wide" : ""));
    const l = el("label", "field-label", label);
    node.id = node.id || "intake-" + label.toLowerCase().replace(/[^a-z]+/g, "-");
    l.htmlFor = node.id;
    box.append(l, node);
    if (copy) box.append(el("span", "copy", copy));
    return box;
  }
  function textInput(key, ph) {
    const n = el("input"); n.type = "text"; n.autocomplete = "off"; if (ph) n.placeholder = ph; n.id = "intake-" + key; I.f[key] = n; return n;
  }
  function area(key, rows, ph) {
    const n = el("textarea"); n.rows = rows; if (ph) n.placeholder = ph; n.id = "intake-" + key; I.f[key] = n; return n;
  }

  function buildIntake() {
    const root = document.getElementById("intake");
    const head = el("div", "intake-head");
    head.append(el("strong", "", "New work"), el("span", "subtle", "Board-level: binds to no card. Fills the whole shaping set; the daemon files it."));
    const close = el("button", "", "Close · keeps your draft");
    close.type = "button";
    close.addEventListener("click", () => { root.hidden = true; });
    head.append(close);

    const grid = el("div", "intake-grid");
    grid.append(field("Title (required)", textInput("title"), "The folder name is made from it."));
    const sel = el("select"); sel.id = "intake-track_select"; I.f.track_select = sel;
    const tbox = el("div", "ifield");
    const tl = el("label", "field-label", "Track (required)"); tl.htmlFor = "intake-track_select";
    tbox.append(tl, sel, textInput("track_new", "or a new track name"));
    I.trackCopy = el("span", "copy");
    tbox.append(I.trackCopy);
    grid.append(tbox);
    const ad = el("select"); ad.id = "intake-adapter"; I.f.adapter = ad;
    for (const [v, t] of [["", "choose an adapter…"], ["claude", "claude"], ["codex", "codex"]]) { const o = el("option", "", t); o.value = v; ad.append(o); }
    grid.append(field("Adapter (required)", ad, "Build items only; gate items are filed by reviewer."));
    grid.append(field("Brief (required)", area("body", 5, "What the agent should do. Stored verbatim."), "", true));
    grid.append(field("Expected files (required, one per line)", area("expected", 3, "sentinel.py\ntest_sentinel.py"),
      "Relative paths only. The header block is generated from this, never typed."));
    grid.append(field("Test command (required)", textInput("test", "python3 -m unittest"),
      "One line, run without a shell after the agent finishes."));
    const more = el("details", "ifield wide");
    more.append(el("summary", "", "Optional: repo, budgets, vault links and context"));
    const mg = el("div", "intake-grid");
    mg.append(field("Repo", textInput("repo", "git remote URL"), "Repo items push a review branch."),
              field("Branch", textInput("branch", "review/…"), "Never main or master."),
              field("Timeout (seconds)", textInput("timeout"), "Blank = the track's budget."),
              field("Quiet limit (seconds)", textInput("quiet"), "Blank = the track's budget."),
              field("Links (one vault path per line)", area("links", 2), "Graph only; ships nothing to the agent."),
              field("Context (one vault path per line)", area("context", 2), "Copied into the run's workspace."));
    more.append(mg);
    grid.append(more);

    I.needs = el("span", "why");
    I.submit = el("button", "primary", "File as new work");
    I.submit.type = "button";
    I.submit.addEventListener("click", async () => {
      const intent = formIntent(formValues());
      if (await send(intent, I.err, "create")) { draft.submitted(intent.id); lastResult = null; updateIntake(); }
    });
    I.err = el("div", "why");
    I.result = el("div", "intake-result");
    I.rail = el("div", "intake-rail");
    I.consequence = el("span", "copy intake-consequence");
    const act = el("div", "intake-act");
    act.append(I.submit, el("span", "copy", "File as a new item in Queued — not approved, nothing runs until approval."), I.consequence, I.needs, I.err);
    root.replaceChildren(head, grid, act, I.result, I.rail);

    for (const k of FIELDS) {
      const n = I.f[k];
      n.addEventListener(n.tagName === "SELECT" ? "change" : "input", () => {
        if (lastResult && lastResult.ok) lastResult = null;
        draft.save(formValues());
        updateIntake();
      });
    }
    const saved = draft.load();
    I.pendingRestore = saved;
    I.built = true;
  }

  function fillForm(v) { for (const k of FIELDS) I.f[k].value = (v && v[k]) || ""; }

  function openIntake() {
    if (!I.built) buildIntake();
    document.getElementById("intake").hidden = false;
    updateIntake();
    I.f.title.focus();
  }

  function updateIntake() {
    if (!I.built) return;
    // track choices: the tracks on the board, plus whatever the draft already picked
    const sel = I.f.track_select;
    const want = I.pendingRestore ? I.pendingRestore.track_select : sel.value;
    const names = data ? data.tracks.map(t => t.id) : [];
    const sig = names.join("\n");
    if (I.trackSig !== sig) { // rebuilt only when the tracks changed, so an open dropdown is not yanked away
      sel.replaceChildren();
      const blank = el("option", "", "choose an existing track…"); blank.value = ""; sel.append(blank);
      for (const n of names) { const o = el("option", "", n); o.value = n; sel.append(o); }
      I.trackSig = sig;
    }
    if (I.pendingRestore) { fillForm(I.pendingRestore); I.pendingRestore = null; }
    sel.value = names.includes(want) ? want : "";
    const v = formValues();
    const track = effectiveTrack(v);
    I.trackCopy.textContent = !track ? "Pick a track, or type a new name: a track exists when its first item does."
      : names.includes(track) ? "Joins the existing track " + track + "; its budgets apply."
      : "Begins a new track " + track + ", filed with its first item and the default budgets.";
    const lifecycle = data && data.tracks.find(t => t.id === track);
    const consequence = intakeConsequence(lifecycle);
    I.consequence.textContent = consequence;
    const missing = [];
    if (!v.title.trim()) missing.push("title");
    if (!track) missing.push("track");
    if (!v.adapter) missing.push("adapter");
    if (!v.body.trim()) missing.push("brief");
    if (!lines(v.expected).length) missing.push("expected files");
    if (!v.test.trim()) missing.push("test command");
    I.submit.disabled = missing.length > 0 || pendingCreate() || (lifecycle && lifecycle.state === "ended");
    I.needs.textContent = lifecycle && lifecycle.state === "ended" ? consequence : missing.length ? "Still needed: " + missing.join(", ") + "." :
      (I.submit.disabled ? "A request is already pending: wait for the daemon." : "");
    // the outcome of the last submission, read from the daemon's receipts, never from the click
    const r = data ? draft.resolve(data.board_receipts || []) : null;
    if (r && r.state === "landed") { fillForm(null); lastResult = { ok: true, text: "Filed " + r.filed + " in Queued. It is not approved." }; }
    else if (r) lastResult = { ok: false, text: (r.state === "refused" ? "Refused · nothing filed: " : "Failed · retryable: ") + r.reason };
    I.result.replaceChildren();
    if (lastResult) I.result.append(el("div", lastResult.ok ? "callout ok" : "callout", lastResult.text));
    I.rail.replaceChildren(el("div", "rail-title", "New-work receipts · state comes from the daemon's outcome and the log"));
    for (const rc of ((data && data.board_receipts) || []).slice().reverse().slice(0, 6)) I.rail.append(receiptRow(rc));
    if (!(data && data.board_receipts && data.board_receipts.length)) I.rail.append(el("div", "copy", "Nothing filed from the floor yet."));
  }

  function pendingCreate() {
    return !!(data && (data.board_receipts || []).some(r => r.state === "pending" || r.state === "reconciling"));
  }

  document.getElementById("new-work").addEventListener("click", openIntake);

  // ---- refresh loop ----
  let timer = null;
  let ticking = false;
  async function tick() {
    if (ticking) return;
    ticking = true;
    try {
      const res = await fetch("/cards.json", { cache: "no-store" });
      const d = await res.json();
      if (!res.ok) throw new Error(d.error || ("HTTP " + res.status));
      banner.hidden = true;
      const outcomes = [];
      const observe = (r, card) => {
        const previous = receiptStates.get(r.id);
        if (receiptsSeen && previous !== r.state && ["landed", "refused", "failed"].includes(r.state)) {
          outcomes.push((card ? card.id + " · " : "") + (KIND_LABEL[r.kind] || r.kind) + (r.summary ? " — " + r.summary : "") + ": " + r.state + (r.reason ? ". " + r.reason : ""));
        }
        if (card && r.state === "landed" && ["review", "adjudicate", "recover", "requeue_interrupted"].includes(r.kind)) {
          let decision = decisionDrafts.get(card.name);
          if (!decision) try { decision = JSON.parse(store.get("depot.decision." + card.name)); } catch (e) {}
          const saved = selected === card.name && panelBuilt ? P.draft.value : store.get(draftKey(card.name));
          // Preserve edits made while the decision was pending; never clear on refusal or failure.
          if (decision && decision.id === r.id && (!saved || saved === decision.draft)) {
            store.del(draftKey(card.name));
            if (selected === card.name && panelBuilt) P.draft.value = "";
          }
          if (decision && decision.id === r.id) {
            decisionDrafts.delete(card.name);
            store.del("depot.decision." + card.name);
          }
        }
        receiptStates.set(r.id, r.state);
      };
      for (const c of d.cards) for (const r of c.receipts || []) observe(r, c);
      for (const t of d.tracks) for (const r of t.receipts || []) observe(r);
      for (const r of d.board_receipts || []) observe(r);
      receiptsSeen = true;
      if (outcomes.length) say(outcomes.join(". "));
      data = d;
      for (const card of d.cards) {
        const key = "depot.review.pending." + card.name;
        let pending;
        try { pending = pendingReviews.get(card.name) || JSON.parse(store.get(key)); } catch (e) { continue; }
        if (!pending) continue;
        const receipt = (card.receipts || []).find(r => r.id === pending.id);
        if (!receipt) continue;
        if (receipt.state === "landed") {
          if (store.get(draftKey(card.name)) === pending.note) store.del(draftKey(card.name));
          if (selected === card.name && panelBuilt && P.draft.value === pending.note) P.draft.value = "";
          store.del(key);
          pendingReviews.delete(card.name);
        } else if (receipt.state === "refused") {
          store.del(key);
          pendingReviews.delete(card.name);
        }
      }
      render(d);
      document.getElementById("subhead").textContent =
        "Work moves by events. Dwell is time in the current stage. Updated " + d.now.slice(11, 19) + " UTC.";
      if (selected) {
        const c = cardByName(selected);
        const track = c && d.tracks.find(t => t.id === c.track);
        if (c && !archive && track && !["tabled", "ended"].includes(track.state)) updatePanel(c); else lowerPanel();
      }
      updateIntake();
    } catch (err) {
      banner.hidden = false;
      banner.textContent = "The floor cannot be drawn: " + err.message + " (showing the last good view, if any)";
    } finally {
      ticking = false;
      clearTimeout(timer);
      const anyPending = data && (data.cards.some(c => pendingOf(c).length) || data.tracks.some(t => pendingOf(t).length) || (data.board_receipts || []).some(r => ["pending", "reconciling"].includes(r.state)));
      timer = setTimeout(tick, anyPending ? FAST_MS : REFRESH_MS);
    }
  }

  tick();
}

if (typeof document !== "undefined") boot();
