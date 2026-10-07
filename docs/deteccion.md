# Auto-detection without stack.yaml

**English** · [Español](es/deteccion.md)

When a project has no `stack.yaml`, StackHelx infers services from the files on
disk. The summary table is in the [README](../README.md#without-stackyaml). This
document explains why each detector matches what it does and rejects everything
else, which is required reading before adding a new detector.

One rule governs every detector below: **only detect services that listen on a
port.** Starting a binary that never opens a socket leaves the orchestrator
waiting on a healthcheck until it times out.

## Where it looks

If the project root has no `package.json`, StackHelx checks one level down in
`frontend/`, `web/`, `client/`, `ui/`, `front/`, `site/`, and the direct
children of `apps/`, `packages/`, and `services/`. Each matching directory
becomes a service named after its folder. If the root itself has a runnable
`package.json`, the root wins and subdirectories are skipped: in a monorepo, the
root `dev` script is usually the workspace orchestrator (`turbo`, `nx`), so
starting child packages as well would launch everything twice.

Backend detection follows the same pattern across `backend/`, `api/`, `server/`,
and the children of the workspace directories. It recognizes Django
(`manage.py`), FastAPI/ASGI (`uvicorn` with an ASGI module), Go (`go.mod` with a
`main` package), Rust (`Cargo.toml` with `src/main.rs`), Rails
(`config/application.rb`), Laravel (`artisan`), ASP.NET Core (`.csproj` with
`Sdk="Microsoft.NET.Sdk.Web"`), JVM web frameworks, Elixir/Phoenix, Deno, and
Bun.

## By language

- **Python**: Prefixes commands with `uv run` when the project contains
  `uv.lock` or a `[tool.uv]` section in `pyproject.toml`. Applies to both Django
  and ASGI services so the command runs inside the project's managed environment
  rather than the system Python.
- **Rust**: Requires a web framework declared in `Cargo.toml` (`axum`,
  `actix-web`, `rocket`, `warp`, `tide`, `poem`, `salvo`, `tonic`, `trillium`,
  `gotham`, `volo-http`). Rust has no HTTP server in its standard library, so a
  binary without one does not serve a port. `hyper` is excluded intentionally:
  it is used just as often as an HTTP client in CLI tools.
- **Go**: A dependency list alone is not enough because `net/http` is in the
  standard library and leaves no trace in `go.mod`. StackHelx checks for known
  frameworks (`gin`, `echo`, `fiber`, `chi`, `gorilla/mux`) or a call to
  `ListenAndServe` / `http.Serve(` in the entry file.
- **Rails & Laravel**: `config/application.rb` and `artisan` only exist in
  runnable web applications, whereas a standalone `Gemfile` or `composer.json`
  might just be a library. Rails runs via `bundle exec rails server` rather than
  the `bin/rails` shebang script so it works natively on Windows.
- **JVM (Java & Kotlin)**: Both share the same build systems (`pom.xml`,
  `build.gradle`, `build.gradle.kts`). Requires a web framework marker: Spring
  Boot (`spring-boot-starter-web`, which also matches `-webflux`), Quarkus,
  Micronaut, or Ktor. Bare `spring-boot-starter` is excluded because batch jobs
  and queue consumers use it without opening a port. Prefers `mvn` or `gradle`
  from `PATH` before falling back to `./mvnw` / `mvnw.cmd` so `stackhelx init`
  produces a cross-platform `stack.yaml`.
- **Elixir**: Requires Phoenix, matched via `{:phoenix,` in `mix.exs` (with the
  comma, so component libraries using `phoenix_html` or `phoenix_live_view` are
  ignored) or a `lib/<name>_web/` directory in umbrella apps.
- **.NET**: Checks the `Sdk` attribute of `.csproj`. Libraries and console apps
  use `Microsoft.NET.Sdk`; web applications use `Microsoft.NET.Sdk.Web`.
- **Node subdirectories**: Subfolders must also declare a known dev server
  dependency (`vite`, `next`, `nuxt`, `astro`, `@nestjs/cli`, `express`,
  `fastify`, `hono`, etc.). Monorepo workspaces contain as many libraries as
  apps, and a library with `"dev": "tsc --watch"` would hang waiting for a port
  that never opens.
- **Deno**: Recognizes `deno.json` or `deno.jsonc` with `dev`, `start`, or
  `serve` tasks (`deno task <name>`), or falls back to `deno run --allow-net
  <entry>` for standalone entry files (`main.ts`, `server.ts`, `app.ts`) when no
  `package.json` is present.
- **Bun**: Projects with `package.json` and a server script go through the Node
  detector using `bun` as the package manager (`bun run dev`). The standalone
  Bun detector handles projects without `package.json` scripts where Bun executes
  an entry file directly (`bun run index.ts`), provided a Bun marker
  (`bunfig.toml`, `bun.lock`, `bun.lockb`) and either `Bun.serve(`,
  `export default { fetch }`, or a web framework (`hono`, `elysia`) are present.

Directory scanning is strictly non-recursive beyond one workspace level so it
never traverses `node_modules`.

## Docker Compose

A Compose file is not treated as a single opaque block: each container becomes
its own service with its published port, health status, and browser link, while
startup order comes from `depends_on`. `docker compose up -d <name>` starts each
container idempotently. Port expressions like `${WEB_PORT:-8080}` are resolved
against the host environment, the project `.env` file, and the inline default,
matching Compose's own precedence.

## How ports are discovered

Ports for `npm run dev` or `uvicorn` are never guessed by parsing config files:
StackHelx starts the process with `ready: listen` and queries the OS socket
table to see which port the process tree actually bound. That works reliably
even when Vite sees port 5173 occupied and shifts to 5174. The trade-off is that
`ready: listen` ports cannot be pre-freed before startup because the port number
is only known after the process binds it. Compose ports are declared statically
and freed beforehand.

`stackhelx init` writes the detected stack to `stack.yaml` for manual editing
and refuses to overwrite an existing file.
