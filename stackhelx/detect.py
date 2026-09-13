"""Deteccion de servicios en un proyecto sin stack.yaml.

Mira la raiz del proyecto y arma el mismo `Stack` congelado que produce
`config.load`, para que `runner`, `server` y `cli` no distingan el origen.

Los detectores corren en el orden en que hay que arrancarlos: contenedores,
backend (Python, Go, Rust, Ruby, PHP, .NET), frontend. Cada uno devuelve los
reconoce, o una lista vacia. El compose devuelve uno por contenedor, para que
cada uno tenga su puerto y su estado propios.

Ninguno detecta un proyecto que no sirva nada por un puerto. Una libreria o una
herramienta de linea de comandos entraria como servicio y el arranque esperaria
un puerto que nunca abre, hasta el timeout.

Ningun detector adivina el puerto de un proceso que no lo declara: compose si lo
declara y se lee del archivo, y los otros dos usan `ready: listen`, que descubre
el puerto real del proceso ya arrancado. Ver el spec en docs/.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
from dataclasses import replace
from pathlib import Path

import yaml

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - solo en Python 3.10
    import tomli as tomllib

from .config import CONFIG_NAMES, ConfigError, Service, Stack, find, load

COMPOSE_NAMES = ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")

# lockfile -> gestor. npm es el fallback: package-lock.json no es obligatorio.
LOCKFILES = {
    "pnpm-lock.yaml": "pnpm",
    "yarn.lock": "yarn",
    "bun.lockb": "bun",
    "bun.lock": "bun",
}

# Donde vive la app ASGI, en orden de preferencia.
ASGI_MODULES = ("main.py", "app.py", "asgi.py", "src/main.py", "api/main.py", "app/main.py")

# Scripts que levantan un servidor, en orden de preferencia. `start:dev` es el
# modo watch de Nest, que no tiene `dev`.
NODE_SCRIPTS = ("dev", "start:dev", "serve", "start")

# Subcarpetas donde vive el frontend de un monorepo, y los grupos cuyos hijos se
# revisan uno por uno.
NODE_DIRS = ("frontend", "web", "client", "ui", "front", "site")
# Sirven para Python, Go y Rust: el nombre de la carpeta no dice el lenguaje.
BACKEND_DIRS = ("backend", "api", "server")
WORKSPACE_DIRS = ("apps", "packages", "services")

# Dependencias que delatan un servidor de desarrollo de verdad.
DEV_SERVERS = (
    "vite",
    "next",
    "nuxt",
    "astro",
    "@remix-run",
    "react-scripts",
    "@angular/cli",
    "webpack-dev-server",
    "@nestjs/cli",
    "expo",
    "@sveltejs/kit",
    "parcel",
    "gatsby",
    "nodemon",
    "ts-node-dev",
    "hono",
    "fastify",
    "express",
    "nitro",
)

# Frameworks web de Rust. No hay servidor HTTP en la stdlib, asi que si el
# Cargo.toml no nombra a ninguno, el binario no sirve nada por un puerto.
#
# `hyper` estaba en esta lista y salio: es la base de casi todo el HTTP de Rust
# y la mitad de las veces entra como cliente. Un CLI que descarga algo lo
# declara igual que un servidor, y detectarlo dejaba al arranque esperando un
# puerto que nunca abre. Los que quedan son frameworks de servidor y nada mas.
RUST_SERVERS = (
    "axum",
    "actix-web",
    "rocket",
    "warp",
    "tide",
    "poem",
    "salvo",
    "tonic",
    "trillium",
    "gotham",
    "volo-http",
)

# Frameworks web de Go. A diferencia de Rust, esta lista no alcanza: net/http es
# stdlib y un servidor escrito con ella no deja rastro en go.mod. Por eso ademas
# se busca la llamada que lo delata en el fuente.
GO_SERVERS = ("gin-gonic/gin", "labstack/echo", "gofiber/fiber", "go-chi/chi", "gorilla/mux")
GO_SERVES = ("ListenAndServe", "http.Serve(")

# Donde buscar el paquete main de un proyecto Go.
GO_MAINS = ("main.go", "cmd/server/main.go", "cmd/api/main.go", "cmd/app/main.go")

# Frameworks JVM que sirven por un puerto, con su tarea en cada build system:
# (marcas en el archivo del build, goal de Maven, tarea de Gradle). `None` donde
# el framework no tiene un camino usable con esa herramienta.
#
# `spring-boot-starter-web` cubre tambien `-webflux`, que lo contiene como
# prefijo. Y no cubre `spring-boot-starter` a secas, que es lo que importa: esa
# es una app de Spring sin servlet container (una tarea batch, un consumidor de
# colas) y no abre ningun puerto.
JVM_SERVERS = (
    (("spring-boot-starter-web",), "spring-boot:run", "bootRun"),
    (("quarkus-maven-plugin", "io.quarkus", "quarkus-gradle-plugin"), "quarkus:dev", "quarkusDev"),
    (("micronaut-http-server",), "mn:run", "run"),
    (("ktor-server",), None, "run"),
)

# `{:phoenix, "~> 1.7"}` y no un `"phoenix" in texto`. Una libreria de
# componentes declara `phoenix_html` o `phoenix_live_view` sin ser una
# aplicacion: no tiene endpoint, y `mix phx.server` ahi falla. La coma es lo
# unico que separa un caso del otro.
PHOENIX_DEP = re.compile(r"\{\s*:phoenix\s*,")

# Que delata un proyecto de Bun. El lockfile ya estaba en LOCKFILES, pero ahi
# sirve para elegir el gestor de paquetes de un proyecto Node; aca dice que el
# runtime es Bun, que es otra pregunta.
BUN_MARKERS = ("bunfig.toml", "bun.lockb", "bun.lock")

# Lo que Bun ejecuta directo, en orden de preferencia.
BUN_ENTRIES = (
    "main.ts", "app.ts", "index.ts", "server.ts",
    "src/main.ts", "src/app.ts", "src/index.ts", "src/server.ts",
    "index.js", "server.js",
)

# Frameworks HTTP del ecosistema. Con cualquiera de estos el fuente no nombra a
# `Bun.serve`, asi que la dependencia es la unica senal.
BUN_SERVERS = ("hono", "elysia", "@elysiajs/", "@hono/", "bun-router")
BUN_SERVES = "Bun.serve("


def stack_for(root: Path) -> Stack:
    """Stack del proyecto: el archivo si existe, la deteccion si no.

    El archivo se busca hacia arriba como siempre, para poder correr desde un
    subdirectorio. La deteccion solo mira `root`.
    """
    try:
        path = find(root)
    except ConfigError:
        path = None
    if path is not None:
        # Un archivo invalido es un error, no una excusa para detectar por atras.
        return load(path)

    found = detect(root)
    if found is None:
        raise ConfigError(
            f"{root} no tiene stack.yaml y no se detecto nada conocido "
            "(compose, package.json, manage.py)"
        )
    return found


def detect(root: Path) -> Stack | None:
    """Servicios detectados en root, o None si no se reconoce nada."""
    root = root.resolve()
    services: dict[str, Service] = {}
    previous: tuple[str, ...] = ()

    opcionales = _compose_profiles(root)

    # `_bun` va ultimo, y no es un detalle de estilo: el primero gana (ver abajo),
    # y un proyecto con `package.json` mas `bun.lock` tiene que salir por `_node`,
    # que es el unico que sabe leer los scripts. `_bun` atrapa lo que sobra.
    for detector in (
        _compose, _python, _go, _rust, _ruby, _elixir, _php, _dotnet, _jvm, _deno, _node, _bun
    ):
        group = []
        for service in detector(root):
            if service.name in services:
                continue  # el primero gana: el contenedor le come el nombre al local
            # Los contenedores traen sus propias dependencias del compose. Los
            # demas heredan la cadena: el frontend espera al backend, y el
            # backend a los contenedores.
            wired = replace(service, needs=service.needs or previous)
            services[wired.name] = wired
            # Un contenedor con perfil no entra en la cadena heredada: si el
            # frontend lo necesitara, el orden topologico lo volveria a arrastrar
            # al arranque por defecto y el perfil no serviria de nada.
            if wired.name not in opcionales:
                group.append(wired.name)
        if group:
            previous = tuple(group)

    if not services:
        return None

    stack = Stack(
        name=root.name,
        root=root,
        path=root,
        services={
            name: replace(service, needs=tuple(d for d in service.needs if d in services))
            for name, service in services.items()
        },
        profiles=_profiles_for(services, opcionales),
        detected=True,
        # Solo cuando hay algo que dejar afuera: sin perfiles, `None` sigue
        # queriendo decir "todo", que es lo que era antes de existir este campo.
        default=tuple(n for n in services if n not in opcionales) if opcionales else None,
    )
    stack.resolve()  # los ciclos fallan al detectar, no a mitad del arranque
    return stack


def _profiles_for(
    services: dict[str, Service], opcionales: dict[str, tuple[str, ...]]
) -> dict[str, tuple[str, ...]]:
    """Un perfil de StackHelx por cada perfil declarado en el compose.

    Pedir un perfil arranca lo de siempre **mas** los contenedores de ese
    perfil, que es lo que hace `docker compose --profile X up`. Un perfil que
    arrancara solo sus propios contenedores dejaria al resto del stack afuera y
    no es lo que nadie quiso decir.
    """
    base = tuple(n for n in services if n not in opcionales)
    nombres = {p for perfiles in opcionales.values() for p in perfiles}
    return {
        nombre: base + tuple(n for n, perfiles in opcionales.items() if nombre in perfiles)
        for nombre in sorted(nombres)
    }


def freeze(root: Path) -> Path:
    """Escribe lo detectado como stack.yaml. Devuelve el archivo escrito.

    Vive aca y no en `cli` porque la interfaz ofrece lo mismo, y `CLAUDE.md`
    pide que el CLI y el servidor sean dos consumidores de la misma funcion.
    """
    target = root / CONFIG_NAMES[0]
    if any((root / name).is_file() for name in CONFIG_NAMES):
        raise ConfigError(f"{root} ya tiene un stack.yaml. No se sobreescribe.")

    stack = detect(root)
    if stack is None:
        raise ConfigError(
            f"No se detecto nada conocido en {root} (compose, package.json, manage.py)"
        )
    target.write_text(to_yaml(stack), encoding="utf-8")
    return target


def to_yaml(stack: Stack) -> str:
    """Serializa un stack detectado al formato de stack.yaml."""
    services = {}
    for service in stack.services.values():
        spec: dict[str, object] = {"command": service.command}
        if service.cwd != stack.root:
            spec["cwd"] = service.cwd.relative_to(stack.root).as_posix()
        if service.port:
            spec["port"] = service.port
        spec["ready"] = service.ready
        if service.needs:
            spec["needs"] = list(service.needs)
        if service.detached:
            spec["detached"] = True
        if service.stop:
            spec["stop"] = service.stop
        services[service.name] = spec

    # Congelar tiene que ser fiel: sin estas dos claves, un compose con
    # `profiles:` volveria a cargarse arrancando lo que deja apagado.
    document: dict[str, object] = {"name": stack.name, "services": services}
    if stack.default is not None:
        document["default"] = list(stack.default)
    if stack.profiles:
        document["profiles"] = {name: list(members) for name, members in stack.profiles.items()}

    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True)


# detectores ---------------------------------------------------------------


def _compose_services(root: Path) -> dict | None:
    """El mapa `services` del compose. None si no hay compose, {} si es ilegible.

    Lo leen dos funciones (los contenedores y sus perfiles) y no queria dos
    copias de la busqueda del archivo: divergen en el primer nombre nuevo.
    """
    path = next((root / name for name in COMPOSE_NAMES if (root / name).is_file()), None)
    if path is None:
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeDecodeError):
        raw = None
    declared = raw.get("services") if isinstance(raw, dict) else None
    return declared if isinstance(declared, dict) else {}


def _compose(root: Path) -> list[Service]:
    """Un servicio por contenedor, no uno solo llamado "docker".

    `docker compose up -d <nombre>` arranca ese contenedor y sus dependencias, y
    es idempotente. Asi cada contenedor tiene su puerto, su estado y su link en
    la interfaz, en vez de esconderse detras de un unico bloque opaco.
    """
    declared = _compose_services(root)
    if declared is None:
        return []
    if not declared:
        # Compose ilegible o sin servicios: se arranca entero y sin puertos.
        return [
            _container("docker", "docker compose up -d", root, None, (), "docker compose stop")
        ]

    env = _dotenv(root)
    names = {str(name) for name in declared}
    found = []
    for name, spec in declared.items():
        name = str(name)
        spec = spec if isinstance(spec, dict) else {}
        needs = tuple(dep for dep in _depends_on(spec) if dep in names and dep != name)
        found.append(
            _container(
                name,
                f"docker compose up -d {name}",
                root,
                _first_port(spec, env),
                needs,
                f"docker compose stop {name}",
            )
        )
    return found


def _container(
    name: str, command: str, root: Path, port: int | None, needs: tuple[str, ...], stop: str
):
    return Service(
        name=name,
        command=command,
        cwd=root,
        port=port,
        # Con puerto publicado se espera a que acepte conexiones; sin el, `up -d`
        # ya termino y no hay nada mas que mirar desde afuera.
        ready="port" if port else "none",
        needs=needs,
        env={},
        detached=True,
        stop=stop,  # el contenedor no muere con el cliente que lo arranco
    )


def _compose_profiles(root: Path) -> dict[str, tuple[str, ...]]:
    """Perfiles declarados por cada contenedor: nombre -> perfiles a los que pertenece.

    Ojo con la semantica, que esta invertida respecto de la de StackHelx: en
    compose, un servicio con `profiles:` queda **excluido** por defecto y solo
    entra cuando pedis uno de sus perfiles. En StackHelx un perfil es la lista
    de servicios a arrancar. Traducir uno al otro es el trabajo de `detect`,
    aca abajo; mapearlos directo arrancaria lo que compose deja apagado a
    proposito.
    """
    declared = _compose_services(root) or {}
    found = {}
    for name, spec in declared.items():
        value = spec.get("profiles") if isinstance(spec, dict) else None
        # Viene como lista, o como string suelto si hay uno solo.
        if isinstance(value, str):
            value = [value]
        if isinstance(value, list):
            perfiles = tuple(str(p) for p in value if isinstance(p, (str, int)))
            if perfiles:
                found[str(name)] = perfiles
    return found


def _depends_on(spec: dict) -> tuple[str, ...]:
    """`depends_on` en sus dos formas: lista de nombres, o mapa con condiciones."""
    value = spec.get("depends_on")
    if isinstance(value, dict):
        return tuple(str(name) for name in value)
    if isinstance(value, list):
        return tuple(str(name) for name in value if isinstance(name, (str, int)))
    return ()


def _first_port(spec: dict, env: dict[str, str]) -> int | None:
    for entry in spec.get("ports") or ():
        port = _published(entry, env)
        if port is not None:
            return port
    return None


def _published(entry: object, env: dict[str, str]) -> int | None:
    """Puerto del host en una entrada de `ports:`.

    Las formas son "8080:80", "127.0.0.1:8080:80", 8080 y {published: 8080}, con
    o sin `${VAR:-default}` adentro. Se descartan los rangos ("8000-8010:80") y
    las entradas de un solo puerto ("80" publica en un puerto del host al azar):
    en los dos casos no hay un puerto fijo que mirar.
    """
    if isinstance(entry, dict):
        return _valid(_interpolate(entry.get("published"), env))
    if not isinstance(entry, str):
        return None
    pieces = _interpolate(entry, env).split(":")
    if len(pieces) < 2:
        return None
    return _valid(pieces[-2])


# ponytail: solo la forma con llaves. `$VAR` a secas tambien es valido en compose
# y no se resuelve; el puerto queda desconocido, que es mejor que inventarlo.
VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?-([^}]*))?\}")


def _interpolate(value: object, env: dict[str, str]) -> object:
    if not isinstance(value, str):
        return value

    def resolve(match: re.Match) -> str:
        # Mismo orden que compose: entorno del shell, .env, y por ultimo el
        # default de la propia expresion.
        found = os.environ.get(match.group(1)) or env.get(match.group(1))
        if found:
            return found
        return match.group(2) if match.group(2) is not None else match.group(0)

    return VARIABLE.sub(resolve, value)


def _dotenv(root: Path) -> dict[str, str]:
    """Variables de `.env`, y solo para resolver puertos del compose.

    Compose lo lee, asi que ignorarlo daria puertos equivocados justo en los
    proyectos que parametrizan el puerto del frontend. Los valores no se
    loguean ni se pasan a ningun proceso.
    """
    values = {}
    for line in _read(root / ".env").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("\"'")
    return values


def _valid(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        port = int(value)
    except ValueError:
        return None
    return port if 1 <= port <= 65535 else None


def _python(root: Path) -> list[Service]:
    at_root = _python_at(root, "api")
    if at_root is not None:
        # Igual que en Node: si la raiz es el proyecto, no se baja una vuelta.
        return [at_root]

    found = []
    for path in _subprojects(root, BACKEND_DIRS):
        service = _python_at(path, path.name)
        if service is not None:
            found.append(service)
    return found


def _python_at(path: Path, name: str) -> Service | None:
    pyproject_text = _read(path / "pyproject.toml")
    has_uv = (path / "uv.lock").is_file() or bool(re.search(r"\[tool\.uv(\]|\.)", pyproject_text))
    prefix = "uv run " if has_uv else ""

    if (path / "manage.py").is_file():
        return _served(name, f"{prefix}python manage.py runserver", path)

    declared = "\n".join(
        _read(path / archivo)
        for archivo in ("pyproject.toml", "requirements.txt", "Pipfile", "poetry.lock", "uv.lock")
    ).lower()
    if "fastapi" not in declared and "uvicorn" not in declared:
        return None

    module = _asgi_module(path)
    if module is None:
        return None
    return _served(name, f"{prefix}uvicorn {module}:app --reload", path)


def _asgi_module(root: Path) -> str | None:
    """Modulo con un `app` de nivel superior, en notacion de puntos."""
    for candidate in ASGI_MODULES:
        path = root / candidate
        if not path.is_file():
            continue
        for line in _read(path).splitlines():
            if line.startswith(("app = ", "app=", "app: ")):
                return candidate[: -len(".py")].replace("/", ".")
    return None


def _go(root: Path) -> list[Service]:
    """Un servicio Go en la raiz, o en una subcarpeta de backend."""
    return _backend_at(root, _go_at)


def _go_at(path: Path, name: str) -> Service | None:
    if not (path / "go.mod").is_file():
        return None

    modulo = _read(path / "go.mod")
    marco = any(server in modulo for server in GO_SERVERS)

    # El paquete main, que ademas es lo que hay que pasarle a `go run`.
    for candidato in GO_MAINS:
        fuente = path / candidato
        if not fuente.is_file():
            continue
        # net/http es stdlib: un servidor escrito con ella no aparece en go.mod,
        # asi que la llamada en el fuente es la unica senal. Sin marco ni
        # llamada es una herramienta de linea de comandos, y arrancarla se
        # quedaria esperando un puerto que nunca abre.
        if not marco and not any(s in _read(fuente) for s in GO_SERVES):
            continue
        objetivo = "." if candidato == "main.go" else f"./{candidato.rsplit('/', 1)[0]}"
        return _served(name, f"go run {objetivo}", path)
    return None


def _rust(root: Path) -> list[Service]:
    """Un servicio Rust en la raiz, en una subcarpeta de backend, o en un Cargo workspace."""
    # 1. Si la raiz misma es un servicio Rust ejecutable
    at_root = _rust_at(root, "api")
    if at_root is not None:
        return [at_root]

    # 2. Revisar si es un Cargo workspace ([workspace] en Cargo.toml)
    workspace_members = _cargo_workspace_members(root)
    if workspace_members:
        found = []
        for member_path in workspace_members:
            service = _rust_at(member_path, member_path.name)
            if service is not None:
                found.append(service)
        if found:
            return found

    # 3. Subcarpetas habituales de backend
    found = []
    for path in _subprojects(root, BACKEND_DIRS):
        service = _rust_at(path, path.name)
        if service is not None:
            found.append(service)
    return found


def _cargo_workspace_members(root: Path) -> list[Path]:
    cargo_file = root / "Cargo.toml"
    if not cargo_file.is_file():
        return []
    try:
        data = tomllib.loads(_read(cargo_file))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    ws = data.get("workspace")
    if not isinstance(ws, dict):
        return []
    members = ws.get("members", [])
    if not isinstance(members, list):
        return []

    results = []
    for pattern in members:
        if not isinstance(pattern, str):
            continue
        # Soporta miembros directos ('crates/api') o globs simples ('crates/*')
        if "*" in pattern:
            results.extend(p for p in root.glob(pattern) if p.is_dir() and (p / "Cargo.toml").is_file())
        else:
            p = root / pattern
            if p.is_dir() and (p / "Cargo.toml").is_file():
                results.append(p)
    return sorted(results, key=lambda p: p.name.lower())


def _rust_at(path: Path, name: str) -> Service | None:
    cargo_path = path / "Cargo.toml"
    if not cargo_path.is_file():
        return None

    raw_cargo = _read(cargo_path)
    try:
        cargo_data = tomllib.loads(raw_cargo)
    except (OSError, tomllib.TOMLDecodeError):
        cargo_data = {}

    # Si es explicitamente un workspace puro (tiene [workspace] pero no [package])
    if "workspace" in cargo_data and "package" not in cargo_data and "bin" not in cargo_data:
        return None

    # Determinar si tiene un binario ejecutable y como arrancarlo:
    # 1. src/main.rs -> cargo run
    # 2. [[bin]] en Cargo.toml -> cargo run --bin <name>
    # 3. src/bin/<bin_name>.rs -> cargo run --bin <bin_name>
    cmd = None
    if (path / "src" / "main.rs").is_file():
        cmd = "cargo run"
    else:
        # Revisar [[bin]] en Cargo.toml
        bins = cargo_data.get("bin")
        if isinstance(bins, list) and bins:
            for b in bins:
                if isinstance(b, dict) and b.get("name"):
                    cmd = f"cargo run --bin {b['name']}"
                    break

        # Revisar src/bin/ si no hubo [[bin]] explicito
        if not cmd:
            bin_dir = path / "src" / "bin"
            if bin_dir.is_dir():
                for rs_file in sorted(bin_dir.glob("*.rs")):
                    cmd = f"cargo run --bin {rs_file.stem}"
                    break

    if not cmd:
        return None

    declared = raw_cargo.lower()
    is_server = any(server in declared for server in RUST_SERVERS)

    return Service(
        name=name,
        command=cmd,
        cwd=path,
        port=None,
        ready="listen" if is_server else "none",
        needs=(),
        env={},
        detached=False,
    )


def _ruby(root: Path) -> list[Service]:
    """Un Rails en la raiz, o en una subcarpeta de backend."""
    return _backend_at(root, _ruby_at)


def _ruby_at(path: Path, name: str) -> Service | None:
    # config/application.rb solo existe en una aplicacion Rails: una gema o un
    # engine tienen Gemfile y no lo tienen. Y una aplicacion Rails sirve por un
    # puerto siempre, asi que no hace falta la pregunta que si necesitan Go y Node.
    if not (path / "config" / "application.rb").is_file():
        return None
    if not (path / "Gemfile").is_file():
        return None
    # `bundle exec` y no el binstub `bin/rails`: es un script con shebang y en
    # Windows no lo ejecuta nadie. Bundler ya es obligatorio si hay Gemfile.
    return _served(name, "bundle exec rails server", path)


@functools.lru_cache(maxsize=None)
def _en_el_path(binario: str) -> bool:
    """Si el binario existe, cacheado.

    `detect` corre en el camino de sondeo de la interfaz: `_project_view` lo
    llama una vez por proyecto y por request, y la interfaz sondea cada 2.5s
    por pestana abierta. `shutil.which` barre el PATH entero, y en Windows lo
    permuta ademas contra cada extension de PATHEXT.

    Medido en Windows con 59 directorios en el PATH: 13.5ms por llamada, contra
    0.06ms cacheada. Con seis proyectos JVM y dos pestanas abiertas eso son
    162ms de barrido de disco en cada `/api/state`, cada 2.5 segundos, sobre el
    mismo threadpool que atiende apagar y matar procesos. Es el problema que
    `server._docker_is_down` ya documenta del otro lado: sin su cache,
    `/api/health` pasaba de 2ms a 9s.

    ponytail: sin vencimiento, a diferencia del cache de Docker. Ahi el valor
    cambia solo, porque el daemon se cae y se levanta; un binario del PATH no.
    Instalar gradle con `serve` abierto pide reiniciar para que lo vea, y ese
    caso no vale un cache con TTL y candado. Los tests lo limpian con
    `cache_clear`, que si no el resultado cruza de un test al siguiente.
    """
    return shutil.which(binario) is not None


def _jvm(root: Path) -> list[Service]:
    """Un servicio JVM en la raiz, o en una subcarpeta de backend.

    Java y Kotlin no son dos detectores: el build es el mismo y `.kts` solo
    cambia la extension del archivo de Gradle.
    """
    return _backend_at(root, _jvm_at)


def _jvm_at(path: Path, name: str) -> Service | None:
    if (path / "pom.xml").is_file():
        build = _read(path / "pom.xml")
        herramienta, wrapper = "mvn", ("./mvnw", "mvnw.cmd")
        maven = True
    else:
        gradle = [path / f for f in ("build.gradle", "build.gradle.kts") if (path / f).is_file()]
        if not gradle:
            return None
        # Los dos si estan los dos: un proyecto puede declarar los plugins en
        # el Groovy y las dependencias en el Kotlin DSL.
        build = "\n".join(_read(f) for f in gradle)
        herramienta, wrapper = "gradle", ("./gradlew", "gradlew.bat")
        maven = False

    # Un `pom.xml` o un `build.gradle` a secas puede ser una libreria o una app
    # de consola, y arrancarla dejaria al runner esperando un puerto que nunca
    # abre. Hace falta el framework, igual que en Rust.
    for marcas, goal, task in JVM_SERVERS:
        tarea = goal if maven else task
        if tarea is not None and any(marca in build for marca in marcas):
            return _served(name, f"{_lanzador(path, herramienta, wrapper)} {tarea}", path)
    return None


def _lanzador(path: Path, herramienta: str, wrapper: tuple[str, str]) -> str:
    """Con que se invoca el build: el binario del PATH, o el wrapper del repo.

    El binario gana cuando esta, y no es una preferencia de estilo. El comando
    detectado termina en el `stack.yaml` que `freeze` escribe, y ese archivo se
    commitea y lo abre alguien en otro sistema operativo. `mvn spring-boot:run`
    es igual en los tres; el wrapper son dos archivos distintos (`./mvnw` no
    corre en cmd.exe, `mvnw.cmd` no corre en bash), asi que congelar el wrapper
    rompe el stack compartido de un equipo mixto.

    Sin binario y sin wrapper se devuelve igual el nombre pelado: es un
    proyecto que existe, y fallar con "command not found" dice mas que no
    detectarlo. Es distinto del caso de la libreria, que no falla sino que se
    cuelga esperando un puerto.
    """
    if _en_el_path(herramienta):
        return herramienta
    elegido = wrapper[1] if os.name == "nt" else wrapper[0]
    nombre = Path(elegido).name
    if (path / nombre).is_file():
        return elegido
    if (path.parent / nombre).is_file():
        return f"..\\{nombre}" if os.name == "nt" else f"../{nombre}"
    return herramienta


def _elixir(root: Path) -> list[Service]:
    """Un Phoenix en la raiz, o en una subcarpeta de backend."""
    return _backend_at(root, _elixir_at)


def _elixir_at(path: Path, name: str) -> Service | None:
    # `mix.exs` dice que hay un proyecto Elixir y nada mas: puede ser una
    # libreria o una app OTP sin puerto, y arrancarla dejaria al runner
    # esperando un socket que nunca abre. Hace falta la segunda senal.
    if not (path / "mix.exs").is_file():
        return None

    # La dependencia, o la carpeta que Phoenix genera siempre. Dos senales
    # porque una sola no alcanza: en un umbrella las dependencias viven en el
    # mix.exs de la raiz y el hijo se queda sin la primera.
    if PHOENIX_DEP.search(_read(path / "mix.exs")):
        return _served(name, "mix phx.server", path)

    # `iterdir` no se traga los errores como `glob`: sin `lib`, sin permisos o
    # con la unidad desconectada, levanta. Y esto corre en el camino de sondeo
    # de la interfaz, asi que una excepcion aca es un 500 cada 2.5 segundos.
    # Es el mismo `try` que ya usa `_subprojects`.
    try:
        hijos = list((path / "lib").iterdir())
    except OSError:
        return None
    if any(hijo.is_dir() and hijo.name.endswith("_web") for hijo in hijos):
        return _served(name, "mix phx.server", path)
    return None


def _php(root: Path) -> list[Service]:
    """Un Laravel en la raiz, o en una subcarpeta de backend."""
    return _backend_at(root, _php_at)


def _php_at(path: Path, name: str) -> Service | None:
    # `artisan` en la raiz es Laravel y nada mas. Se pide ademas el
    # composer.json, que es lo que hace que las dependencias esten instaladas.
    if not (path / "artisan").is_file() or not (path / "composer.json").is_file():
        return None
    return _served(name, "php artisan serve", path)


def _dotnet(root: Path) -> list[Service]:
    """Un ASP.NET Core en la raiz, o en una subcarpeta de backend."""
    return _backend_at(root, _dotnet_at)


def _dotnet_at(path: Path, name: str) -> Service | None:
    # El atributo Sdk del csproj es lo unico que distingue una app web de una
    # libreria o una consola, que usan `Microsoft.NET.Sdk` a secas. Ni el nombre
    # del proyecto ni sus paquetes lo dicen.
    proyectos = sorted(path.glob("*.csproj"))
    for proyecto in proyectos:
        if "microsoft.net.sdk.web" not in _read(proyecto).lower():
            continue
        # Con varios csproj en la misma carpeta, `dotnet run` no sabe cual y
        # falla pidiendo que se lo digan.
        objetivo = f' --project "{proyecto.name}"' if len(proyectos) > 1 else ""
        return _served(name, f"dotnet watch run{objetivo}", path)
    return None


def _backend_at(root: Path, detector) -> list[Service]:
    """La raiz si es el proyecto, y si no las subcarpetas de backend.

    Es la forma que ya tenian `_python` y `_node`: si la raiz es el proyecto no
    se baja una vuelta, porque el servicio se llamaria como la subcarpeta que no
    existe.
    """
    at_root = detector(root, "api")
    if at_root is not None:
        return [at_root]
    found = []
    for path in _subprojects(root, BACKEND_DIRS):
        service = detector(path, path.name)
        if service is not None:
            found.append(service)
    return found


def _web_or_backend_at(root: Path, detector) -> list[Service]:
    """La raiz si es el proyecto, y si no las subcarpetas de front o de back.

    El analogo de `_backend_at` para los runtimes que sirven las dos cosas.
    Deno y Bun corren igual un frontend que una API, asi que mirar solo
    BACKEND_DIRS dejaria afuera un `frontend/` servido con cualquiera de los
    dos. Se extrajo cuando aparecio el segundo uso, no antes.
    """
    at_root = detector(root, "web")
    if at_root is not None:
        return [at_root]
    found = []
    for path in _subprojects(root, (*NODE_DIRS, *BACKEND_DIRS)):
        service = detector(path, path.name)
        if service is not None:
            found.append(service)
    return found


def _deno(root: Path) -> list[Service]:
    return _web_or_backend_at(root, _deno_at)


def _deno_at(path: Path, name: str) -> Service | None:
    for f in ("deno.json", "deno.jsonc"):
        deno_file = path / f
        if deno_file.is_file():
            try:
                raw_text = _read(deno_file) or "{}"
                clean_text = re.sub(r"(?m)^\s*//.*$|(?<=\s)//.*$", "", raw_text)
                data = json.loads(clean_text)
                if isinstance(data, dict):
                    tasks = data.get("tasks", {})
                    if isinstance(tasks, dict):
                        for task_name in ("dev", "start", "serve"):
                            if task_name in tasks:
                                return _served(name, f"deno task {task_name}", path)
            except json.JSONDecodeError:
                pass
            if not (path / "package.json").is_file():
                for entry in ("main.ts", "server.ts", "main.js", "server.js", "app.ts"):
                    if (path / entry).is_file():
                        return _served(name, f"deno run --allow-net {entry}", path)
    return None


def _node(root: Path) -> list[Service]:
    at_root = _package(root, root, "web", strict=False)
    if at_root is not None:
        # El package.json de la raiz manda: en un monorepo su script `dev` suele
        # ser el orquestador (turbo, nx) y arrancar ademas los hijos duplicaria
        # todo. Si no hay ninguno, se busca una vuelta mas abajo.
        return [at_root]

    found = []
    for path in _subprojects(root, NODE_DIRS):
        service = _package(path, root, path.name, strict=True)
        if service is not None:
            found.append(service)
    return found


def _bun(root: Path) -> list[Service]:
    """Bun como runtime, lo que `_node` deja pasar.

    Va **despues** de `_node` en la tupla de `detect`, y esa posicion es la
    mitad de la logica. Un proyecto con `package.json` y `bun.lock` ya salia
    bien de antes: `_package` lee el script y `_manager` devuelve `bun` por el
    lockfile. Adelantar `_bun` le robaria el nombre del servicio y lo arrancaria
    con el archivo en vez del script.

    Lo que queda para aca es el proyecto sin `package.json`, sin `scripts`, o
    con scripts que no sirven nada: ahi Bun corre el archivo directo.
    """
    return _web_or_backend_at(root, _bun_at)


def _bun_at(path: Path, name: str) -> Service | None:
    if not any((base / marca).is_file() for base in (path, path.parent) for marca in BUN_MARKERS):
        return None

    try:
        raw = json.loads(_read(path / "package.json") or "{}")
    except json.JSONDecodeError:
        raw = {}
    declaradas = {
        *(raw.get("dependencies") or {}),
        *(raw.get("devDependencies") or {}),
    } if isinstance(raw, dict) else set()
    marco = any(dep.startswith(server) for dep in declaradas for server in BUN_SERVERS)

    for candidato in BUN_ENTRIES:
        fuente = path / candidato
        if not fuente.is_file():
            continue
        # `Bun.serve` es la API nativa, o `export default { fetch }`, o framework HTTP.
        # Sin ninguna de las tres es una CLI, y arrancarla dejaria al runner
        # esperando un puerto que nunca abre.
        contenido = _read(fuente)
        es_servidor = (
            marco
            or (BUN_SERVES in contenido)
            or ("export default" in contenido and "fetch" in contenido)
        )
        if not es_servidor:
            continue
        return _served(name, f"bun run {candidato}", path)
    return None


def _subprojects(root: Path, names: tuple[str, ...]):
    """Subcarpetas candidatas, una sola vuelta.

    Sin recursion a proposito: un scan profundo entra en `node_modules` y en cada
    template de ejemplo que tenga el repo.
    """
    for name in names:
        yield root / name
    for group in WORKSPACE_DIRS:
        parent = root / group
        if not parent.is_dir():
            continue
        try:
            children = sorted(parent.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for child in children:
            if child.is_dir() and not child.name.startswith(".") and child.name != "node_modules":
                yield child


def _package(path: Path, root: Path, name: str, strict: bool) -> Service | None:
    try:
        raw = json.loads(_read(path / "package.json") or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None

    scripts = raw.get("scripts")
    if not isinstance(scripts, dict):
        return None
    script = next((s for s in NODE_SCRIPTS if isinstance(scripts.get(s), str)), None)
    if script is None:
        return None
    if strict and not _serves(raw):
        return None

    return _served(name, f"{_manager(raw, path, root)} run {script}", path)


def _serves(raw: dict) -> bool:
    """Si el paquete declara un servidor de desarrollo.

    En un workspace hay tantas librerias como apps, y una libreria con
    `dev: tsc --watch` entraria como servicio y nunca abriria un puerto: el
    arranque se quedaria esperando a que este lista hasta el timeout.
    """
    declared = {
        *(raw.get("dependencies") or {}),
        *(raw.get("devDependencies") or {}),
    }
    return any(
        dep == server or dep.startswith(server) for dep in declared for server in DEV_SERVERS
    )


def _manager(raw: dict, path: Path, root: Path) -> str:
    pinned = raw.get("packageManager")
    if isinstance(pinned, str):
        for tool in ("pnpm", "yarn", "bun", "npm"):
            if pinned.startswith(tool):
                return tool
    # El lockfile del propio paquete, y si no el de la raiz: en un monorepo hay
    # uno solo y esta arriba.
    for base in (path, root):
        for lock, tool in LOCKFILES.items():
            if (base / lock).is_file():
                return tool
    return "npm"


# helpers ------------------------------------------------------------------


def _served(name: str, command: str, root: Path) -> Service:
    """Servicio de larga duracion cuyo puerto se descubre al arrancar."""
    return Service(
        name=name,
        command=command,
        cwd=root,
        port=None,
        ready="listen",
        needs=(),
        env={},
        detached=False,
    )


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
