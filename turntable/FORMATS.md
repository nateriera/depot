# Depot turntable formats (S1, as built)

All JSON is UTF-8. Timestamps are ISO 8601 UTC with milliseconds (`2026-10-30T21:04:05.123Z`), except `run_id`, which is a compact UTC stamp (`20261030T210405Z`).

## Waybill — `items/<id>-<slug>/waybill.json`

| field | meaning |
|---|---|
| `id`, `slug`, `title`, `track` | identity; `id` is sequential and zero-padded (`0001`) |
| `created` | ISO 8601 |
| `brief` | yard-relative path to `brief.md` |
| `adapter` | default `"codex"`. Absent only on an unshaped vault capture (S9), which cannot be approved or dispatched until it is set. |
| `model` | pinned model string or `null` (passed as `-m` when set) |
| `timeout_s` | run timeout (default 900; stamped at filing, see Track budgets) |
| `quiet_after_s` | adapter-quiet limit before the floor raises `stalled?` (default 300; stamped at filing) |
| `expected_files` | list of paths; copied from the brief header by `approve` and `dispatch` |
| `test_command` | string; copied from the brief header the same way; run without a shell |
| `repo` | optional. Git remote URL for a repo-targeted item; absent or `null` means local-only (S1 behaviour). Written only when `new-item --repo` is given, and only together with `branch`. |
| `branch` | optional. The review branch for the return push. Never `main` or `master`. |
| `links` | optional (S9). Vault-relative note paths (`"Contacts/example-contact.md"`) rendered as wikilinks in the item's projection note. Graph only: a link ships nothing to an agent. Absent means empty. |
| `context` | optional (S9). Vault-relative note paths copied into the work dir at dispatch (see The vault bridge). Inclusion only. Absent means empty. |
| `kind` | optional. `"build"` (default when absent) or `"gate"`. |
| `target` | gate items only (required): the name of the item being gated. |
| `materials` | gate items only, optional: extra materials beyond the default five (see Gate items). |
| `status` | derived cache: `queued\|approved\|dispatched\|returned\|verified\|blocked`. Events are the truth. |

Status derivation (last matching event wins): `item_created`→queued, `brief_approved`→approved, `dispatched`→dispatched, `run_finished`→returned, `input_required`→blocked, `verified` with `result: "pass"`→verified, `verified` with any other result→blocked.; also `push_failed`→blocked, `reviewed` with `result: "pass"`→reviewed (any other result→blocked), and `gate_result` with a result other than `pass`→blocked (a `pass` leaves the status unchanged). `verified` results are `pass`, `discrepancies`, `tests_failed`, `workspace_changed` and `commit_failed`. 0026: `run_interrupted`→interrupted, `interrupted_requeued`→queued (requires fresh approval). 0022: `adjudicated` and `recovered`→review (human review, no dispatch). S4: `reopened`→queued and `blocked`→blocked; `message` never changes the status. Readers treat a missing `repo`, `branch`, `kind` or `target` as null/`"build"`.

## Event — one JSON object per line

Appended to both `items/<id>-<slug>/events.jsonl` and `log/events.jsonl`. A `gate_result` is appended to the target's log, not the gate item's.

`{"ts", "item", "type", "actor", "data"}` — `item` is the item directory name; `data` is an object.

| type | actor | data |
|---|---|---|
| `item_created` | operator | `title`, `track` |
| `brief_approved` | operator | `contract_fingerprint` (from the floor panel: `via: "floor"`, `directed_by: "operator"`, `intent`) |
| `dispatched` | turntable | `adapter`, `model`, `sandbox`, `timeout_s`, `run_id`, `contract_fingerprint`, `approval_seq`, and `attachments` (`[{file, sha256}]`, only when attachments were delivered) |
| `heartbeat` | turntable | `since_last_adapter_event_s`, `elapsed_s` (at least every 30s while the run lives) |
| `run_interrupted` | turntable | `run_id`, `dispatched_at`, `bound_s`, `reason`; see Interrupted runs (0026) |
| `interrupted_requeued` | operator | `run_id`, required `note`, `via`, `intent`; Queued, fresh approval required |
| `run_finished` | turntable | `exit_code`, `elapsed_s`, `timed_out` |
| `claimed` | adapter name | `report` (yard-relative path to `report.md`) |
| `input_required` | adapter name | `question` (report text; emitted when the report ends with `?`; verification is then skipped), `run_id` (S4; readers fall back to the `run_id` of the `dispatched` event before it) |
| `adjudicated` | operator | `run_id`, `note` (required human override); `via`, `intent` |
| `recovered` | operator | `run_id`, `directory`, `branch` (null for detached HEAD), `commit`; `via`, `intent` |
| `verified` | turntable | `result` (`pass`, `discrepancies`, `tests_failed`, `workspace_changed`, or `commit_failed`; `pass` requires the pinned-tree verification/commit contract below, including successful tests or no `test_command`), `discrepancies`, `test_exit_code` |
| `committed` | turntable | `hash`, `files` |
| `pushed` | turntable | `repo`, `branch`, `remote_head` (read back with `git ls-remote` after the push), `commit`. Repo items only, after a passing `committed`. |
| `push_failed` | turntable | `branch`, `error` (git's text, or a read-back mismatch). No `pushed` follows; dispatch exits 1. |
| `gate_result` | gate item's adapter | appended to the **target** item's log: `result` (`pass`, `fail` or `unparseable`), `gate` (gate item name), `run_id`, `report`, and for `pass`/`fail` `criteria` (the report's `CRITERION` lines, verbatim). |
| `message` | operator | `text` (verbatim) (from the floor panel also `via: "floor"`, `intent`), and `answers_input_required` (a `run_id`) when it was written while the newest non-heartbeat event was an `input_required`. Never a state change. Written by `depot message`. |
| `reopened` | operator | `text` (verbatim; operator's message riding on the event). Written by `depot reopen`, only on a Done item. |
| `parked` | operator | `reason`. Written by the floor panel (S8) with `via: "floor"`, `intent`; the floor derivation also reads a `parked` written by hand. Sets condition `parked`; no stage change, and the card keeps its stage (an Awaiting operator card stays Awaiting operator). |
| `unparked` | operator | `via: "floor"`, `intent`. Clears the parked condition. Dispatches nothing and approves nothing. |
| `attached` | operator | `file`, `bytes`, `sha256`, `via: "floor"`, `intent`. The file is at `items/<id>/files/<file>`. |
| `attachment_removed` | operator | `file`, `via: "floor"`, `intent`. Only before the file was delivered. |
| `blocked` | turntable | `reason`. Appended when a dispatch is refused because an answered `input_required` carries no question text. Cleared by the next `brief_approved`. |
| `item_created` via floor intake | operator | `via: "floor"`, `intent` (the create intent's id), `track`. Written by the daemon's `create` handling (S10); the item lands Queued and unapproved. |
| `item_created` via vault capture | turntable | `title`, `track: "inbox"`, `via: "vault-capture"`, `source` (the capture's filename). Written by the daemon's capture scan (S9). |
| `reviewed` | operator or reviewer | Operator acceptance: `result` (`pass` or `fail`), `note` (required), `commit` (full verified work hash); floor also adds `via: "floor"`, `intent`. Existing connector reviews remain supported, including older records without a commit. |

Verification's changed set (S9) excludes the work dir's top-level `context/` directory (`context` and everything under it, nothing else: `src/context/x` and `contextual.py` still count). It holds vault notes copied in as reference, never deliverables. The exclusion applies to every run; a run with no context has no such directory, so nothing changes for it. When notes are delivered, `context/` is also added to the work repo's `.git/info/exclude`, so `diff.patch` and the host-side commit never see it.

Normal order: `item_created, brief_approved, dispatched, (heartbeat…), run_finished, claimed, verified, committed` and, for repo items, `pushed`. Without a passing verification there is no `committed` (or `pushed`). A gate item's own trail is `…dispatched, run_finished, claimed` only.

## Brief header

`brief.md` begins with a fenced block tagged `depot` containing one JSON object, followed by the human brief:

    ```depot
    {"expected_files": ["a.py"], "test_command": "python3 -m unittest"}
    ```

`expected_files` must be a list of strings and `test_command` a string. The turntable reads only this block. The block is removed from the text sent to the agent on stdin.


## Single-use task approval

`brief_approved.data.contract_fingerprint` authorizes exactly one dispatch of the
approved contract. Both `depot approve` and the panel's daemon-side approval
handler write it. The panel displays the full current brief (without clipping),
expected files, test command, and repository URL and branch before approval.
Local-only work is labelled as a local work repository with no remote destination.
The panel submits the displayed `contract_fingerprint`; the daemon refuses it if
the contract changed before the intent landed. A Briefed card whose contract has
changed permits a fresh approval. Missing or invalid briefs disable panel approval.

The fingerprint is lowercase SHA-256 of UTF-8 encoded canonical JSON with these
five keys: `brief`, `expected_files`, `test_command`, `repo`, `branch`. `brief` is
all of `brief.md` as read as text (including the depot header and whitespace;
Python text reading normalizes line endings). Expected files and test command
come from that header, the authoritative source; their waybill fields are mirrors.
Repository and branch come from the waybill, with absent values represented as
JSON `null`. Canonical JSON uses sorted keys, separators `(",", ":")`, and
`ensure_ascii=True` (Python `json.dumps`). List order is preserved. There is no
trimming, sorting of paths, or normalization of repository/branch strings.

All dispatch callers go through `dispatch_item`. It requires the newest approval
after the newest reopening, rejects it if any later `dispatched` event exists,
and checks its fingerprint against the same contract snapshot used for the run.
Old approvals without a fingerprint require explicit re-approval. Dispatches are
serialized per item with an advisory exclusive lock on its existing event log;
an overlapping attempt is refused. The `dispatched` event consumes approval,
before starting the adapter, and records `contract_fingerprint` and `approval_seq`
(the approval's one-based line number in the item's event log). Adapter failures,
timeouts, and verification failures still consume it. A pre-dispatch refusal
writes no consumption event and leaves an otherwise matching approval usable.
Re-approval is always explicit; no approval or dispatch is created automatically
by a contract edit. This does not change sandbox posture or the records rule.

## Run record — `items/<id>-<slug>/runs/<run-id>/`

- `manifest.json`: `run_id`, `adapter`, `model`, `sandbox`, `started`, `elapsed_s`, `exit_code` (124 on timeout kill, 127 if the adapter could not start), `session_or_thread_id` (from the adapter's `thread.started` event), `usage` (verbatim from the adapter's last event carrying `usage`), `context` (S9: `[{path, sha256}]` of the vault notes copied into the run, in waybill order; `[]` when none, so a run's manifest always says what the agent was handed), `artifacts` (file names in the run dir), plus `timed_out` (boolean; addition to the brief).
- `events.jsonl`: the adapter's raw event stream.
- `report.raw`: what `codex exec -o` wrote. `report.md`: verbatim copy (empty if none was written).
- `stderr.log`: adapter stderr (addition to the brief).
- `verify.json`: `ts`, `result`, `expected_files`, `changed_files`, `discrepancies` (`{"path", "kind": "extra"|"missing"}`), `test` (`command`, `exit_code`, `tail`), `commit` (hash or `null`), `base_commit` (pinned starting commit or `null` for an unborn branch), `candidate_tree` (pre-test Git tree hash), `tree` (post-test evidence tree hash). A publication failure also records `error`.
- `diff.patch`: a binary-capable, full-index diff from `base_commit` (or the empty tree) to `tree`, captured after tests. It includes additions, deletions, modes and exact blob bytes, and is empty when the trees match.

Verification pins the starting commit and ref before the adapter runs. After the adapter returns, the host builds a candidate tree using a private index seeded from that starting commit; the agent's existing index cannot supply extra files or alternate staged bytes. Tests run in the work directory against that candidate. The host captures the post-test tree, changed set and diff against the pinned base. Any change in the tree or starting commit/ref fails with `workspace_changed`, even if tests exited zero and the changed set still equals `expected_files`. Test-created expected files appear in the failed run's post-test evidence. A changed candidate is never silently accepted as a new tested baseline.

A pass requires an exact expected/changed set and successful tests (or no test command), with identical pre/post trees and an unchanged starting ref. The host rechecks the candidate before publication, creates the commit directly from the recorded immutable `tree` object, and advances the pinned ref only if it still has the starting commit. Publication errors fail with `commit_failed`; no pass event is emitted first. `verify.json.commit` identifies that commit, whose parent is `base_commit` and whose tree is exactly `verify.json.tree`. Applying `diff.patch` to the pinned base reconstructs that tree byte-for-byte. Repo publication pushes the recorded commit hash to the review branch, never a re-read local branch tip; remote read-back must match that hash. Existing context, attachment, sandbox and records rules continue to apply. Runs with an empty expected/changed set produce no new commit.

### Verification sandbox contract

Every nonempty `test_command` runs in a fresh Linux Bubblewrap sandbox, independently
of the selected agent adapter. It requires `/usr/bin/bwrap`, `libseccomp.so.2`, user
and PID namespace support, and seccomp filtering. Missing dependencies or sandbox
setup failures fail verification; Depot never retries tests with host permissions.
An empty command retains the existing `no test_command` result.

The sandbox sees the work directory as `/work` (its working directory), read-only
system runtimes under `/usr`, `/bin`, `/lib`, and `/lib64`, a private process view,
and minimal virtual devices. The filesystem root is read-only; `/work` is the only
writable host directory. Host home directories, `/etc`, host temporary directories,
the yard's records, and publication credentials are absent. Symlinks cannot expose
paths outside this filesystem view. Repository metadata (`.git`) and conventional
credential/configuration paths (`.ssh`, `.aws`, `.codex`, `.agents`, `.config`,
`.netrc`, `.git-credentials`, `.npmrc`, `.pypirc`) anywhere in the work tree are
covered with empty read-only mounts. A symlink at one of those protected paths
fails sandbox setup. Other work-tree content is available to tests, so the work
tree must not contain secrets as ordinary source files.

The environment is built from scratch: `PATH=/usr/bin:/bin`, `HOME=/nonexistent`,
`TMPDIR=/work`, `LANG=C.UTF-8`, and `PYTHONDONTWRITEBYTECODE=1`; Bubblewrap sets
`PWD=/work`, and runtimes may derive locale variables. No daemon environment
variables, tokens, keys, auth sockets, Git settings, or credential-bearing paths
are inherited. Stdin is `/dev/null` and host file descriptors are not inherited.
There is no writable host `/tmp`; tests can use `TMPDIR` for temporary files.

User, PID, IPC, UTS, mount, and supported cgroup namespaces isolate the test
processes; capabilities are dropped and further user namespaces are disabled.
Network access is denied by a mandatory seccomp filter, including Internet and
Unix sockets and io_uring, even though the network namespace is shared. The filter
rejects other syscall architectures. Tests cannot contact local publication
services or SSH agents. Sandbox descendants die when the sandbox exits, including
on the existing test timeout (exit 124). Launch/setup errors return a failing test
exit (127 for host-side setup errors), recorded in the existing `verify.json` test
fields; no new record format or dispatch behavior is introduced.

Commit and push remain host-side operations after successful verification, using
the daemon's identity and credentials. Tests cannot access or change the host
repository metadata, invoke its hooks, or reach its remote or publication services.
Tests that need only the work tree and installed system runtimes continue to run
with the same command; tests requiring host configuration, networking, or external
write paths fail under this contract. Agent adapter grants are unchanged.

## Dispatch exit codes

0 run exit 0, verification passed, and (repo items) the push read back; 1 run failed, verification did not pass, or the push failed; 2 refusal or usage error; 3 input required. A gate item exits 0 when its run exited 0 and its verdict parsed (pass or fail), 1 otherwise.

## Adapters

The waybill's `adapter` selects a registry entry that supplies the command line and the event-stream parser (session id, usage, and for in-stream reporters the final message). Binaries are overridable for tests with `DEPOT_CODEX` and `DEPOT_CLAUDE`.

| adapter | command | session id | usage | final message |
|---|---|---|---|---|
| `codex` | `codex exec -s workspace-write --json -C <work> -o <run>/report.raw --skip-git-repo-check [-m <model>] -` | `thread_id` of `thread.started` | last event with `usage` | the `-o` file |
| `claude` | `claude -p --output-format stream-json --verbose --permission-prompts none --allowedTools Bash,Read,Write,Edit,Grep,Glob [--model <model>]`, brief on stdin, process cwd = the work dir | `session_id` (the result event's wins) | the `result` event's `usage`, verbatim | the `result` event's `result` text, written to `report.raw` |

**Posture asymmetry.** Codex runs in a sandbox (`workspace-write`, with `.git` read-only). Claude has no sandbox flag: its posture is a prompt gate with two halves. `--allowedTools Bash,Read,Write,Edit,Grep,Glob` pre-approves the local work tools, `Bash` itself (tool-level, so any shell command runs), so a build can run its commands. `--permission-prompts none` stays on, so anything not on that list (web tools, MCP tools, any future tool) is denied automatically without a prompt. The grant is the same for every item kind, builds and gates alike. The manifest's `sandbox` field records `workspace-write` for Codex and `allowed-tools+prompts-none` for Claude (items dispatched before S7 recorded `permission-prompts-none`). This is a permission posture, **not a filesystem sandbox**: the agent process runs as the user, and only the prompt gate constrains it. What Depot relies on instead is host-side verification of the exact changed set, and that pushes are the host's, never the agent's. The grant composes with `--permission-prompts none` on 2.1.316 (probed live: listed tools run, an unlisted `WebFetch` is denied without a prompt); a broader grant such as `--permission-mode bypassPermissions` is a different posture and is not used. `--verbose` is not optional: Claude Code 2.1.316 rejects `-p --output-format stream-json` without it. `--model`, when pinned, follows the grant, because `--allowedTools` takes a list and ends at the next flag. The model string is passed through verbatim.

## Repo-targeted items

With `repo` and `branch` set, the first dispatch clones `repo` into `work/`; later dispatches fetch. The branch is checked out if it exists locally, else tracked from `origin/<branch>`, else created from the remote's default branch (or unborn on an empty remote). The host-side commit lands on that branch. After a passing `committed`, the turntable runs `git push origin refs/heads/<branch>:refs/heads/<branch>` — never forced, never any other ref — then reads the remote head back with `ls-remote`; only a read-back equal to the commit yields `pushed`.

## Gate items

`kind: "gate"` with a `target`. The work dir is a fresh plain directory, rebuilt at each dispatch, holding copies of: the target's `brief.md`, and from the target's latest run `diff.patch` (when one exists), `verify.json`, `manifest.json` and `report.md`. Extra files come from the waybill's `materials` list (`new-item --material`): `item:<name>` is taken from the target's item dir (for example `item:events.jsonl`, the target's event log), `run:<name>` from its latest run dir (for example `run:probes.txt`), and each is copied under its base name. This extends the five-file default; it exists because gate criteria can name evidence outside it. Nothing of the target is opened for writing; its log gains only the `gate_result` line. Verification-lite, the test re-run and the host commit are skipped for gates, and the adapter defaults to `claude`.

The gate's report must end with one line `GATE: PASS` or `GATE: FAIL`, preceded by one line per criterion, `CRITERION <n>: PASS|FAIL — <evidence>`. Parsing is strict: exactly one `GATE:` line and it is the last non-empty line; at least one `CRITERION` line, each well formed; the verdict is `PASS` exactly when every criterion is `PASS`. Anything else records `gate_result` with `result: "unparseable"`.

## Poll daemon

`depot.py poll [--once] [--interval S]` (default 60s). One instance: `log/poll.lock` holds the PID; a second `poll` refuses while that PID lives and replaces a stale lock. Each cycle: (1) `git pull --ff-only` on the yard clone — a failed pull (including non-fast-forward or dirty pulls) logs the failure, local `main`'s ahead/behind counts against `origin/main` after the pull's fetch (`unknown` if unavailable), and the pull's stderr (stdout if stderr is empty). The failed cycle dispatches nothing and drains no intents; the daemon stays up, holds its lock, waits the normal interval, and retries the pull next cycle. Once an operator reconciles the clone, a clean pull resumes normal processing without a restart. The daemon never rebases, merges, resets, or pushes to resolve divergence. `--once` logs and skips a failed cycle, then exits normally; (2) every item whose derived status is `approved` is dispatched, one at a time; (3) after each dispatch the yard's `items/` and `log/events.jsonl` are committed as `depot: <item> <outcome>` and pushed, never forced, with push failures logged and retried next cycle. SIGTERM/SIGINT stop the loop after the current dispatch finishes; a dispatch is never interrupted.

**Operational-branch rule.** The daemon operates on `main` (it refuses to run if a git yard is on another branch): the yard is the operational record, so briefs arrive and run records return through `main`. Turntable code changes go through feature branches and review; the daemon commits records only. A yard that is not a git repository is polled without the pull, commit and push steps.

`log/poll.log` is a plain operational log of daemon starts, stops and cycle outcomes. It is not part of the event spine and holds no item facts. A refused dispatch (for example a missing gate material) leaves the item `approved` and is retried, and logged, each cycle.

## The floor (S3)

`depot.py floor` serves the stage map on `127.0.0.1` only (port `DEPOT_FLOOR_PORT`, default 8765): `/` is the page (`floor/index.html`, `floor/floor.js`), `/cards.json` is the projection, computed per request from the yard on disk. After the origin check below, methods other than GET and the one write route `POST /intents` (S8, below) are refused with 405; the server writes nothing else, not to the yard's records and not to a log file; its output goes to the terminal. `turntable/demo_yard.py` builds a throwaway yard for the legibility checks.

**Origin contract.** Every handler enforces the local hostname allowlist before serving or refusing a route: `Host` must be exactly `127.0.0.1:<bound-port>` or `localhost:<bound-port>`. `Origin` may be absent; if present, it must be exactly `http://127.0.0.1:<bound-port>` or `http://localhost:<bound-port>`. A missing or foreign Host, or a foreign Origin, receives **403** (`{"error": "cross-origin or foreign-host request refused"}`). This covers GET `/`, `/floor.js`, `/cards.json`, and `/records/<item>/<run>/verify.json` or `report.md`, POST `/intents`, unknown paths, and the refused PUT/DELETE/PATCH/HEAD/OPTIONS handlers. Local requests keep their existing responses, including 404 for unknown GET paths and 405 for refused methods or POST paths. This guard is not authentication; local processes can still reach the floor.

`floor.project(root, now)` is a pure function of the yard's files and `now`. Output: `{now, stages, tracks, cards}`. Columns follow `stages`: Queued → Briefed → Building → Gating → Review → Awaiting operator → Interrupted → Done. Interrupted is the stage of a stranded run, distinct from a live run, not a condition-only marker or a required gate before Done. A card carries `id`, `slug`, `name` (the item directory), `title`, `track`, `kind`, `stage`, `hands`, `stage_since`, `dwell_s`, `last_event` (`type`, `actor`, `ts`, `text`), `truth` (`claimed|recorded|verified|checked_failed`), `checker`, `condition` (`null|blocked|stalled?|parked|interrupted`), `condition_reason`, `heartbeats`, and `trail` (the item's non-heartbeat events as `{type, actor, ts, text, truth, checker}`).

Stage, from the item's own trail (the global log and other items' trails are read only for gate completion): `item_created`→Queued; `brief_approved`→Briefed (hands Turntable); `dispatched`→Building, or Gating for a gate item (hands: the adapter); `run_finished`/`claimed`→Building (Turntable); `verified` pass→Review (reviewer); `reviewed` pass→Done; a newest-unanswered `input_required`→Awaiting operator (operator), returning to the derived stage at the next non-heartbeat event. A gate item whose `gate_result` exists (in its target's trail or the global log, `data.gate` equal to the gate item's name) is Done. `verified` not pass, `push_failed`, `reviewed` not pass, and a target's non-pass `gate_result` set condition `blocked` without moving the stage. `pushed`, `committed` and `heartbeat` move nothing. `stalled?` applies in Building or Gating while a run is live (a `dispatched` with no later `run_finished`) when the run's adapter-quiet age exceeds the waybill's `quiet_after_s`; `blocked` outranks it, and it is never raised over an `input_required` ending or a finished (including `timed_out`) run. See Supervision (S5). `dwell_s` runs from the event at which the card entered its current stage. An event type or trail shape with no row raises; the server answers 500 with the reason and the page shows it.

Truth marks apply to both the newest fact and each trail entry:

- **Claimed** (`claimed`, ◇, dashed border): an agent report.
- **Recorded** (`recorded`, ■, thin solid border): a system observation, including an unparseable `gate_result` (no usable check verdict).
- **Verified** (`verified`, ✓, green double border): an independent check **passed**, with its checker and evidence named. Only `result: "pass"` on `verified`, `reviewed`, or `gate_result` receives this mark.
- **Checked · failed** (`checked_failed`, ✕, thick solid border in the failure colour): a check returned a non-passing result. Applies to non-pass `verified` results (including discrepancies and test failures), non-pass `reviewed` results (including legacy `hold`), and failed gates. It is stronger evidence than a mere record, but never a passing verification.

Both check marks retain the checker: `turntable (mechanical)` for mechanical verification, `operator` for operator review or otherwise `reviewer`, and `<gate slug> (<adapter>)` for gates. If the gate data cannot name its checker, show `Checker: not supplied`. `claimed` events remain claimed; other system observations remain recorded. No failed check may wear the Verified mark.

## Conversation (S4)

**`depot message <item> "<text>"`** appends a `message` event (actor `operator`) to both logs, text verbatim (empty text is refused). It is valid in every status, including Done, and changes nothing else: no status, no dispatch, no waybill edit.

**`depot reopen <item> "<text>"`** appends `reopened` (actor `operator`, `text`) to both logs. Validity rule: the derived status must be `reviewed` (a reviewed pass with no later `reopened`); in any other status it is refused (exit 2) and nothing is appended. The item then derives `queued`. The waybill's `status` cache is not rewritten by either command.

**No auto-approval.** A reopened item is re-queued, not re-approved. `dispatch` (and so the poll daemon) requires a `brief_approved` event *after* the newest `reopened`; the earlier approval does not count. Likewise an answered `input_required` becomes dispatchable only through a new `brief_approved`.

**Records commit.** Both commands run on a git yard clone on `main`: they refuse (exit 2, nothing appended) on a non-main checkout or a dirty working tree, fast-forward from `origin`, append, commit exactly `items/<item>/events.jsonl` and `log/events.jsonl` as `depot: message on <item>` / `depot: reopen <item>`, and push `main` (never forced). A push failure keeps the local commit, reports it on stderr and exits 1. A yard that is not a git repository is appended to without the git steps. The poll daemon commits a `blocked` refusal as `depot: <item> blocked`.

**Delivery.** On every dispatch, each `message` (and each `reopened` that carries text) newer than the item's most recent `dispatched` event is delivered, once, in the dispatched brief. If the last run ended in `input_required` and a message has `answers_input_required` equal to that run, the brief gains `## Addendum — input-required answer` with the question and the answer verbatim; all other messages follow in order under `## Messages from operator` (a reopening is labelled `(reopened)`). If the answered `input_required` has no question text, dispatch is refused, a `blocked` event is appended once, and nothing is invented. The exact text sent to the agent is saved as `runs/<run-id>/brief.dispatched.md` for every run; with no messages owed it equals the brief minus its `depot` block.

**Floor.** Card fields `annotation` (`null` or `{type: message|reopened, author, ts, text}`; newest message or reopening text, whitespace collapsed, clipped to 120 characters) and `question` (the question while the card is Awaiting operator, clipped to 200). A `message` is never the card's newest fact: stage, hands, truth and `last_event` come from the newest non-message event. `reopened` (valid only after `reviewed`) moves the card to Queued with dwell from that event. A message after an `input_required` takes the card out of Awaiting operator by the existing supersession rule. `stalled?` is not raised for a run that ended by asking (`input_required`). `blocked` events set condition `blocked` with their reason.

## Supervision (S5)

**Adapter-quiet age.** Every `heartbeat` carries `since_last_adapter_event_s` (seconds since the adapter last wrote a line) and `elapsed_s` (seconds since the run began). For a live run the floor computes `quiet age = (now - newest heartbeat ts) + since_last_adapter_event_s`, or `now - dispatched ts` when no heartbeat has landed yet. Heartbeats stay out of the trail and out of newest-fact computation. Because the age is carried by the heartbeat, a run that goes quiet while the turntable keeps heartbeating raises `stalled?`.

**The mark.** `stalled?` raises when quiet age is greater than the waybill's `quiet_after_s`. Its evidence names the three spec §7 elements: `no adapter event for 3m 12s · elapsed 6m 40s of 10m budget · quiet limit 45s` (last adapter event age, elapsed, the waybill's `timeout_s`). It is a mark, never a verdict: nothing is killed or restarted, and `timeout_s` remains the only mechanical limit.

**Card fields.** Every card carries `last_adapter_event_age_s` and `elapsed_s` (both `null` unless a run is live), and `timeout_s` (the resolved budget). The page shows "last event Xs ago · Ym of Zm budget" on a live card whether or not the mark is raised.

**Track budgets (`tracks.json`).** A yard-root file: `{"_default": {"timeout_s": 900, "quiet_after_s": 300}, "<track>": {"timeout_s": ..., "quiet_after_s": ...}}`; entries may hold either field. `depot new-item` resolves each field, in order: explicit `--timeout-s` / `--quiet-after-s`, the item's track entry, the `_default` entry, then the built-in 900 / 300, and stamps the result into the waybill, so the budget a run had is part of the item's own record. Items filed by hand or connector keep their waybill's values. Read-time fallback: when a waybill lacks `timeout_s` or `quiet_after_s`, the floor resolves it from `tracks.json` (track entry, then `_default`), then the built-in defaults, without rewriting the waybill.

**Daemon refusal cap.** A dispatch that raises `DepotError` before any run starts (bad adapter name, corrupt brief header, approval race) is a refusal. The poll daemon counts consecutive refusals per item in memory; on the 3rd it appends one `blocked` event (actor `turntable`, `reason`: `dispatch refused 3 times: <last refusal>`), commits it as `depot: <item> blocked`, and attempts the item no more. The count resets when the item's log grows or a dispatch succeeds, and a daemon restart forgives it; the `blocked` event is the durable record, and a later `brief_approved` or `reopened` supersedes it in the fold. A run that starts and then fails is an outcome with its own events and is never counted or retried.

## The floor write panel (S8)

The floor can now drop **intents**; it still writes no record. `depot-poll` lands each intent as an ordinary event, so the daemon stays the single writer of records, and an act shows as *pending* until it has landed.

**Route.** `POST /intents`, JSON, `Content-Type: application/json` required (415 otherwise), `Content-Length` required (411), body at most 8 MiB (413, not read). Same-origin guard: `Host` must be `127.0.0.1:<port>` or `localhost:<port>` and any `Origin` must be `http://` plus the same (403 otherwise), so a page from another origin cannot post to it. **This is not authentication.** The floor is localhost-only with no auth beyond that, the same trust posture as the read floor: any process on this machine, and any user logged in to it, can drop an intent. Responses: `202 {id}` new; `200 {id, duplicate: true}` the same intent re-presented; `200 {id, retried: true}` the same intent re-presented after its outcome was `failed` (the outcome file is removed, so the daemon retries it); `409` the id exists with different content; `400 {error}` bad shape; the intent file is not written for any 4xx. `GET /intents` does not exist; receipts ride `/cards.json`.

**Intent** — `log/intents/<id>.json` (git-ignored: runtime state must not dirty the yard, because dirty changes can prevent the daemon's ff-only pull): `{id (lowercase uuid; the client may supply one so a retry reuses it), ts (the server's clock, always), item (a directory name like 0010-panel-question), kind, data}`. Kinds and their `data` (all strings; unknown fields are refused): `message {text}`, `approve {contract_fingerprint}` (legacy empty payloads may be queued but are refused at landing), `park {reason}`, `unpark {}`, `attach {name, content_b64}`, `remove_attachment {name}`, `review {result, note, commit}` (0029), `requeue_interrupted {run_id, note}` (0026). The floor validates shape only: text and reason at most 20000 characters; `attach` content is base64 of at most **5 MiB** decoded (the whole request is capped at 8 MiB); a filename is reduced to a safe basename (directories and traversal dropped, characters outside `A-Za-z0-9._-` become `_`, leading dots dropped, 120 characters; nothing left is a 400). Whether the item exists, the card is in a valid state, or a reason is empty is the daemon's call.

**Outcome** — `log/intents/<id>.outcome.json`, written by the daemon after it handles an intent: `{intent, ts, state, reason?, event?, reconciled?}`. `state` is `landed` (`event: {type, seq}`; `seq` is the event's line number in `log/events.jsonl`), `refused` (a deterministic validation said no; `reason`; the intent is closed and re-presenting it does not reopen it), or `failed` (an infrastructure error, e.g. a disk error; `reason`; retryable by re-presenting the same id and payload). No outcome file means *pending*. The panel shows a fourth state, *reconciling*, when an intent has no outcome but an event carrying its id is already in the log (the daemon committed the event and died before writing the outcome): the next cycle finds the event by its `data.intent` and writes `landed` with `reconciled: true`. It never appends a second event.

**Processing.** At cycle start after the pull, and again before each candidate dispatch, the daemon handles every intent with no outcome, oldest `ts` first (ties by id), appending events with actor `operator`, `via: "floor"` and `intent: <id>` in `data`. Landed events are committed as `depot: floor intents (<n> landed)` through the daemon's records-commit path and pushed with `main` like any other. A crash while draining intents is logged and defers further dispatches in that cycle.

| kind | lands as | refused when |
|---|---|---|
| `message` | `message` `{text, via, intent}`, plus `answers_input_required` when the newest non-heartbeat event is an `input_required` (as `depot message`) | text empty |
| `approve` | `brief_approved` `{via, directed_by: "operator", intent, contract_fingerprint}`; the brief's `depot` block is re-synced into the waybill as `depot approve` does. Approval **is** the dispatch authorization: the same cycle dispatches it | the card cannot take an approval (table below), or an earlier `message` intent for the same item is not landed: still pending, or its newest outcome is `refused`/`failed` (approval is never dispatched around an unlanded answer), or the brief has no valid `depot` block |
| `park` | `parked` `{reason, via, intent}` | reason empty; the card is not in a parkable state (below) |
| `unpark` | `unparked` `{via, intent}`; nothing else | the card is not parked |
| `attach` | writes `items/<id>/files/<name>`, appends `attached` `{file, bytes, sha256, via, intent}` | name not already reduced, content not base64 or over 5 MiB, a file of that name already attached |
| `remove_attachment` | deletes the file, appends `attachment_removed` `{file, via, intent}` | no such attachment, or a dispatch has happened since it was attached (delivered) |

**Which states take which act** (`floor.allowed_actions`; the one table the panel's disabled controls and the daemon's refusals both use; it reads the derived card): *approve* is valid for Queued, Awaiting operator, and a Building/Gating card with no live run that is blocked or whose last run ended on a question; refused when parked ("unpark it first"), when a run is live, when Briefed with a matching contract (already approved; a changed or unbound contract permits re-approval), and for Review and Done. *park* is valid for Queued, Briefed, Awaiting operator, and a Building/Gating card with no live run whose last run ended on a question; refused when already parked, live, or any other stage. *unpark* only when parked. A message is always valid; the panel labels it "Send answer" when the card has an open question and "Post note (annotation)" otherwise. `parked` and `unparked` do not move a card out of Awaiting operator; an answering `message` still does (S4).

**Attachments** ride the next dispatch. Every dispatch copies the item's active attachments (attached and not removed) into `<work>/depot-attachments/`, replacing the previous copy, after checking each file's `sha256` against its `attached` event (a mismatch refuses the dispatch). The directory is added to the work repo's `.git/info/exclude`, so changed-file verification, `diff.patch` and the host-side commit never see it: a delivered input is not an `extra` file, and the exact-set check stays exact. The dispatched brief gains `## Attachments from operator` naming each file, size and hash, and the `dispatched` event lists them. Delivery state is derived from the log: *attached* until a `dispatched` event follows the `attached` event, then *delivered*.

**Card fields added.** `parked` (the reason, or `null`; unlike `condition` it is not outranked by `blocked`), `open_question`, `question_text` (the open question verbatim), `attachments` (`[{file, bytes, sha256, ts, state: attached|delivered}]`), `posture` (`live`: the newest `dispatched` event's `adapter`, `model`, `sandbox`, `run_id`, `ts`, `running`, or `null`; `next`: the adapter's current grant, `{adapter, model, sandbox, tools}`, or `null`), `review_commit` (the current verified commit hash or null), `actions` (`{approve, park, unpark, answer, review}`, each `{ok, why}`), and `receipts` (the card's intents, oldest first, newest 60: `{id, ts, kind, summary, state: pending|reconciling|landed|refused|failed, reason, event}`; attachment content is never included).

**Posture is display only.** Per-item posture toggles are out of scope: after S7 the posture is adapter-level code truth with no per-dispatch field to bind to. The panel shows the last run's posture and the next dispatch's grant, and names the grant on the Approve control itself (for Claude: shell access with Bash, Read, Write, Edit, Grep and Glob, everything else denied; a prompt gate, not a filesystem sandbox).

**Page.** Selecting a card opens a bottom panel (up to 45vh, in layout, not over the board); Esc lowers it. Drafts and the park reason are kept per card in the browser's `localStorage` (a draft is not an input and creates no event), as is the theme (`depot.theme`: auto follows `prefers-color-scheme`, or light/dark). The page works with storage blocked.

**Panel usability contract.** Every unavailable control in the panel and track menus has a visible `.why` line beside or directly below it, including missing notes, pending requests, pipeline holds, track-order limits, and attachment limits/removals. Action buttons use `aria-disabled` and guard activation so pending requests keep their keyboard focus without accepting another click. The selected card stays visible: opening the panel scrolls it into view with `block: "nearest"` after layout shrinks the board. Context starts with condition, blocked cause/explanation/discrepancies and the open question; the full task contract follows. Each context/response/posture zone can scroll within the panel.

Board and panel controls have stable keys tied to their entity and action, so refreshes (15 seconds normally, 3 seconds while pending) and immediate updates preserve focus, including path disclosures, track menus, evidence links, attachments and decision buttons. Opening a card moves focus to the panel heading; Escape or Lower panel returns focus to that card. Draft inputs retain their nodes and values across updates. These are browser display behaviors only; they create no events and change no dispatch authority.


## The vault bridge (S9)

Two one-way bridges between the yard and operator's Obsidian vault, plus dispatch-time context. The yard does not move, the event spine never enters the vault, and the poll daemon is the only writer on both sides. The daemon writes the vault in exactly three places: `Depot/Items/` (projection notes), `Depot/Inbox/Processed/` (stamped captures), and it removes a filed capture from `Depot/Inbox/`.

**Configuration.** The vault is the directory named by **`DEPOT_VAULT`** (disabled by default; select a directory explicitly during setup). Unset, or not a directory, the bridge is fully disabled: no scan, no projection, no context, and every command behaves as before. A bridge failure (an unreadable vault, a bad capture) is logged to `log/poll.log` and never stops the daemon or blocks a dispatch. A waybill whose `context` lists notes while the bridge is disabled refuses its dispatch (it never silently skips).

**Cycle.** After the pull: intents (S8), then the capture scan, then the dispatch scan as before. Projection regeneration runs after the cycle's dispatches and their records commits, so notes describe committed state. **`depot vault-sync`** runs one capture scan plus one projection pass with the same records-commit behaviour (it needs a clean `main` yard like `depot message`; it exits 2 when the bridge is disabled) and prints `filed <item> from <filename>`, `projected <item>`, or `nothing to do`. Do not run it while `depot-poll` is running: two writers.

**Capture (Piece 2).** Each `.md` file at the top level of `<vault>/Depot/Inbox/` (dotfiles, other extensions and subfolders are ignored) is filed as an item:
- *Idempotency.* The ledger `log/vault-captures.json` (committed with the filing) maps `"<filename>:<sha256 of the file's bytes>"` to the item directory name. A file whose key is in the ledger is never filed again; if such a file is still in `Inbox/` (a re-sync, or a receipt that failed) the daemon only repairs its receipt. Editing a capture after it was filed edits the copy in `Processed/`, which is not scanned, so nothing re-files.
- *Malformed.* A file with no text (empty, whitespace, or only `#`), or that is not UTF-8, is skipped and logged, not filed, not ledgered. Fixing the note lets the next cycle file it.
- *The item.* Sequential id; title is the first non-empty line with leading `#` stripped; slug is that title slugified and cut to at most 40 characters (`capture` if nothing is left). `waybill.json` holds `id`, `slug`, `title`, `track: "inbox"`, `created`, `brief`, `model: null`, `timeout_s`/`quiet_after_s` (the normal resolution: `tracks.json` track `inbox`, then `_default`, then built-in 900/300), `links: []`, `context: []`, `status: "queued"`, and **no `adapter` key**. `brief.md` is the ` ```depot ` block `{"expected_files": [], "test_command": ""}` followed by the capture's bytes, verbatim. The `item_created` event (actor `turntable`, `via: "vault-capture"`, `source`) goes to both logs; the records commit is `depot: <item> created (vault capture)`.
- *Receipt.* The capture is rewritten with `depot_item: <item dir>` and `depot_filed: <ISO ts>` frontmatter prepended (merged into an existing frontmatter block) and moved to `Depot/Inbox/Processed/<filename>` (`<name>-2.md` etc. if that name is taken). If the stamp or move fails after filing, the daemon logs it and leaves the file; the ledger prevents a duplicate.

**Approval gate for captured items.** An item whose trail holds a `vault-capture` `item_created` cannot be approved while any of these hold, and the refusal names each: the waybill has no `adapter` key; the brief header's `expected_files` is empty; its `test_command` is empty. `depot approve` exits 2 with nothing appended; the panel's approve intent is refused with the same reason; and `dispatch` refuses such an item even if a `brief_approved` was appended by hand, so no path dispatches an unshaped capture. Shaping means editing the brief header and setting the waybill's `adapter`, then approving as usual. Every other item's approval is unchanged.

**Projection (Piece 1).** For every item in the yard the daemon keeps `<vault>/Depot/Items/<item dir>.md`, rewritten only when its content would change (so a heartbeat, which changes nothing the note shows, never rewrites it, and Syncthing sees no churn). Hand edits are overwritten the next time the daemon checks that item; nothing flows back. Done items' notes are never deleted. `stage` and `truth` come from `floor.card_for`, the floor's own derivation, not a second implementation.

    ---
    depot_id: "0012"
    track: inbox
    adapter: codex            # only when the waybill has one
    stage: Queued
    truth: recorded
    created: <waybill created>
    updated: <ts of the newest non-heartbeat event>
    tags: [depot/item]
    ---
    <!-- Generated by Depot — the record lives in the yard (items/0012-…). Annotate the card, not this note. -->

    # 0012 — <title>

    <headline: the card's newest fact in the floor's wording; never a message, never a heartbeat>

    Brief: items/0012-<slug>/brief.md (yard)

    ## Links                 # only when `links` has entries
    - [[Contacts/example-contact]]

A `links` entry becomes a wikilink: the vault-relative path without `.md`; an entry containing `[`, `]`, `|` or a newline, an absolute path, or `..` is skipped. A string value is quoted in the frontmatter only when plain YAML would misread it.

**Dispatch context (Piece 3).** For each path in the waybill's `context`, in order, the dispatch copies `<vault>/<path>` to `<work>/context/<basename>` (replacing a previous `context/`). A note that is missing, resolves outside the vault, is not a file, or shares a basename with another listed note refuses the dispatch before anything is created (a pre-run `DepotError`, counted by the S5 refusal cap, naming the path). The manifest records `context: [{path, sha256}]`; `brief.dispatched.md` gains `## Context from vault` listing `<vault path> → context/<basename>`; with no context the dispatched brief is unchanged. A run that lists no context removes a `context/` left by an earlier run that did, so the manifest never understates what the agent could see. A wikilink in `links` ships nothing: only `context` does.

## Floor intake (S10)

Work is born on the floor with one board-level intent. It rides the same channel as every S8 intent (`POST /intents` into `log/intents/`, the same outcome files, the same `process_intents` pass); only its binding differs.

**The `create` intent.** `{id, ts, item: null, kind: "create", data}`. The `item` must be absent (400 otherwise): creation binds to no card. `data` (the floor checks shape only: types and sizes, unknown fields refused, so a `kind` or `target` for a gate item is refused at the door):

| field | type | notes |
|---|---|---|
| `title` | string, required | slug derives from it with S9's capture slugifier (`slugify_capture`), not a second one |
| `track` | string, required | an existing track or a new name |
| `adapter` | string, required | `claude` or `codex` |
| `body` | string, required | stored verbatim as the brief's prose; at most 200000 characters |
| `expected_files` | list of strings, required | the header's list |
| `test_command` | string, required | the header's command |
| `repo`, `branch` | strings, optional | both or neither |
| `timeout_s`, `quiet_after_s` | numbers, optional | absent = the track's budget resolution |
| `links`, `context` | lists of strings, optional | vault-relative paths (S9 fields) |

Gate-kind items are not creatable from the floor; the form files `build` items only.

**Daemon landing.** Each create is validated by the daemon, which names **every** failure at once and writes nothing: a refusal's `reason` is `nothing filed: (1) …; (2) …`. A create is refused when: the title is blank; the track is blank, over 60 characters, starts with `_` or a symbol, or uses characters outside letters, digits, space and `. _ -`; the adapter is not a known adapter; the body is blank; there is no expected file after dropping blank lines, or an entry is empty, absolute, drive-lettered, contains a backslash or a control character, or climbs out with `..`; the test command is blank, spans lines, or cannot be split into arguments; repo and branch are not given together, or the branch is `main`/`master`; a budget override is not positive; a `links`/`context` entry is not a plain relative path. Otherwise, in one step: the next sequential id is allocated, `items/<id>-<slug>/brief.md` is written as the generated header block, one blank line, then the body verbatim, so `brief.md` is byte-for-byte ```` ```depot\n<json>\n```\n\n<body> ````, and `parse_header`/`strip_header` read it back exactly; `waybill.json` gets the same fields `new-item` writes (`id`, `slug`, `title`, `track`, `created`, `brief`, `adapter`, `model: null`, `timeout_s`, `quiet_after_s` resolved as always, `expected_files`, `test_command`, `status: "queued"`), plus `repo`/`branch`/`links`/`context` only when given; and `item_created` (actor `operator`, `data: {via: "floor", intent, track}`) goes to both logs. The records commit is one per landed create, `depot: <item> created (floor)`. **Creation is never approval:** no `brief_approved` is written, the item derives `queued`, and the daemon dispatches nothing for it until a separate approval lands (the panel's, as for any item). The S9 approval gate is not involved: a floor item is fully shaped.

**Outcome.** `{intent, ts, state: "landed", item: "<id>-<slug>", event: {type: "item_created", seq}}`, or `refused` with the reasons above, or `failed` for an infrastructure error (retryable by re-presenting the same id). A create whose event is in the log but whose outcome file is missing is reconciled by `data.intent` on the next cycle, with `reconciled: true`, never filed twice. On the projection, create receipts are not on any card: they are `board_receipts` at the top level of `cards.json` (same receipt shape as a card's, plus `filed`: the item dir name once landed), and a landed one reads "Filed <item>".

**Tracks.** A track exists when its first item exists, and there is no empty-track creation. If the create's track is carried by no item and has no `tracks.json` entry other than `_default`, the same records commit adds exactly one `tracks.json` entry for it whose budgets are the `_default` values (the built-in 900/300 where `_default` is absent), written in the file's existing one-key-per-line style; the item's budgets then resolve through the normal path. An existing track (one with an entry, or one any item carries) changes nothing in `tracks.json`. A refused create never begins a track.

**Shared id allocation.** Every creator (`depot new-item`, vault capture, floor intake) allocates through `next_item_id()` inside the daemon's serialized cycle, each reading the yard as the previous step left it, so ids stay unique and sequential across all doors. The cycle order is the S9 one: intents (which include creates), then the capture scan.

**Track order is not a record.** The move earlier / move later controls on each track lane change a display preference stored in the page's per-browser store (`depot.trackorder`, a JSON list of track names): no intent, no event, no server state. With nothing saved the order is the projection's; tracks absent from a saved list append in projection order; names no longer present are ignored; with storage blocked the controls still work for the session and a fresh load shows the default order. The form's draft (`depot.intake.draft`) is kept the same way and cleared only when the submitted create's receipt reads `landed`; a refusal keeps it.

## Track lifecycle (S11)

Track lifecycle is a separate append-only spine at `tracks/<track>/events.jsonl`.
One UTF-8 JSON object per line, using the item-event shape with `track` instead
of `item`:

```json
{"ts":"2026-11-22T12:00:00.000Z","track":"s11","type":"track_paused","actor":"operator","data":{"via":"floor","intent":"<intent uuid>"}}
```

Lifecycle events are **never mirrored** into `log/events.jsonl`. That log's
invariant remains: every line belongs to an item. End's sweep emits ordinary
`parked` item events, mirrored to the global log as always; those are the only
global additions from an End. An End with no open items adds zero global lines.
No existing item status, card-stage, verification, or parked-condition derivation
changes meaning. Track budgets remain in `tracks.json` and no lifecycle act
rewrites them.

Latest lifecycle event in file order wins; no log means `active`.

| Intent kind | Allowed from | Event | Resulting state / consequence |
|---|---|---|---|
| `track_pause` | active | `track_paused` | paused: hold new dispatches, keep lane in place marked PAUSED |
| `track_resume` | paused | `track_resumed` | active: approved items dispatch on the next cycle |
| `track_table` | active, paused | `track_tabled` | tabled: hold dispatch and move lane to the main-board shelf |
| `track_untable` | tabled | `track_untabled` | active: Resume on the shelf returns the lane to its arranged position |
| `track_end` | active, paused, tabled | `track_ended` | ended: sweep open items, move entire lane and cards to Archive |
| `track_reopen` | ended | `track_reopened` | active: lane returns; swept items stay parked; unpark remains per-item |

These intents use the existing `POST /intents` route and envelope
`{id, ts, item: null, kind, data: {track}}`. Names use the intake track-name
rules. The floor drops only the intent; the daemon checks existence, current
state, and End's pipeline precondition at landing. Unknown tracks and invalid
transitions are refused with a named reason and no event. Actor is `operator`, data
is exactly `{via: "floor", intent: "<id>"}` for all six track events. Outcomes
retain the existing landed / refused / failed grammar. Track outcomes include
`track` and `event: {type, seq}`; `seq` is **track-log-local**, not a global
sequence. Retry reconciliation uses the intent id in the track log and never
appends a duplicate lifecycle event. Track receipts appear on their lane/shelf,
not on a card or the new-work rail. A track event without an outcome shows
reconciling; an intent without an event shows pending.

**End and its records commit.** The daemon projects the existing cards without
changing derivation. Any Building, Gating, or Review card refuses End, naming
all such items, even if parked or blocked. Otherwise every non-Done card
(Queued, Briefed, Awaiting operator, including an already parked card) receives
`parked`, actor `operator`, data `{reason: "track ended", via: "floor", intent}`.
Done cards and other tracks are untouched. The lifecycle event is appended
after the sweep. The track log, swept item logs and waybill caches, and item
events' global mirrors are staged together by the daemon's records path in
**one commit**, `depot: <track> track_ended (floor)`. There is no second sweep
commit. Append errors restore the affected files; a track-commit error leaves
the intent pending for reconciliation. Other explicit lifecycle acts also
receive their own `depot: <track> <event> (floor)` records commit. The usual
main-only daemon posture, pull/push behavior, and adapter permissions persist.

**Dispatch hold.** Only the daemon's dispatch decision is gated: paused, tabled,
and ended tracks skip approved items. One diagnostic per skipped track per
cycle goes to `log/poll.log`, never to an item or lifecycle event log. Approval,
messages, and other records remain allowed. In-flight work and its verification
finish normally. Resume/Untable/Reopen restore normal dispatch eligibility;
they do not approve or unpark items.

**Item dispatch hold (CLI and daemon).** An active park is a `parked` item
event with no later `unparked` event in file order, regardless of timestamps
or intervening approvals, messages, item reopenings, or track lifecycle events.
The shared `active_park` check excludes held items from poll selection and
refuses them at the final `dispatch_item` boundary used by both `depot dispatch`
and the daemon, before any run or work directory is created. Poll skips do not
count as dispatch refusals or append item events. A track-end park survives
track reopening and re-approval. Only an explicit per-item unpark releases it;
unparking restores eligibility under the existing approval and track rules
and does not itself approve or dispatch anything. The floor keeps displaying
the park and disabling approval until that explicit unpark, including after
an item reopening or a CLI re-approval.

**Intake link.** `apply_create` checks lifecycle at landing. Paused intake lands
Queued and unapproved as usual; even a later approval cannot dispatch until
Resume. Tabled intake appends `track_untabled` using the create intent's id in
the **same creation commit** as `item_created`; that item event alone adds
`revives_track: true` to its data, a recovery marker so an interrupted pair
finishes before committing or reporting landed. Revival never adds a global
track event. Ended intake refuses with `track ended — reopen it from the Archive
to file new work`, writing no records. Explicit Reopen is required. The form
states each selected track's consequence before submission, including “Files
the item and returns <track> to the board” for tabled tracks and the ended-track
refusal. Typing an existing name obeys the same rule as selecting it.

**Projection and display.** `floor.project` reads both spines. `tracks` retains
`id` and `name` and adds `state`, `card_count`, `done_count`, `open_count`,
`pipeline` (item names), `shelf`, `archive`, and track `receipts`. Track names
come from items, configured budgets (except `_default`), and lifecycle logs;
there is no new empty-track creation. `cards` remains complete, including
shelved and archived cards, with unchanged card derivation. The page shows
active/paused lanes on the main board, tabled lanes on its foot shelf, and only
ended lanes in Archive. Archive shows every card, regardless of hide-done
preferences, with no card write panel or intake; Reopen is its only write.

The lane `…` menu hosts lifecycle, arrange, and a checkable “Hide done cards”
toggle. Arrange keeps S10's order algorithm/store. Done folding is per-track,
default shown, in the same guarded browser store (`depot.hideDone`); no intent,
event, or server state. Blocked storage still works for the session and resets
to shown on reload. A folded lane always shows its count and “N done — show”
control. Menu consequences are stated in place, with current done/open counts
and named pipeline blockers. Membership of ended tracks and their Done cards
is records-derived, independent of this display preference.

## Blocked-item view and operator decisions (0022)

A blocked card's item panel shows every verification discrepancy as `path · kind`,
without truncating the list. It names the cause and explains it in plain language.
Evidence belongs to the most recent dispatched run (`dispatched.data.run_id`),
never an arbitrary older run. Its `verify.json` and `report.md`, when present,
are linked through the floor's read-only `/records/<item>/<run>/<file>` route.
Only those two artifacts are served; paths must stay inside that item's runs.

Cause classes are observations, not a verdict that the work is correct:

- `wrong-directory`: all expected paths are missing from the verified changes,
  extras include `repo/` at work root, and every expected file exists inside
  `work/repo/`. Expected paths come from this run's verify.json, falling back to
  the waybill when the record is absent.
- `extra-files-only`: all discrepancies are extra files.
- `missing-files-only`: all discrepancies are missing files.
- `tests-failed`: the verified result is tests_failed without a more specific
  file-set class.
- `unknown`: the record does not establish one of these causes. The panel says
  so and directs the operator to review the records.

Actions reflect the current state and are checked again when the daemon lands
an intent. Every transition requires an explicit operator click:

- Re-run uses the existing approve intent and dispatch authorization. Existing
  holds, live-run checks, and next-dispatch posture still apply.
- Recover and review is offered only with no live run and a resolvable HEAD in
  `work/repo/` or `work/` with its own `.git`. It names the actual branch and
  commit (or detached HEAD and commit). The `recover` intent carries run_id and
  commit; stale values are refused. It records a `recovered` event containing
  run_id, directory, branch, and commit, and moves the item to Review with reviewer.
  Recovery preserves the existing work pointer for inspection. The commit is
  unverified and cannot be accepted in Review. The panel explicitly explains
  that **Re-run to verify** needs a separate approval and starts fresh from the
  base rather than resuming or verifying the recovered commit. Recovery itself
  does not copy files, reshape a repository, merge, push, or dispatch.
- Mark adjudicated requires a non-empty human note and no live run. The
  `adjudicate` intent carries run_id and note; a stale run is refused. An
  `adjudicated` event records both and moves the item to Review with reviewer.
  This is a recorded human override, not a mechanical verification pass.

Both new events include the existing floor `via` and intent receipt fields.
Their derived waybill status is `review`, which cannot auto-dispatch. Original
verification events and run artifacts remain intact. No automatic recovery,
resolution, re-dispatch, posture change, or sandbox change is introduced.


### Recovered commits and re-runs (0030)

A recovered Review card offers **Re-run to verify** using the existing `approve`
intent and contract fingerprint. Approval records `brief_approved`; only then
may the normal daemon cycle dispatch one run. Parked and live-run guards,
next-run adapter/model/sandbox posture, message delivery checks, and single-use
approval still apply. Recovery alone never authorizes dispatch. The waybill's
adapter remains the default until operator chooses the lane at approval.

The recovered event retains run_id, directory, branch, and commit permanently.
A recovery re-run preserves that repository and creates a separate workspace
under `work/.depot-reruns/<approval-seq>/`. For repository items it clones the
remote and starts the review branch from the remote default branch; a local
item starts with an empty repository. It carries the approved brief and landed
inputs, not the recovered output. `dispatched.data.work_directory` records the
item-relative workspace; absent this field, the workspace is `work/`. Later
verification, recovery evidence, and ordinary re-dispatch use the recorded
workspace. Workspaces stay beneath the existing ignored `work/` directory;
records serving and publication rules do not change. Branch pushes retain the
existing non-force policy; a divergent remote branch must be resolved by the
operator, never overwritten automatically.

The floor's `recovered_work` projection retains the latest recovered pointer and
`superseded_by: null` until a subsequent dispatched run passes verification and
records a valid `committed.hash`. It then marks the pointer superseded with that
new hash and displays both commits. This is derived from append-only events;
no recovery event or original evidence is rewritten. Failed verification or a
run without a committed deliverable does not supersede recovery. The new
verified commit can be reviewed subject to the normal block/live-run guards;
the recovered pointer never supplies a review candidate. Both the floor and CLI
refuse acceptance of the unverified recovered commit.


## Review / acceptance (0029)

On a Review card with a passing mechanical verification and a recorded work
commit, the floor panel offers **Accept — Done** and **Reject — blocked**.
Enter a non-empty review note in Your response first. Both send a `review`
intent with `data: {result: "pass"|"fail", note, commit}`; the daemon appends
`reviewed` as actor `operator` to both item and global logs. The full commit hash
is shown on the panel and retained verbatim in the event.

The equivalent CLI is:

```
python3 turntable/depot.py review <item> pass "Meets the brief" --commit <full-hash>
python3 turntable/depot.py review <item> fail "Missing edge case" --commit <full-hash>
```

The hash must equal the `committed.hash` after the current run's passing
`verified` event (also recorded in that run's `verify.json`). This is the
verified work commit, not the yard records commit or a mutable workspace HEAD.
A new approval, dispatch, reopening, verification, or recovery invalidates an
older candidate. No commit is inferred for a gate-only run, a recovered
unverified commit, or a run with no committed deliverables. Those need passing
verification with a recorded commit before using this action. A blocked item
or live run cannot be accepted. Wrong hashes, empty notes, and decisions after
Done are refused without appending a review event. A floor retry with the same
intent id reconciles an existing event without duplicating it.

A pass derives status `reviewed` and stage Done; a fail derives status blocked
and keeps Review with the review note as its blocked reason. The note and hash
remain in both logs. There is no automatic acceptance, approval, or dispatch.
Rejected-review contract (0031): a failed `reviewed` event records the result,
review note (or historical reason), and reviewed commit in the append-only
trail. The card stays blocked in Review, with operator as hands for a operator verdict.
The panel shows that review reason, without inferring a verification failure
from the preceding passing verification. `blocked_details` is null for this
review block; `rejected_review` carries `{note, commit, run_id}` for the rejected
run, and remains available after adjudication until a new approval/run or
verification supersedes it.

Reject copy: “Card stays in Review, blocked, with your note. Next: Re-run
(approve) or adjudicate.” The response draft clears from the panel and browser
storage once the submitted review's receipt lands, for either pass or fail.
Pending or refused reviews retain their draft. A newer draft typed while the
review is pending is preserved.

Adjudication requires a separate non-empty note and the current rejected run's
`run_id`. It records `adjudicated` and clears the block, without changing the
rejection in the trail or restoring the rejected commit's eligibility for
acceptance. A failed review invalidates `review_commit`; a fresh passing
verification followed by a recorded commit is required before another review
can land. Adjudication alone supplies neither. Re-run remains available both
before and after adjudication of a rejected review.

Re-run uses the existing `approve {contract_fingerprint}` intent. The daemon
checks the current contract, landed inputs, parked/live state and existing
approval rules, then records `brief_approved`. This authorizes one fresh
dispatch through the normal poll path; rejection and adjudication never
auto-dispatch. Posture, sandbox and records rules are unchanged. Rejection
notes remain in the trail; editable drafts are never dispatch inputs.

The CLI uses the existing records command guard (clean main yard clone,
fast-forward pull, records-only commit and push); on a plain directory it only
appends logs. Like message/reopen, it does not rewrite the status cache. Floor
landing uses the daemon's existing records commit and status-cache behavior.
The existing reviewer connector path, including historical review payloads,
continues to work; new operator reviews identify `operator` as their checker.

The poll drains pending intents oldest first at cycle start and again before
each candidate dispatch, including between runs in one batch. It rechecks
current approval, parked state and track state after draining. A pause or table
(hold), park, or track end submitted during a run therefore lands before the
next dispatch; in-flight work finishes normally. If draining crashes or leaves
unresolved intent files, further dispatches are deferred. The original batch's
candidate list is retained: a newly approved item waits for the next cycle.

Park is now also available in Briefed (already approved, not yet running), so
an operator can hold queued approved work while another run is in flight.
Parking preserves the recorded approval and stage; the poll skips the card
while parked. Unpark releases that hold and adds no approval or dispatch event;
the existing approval remains eligible for a later poll dispatch.

## Interrupted runs (0026)

On each successful poll cycle, after the yard pull and before intents or dispatch,
Turntable reconciles the latest `dispatched` event when it has no later
`run_finished` or `run_interrupted`. Once its age exceeds the dispatch event's
`timeout_s` plus 300 seconds of host cleanup grace, Turntable records
`run_interrupted` (actor `turntable`), with `run_id`, `dispatched_at`, `bound_s`,
and `reason`. Legacy dispatches without a recorded timeout use the waybill timeout,
then the built-in 900 seconds. Heartbeats and messages do not extend this bound.
The scan runs between synchronous dispatches under the existing poll lock. A failed
pull still skips the entire cycle under the existing records rule; reconciliation
resumes on the next successful cycle. Normally finished runs, including timed-out
runs with `run_finished`, are unaffected. Repeated scans append no duplicate event.

`run_interrupted` derives status `interrupted` (also mirrored to the waybill).
The floor places the card in the **Interrupted column**, shows the **interrupted**
marker and hands **operator**, and counts it in **Stuck**. The projection's `condition`
field carries that marker alongside stage **Interrupted**; it does not replace
the stage. The recorded run posture has `running: false`. This means completion was not
recorded; it is not proof that an orphan worker has exited. The operator must check
that the old worker has stopped before approving a replacement. Records and work
artifacts are preserved. This detection neither kills workers nor resumes them.

The floor offers **Re-queue interrupted run with note**. Its intent kind is
`requeue_interrupted`, with string `run_id` and non-empty `note`. The daemon checks
that the card is still interrupted and the run ID still matches. It appends
`interrupted_requeued` (actor `operator`, data `run_id`, `note`, `via: "floor"`, `intent`),
which derives status `queued` and floor stage Queued. The recovery receipt is
idempotent under the existing intent reconciliation rules. A separate, fresh
approval is required before any replacement dispatch; historical approvals do not
suffice. The floor and daemon refuse approval and parking while interrupted, and
the direct CLI refuses approval and dispatch until recovery. Interrupted cards
also prevent ending their track, so the track sweep cannot park them.

Both new events are recorded observations in the floor truth mapping. The card's
`actions` includes `requeue_interrupted: {ok, why}`. Approval, parking, blocked-run
adjudication, and commit-pointer recovery are disabled while interrupted. The
daemon never synthesizes a recovery intent or re-queues an interrupted run on its
own; detection alone never dispatches anything. Adapter, posture, sandbox, and
records rules are unchanged. The waybill adapter remains the default until operator's
lane choice at approval.


### Floor display contract (0035)

- Decision receipt summaries identify Accept/Reject with the submitted note and short commit, or recovery/adjudication/re-queue with the submitted run, note and commit where present; summaries describe the request, while receipt state describes its outcome.
- Receipt state is still derived from daemon outcomes and the log; the live region announces newly observed landed/refused/failed outcomes after initial load, including reasons, and never treats the click as a landed event.
- Awaiting operator, Stuck and Building / gating readouts count the whole yard, including tabled and ended tracks, regardless of Board/Archive view or local Done folding.
- Station counts count visible cards in the current Board/Archive view; Done folding and track arrangement are browser display preferences and change no records.
- Stage dwell remains elapsed time since the current stage began; condition_age_s is separate: blocked/parked use the latest establishing event, interrupted uses its stage entry, and stalled? uses time since the adapter last spoke rather than time past its quiet limit.
- Condition markers display that separate age; messages and annotations do not restart stage or condition clocks, and a later block or park event starts a new condition clock.
- Path rows show each projected non-heartbeat event's UTC timestamp, truth class and checker when supplied; no checker is inferred for claimed or recorded events.
- A landed review, adjudication, recovery or interrupted re-queue clears its submitted response draft only if it has not since been edited; pending/refused/failed requests preserve drafts, and draft cleanup appends no event.
- Disabled approval uses the existing action-table reason instead of affirmative dispatch copy; responsive layout, hidden text and empty-state messages confer no new authority or dispatch behavior.
