#!/usr/bin/env python3
"""Generate an entirely fictional yard; optionally serve it at a fixed synthetic clock.

    python3 turntable/demo_yard.py .demo --serve --port 8876

No adapter, account, remote repository, or note directory is needed. Existing
non-demo directories are refused. Every event is generated here, never copied
from a real run. The fixed clock makes the answer key reproducible.
"""
import argparse
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEMO_NOW = datetime(2040, 6, 15, 12, tzinfo=timezone.utc)
DEFAULT_DIR = Path(__file__).resolve().parent.parent / ".demo"
ANSWER_KEY = """Fictional demo, fixed clock 2040-06-15 12:00 UTC.
Awaiting operator: 6001 (Paper Kites: choose a poster size), 6008 (Lantern Library: choose an icon).
Stalled: 6004 (Orbit Garden gate). Blocked: 6009 (palette review), 6012 (verification failed).
Parked: 6006; it remains Queued. Approved: 6005. Unapproved: 6011.
Completed: 6003 and 6010. Claimed report: 6007. Live recorded heartbeat: 6002.
All people, projects, timestamps, and commit references in this yard are fictional.
"""


def build(root):
    root = Path(root)
    if root.is_symlink():
        raise SystemExit("refusing a symlink demo directory")
    if root.exists() and any(root.iterdir()) and not (root / ".demo-yard").is_file():
        raise SystemExit(f"refusing: {root} exists, is not empty, and is not a demo yard")
    if root.exists():
        shutil.rmtree(root)
    (root / "items").mkdir(parents=True)
    (root / "log").mkdir()
    (root / ".demo-yard").write_text("fictional generated demo; safe to replace\n")
    global_events = []
    sequence = 6000

    def add(slug, track, title, stage, *, question=None, parked=None, failure=False, hold=False):
        nonlocal sequence
        sequence += 1
        num = str(sequence)
        name = f"{num}-{slug}"
        directory = root / "items" / name
        directory.mkdir()
        events = []
        age = 36 * 3600

        def emit(kind, actor="turntable", **data):
            nonlocal age
            stamp = (DEMO_NOW - timedelta(seconds=age)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            events.append({"ts": stamp, "item": name, "type": kind, "actor": actor, "data": data})
            age -= 60

        emit("item_created", "operator", title=title, track=track)
        kind = "gate" if stage == "Gating" else "build"
        if stage != "Queued":
            emit("brief_approved", "operator")
        if stage not in ("Queued", "Briefed"):
            age = 6 * 3600
            emit("dispatched", adapter="codex", model=None, sandbox="workspace-write", run_id=f"demo-run-{num}")
            if stage == "Gating":
                age = 3 * 3600 + 41 * 60
                emit("heartbeat", since_last_adapter_event_s=22, elapsed_s=240)
            elif slug == "word-counter":
                age = 20
                emit("heartbeat", since_last_adapter_event_s=8, elapsed_s=340)
            else:
                emit("run_finished", exit_code=0, elapsed_s=41, timed_out=False)
                emit("claimed", "codex", report="report.md")
                if question:
                    emit("input_required", "codex", question=question, run_id=f"demo-run-{num}")
                elif stage in ("Review", "Done") or failure:
                    emit("verified", result="tests_failed" if failure else "pass", discrepancies=[], test_exit_code=1 if failure else 0)
                    if not failure:
                        emit("committed", hash="0" * 36 + num, files=["example.py"])
                        if stage == "Done":
                            emit("gate_result", "reviewer", result="pass", gate="9090-fictional-gate", run_id="demo-gate",
                                 criteria=["CRITERION 1: PASS — fictional evidence"], report="report.md")
                            age = 19 * 3600
                            emit("reviewed", "reviewer", result="pass")
                        elif hold:
                            age = 5 * 3600 + 12 * 60
                            emit("reviewed", "reviewer", result="hold", reason="waiting on palette confirmation")
        if parked:
            emit("parked", "operator", reason=parked)
        wb = {"id": num, "slug": slug, "title": title, "track": track, "adapter": "codex", "kind": kind,
              "quiet_after_s": 3600 if slug == "word-counter" else 300, "timeout_s": 28800,
              "expected_files": ["example.py"], "status": "demo"}
        if kind == "gate":
            wb["target"] = "6005-layout-preview"
        (directory / "waybill.json").write_text(json.dumps(wb, indent=2) + "\n")
        (directory / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
        global_events.extend(events)

    add("poster-size", "paper-kites", "Paper Kites: poster size", "Awaiting operator", question="Pip asks: small or large poster?")
    add("word-counter", "paper-kites", "Paper Kites: word counter", "Building")
    add("output-watcher", "paper-kites", "Paper Kites: output watcher", "Done")
    add("layout-gate", "orbit-garden", "Orbit Garden: layout gate", "Gating")
    add("layout-preview", "orbit-garden", "Orbit Garden: layout preview", "Briefed")
    add("color-sweep", "orbit-garden", "Orbit Garden: color sweep", "Queued", parked="waits on fictional layout review")
    add("seed-map", "orbit-garden", "Orbit Garden: seed map", "Building")
    add("shelf-icon", "lantern-library", "Lantern Library: shelf icon", "Awaiting operator", question="Rowan asks: circle or square icon?")
    add("palette-review", "lantern-library", "Lantern Library: palette review", "Review", hold=True)
    add("catalog-card", "lantern-library", "Lantern Library: catalog card", "Done")
    add("bookmark", "lantern-library", "Lantern Library: bookmark", "Queued")
    add("clock-face", "clockwork-workshop", "Clockwork Workshop: clock face", "Building", failure=True)
    global_events.sort(key=lambda event: event["ts"])
    (root / "log/events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in global_events))
    return root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=None, help="demo HTTP port; otherwise DEPOT_FLOOR_PORT or 8765")
    args = parser.parse_args()
    root = build(args.directory)
    print(ANSWER_KEY, flush=True)
    if args.serve:
        import os
        import floor
        port = args.port if args.port is not None else int(os.environ.get("DEPOT_FLOOR_PORT", "8765"))
        server = floor.make_server(root, port, now_fn=lambda: DEMO_NOW)
        print(f"fictional demo: http://127.0.0.1:{server.server_address[1]}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
