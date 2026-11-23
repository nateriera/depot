# Fictional demo legibility checks

Generate and serve the demo with its fixed clock:

```sh
python3 turntable/demo_yard.py .demo --serve --port 8876
```

All projects and personas are invented. No original run records are loaded.
Use a 1600×900 viewport for the basic check; also inspect keyboard controls and
dark mode. Time each answer from revealing the board to the complete answer.
Record elapsed seconds and errors; a partial answer is not a pass.

| Prompt | Answer key |
|---|---|
| What awaits the operator? | 6001 Paper Kites poster size and 6008 Lantern Library shelf icon |
| What is stuck? | 6004 Orbit Garden gate is stalled; 6009 palette review is blocked; 6012 clock face failed verification |
| Which hold is deliberate? | 6006 Orbit Garden color sweep is parked and remains Queued |
| Which work is approved? | 6005 layout preview is Briefed; 6011 bookmark is unapproved Queued work |
| How did 6003 finish? | Done, reviewed by the reviewer, with gate evidence preceding review in its path |
| What differs in truth? | 6007 is claimed; 6002 heartbeat is recorded; 6003 completion is verified |

The generator prints the same answer key. Tests assert these states and the
one-card-per-stage-zone layout. Operator controls in this display-only demo drop
pending intents; no daemon or adapter runs. Track lifecycle and blocked decisions
are covered by disposable-yard automated tests. Test actual dispatch separately
in a private operational yard with an explicitly configured adapter.
