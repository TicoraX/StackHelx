# Detección sin stack.yaml

Cuando un proyecto no tiene `stack.yaml`, StackHelx infiere los servicios de
lo que encuentra en el disco. La tabla de qué reconoce está en el
[README](../README.md#sin-stackyaml). Acá está por qué reconoce eso y no otra
cosa, que es lo que hay que leer antes de agregar un detector.

La regla que ordena todo lo de abajo: solo se detecta lo que sirve en un
puerto. Un binario que no abre ninguno, arrancado como servicio, deja al stack
esperando un healthcheck que no va a llegar nunca.

## Dónde busca

Si la raíz no tiene `package.json`, busca una vuelta más abajo: `frontend/`,
`web/`, `client/`, `ui/`, `front/`, `site/`, y los hijos de `apps/`,
`packages/` y `services/`. Cada app encontrada es un servicio con el nombre de
su carpeta. Si la raíz sí tiene `package.json`, gana ese y no se baja: en un
monorepo su script `dev` suele ser el orquestador (turbo, nx) y arrancar además
los hijos duplicaría todo.

El backend sigue la misma regla, en `backend/`, `api/`, `server/` y los hijos de
los mismos grupos. Reconoce Django (`manage.py`), FastAPI (`uvicorn` con un
módulo ASGI), Go (`go.mod` con un paquete `main`),
Rust (`Cargo.toml` con `src/main.rs`), Rails (`config/application.rb`), Laravel (`artisan`)
y ASP.NET Core (un `.csproj` con `Sdk="Microsoft.NET.Sdk.Web"`).

## Por lenguaje

En Python el comando lleva `uv run` adelante cuando el proyecto tiene `uv.lock`
o una sección `[tool.uv]` en el `pyproject.toml`. Vale para los dos casos que se
detectan, Django y ASGI, porque es el mismo prefijo: con `uv` el intérprete del
proyecto vive en su entorno y un `python manage.py` a secas correría con el del
sistema, que no tiene las dependencias.

En Rust hace falta un framework declarado en el `Cargo.toml`: axum, actix-web,
rocket y compañía. No hay servidor HTTP en la stdlib, así que sin uno el binario
no sirve nada por un puerto. `hyper` no cuenta, aunque sea la base de casi todo
el HTTP de Rust: entra como cliente tan seguido como de servidor, y un CLI que
descarga algo declara exactamente la misma dependencia.

En Go no alcanza con la lista, porque `net/http` es stdlib y un servidor escrito
con ella no deja rastro en `go.mod`: se busca además la llamada a
`ListenAndServe` en el fuente. Un binario que no sirve nada no se detecta,
porque arrancarlo dejaría al stack esperando un puerto que nunca abre.

Rails y Laravel no tienen ese problema: `config/application.rb` y `artisan` solo
existen en aplicaciones que sirven, y un `Gemfile` o un `composer.json` sueltos
no alcanzan. Rails arranca con `bundle exec rails server` y no con el binstub
`bin/rails`, que es un script con shebang y en Windows no lo ejecuta nadie.

En la JVM da lo mismo Java que Kotlin: el build es el mismo y `build.gradle.kts`
sólo cambia la extensión. Hace falta un framework declarado, porque un `pom.xml`
o un `build.gradle` sueltos pueden ser una librería o una app de consola: Spring
Boot (`spring-boot-starter-web`, que también cubre `-webflux`), Quarkus,
Micronaut o Ktor. `spring-boot-starter` a secas no cuenta, y esa es la
diferencia que importa: es una app de Spring sin servlet container, una tarea
batch o un consumidor de colas, y no abre ningún puerto.

El comando prefiere `mvn` o `gradle` del PATH, y sólo cae al wrapper del repo
cuando no están. No es una preferencia de estilo. El comando detectado termina
en el `stack.yaml` que escribe `stackhelx freeze`, ese archivo se commitea, y
lo abre alguien en otro sistema operativo: `mvn spring-boot:run` es igual en los
tres, mientras que `./mvnw` no corre en `cmd.exe` y `mvnw.cmd` no corre en bash.
Un repo que commiteó sólo el wrapper de POSIX, visto desde Windows, cae al
binario pelado por la misma razón. Sin binario y sin wrapper se emite igual el
nombre a secas: fallar con "command not found" dice más que no detectar nada.

En Elixir hace falta Phoenix, y la señal es `{:phoenix, ...}` en el `mix.exs`,
con la coma. No alcanza con que diga "phoenix" en algún lado: una librería de
componentes declara `phoenix_html` o `phoenix_live_view` sin ser una aplicación,
no tiene endpoint y `mix phx.server` ahí falla. La otra señal aceptada es
`lib/<algo>_web/`, que Phoenix genera siempre y que sirve para el proyecto de un
umbrella, donde las dependencias viven en el `mix.exs` de la raíz. Un `mix.exs`
solo es una librería o una app OTP sin puerto, y no se detecta.

En .NET la señal está en el atributo `Sdk` del `.csproj` y en ningún otro lado:
una librería y una app de consola usan `Microsoft.NET.Sdk` a secas, y ni el
nombre del proyecto ni sus paquetes distinguen una cosa de la otra.

En las subcarpetas hace falta además una dependencia que declare un servidor de
desarrollo (vite, next, nest, astro, nodemon y compañía). Un workspace tiene
tantas librerías como apps, y una librería con `dev: tsc --watch` entraría como
servicio y se quedaría esperando un puerto que nunca abre.

Con Bun hay dos caminos distintos y conviene no confundirlos. Un proyecto con
`package.json` y un script (`dev`, `serve`, `start`) sale por el camino de Node,
y el `bun.lock` sólo decide el gestor: el comando queda `bun run dev`. El
detector propio de Bun es para lo otro, el proyecto sin `package.json`, sin
`scripts`, o con scripts que no sirven nada, donde Bun ejecuta el archivo
directo: `bun run index.ts`.

Hace falta una marca de Bun (`bunfig.toml`, `bun.lock` o `bun.lockb`) y además
la señal de que eso sirve por un puerto, que es la llamada a `Bun.serve(` en el
fuente o un framework declarado (hono, elysia). Es el mismo par que Go: `Bun.serve`
es API nativa y no figura en ninguna dependencia, igual que `net/http`. Sin
ninguna de las dos señales es una CLI y no se detecta.

Nada de esto es recursivo: un scan profundo termina dentro de `node_modules`.

## Compose

Un compose no entra como un bloque único: cada contenedor es un servicio con su
puerto publicado, su estado y su link, y el orden sale del `depends_on` del
propio archivo. `docker compose up -d <nombre>` arranca ese contenedor con sus
dependencias y es idempotente. Los puertos escritos como `${WEB_PORT:-8080}` se
resuelven con el entorno, con el `.env` del proyecto y por último con el default
de la expresión, el mismo orden que usa compose.

## De dónde sale el puerto

El puerto de `npm run dev` y de `uvicorn` no se adivina: se arranca el proceso y
se le pregunta cuál quedó escuchando. Es más confiable que parsear
`vite.config.ts`, los flags del script y `.env`, y acierta cuando Vite encuentra
5173 tomado y se corre a 5174. El costo es que esos puertos no se pueden liberar
antes de arrancar, porque no se saben hasta después. Los de compose sí, que
están declarados en el archivo.

`stackhelx init` escribe lo detectado como `stack.yaml` para editarlo a mano.
No sobreescribe uno existente.
