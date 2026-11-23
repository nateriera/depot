# Depot launch-gate walkthrough — Nov 23, 2026

Disposable-repository walkthrough named in the public README's general-launch note: approval, execution, exact-commit verification, human acceptance,
interruption, and recovery. Run on a disposable yard against a bare local
work repo. All times PT (UTC in the record).

## Setup

- Disposable yard `~/depot-walkthrough` (git, branch main, no remote),
  disposable work repo `~/depot-walkthrough-work.git` (bare, seeded main
  c8ca9dd, a README only).
- Walkthrough floor + poll run as backgrounded nohup processes (not systemd):
  floor on 127.0.0.1:8766, poll 60s cycle, `DEPOT_YARD=~/depot-walkthrough`.
  Live yard and services untouched throughout.
- Base code: depot-yard main at 580de71 (all seven pre-launch findings fixed,
  UI/UX batch 0030–0035 merged, suite 421/421).

## W1 — approval, execution, exact-commit verification, acceptance

Item `0001-walkthrough-hello`, branch `feat/walkthrough-hello`, timeout_s 900.
Brief: create hello.py with `greet()` returning "Hello" plus test_hello.py;
exactly two files; `python3 -m unittest`.

- The operator approved (intent 482540cb, 10:46 PT). Poll dispatched to Codex;
  exit 0, outcome pass in ~20s. Card moved to Review.
- Exact-commit verification, read firsthand from
  `runs/20261123T184629Z/verify.json`: result pass; expected_files ==
  changed_files == [hello.py, test_hello.py]; discrepancies [];
  test exit 0 (1 test OK); **candidate_tree == tree ==
  3fd248dc01d579d7a4e3feaa2edb4708bcd5c0cd**; commit
  4ddab6d4ebf1e8af0f5706f821b272a87f6a99c7 on `feat/walkthrough-hello`,
  base c8ca9dd. Committed-vs-work-dir bytes identical (hello.py
  sha256 bc3e2688…, test_hello.py a95b0a4e…).
- The operator accepted (~11:00 PT, review intent 2c9038d8, note "approved for
  codex"). Card **Done**.

## W2, W3, W4 — interruption attempts that missed

Three clean normal runs, each a failed interruption attempt. They are not
failures of the product: each run passed verification and sits in Review.
The misses taught the mechanism.

- **W2** (`0002-walkthrough-interrupt`, calc.py, timeout_s 120): The operator approved 11:06 PT; the kill round-trip was slower than the run — Codex
  finished in 30 seconds before the SIGTERM landed. Card Review, pass.
- **W3** (`0003-walkthrough-interrupt-2`, geometry.py, timeout_s 120):
  the kill watcher was armed with a 20-minute window that expired at
  ~11:27 PT, long before the operator approved at 13:17 PT. Codex finished in
  54 seconds. Card Review, pass. A setup defect (the watcher window was too short), owned at the time.
- **W4** (`0004-walkthrough-interrupt-3`, stats.py, timeout_s 120): the
  watcher SIGTERMed the poll in the same second as dispatch (21:35:35Z)
  — and the run still passed (exit 0, verified, committed a17c8c9a,
  pushed). Mechanism, read firsthand from depot.py:2351–2352: the poll
  installs a SIGTERM/SIGINT handler that only sets a stop flag, so the
  poll finishes the in-flight dispatch and then stops. **SIGTERM to the
  poll can never strand a run.** The agent also runs in its own session
  (depot.py:539, `start_new_session=True`), so a process-group kill of
  the poll would not touch it. Card Review, pass.

## W5 — genuine interruption, reconciliation, operator recovery

Item `0005-walkthrough-interrupt-4`, branch `feat/walkthrough-interrupt-4`,
timeout_s 120. Brief: textstats.py (word/sentence counts, average word
length, most common word; ValueError on empty input) plus test_textstats.py;
exactly two files; `python3 -m unittest`.

- **Interruption (13:41 PT):** The operator approved (intent 13f067ab, 21:41:26Z).
  The watcher SIGKILLed the walkthrough poll (pid 391443) in the same
  second as dispatch and SIGKILLed the agent's process group (child
  394910). Poll log ends at the dispatch line; W5's record ends at
  `dispatched` (run 20261123T214126Z); no `run_finished`, no heartbeat.
  The run was genuinely stranded. Poll restarted (pid 395979).
- **Reconciliation (13:48 PT):** the reconciler fired on the 21:48:45Z
  cycle — `run_interrupted`, run 20261123T214126Z, dispatched
  21:41:26.099Z, bound 420.0s (timeout_s 120 + 300s grace), reason "No
  run_finished recorded after the run timeout plus cleanup grace;
  operator recovery required." Yard commit d27421bc. Card **Interrupted**.
- **Operator recovery (13:50 PT):** A `requeue_interrupted` intent (9e58543c) was filed via an intent file dropped into `log/intents/` — the
  mechanical equivalent of the floor's Re-queue control, disclosed to the operator at the time. It landed 21:51:45Z (`interrupted_requeued`, yard
  commit a5b65802). Card **Queued**.
- **Design fact learned from the code** (depot.py:1053–1060): the
  re-queue resets the approval — a dispatch needs a `brief_approved`
  AFTER the `interrupted_requeued` event. One approval covers one
  dispatch, so the retry needed the operator's fresh approval. This is the
  single-use approval contract working as designed, not a defect.
- **Clean re-run (13:54 PT):** The operator re-approved; the poll dispatched
  21:54:45Z; the run finished exit 0, verified pass (zero discrepancies,
  6 tests OK), committed **4f641cd4b5fd45c281caa30df16c6b5f2565f69d** on
  `feat/walkthrough-interrupt-4`, pushed (remote_head == commit).
  Exact-commit binding confirmed firsthand from
  `runs/20261123T215445Z/verify.json`: expected_files == changed_files
  == [textstats.py, test_textstats.py]; **candidate_tree == tree ==
  3ab2c39e744e567ab9a92c075061b03492a10178**. Card Review.
- **Acceptance (13:56 PT):** The operator accepted — `reviewed`, result pass,
  note "approved", commit 4f641cd4, intent 3abbfeb5, 21:56:27Z.
  Card **Done**.

## Result

All six walkthrough steps demonstrated on the disposable repo:

1. Approval — W1, The operator's floor approval dispatched the run.
2. Execution — W1 and W5's re-run, Codex runs to completion.
3. Exact-commit verification — W1 (3fd248dc…) and W5's recovery run
   (3ab2c39e…): verified output is the committed output.
4. Human acceptance — W1 and W5, the operator's review intents.
5. Interruption — W5, SIGKILL mid-dispatch stranded the run with no
   `run_finished`; W4 additionally proved SIGTERM cannot strand one.
6. Recovery — W5, reconciler recorded `run_interrupted` at the 420s
   bound, operator re-queue, fresh approval, clean re-run, acceptance.

W2, W3, and W4 sit in Review as clean passes; they were not needed for
the walkthrough.

## Open

- The public README now carries the general-launch wording, and this note
  ships with the re-snapshot at 365b356.
- The walkthrough floor/poll processes and the disposable dirs are
  throwaway by design and are removed separately.
