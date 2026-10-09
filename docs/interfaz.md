# Web dashboard

**English** · [Español](es/interfaz.md)

How to launch the dashboard and what it displays is covered in the
[README](../README.md#web-dashboard). This document explains how each control
behaves under the hood and why.

## Language selector (EN / ES)

The header includes an `EN / ES` toggle that switches every label, status badge,
tooltip, and modal between English and Spanish in real time without reloading the
page. The dashboard defaults to English and persists your choice in
`localStorage` (`stackhelx.lang`).

## Crashes and browser notifications

When a running service exits unexpectedly, the browser tab title shows a counter
and the header reports how many services have fallen. Clicking `Notify me`
requests browser notification permissions so you get alerted on unexpected
crashes, never when you click `Stop` or `Restart` yourself. Permission is
requested only on that click, never on page load.

## Freezing to stack.yaml

Auto-detected projects show a `Freeze to stack.yaml` button, which runs
`stackhelx init` directly from the browser. Useful when you want to customize a
detected command or pin a port. It asks for confirmation inline on the same
button, writes to the registered project path on the server (never trusting a
path sent from the client), and refuses to overwrite an existing file.

## Restarting a single service

Every service started from the dashboard has its own `Restart` button that
cycles only that service. When your frontend hangs, your database containers do
not need to restart with it.

## The Open button

Services that answer HTTP display an `Open` button, and the project card has a
top-level `Open` button that opens the last HTTP-capable service in startup
order (typically the frontend). StackHelx does not guess by service name: once a
service is ready, it probes the bound port over HTTP. Even a `404` counts as an
HTTP server because many APIs do not serve a root route; only non-HTTP listeners
like databases are excluded.

## Docker controls

When any registered project uses Docker, the toolbar displays `Docker running`
or `Docker stopped` alongside an action button: `Start Docker` when the daemon
is down, or `Restart Docker` when the daemon is up. Both controls remain
visible whenever a project uses Docker: a status indicator that disappears when
healthy cannot distinguish "everything is fine" from "the check stopped
working".

The stray processes section (`Stray processes`) follows the same principle: as
long as at least one project is registered, the section stays visible and
explicitly states when zero stray processes are holding your ports.

Restarting Docker asks for inline confirmation because it restarts every running
container on the machine, including containers from other projects. Starting
Docker runs immediately without confirmation.

Note that `Docker stopped` checks whether the Docker daemon answers `docker
info`, not whether the Docker Desktop window is open.

Under the hood, StackHelx runs `docker desktop start --detach` or
`docker desktop restart --detach` via the official Docker CLI plugin, returning
immediately so the HTTP request does not block for 30 seconds while the engine
boots.

## Folder browser

You do not need to copy-paste paths to register a project: `Browse…` opens a
directory browser starting at your home folder and mounted drives, highlighting
folders that contain `stack.yaml`, a Compose file, `package.json`, or
`manage.py`. The server builds the listing and returns only folder names and
marker filenames, never file contents.

## Security model

The server binds strictly to loopback (`127.0.0.1`) and requires a 32-byte token
stored in `~/.stackhelx/token` with `0600` permissions (or custom via
`STACKHELX_TOKEN`). Because `stack.yaml` executes shell commands, the API
enforces per-route rate limiting (`QUOTA_READ`, `QUOTA_WRITE`, `QUOTA_KILL`), a
strict local-only Content Security Policy (`default-src 'self'`), and `Host`
header validation against `127.0.0.1`, `localhost`, and `[::1]` to block DNS
rebinding attacks.
