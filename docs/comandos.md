# Other commands

**English** · [Español](es/comandos.md)

> **Note:** Every command can be run either as `stackhelx <cmd>` or via the official short alias **`shx <cmd>`** (for example: `shx down`, `shx doctor`, `shx switch`, `shx run`, `shx share`).

`up`, `serve`, `ports`, and `free` are covered in the [README](../README.md#commands).
Below are the remaining commands, what each one checks, and why.

## Stopping services that outlive the terminal

```bash
shx down
# or stackhelx down
shx down --profile backend
```

Pressing `Ctrl-C` on `stackhelx up` terminates its child processes, but
`docker compose up -d` exits immediately and leaves containers running in the
background. `down` runs the `stop` command of every service that declares one,
in reverse startup order. If no service declares `stop`, it reports that and
exits: those services are direct children of the terminal and already stopped
with `Ctrl-C`.

## Switching between projects

```bash
stackhelx switch fitness
stackhelx switch A:\Proyectos\Fitness    # or the path, if two projects share a name
```

Stops registered projects that declare any of the ports required by the target
project, then starts the target. Only colliding projects are stopped: shutting
down an uncontested database does not help startup and is the slowest service to
bring back up.

It stops services that declare `stop` (containers). An `npm run dev` running in
another terminal is not managed by `down`, so if it still holds a needed port,
the port-freeing step of `up` catches it and asks before closing anything.

## Diagnostics

```bash
stackhelx doctor
```

Checks common startup blockers without starting anything: which stack is loaded
or auto-detected, whether every service binary exists in `PATH`, whether the
Docker daemon responds, and which declared ports are currently occupied and by
whom. Every failed check includes the exact command to fix it.

```
ok    token                  C:\Users\vos\.stackhelx\token
ok    stack                  detectado (3 servicios)
ok    comando docker         C:\Program Files\Docker\...\docker.EXE
FALLA daemon de docker       no esta en ejecucion
                             -> abri Docker Desktop
aviso puerto 3000            ocupado por node.exe (pid 24180), lo pide web
                             -> stackhelx free 3000
```

If `.env.example` exists, `doctor` compares its keys against `.env` and flags
which keys are missing or empty. Only key names are printed: secret values never
appear in terminal output or API responses.

Exits with code 1 only when a check prevents startup. An occupied port is a
warning because `stackhelx up` offers to free it, and a missing `.env` key is a
warning because it may be optional or provided by the host environment. Outside
a project directory, `doctor` checks the global environment only.

## Opening the stack in your browser

```bash
stackhelx open         # last service in the stack that answers HTTP
stackhelx open 3000    # or a specific port
```

Useful when your stack is already running in another terminal. It probes ports
in reverse startup order because the service you want to view in a browser is
almost always the frontend, opening the first one that responds to HTTP. A
database does not speak HTTP, so it is skipped automatically.

## Running scripts and task pipelines

```bash
stackhelx run              # list tasks declared in stack.yaml
stackhelx run test         # run a specific task
stackhelx run test -k foo  # forward extra arguments to the command
stackhelx run check        # run a sequential pipeline of scripts
```

Lets you define project tasks in `stack.yaml` (tests, linters, migrations,
seeders) and execute them with the full injected environment (`.env`,
`env.global`). If any step in a pipeline returns a non-zero exit code, execution
stops immediately with that exit code.

## Sharing services via public tunnels

```bash
stackhelx share               # expose the main web service
stackhelx share 3000          # expose a specific port
stackhelx share api           # expose a service by name
stackhelx share --provider ngrok  # force provider (cloudflared, ngrok, lt, tailscale)
```

Opens an ephemeral HTTPS tunnel to a local port for testing webhooks, sharing
previews, or testing on mobile devices. Press `Ctrl-C` to close the tunnel
immediately.

**You cannot share the `stackhelx serve` port.** Behind that port sits the API
that executes commands from your `stack.yaml`: exposing it would leave the token
as the only barrier between the public internet and your shell. This block
applies to both the CLI and the web dashboard:

```console
$ stackhelx share 7667
Iniciando tunel hacia 127.0.0.1:7667...
Error: el puerto 7667 es de un `stackhelx serve`. Publicarlo expone la API que
ejecuta los comandos de tu stack.yaml, no tu proyecto.
```

## Cleaning Docker resources

```bash
stackhelx clean                        # stopped containers, untagged images, unused networks, build cache
stackhelx clean --solo cache --solo images   # only those two categories
stackhelx clean --volumes              # also prune anonymous/orphaned volumes
```

Cleans **by category**, running a dedicated command for each instead of a
blanket `docker system prune` that wipes everything together. Deleting stopped
containers is not the same as wiping build cache or untagged base images you may
need on the next build.

**Volumes are always separate** and never included by default: they hold state
that cannot be regenerated. You must opt in with `--volumes`; in the web
dashboard they have their own checkbox, and the MCP server rejects volume
deletion altogether.

Before deleting anything, `clean` prints `docker system df` so you can see what
is at stake and asks for confirmation (`--yes` skips the prompt in scripts). If
one category fails, the remaining categories still run and the summary reports
which one failed.

## Validating the stack without starting it

```bash
stackhelx test-stack
stackhelx test-stack ../other-project
```

Loads `stack.yaml`, resolves the topological startup order, and checks whether
declared ports are free. Because it starts nothing, it is the fastest way to
verify a `stack.yaml` you just edited.

```
Validando stack demo en C:\...\demo...
OK: 1 servicio(s) resueltos en orden topológico:
  - api: python -c "..." -> puerto 8123
OK: Todos los puertos declarados están libres

Stack validado con éxito.
```

An invalid file exits with code 1 and prints the exact validation error that
`up` would show:

```
Configuración inválida: services.x.restart debe ser 'no', 'on-failure' o 'always'
```

Occupied ports are reported as warnings rather than errors because `up` can free
them interactively before starting.

## Startup history

```bash
stackhelx history
stackhelx history --limit 20
```

```
            Historial de arranques: demo
+--------------------------------------------------+
| Fecha            | Perfil | Duración | Resultado |
|------------------+--------+----------+-----------|
| 2026-08-29 05:38 | -      | 7.4s     | running   |
+--------------------------------------------------+
```

**History is recorded by the web dashboard, not the CLI.** Every startup
triggered from `stackhelx serve` appends an entry with its duration and outcome;
running `stackhelx up` in a terminal does not write history. If you have only
used the CLI, `history` prints `No hay historial para el proyecto <nombre>`.

History files live in `~/.stackhelx/history/<id>.jsonl`, one per project, and
automatically rotate to the last 250 runs. `--limit` accepts values from 1 to 50.

## Live logs and metrics from the web server

```bash
stackhelx logs                    # print buffered logs
stackhelx logs --follow           # stream live logs
stackhelx logs --service api      # filter by service name
stackhelx stats                   # CPU and memory table (alias: stackhelx top)
```

Both commands query the active `stackhelx serve` instance, so `serve` must be
running. Use `--port` if `serve` is listening on a custom port:

```
       Métricas en tiempo real: demo
+-----------------------------------------+
| Servicio | PID  |  CPU % | Memoria (MB) |
|----------+------+--------+--------------|
| api      | 7532 | 108.8% |      25.6 MB |
+-----------------------------------------+
```

Metrics aggregate the **entire process tree** of each service, not just the
direct child PID: with `shell=True`, the immediate child is the shell wrapper
and the actual server is a grandchild process. CPU percentage sums usage across
all cores, so values above 100% indicate multi-core utilization.

If `serve` is not running, both commands exit with code 1:

```
No se pudo conectar con StackHelx en http://127.0.0.1:7666.
Asegúrate de que `stackhelx serve` está corriendo.
```

## MCP Server for AI Agents

```bash
stackhelx mcp
stackhelx mcp --config   # print JSON config for your MCP client
stackhelx mcp --prompt   # print recommended system prompt for AI agents
```

Starts a standard Model Context Protocol (MCP) server over `stdio`. Enables AI
coding assistants (Claude Desktop, Claude Code, Cursor, Gemini, Antigravity) to
inspect stack status, execute tasks declared in `stack.yaml`, scan ports, and
diagnose errors in real time.

The nine exposed tools (available under `stackhelx_*` and legacy `portmaster_*`
aliases):

| Tool | What it does |
|---|---|
| `stackhelx_status` | Status of services, registered projects, and ports |
| `stackhelx_doctor` | Environment diagnostics with actionable remediation per check |
| `stackhelx_ports` | Scans specific ports or the ports declared by the stack |
| `stackhelx_free_port` | Terminates the process holding a port |
| `stackhelx_share` | Opens a public tunnel to a local port |
| `stackhelx_run` | Runs a script or pipeline from `stack.yaml` |
| `stackhelx_clean` | Cleans Docker resources |
| `stackhelx_history` | Reads startup history for the project |
| `stackhelx_init` | Freezes auto-detected services into a `stack.yaml` |

Every tool accepts an optional `path` argument; when omitted, it operates on the
current working directory.

### Guardrails on agent actions

Because an autonomous agent is calling these tools rather than a human at a
terminal, three restrictions are enforced in code:

- **`stackhelx_share` only publishes ports declared by the project.** Requesting
  an undeclared port returns an error, and the `stackhelx serve` port is blocked
  explicitly.
- **`stackhelx_clean` cannot delete volumes.** The `--volumes` flag exists in
  the CLI but is omitted from the MCP tool schema.
- **Rate-limited to 30 calls per minute.** Exceeding that threshold returns a
  rate-limit error to stop runaway agent loops.

Any tunnel opened during an MCP session is closed automatically when the session
ends so tunnel processes never outlive the agent.
