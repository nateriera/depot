# Configuration

Depot reads environment variables. The Linux installer writes their selected
values to `depot/config.env`; service units, checker, and desktop launcher share
that configuration. The file is data, not a shell script. Reinstall/restart after
changing service configuration.

| Variable | Meaning | Default |
|---|---|---|
| `DEPOT_YARD` | Operational records directory | Source root for direct CLI use; installer selects `yard/` under its source checkout unless `--yard` or the variable is supplied |
| `DEPOT_FLOOR_PORT` | Loopback HTTP port | 8765; installer `--port` and demo `--port` override it |
| `DEPOT_CODEX` | Codex executable name/path | `codex` through PATH |
| `DEPOT_CLAUDE` | Claude executable name/path | `claude` through PATH |
| `DEPOT_VAULT` | Explicit note directory for the optional bridge | Unset/empty: disabled; installer requires `--vault` to enable it |
| `DEPOT_HEARTBEAT_S` | Run supervision heartbeat interval | 30 seconds |
| `PATH` | Executable search path | Inherited for direct CLI; captured explicitly by the service installer |
| `DEPOT_ADAPTERS` | Adapters checked by the installation checker | Set by installer `--adapters`; does not change per-item adapter selection |

The checker and desktop launcher accept `--config PATH`. The launcher and checker
also honor an explicit `DEPOT_FLOOR_PORT` override in their own environment. Use
the same value as the running floor. To change installed services and launcher
together, rerun `ops/install.sh --port PORT ...`; the floor service itself reads
that port from its EnvironmentFile. No launcher contains a separate fixed URL.

## Yard and source are separate

Use a private plain directory for initial operation. The source distribution's
`items/`, `tracks/`, and global event log are empty. Its budget file contains only
`_default`; no sample work is queued automatically.

If you choose a Git-backed records yard, configure its identity and private
origin yourself. The daemon requires `main`, pulls only fast-forward updates,
and pushes records to that origin. Agents commit no yard records themselves.
A failed pull is logged and suspends that cycle's dispatch; it is not permission
to rewrite or merge the yard. Keep source updates separate from running records.

## Budgets and permissions

Each budget resolves from the explicit item override, track entry, `_default`,
then built-in defaults: 900 seconds wall timeout and 300 seconds quiet limit.
The wall timeout ends a run; the quiet limit only changes its visible supervision
mark. Track pause/table/end gate new dispatches, not already running work.

Codex uses `workspace-write`. Claude uses its configured allowed-tool list plus
noninteractive permission-prompt denial; that is a prompt gate, not a filesystem
sandbox. The approval panel displays the actual next-run posture. This setup
work does not expand either adapter's permissions. CLI flag compatibility should
be checked with the installed adapter version; see [FORMATS](../turntable/FORMATS.md).

## Optional note integration

Select an existing directory explicitly. The daemon reads captures in
`Depot/Inbox/`, stamps and moves filed captures to `Depot/Inbox/Processed/`, and
regenerates projection notes in `Depot/Items/`. A copied note is reference input;
a link alone never includes its content. Only the waybill's `context` list
includes notes at dispatch. Paths must stay inside the selected directory.

Captures arrive Queued and unapproved. Before approval, complete their adapter,
expected files, and test command. The detailed contract is in
[the bridge guide](vault-bridge-design-note-20261101.md) and FORMATS.

Leaving `DEPOT_VAULT` unset or pointing at a missing directory disables the bridge.
An item explicitly requesting context cannot silently omit it: dispatch is
refused while its required note input is unavailable.
