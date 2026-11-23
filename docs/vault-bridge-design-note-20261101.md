# Optional note-directory bridge

The filename is retained for links. This guide describes current behavior,
without private design history or an assumed machine layout.

Set `DEPOT_VAULT` explicitly for direct CLI use, or pass `--vault PATH` to the
Linux installer. Nothing is auto-discovered. The yard remains the record; it is
not moved into the note directory. Only the daemon writes bridge outputs.

## Generated item notes

The daemon projects each item's current state to `Depot/Items/<item>.md`, with
frontmatter, its newest fact, a brief reference, and explicit related-note links.
Hand edits to generated notes are overwritten. Annotate the item through the
normal message path. Completed items' projection notes are retained.

## Capture

A finished Markdown note placed in `Depot/Inbox/` becomes Queued work. The first
nonempty line becomes its title. The capture body is retained verbatim. A ledger
keyed by filename and content hash prevents duplicate filing. The daemon stamps
a receipt and moves the note to `Depot/Inbox/Processed/`.

A capture is never automatically approved. Set its adapter and complete the
brief's expected-file list and test command before approval. Keep the operational
yard out of filesystem synchronization; synchronize note inputs separately if
needed. Resolve note conflicts before treating them as new captures.

## Link versus include

A waybill `links` entry creates a graph link only. It does not send a note to an
agent. A `context` entry explicitly includes a contained note at dispatch. The
manifest records each included path and hash; the dispatched brief lists the
copied inputs. Missing, escaping, or colliding input paths refuse the dispatch.

The copied top-level `context/` directory is reference input and excluded from
deliverable verification/commits. Nothing grants whole-directory agent inclusion.
The configuration is disabled when absent or missing; explicitly requested
context still cannot be silently omitted. See [FORMATS](../turntable/FORMATS.md)
for exact field shapes, idempotency, and receipt behavior.
