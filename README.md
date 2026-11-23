# Depot

Depot runs bounded coding tasks through local agent CLIs and shows their progress
on a local stage map. Briefs, approvals, messages, verification, and review are
records in an append-only event log. A card's state is derived from those records.
The source distribution contains an **empty operational yard** and a separate,
entirely fictional demo generator.

## Status

Depot is generally available. An adversarial pre-launch review (November
2026) found seven findings; all seven are fixed in this tree, and a
disposable-repository walkthrough (November 23, 2026) covered approval,
execution, exact-commit verification, human acceptance, interruption, and
recovery.

The daemon still runs agent-written tests with host permissions inside its
sandbox, binds loopback only, and its same-origin checks are not
authentication. Do not point an operational yard at a public source
repository.

## Try the demo

Install Python 3.10 or newer. No adapter, account, repository remote, or note
integration is needed for the demo:

```sh
python3 turntable/demo_yard.py .demo --serve --port 8876
```

Open `http://127.0.0.1:8876/`. The demo uses a fixed synthetic clock so the
[answer key](docs/legibility-tests.md) stays reproducible. It generates records
only in `.demo`, refuses unrelated nonempty directories, and never runs an agent.
The page offers normal controls, but without a daemon their intents remain
pending. Regenerate the demo to reset it.

## The loop

1. **File work.** Supply a brief, expected files, test command, track, and adapter.
   Filing creates a Queued item; it does not approve or dispatch it.
2. **Approve.** Inspect the next-run permissions and inputs, then approve one run.
   The daemon dispatches approved work unless the item or its track is held.
3. **Watch.** Heartbeats show elapsed time and adapter silence. A question ends the
   run and waits for an operator answer. Answering and approving are separate acts.
4. **Verify.** The host checks the exact changed-file set and runs the specified
   tests. It commits passing work host-side; repository work goes to its named
   review branch. A failed check stays visible with its evidence and explicit
   recovery or re-run choices.
5. **Review.** A human reviews the result and controls merging. Mechanical
   verification is not human acceptance. Completion is a `reviewed` event with a
   passing result, recorded through the review integration; the floor currently
   has no general “mark Done” button or review-completion CLI.

Messages annotate records. Park holds an item. Pause holds new dispatches in a
track; Table shelves it; End refuses active pipeline work and parks remaining
open items atomically with the track event. Archive includes ended tracks;
Reopen returns a track while leaving its parked items parked.

## Use an operational yard

See [installation](docs/installation.md) for a first local task and optional
Linux/systemd user services, and [configuration](docs/configuration.md) for paths,
ports, adapters, budgets, and opt-in note integration.

Use a separate private records directory for real work. The daemon can operate
on a plain directory without Git. A Git-backed yard operates on `main` and may
pull/push its configured origin; do not point it at a public source repository.
The server binds to loopback only. Its same-origin checks are not authentication.

The [public specification](docs/spec-v1-20261030.md) describes responsibilities
and invariants. [FORMATS](turntable/FORMATS.md) documents the actual record and
intent contracts, including track lifecycle and blocked-item decisions.

## Development

Python tests use the standard library and disposable yards. Node.js must be on
`PATH` for the JavaScript checks. Tests need permission to bind localhost sockets.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s turntable
```

No adapter account is needed for tests; adapters are replaced with test programs.
The Linux service installer/checker additionally needs systemd, and the desktop
launcher uses `xdg-open`.

Author credit is in [AUTHORS](AUTHORS.md). Licensed under the MIT License. See [LICENSE](LICENSE).
