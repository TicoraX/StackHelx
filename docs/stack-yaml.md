# stack.yaml Reference

**English** · [Español](es/stack-yaml.md)

A minimal example is in the [README](../README.md#stackyaml). Below is the
complete specification for every field, alongside the annotated template in
[`stack.example.yaml`](../stack.example.yaml).

## Fields

`command` is the only required field on a service. `needs` defines the startup
order, and dependency cycles fail immediately when loading the file rather than
mid-startup. Profiles pull in their transitive dependencies automatically:
requesting `api` without its database is never what you want.

Services that do not depend on each other start in parallel within the same
topological wave, and each wave waits until all its services pass their
healthcheck before the next wave begins. Startup time equals the slowest
healthcheck in each wave rather than the sum of all services. Logs are prefixed
and color-coded per service.

`default` is optional and lists which services start when no `--profile` flag is
passed. Without `default`, every declared service starts.

## Profiles from an auto-detected Compose file

When a `compose.yaml` uses `profiles:`, StackHelx preserves that behavior:

```yaml
services:
  db: { image: postgres }
  seed:
    image: seed
    profiles: [tools]
```

Here `seed` stays off during a default startup and joins when you run
`stackhelx up --profile tools`, matching `docker compose --profile tools`.
Notice the inverted semantics: in Compose, `profiles:` **excludes** a container
from default startup until requested, whereas in `stack.yaml` a profile is the
explicit list of services to start. `detect` translates between the two and
writes a `default` list when you run `stackhelx init` so frozen files do not
accidentally boot optional containers by default.

## ready

`ready` controls when a service is considered healthy and accepts five formats:

| Value | Waits until |
|---|---|
| `port` | The declared TCP port accepts connections (default when `port` is set) |
| `listen` | The process tree binds any TCP port, then reports which port it chose |
| `log:<text>` | `<text>` appears in the service's stdout/stderr |
| `http://...` | The URL responds with an HTTP status code below 400 |
| `none` | Immediately ready without waiting (default when `port` is omitted) |

`listen` is designed for dev servers that pick their own port at runtime. It
cannot be combined with `port`: if you already know the port, use `port`.

`port` checks whether a listener accepts connections on that port. If another
process was already listening before startup (for example when running with
`--no-free` or when a Docker container was already up), StackHelx notes it in
the startup log: `listo (3000) · el puerto ya estaba ocupado antes de arrancar`.

## stop

`stop` is an optional custom shutdown command. Without it, StackHelx terminates
the service's process tree, which works for `npm run dev` but not for detached
containers (`docker compose up -d` exits immediately while the container lives
inside the Docker daemon). Auto-detected Compose services automatically include
`stop: docker compose stop <name>`. If a `stop` command fails or exceeds 90
seconds, StackHelx logs a warning and continues shutting down the remaining
services.

## env_file

`env_file` loads environment variables from one or more files (e.g.
`env_file: .env` or `env_file: [.env, .env.local]`).

Paths must be **relative to the project root and cannot escape it**:
`../../.env` or an absolute path raises a `ConfigError` when loading the file,
just like `cwd`. An untrusted `stack.yaml` cannot read `.env` files from your
other projects.

Variable precedence (from lowest to highest):
1. Host `os.environ`.
2. `~/.stackhelx/env.global` (global shared environment file, if present).
3. Files listed in `env_file` (in declaration order).
4. Explicit `env:` key-values declared on the service.

One exception: after merging all four layers, `build_env` sets
`PYTHONUNBUFFERED=1` and `FORCE_COLOR=1` so service logs stream to the terminal
and web dashboard in real time instead of buffering.

## url

Controls where the `Abrir` button and `stackhelx open` navigate. Defaults to
`http://localhost:<port>`, which works for most services but falls short when an
app mounts on a subpath or expects a query parameter:

```yaml
services:
  studio:
    command: uv run python ui/server.py 8765
    port: 8765
    env_file: [.env]
    url: http://127.0.0.1:8765/?token=${ORQUESTER_TOKEN}
```

Rules:

- Only `http://` and `https://` schemes are allowed. Any other scheme raises a
  `ConfigError` on load.
- Supports `${VAR}` and `${VAR:-default}`, interpolated against **the exact
  environment passed to the service** (`env`, `env_file`, `env.global`, and host
  env).
- Cannot be combined with `ready: listen`. Because a `listen` service picks its
  port dynamically at runtime, hardcoding a port in `url:` would override the
  discovered port and point the button to the wrong address.
- If a referenced `${VAR}` is unset and has no default, the service URL resolves
  to `None`. If `port:` is set, it falls back to `http://localhost:<port>`;
  otherwise the service is skipped by `stackhelx open`, preventing the browser
  from opening a broken literal `${TOKEN}` URL.

## pre_start and post_start

Synchronous lifecycle hooks:
- `pre_start`: Command executed before spawning the main service process (e.g.
  database migrations or build steps). If it exits with a non-zero code, startup
  aborts immediately.
- `post_start`: Command executed right after the service passes its `ready`
  healthcheck. If it fails, the error is reported and the stack shuts down.

## restart and max_retries

`restart` controls what happens when a service exits on its own: `no` (default),
`on-failure`, or `always`. `on-failure` restarts when the exit code is non-zero;
`always` restarts regardless of exit code. `max_retries` caps the number of
restart attempts (default `3`).

```yaml
services:
  worker:
    command: python worker.py
    ready: none
    restart: on-failure
    max_retries: 2
```

When a process exits with code 3:

```
worker | proceso terminado con codigo 3. Reiniciando automaticamente (intento 1/2)...
worker | proceso terminado con codigo 3. Reiniciando automaticamente (intento 2/2)...
```

Once `max_retries` is exhausted, that service stays down while the rest of the
stack keeps running. The stack only exits when no live services remain.

Three constraints:
1. The restart supervisor lives inside the log follower, so `restart` is active
   while `stackhelx up` is attached or while `stackhelx serve` holds the session.
2. The retry counter does not reset over time.
3. `detached: true` services are excluded from automatic restarts because their
   launch command exits immediately by design.

## scripts

The `scripts` section declares development tasks or sequential pipelines:

```yaml
scripts:
  test: pytest tests/ -v
  lint: ruff check .
  check: [lint, test]           # runs lint, then test if lint succeeds
  migrate: alembic upgrade head
```

Run them with `stackhelx run <name>` (e.g. `stackhelx run test`). Each command
executes at the project root with the full injected environment.

## includes

Compose multi-repo or modular stacks by importing services from other
directories:

```yaml
includes:
  - ../auth-service
  - ./services/payments
```

Each path is resolved relative to the parent `stack.yaml`. Imported services run
in their own working directory (`cwd`) and can be referenced in `needs:` across
the stack. Circular includes are detected and rejected at load time.
