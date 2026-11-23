# Linux/systemd user services

Read [installation](../docs/installation.md) before running the installer.
`install.sh` generates units for the actual source checkout and selected yard;
the `.service` files here are templates, not units to copy directly.

```sh
ops/install.sh --yard "$PWD/yard" --port 8877 --adapters codex
ops/check.sh
```

Adapter discovery uses the configured PATH or DEPOT_CODEX / DEPOT_CLAUDE, without
assuming links in a particular bin directory. DEPOT_VAULT is empty by default;
only an explicit `--vault PATH` enables note-directory writes. Configuration,
service units, desktop launcher, and checker share the same port selection.

For review without starting or changing user services:

```sh
ops/install.sh --prefix "$PWD/.verification/install" --no-start \
  --yard "$PWD/.verification/yard" --port 8877 --adapters none
```

No lingering/account policy is changed. The floor remains loopback-only.
`check.sh --config PATH` verifies service state, HTTP response, selected adapter
executables, and that the poll service owns the chosen yard's lock.
