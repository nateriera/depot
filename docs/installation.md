# Installation

Depot's dispatcher and service setup target Linux. The optional installer uses
**systemd user services**; it is not a cross-platform installer. The demo and
floor need Python 3.10+; operational builds also need Git and at least one locally
installed, authenticated adapter CLI (`codex` or `claude`). Development tests also
need Node.js on PATH and permission to bind localhost sockets.

Clone the public repository:

```sh
git clone https://github.com/nateriera/depot.git
```

Run the commands below from that checkout. No fixed checkout name
or home-directory layout is required.

## First operational task, without services

Choose a private, separate yard directory. A plain directory needs no Git remote,
no automatic commit setup, and no public repository:

```sh
export DEPOT_YARD="$PWD/yard"
export DEPOT_FLOOR_PORT=8877
python3 turntable/depot.py new-item --title "First task" --slug first-task --track examples
```

The command prints `0001-first-task` in an empty yard. Edit its generated brief
at `$DEPOT_YARD/items/0001-first-task/brief.md` to contain:

````markdown
```depot
{"expected_files": ["hello.py", "test_hello.py"], "test_command": "python3 -m unittest"}
```

Create hello.py with greet() returning "Hello". Create test_hello.py with a
unittest checking that value. Change exactly these two files.
````

The default adapter is `codex`. To use another configured adapter, set the item's
`adapter` in `waybill.json` before approval. Install and authenticate the adapter
using its own CLI documentation. Depot does not create accounts or credentials.
The selected command must resolve on PATH, or set `DEPOT_CODEX` / `DEPOT_CLAUDE`
to its executable. See [configuration](configuration.md).

In a second terminal, export the same yard and port, then start the floor:

```sh
python3 turntable/depot.py floor
```

Open `http://127.0.0.1:8877/`. Inspect the item and permission consequences before
approving it. The floor drops intents; a separate daemon must land them:

```sh
python3 turntable/depot.py poll --once
```

Or run `poll` without `--once` to continue polling. Only one daemon may own a yard.
A successful build reaches Review; a failed check shows its evidence. Human
review/completion is a separate records integration, described in
[FORMATS](../turntable/FORMATS.md). Never interpret Review as merged or Done.

## Optional Linux/systemd user installation

Keep the source checkout in a stable location. This command records its actual
path, the chosen yard, service PATH, adapter selection, and port:

```sh
ops/install.sh --yard "$PWD/yard" --port 8877 --adapters codex
ops/check.sh
```

The installer defaults to your current PATH; it assumes no adapter symlinks or
version-manager installation location. Select `--adapters claude` or
`--adapters codex,claude` as appropriate. `--codex /path/to/executable` and
`--claude /path/to/executable` override discovery. These values are literal
executable names or paths, not shell commands with appended flags.

A note directory is **disabled by default**. Enable one only by passing
`--vault /path/to/chosen-notes`. This is permission for the documented capture
and projection writes. Omitting `--vault` on reinstall disables the integration.

Files are installed under `$XDG_CONFIG_HOME/depot/config.env`,
`$XDG_CONFIG_HOME/systemd/user/`, and
`$XDG_DATA_HOME/applications/depot-floor.desktop`, with normal XDG defaults.
The configuration is written with mode 0600. Services load the same configuration
that the checker and desktop launcher read. Re-run the installer after moving
the source checkout or changing settings; it reloads and restarts both services.

The operational yard must not be the demo directory. Do not run real dispatches
against seeded display fixtures.

## Inspect installation output without starting anything

```sh
ops/install.sh --prefix "$PWD/.verification/install" --no-start \
  --yard "$PWD/.verification/yard" --port 8877 --adapters none
```

This writes only the staged files and empty yard at those explicitly chosen
locations. It does not contact systemd. Staged units still refer to the source
checkout and chosen yard; staging is for inspection, not a relocatable package.
`--no-start` without a prefix writes the normal user installation, so use the
prefix when reviewing files in isolation.

## Service operation and removal

```sh
systemctl --user status depot-poll depot-floor
systemctl --user restart depot-poll depot-floor
journalctl --user -u depot-poll -f
systemctl --user disable --now depot-poll.service depot-floor.service
```

To remove the installation, delete its two unit files, desktop entry, and
`depot/config.env`, then run `systemctl --user daemon-reload`. Keep or back up the
chosen yard separately. Startup before login requires the machine's user-service
lingering policy; the installer does not alter that policy.

A stop/restart can interrupt a sufficiently long run when systemd's stop timeout
expires. Prefer restarting between dispatches. The floor remains loopback-only;
changing its port does not change its bind address.
