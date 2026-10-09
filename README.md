# StackHelx

[![pypi](https://img.shields.io/pypi/v/stackhelx?logo=pypi&logoColor=white)](https://pypi.org/project/stackhelx/)
[![tests](https://github.com/TicoraX/StackHelx/actions/workflows/ci.yml/badge.svg?branch=main&cache=none)](https://github.com/TicoraX/StackHelx/actions/workflows/ci.yml)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

**English** · [Español](README.es.md)

Local development environment orchestrator. One file at the project root, one
command, and your entire stack is up: free ports, Docker, backend, and frontend,
without juggling four open terminals.

## Installation

```bash
uv tool install stackhelx
# or
pipx install stackhelx
```

Installing registers two identical executables on your system: the full command
**`stackhelx`** and its short alias **`shx`**.

Requires Python 3.10 or later. Runs on Windows, macOS, and Linux.

## Commands

Every command runs as `stackhelx <command>` or via its official short alias
**`shx <command>`** (e.g. `shx up`, `shx down`, `shx doctor`, `shx ports`):

| Command (`stackhelx` / `shx`) | What it does |
|---|---|
| `stackhelx up` | Boots the entire stack: frees ports, starts services in topological order, and tails logs |
| `stackhelx down` | Stops services that outlive the terminal, such as containers |
| `stackhelx serve` | Opens the web dashboard at `http://127.0.0.1:7666` |
| `stackhelx doctor` | Checks what could block startup without starting anything |
| `stackhelx ports` | Shows the status of declared ports |
| `stackhelx free 3000` | Terminates the process holding a port, asking first |
| `stackhelx free --all` | Frees all occupied ports across all registered projects |
| `stackhelx switch fitness` | Stops registered projects that collide on ports with the target, then boots it |
| `stackhelx open` | Opens the first service that answers HTTP in your browser |
| `stackhelx init` | Freezes auto-detected services into an editable `stack.yaml` |
| `stackhelx add .` | Registers the project so it appears in the web dashboard |
| `stackhelx list` | Lists registered projects (alias: `ls`) |
| `stackhelx remove .` | Unregisters a project (alias: `rm`) |
| `stackhelx run [task]` | Runs project scripts or sequential task pipelines |
| `stackhelx share [target]` | Exposes a local service to the internet over an ephemeral tunnel |
| `stackhelx clean` | Cleans Docker resources by category: stopped containers, untagged images, unused networks, and build cache. Volumes are separate via `--volumes`. Prompts before deleting |
| `stackhelx mcp` | Starts the Model Context Protocol (MCP) server over stdio for AI agents |
| `stackhelx test-stack` | Validates `stack.yaml` without starting anything: topological order, dependencies, and ports |
| `stackhelx history` | Shows recent stack runs with duration and final state |
| `stackhelx logs` | Reads logs from a project running in `serve` (`--follow` to stream) |
| `stackhelx stats` | Displays real-time CPU and memory usage for services running in `serve` (alias: `top`) |
| `stackhelx version` | Prints the installed version (also `--version`) |

`logs` and `stats` query a running `stackhelx serve` instance, so `serve` must
be active. `history` and `test-stack` read directly from disk.

Pass `--help` to any command for full flag details.

## Starting a stack

```bash
shx up
# or
stackhelx up

shx up --profile backend    # start only a subset
shx up --no-free            # leave occupied ports untouched
shx up --env-file .env.qa   # load this .env file before booting
```

`--env-file` adds to `env_file:` in `stack.yaml` rather than replacing it: it
loads the file into the process environment before resolving the stack, making
variables visible to all services. Use it for one-off runs against another
environment without editing `stack.yaml`. Unlike `env_file:`, it accepts paths
outside the project root because you type the path yourself at the terminal
instead of inheriting it from an untrusted repository.

Before starting, StackHelx checks every declared port and prompts before
terminating any stray process holding one. Ports already published by Docker are
skipped automatically because the container is already up.

```
demo  stack.yaml
db  | $ docker compose up -d postgres
db  | listo (5432)
api | $ npm run dev
api | escuchando en 8080
api | listo (8080)
web | $ npm run dev
web | listo (3000)
Todo listo. Ctrl-C para apagar.
api | GET /health 200
web | ready in 412 ms
```

Pressing `Ctrl-C` shuts down services in reverse topological order, killing the
entire process tree of each service.

## Without stack.yaml

`stack.yaml` is optional. When none is present, StackHelx inspects the project
root:

| Finds | Starts |
|---|---|
| `compose.yaml`, `compose.yml`, `docker-compose.yml`, `docker-compose.yaml` | One service per container: `docker compose up -d <name>` |
| `manage.py` | `python manage.py runserver` |
| `fastapi` or `uvicorn` declared, with a module defining `app` | `uvicorn <module>:app --reload` |
| `package.json` with a server script (`dev`, `start:dev`, `serve`, `start`) | `npm run dev`, switching to `pnpm`/`yarn`/`bun` based on lockfile or `packageManager` |

```
my-app  A:\Proyectos\my-app
No stack.yaml. Detected:
  docker  docker compose up -d        5433
  web     pnpm run dev                on start
To freeze into an editable file: stackhelx init
Start? [Y/n]
```

Services start in that order and chain dependencies automatically: frontend
waits for backend, and backend waits for containers.

`stackhelx init` writes the detected configuration to `stack.yaml` so you can
edit it by hand. It never overwrites an existing file.

Where StackHelx searches for each language and why it matches specific signals
is documented in [`docs/deteccion.md`](docs/deteccion.md).

## stack.yaml

Place `stack.yaml` at the project root. StackHelx searches upward from your
current working directory, so you can run commands from any subdirectory.

```yaml
name: my-project

services:
  db:
    command: docker compose up -d postgres
    port: 5432
    detached: true       # command exits while the container stays alive

  api:
    command: npm run dev
    cwd: backend
    port: 8080
    needs: [db]
    env:
      DATABASE_URL: postgres://localhost:5432/app

  web:
    command: npm run dev
    cwd: frontend
    port: 3000
    needs: [api]

profiles:
  backend: [api]         # pulls in db automatically via its dependency chain
```

`command` is the only required field. The complete field reference, `ready`
healthcheck modes, and inherited Compose profiles live in
[`docs/stack-yaml.md`](docs/stack-yaml.md).

## Web dashboard

When you work across multiple projects, the CLI only sees the current directory.
The web dashboard shows all registered projects at once.

```bash
stackhelx serve        # opens http://127.0.0.1:7666
```

Included out of the box with no extra dependencies. Register projects directly
from the browser via `Browse…` or from the terminal with `stackhelx add .`. Use
the `EN / ES` toggle in the header to switch the interface between English and
Spanish at any time.

Monitor service states, start and stop stacks, free ports held by stray
processes, inspect system-wide listening ports, and stream live logs per project.

Control details and the local server security model are covered in
[`docs/interfaz.md`](docs/interfaz.md).

## Ports

Inspect port status without starting anything:

```bash
stackhelx ports              # ports declared in stack.yaml
stackhelx ports 3000 8080    # specific ports
```

```
PORT    STATUS    PID    PROCESS   COMMAND
3000    occupied  24188  node.exe  node C:\proj\frontend\node_modules\.bin\vite
8080    free      -      -         -
5432    occupied  9012   com.docker.backend.exe
```

Free a port held by a zombie process:

```bash
stackhelx free 3000
```

Shows the owning process and asks for confirmation before terminating it. If you
decline, it suggests the next available port.

Flags: `--yes` skips confirmation (for scripts), `--force` escalates to `kill()`
when the process ignores graceful termination.

After a crash or branch switch, multiple ports may stay occupied:

```bash
stackhelx free --all
```

Scans every port declared across all registered projects, lists what is occupied,
and asks for a single confirmation. Exits with code 1 if any port could not be
freed.

The CLI does not track which processes you started in other terminals: if another
stack is running in a separate shell, its services appear in that list too. That
is why the CLI prints the full list before touching anything and defaults the
prompt to "no". The web dashboard tracks its own active sessions and excludes
them automatically when clicking "Free all".

## What the kill switch refuses to do

These guardrails are enforced in code:

- Never terminates PID 0, PID 4, StackHelx itself, or any of its parent
  processes. Killing your own terminal is not a feature.
- Revalidates process creation time (`create_time`) between scanning and
  signaling. OS PIDs recycle quickly; without this check, you risk killing an
  unrelated process.
- Sends `terminate()` and waits 5 seconds. Escalates to `kill()` only with
  explicit `--force`, because force-killing an `npm run dev` wrapper leaves
  orphaned child processes behind.
- Fails fast when permissions are insufficient instead of attempting privilege
  escalation.
- Never kills the Docker or WSL proxy process. A port published by a container
  is bound by a shared host proxy process; killing it takes down the entire
  Docker engine. Instead, StackHelx tells you which container to stop.

## Other commands

`down`, `switch`, `doctor`, `open`, `run`, `share`, `clean`, and `mcp` are
detailed in [`docs/comandos.md`](docs/comandos.md).

## Trust model

`stack.yaml` runs arbitrary commands, just like `package.json` or a `Makefile`.
StackHelx does not sandbox them. Treat a `stack.yaml` from an untrusted
repository with the same caution you give its build scripts.

Without `stack.yaml`, commands come from auto-detection, and `scripts.dev` in an
untrusted `package.json` is equally arbitrary. That is why `up` prints the exact
commands it detected and asks for confirmation before executing anything, while
`-y` lets you skip the prompt once you trust the project.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"    # .venv\Scripts\pip on Windows
pytest -q -n auto
```

Tests run against real OS sockets and processes with zero mocks. That is the
only way to verify software whose job is talking to the operating system.

## License

MIT
