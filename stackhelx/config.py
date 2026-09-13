"""Carga y validacion de stack.yaml.

La validacion es manual y explicita a proposito: el esquema tiene ocho campos
y no justifica una dependencia de schemas. Cuando crezca, se revisa.

Todo lo que entra por el archivo se trata como input no confiable: safe_load,
tipos verificados uno por uno, y `cwd` obligado a quedar dentro de la raiz del
proyecto.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_NAMES = ("stack.yaml", "stack.yml")
READY_KINDS = ("port", "none", "listen")


class ConfigError(Exception):
    """stack.yaml ausente, ilegible o invalido."""


@dataclass(frozen=True)
class Service:
    name: str
    command: str
    cwd: Path
    port: int | None
    ready: str
    needs: tuple[str, ...]
    env: dict[str, str]
    detached: bool
    # Comando de apagado propio, para lo que no muere matando al proceso que lo
    # arranco: un contenedor vive fuera de nuestro arbol.
    stop: str | None = None
    env_file: tuple[Path, ...] = ()
    pre_start: str | None = None
    post_start: str | None = None
    # Adonde lleva "Abrir". Sin esto es la raiz del puerto, que no alcanza para
    # una app que vive en un path o que necesita un token en la query. Puede
    # traer ${VAR}: lo expande runner.service_url, no la carga.
    url: str | None = None
    restart: str = "no"
    max_retries: int = 3


@dataclass(frozen=True)
class Stack:
    name: str
    root: Path
    path: Path
    services: dict[str, Service]
    profiles: dict[str, tuple[str, ...]]
    scripts: dict[str, tuple[str, ...]] = field(default_factory=dict)
    detected: bool = False
    # Que arranca sin pedir perfil. None significa "todo", que es lo que un
    # stack.yaml siempre quiso decir. Existe por los `profiles:` de compose, que
    # marcan servicios que quedan afuera salvo que los pidas: sin esto, un stack
    # detectado arrancaria lo que el compose deja apagado a proposito.
    default: tuple[str, ...] | None = None

    def ports(self) -> list[int]:
        """Puertos declarados, en orden de aparicion y sin repetir."""
        seen = dict.fromkeys(s.port for s in self.services.values() if s.port)
        return list(seen)

    def resolve(self, profile: str | None = None) -> list[Service]:
        """Servicios del perfil en orden de arranque.

        Las dependencias transitivas entran aunque el perfil no las liste: pedir
        `api` sin su base de datos nunca es lo que alguien quiso decir.
        """
        if profile is None:
            wanted = list(self.default if self.default is not None else self.services)
        elif profile in self.profiles:
            wanted = list(self.profiles[profile])
        else:
            known = ", ".join(self.profiles) or "ninguno"
            raise ConfigError(f"perfil desconocido: {profile!r}. Definidos: {known}")
        return [self.services[name] for name in _topological(self.services, wanted)]


def _topological(services: dict[str, Service], wanted: list[str]) -> list[str]:
    order: list[str] = []
    done: set[str] = set()

    def visit(name: str, path: list[str]) -> None:
        if name in done:
            return
        if name in path:
            chain = " -> ".join(path[path.index(name):] + [name])
            raise ConfigError(f"dependencia circular: {chain}")
        for dep in services[name].needs:
            visit(dep, path + [name])
        done.add(name)
        order.append(name)

    for name in wanted:
        visit(name, [])
    return order


def find(start: Path | None = None) -> Path:
    """Busca stack.yaml desde start hacia arriba. Permite correr desde subdirs."""
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        for filename in CONFIG_NAMES:
            candidate = directory / filename
            if candidate.is_file():
                return candidate
    raise ConfigError(f"no se encontro {CONFIG_NAMES[0]} desde {current}")


def load(path: Path | None = None, _visited: set[Path] | None = None) -> Stack:
    path = (path or find()).resolve()
    root = path.parent

    if _visited is None:
        _visited = set()
    if path in _visited:
        raise ConfigError(f"ciclo de inclusion detectado en: {path}")
    _visited.add(path)

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"no se pudo leer {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: la raiz debe ser un mapa")

    services_raw = raw.get("services") or {}
    if not isinstance(services_raw, dict):
        raise ConfigError(f"{path}: 'services' debe ser un mapa")

    services = {
        name: _service(name, spec, root)
        for name, spec in services_raw.items()
    }

    includes_raw = raw.get("includes")
    if includes_raw is not None:
        if isinstance(includes_raw, str):
            includes_list = [includes_raw]
        elif isinstance(includes_raw, list) and all(isinstance(i, str) for i in includes_raw):
            includes_list = includes_raw
        else:
            raise ConfigError(f"{path}: 'includes' debe ser una ruta o lista de rutas")

        for inc_item in includes_list:
            inc_path = (root / inc_item).resolve()
            if inc_path.is_dir():
                target_file = None
                for cname in CONFIG_NAMES:
                    if (inc_path / cname).is_file():
                        target_file = inc_path / cname
                        break
                if target_file is None:
                    raise ConfigError(f"no se encontro stack.yaml en la ruta incluida: {inc_path}")
                inc_stack = load(target_file, _visited=set(_visited))
            elif inc_path.is_file():
                inc_stack = load(inc_path, _visited=set(_visited))
            else:
                raise ConfigError(f"ruta de inclusion no encontrada: {inc_path}")

            for s_name, s_svc in inc_stack.services.items():
                if s_name in services:
                    raise ConfigError(
                        f"conflicto de servicio: '{s_name}' ya esta declarado y no puede ser importado desde '{inc_path}'"
                    )
                services[s_name] = s_svc

    if not services:
        raise ConfigError(f"{path}: falta la seccion 'services' o esta vacia")

    for service in services.values():
        for dep in service.needs:
            if dep not in services:
                raise ConfigError(f"'{service.name}.needs' apunta a '{dep}', que no existe")

    profiles = _profiles(raw.get("profiles"), services)
    scripts = _scripts(raw.get("scripts"))

    stack = Stack(
        name=str(raw.get("name") or root.name),
        root=root,
        path=path,
        services=services,
        profiles=profiles,
        scripts=scripts,
        default=_default(raw.get("default"), services),
    )
    stack.resolve()  # falla al cargar si hay ciclos, no en tiempo de arranque
    return stack


def _service(name: str, spec: object, root: Path) -> Service:
    where = f"services.{name}"
    if not isinstance(spec, dict):
        raise ConfigError(f"{where} debe ser un mapa")

    unknown = set(spec) - {
        "command", "cwd", "port", "ready", "needs", "env", "detached", "stop",
        "env_file", "pre_start", "post_start", "url", "restart", "max_retries",
    }
    if unknown:
        raise ConfigError(f"{where}: campos desconocidos: {', '.join(sorted(unknown))}")

    command = spec.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ConfigError(f"{where}.command es obligatorio y debe ser texto")

    port = spec.get("port")
    if port is not None:
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ConfigError(f"{where}.port debe ser un entero entre 1 y 65535")

    ready = spec.get("ready", "port" if port else "none")
    if not isinstance(ready, str) or not _valid_ready(ready):
        raise ConfigError(
            f"{where}.ready debe ser 'port', 'listen', 'none', 'log:<texto>' o una URL http"
        )
    if ready == "port" and port is None:
        raise ConfigError(f"{where}.ready es 'port' pero no hay 'port' declarado")
    if ready == "listen" and port is not None:
        raise ConfigError(
            f"{where}.ready es 'listen' pero el puerto esta declarado: usa 'port'"
        )

    needs = spec.get("needs", [])
    if not isinstance(needs, list) or not all(isinstance(n, str) for n in needs):
        raise ConfigError(f"{where}.needs debe ser una lista de nombres")
    if name in needs:
        raise ConfigError(f"{where}.needs se incluye a si mismo")

    detached = spec.get("detached", False)
    if not isinstance(detached, bool):
        raise ConfigError(f"{where}.detached debe ser true o false")

    stop = spec.get("stop")
    if stop is not None and (not isinstance(stop, str) or not stop.strip()):
        raise ConfigError(f"{where}.stop debe ser texto no vacio")

    pre_start = spec.get("pre_start")
    if pre_start is not None and (not isinstance(pre_start, str) or not pre_start.strip()):
        raise ConfigError(f"{where}.pre_start debe ser texto no vacio")

    post_start = spec.get("post_start")
    if post_start is not None and (not isinstance(post_start, str) or not post_start.strip()):
        raise ConfigError(f"{where}.post_start debe ser texto no vacio")

    url = spec.get("url")
    if url is not None:
        if not isinstance(url, str) or not url.strip():
            raise ConfigError(f"{where}.url debe ser texto no vacio")
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            raise ConfigError(
                f"{where}.url debe empezar con http:// o https://, y es {url!r}"
            )

    restart = spec.get("restart", "no")
    if not isinstance(restart, str) or restart not in ("no", "on-failure", "always"):
        raise ConfigError(f"{where}.restart debe ser 'no', 'on-failure' o 'always'")

    max_retries = spec.get("max_retries", 3)
    if not isinstance(max_retries, int) or isinstance(max_retries, bool) or max_retries < 0:
        raise ConfigError(f"{where}.max_retries debe ser un entero positivo o 0")

    return Service(
        name=name,
        command=command,
        cwd=_cwd(where, spec.get("cwd", "."), root),
        port=port,
        ready=ready,
        needs=tuple(needs),
        env=_env(where, spec.get("env")),
        detached=detached,
        stop=stop,
        env_file=_env_files(where, spec.get("env_file"), root),
        pre_start=pre_start,
        post_start=post_start,
        url=url,
        restart=restart,
        max_retries=max_retries,
    )


def _valid_ready(ready: str) -> bool:
    if ready in READY_KINDS:
        return True
    if ready.startswith("log:") and len(ready) > 4:
        return True
    return ready.startswith(("http://", "https://"))


def _cwd(where: str, value: object, root: Path) -> Path:
    if not isinstance(value, str):
        raise ConfigError(f"{where}.cwd debe ser una ruta relativa")
    candidate = Path(value)
    if candidate.is_absolute():
        raise ConfigError(f"{where}.cwd debe ser relativa a la raiz del proyecto")

    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root):
        raise ConfigError(f"{where}.cwd sale de la raiz del proyecto: {value!r}")
    if not resolved.is_dir():
        raise ConfigError(f"{where}.cwd no existe: {value!r}")
    return resolved


def _env(where: str, value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where}.env debe ser un mapa")
    env = {}
    for key, item in value.items():
        if isinstance(item, (dict, list)) or item is None:
            raise ConfigError(f"{where}.env.{key} debe ser un valor simple")
        env[str(key)] = str(item)
    return env


def _default(value: object, services: dict[str, Service]) -> tuple[str, ...] | None:
    """Servicios que arrancan sin pedir perfil. Ausente significa todos.

    Existe para que `stackhelx init` pueda congelar un compose con `profiles:`
    sin cambiar lo que arranca: en compose esos contenedores quedan afuera hasta
    que los pedis, y sin esta clave el archivo congelado los prenderia a todos.
    """
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise ConfigError("'default' debe ser una lista no vacia de servicios")
    for member in value:
        if member not in services:
            raise ConfigError(f"'default' incluye '{member}', que no existe")
    return tuple(str(m) for m in value)


def _profiles(value: object, services: dict[str, Service]) -> dict[str, tuple[str, ...]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError("'profiles' debe ser un mapa de nombre a lista de servicios")

    profiles = {}
    for name, members in value.items():
        if not isinstance(members, list) or not members:
            raise ConfigError(f"profiles.{name} debe ser una lista no vacia")
        for member in members:
            if member not in services:
                raise ConfigError(f"profiles.{name} incluye '{member}', que no existe")
        profiles[str(name)] = tuple(members)
    return profiles


def _env_files(where: str, value: object, root: Path) -> tuple[Path, ...]:
    if value is None:
        return ()
    raw_list: list[object]
    if isinstance(value, str):
        raw_list = [value]
    elif isinstance(value, list):
        raw_list = value
    else:
        raise ConfigError(f"{where}.env_file debe ser una ruta o lista de rutas")

    paths: list[Path] = []
    for item in raw_list:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{where}.env_file debe contener rutas relativas de texto no vacias")
        candidate = Path(item)
        if candidate.is_absolute():
            raise ConfigError(f"{where}.env_file debe ser relativa a la raiz del proyecto")
        resolved = (root / candidate).resolve()
        if not resolved.is_relative_to(root):
            raise ConfigError(f"{where}.env_file sale de la raiz del proyecto: {item!r}")
        paths.append(resolved)
    return tuple(paths)


def parse_env_file(path: Path) -> dict[str, str]:
    """Lee un archivo .env simple sin dependencias externas."""
    if not path.is_file():
        return {}
    env: dict[str, str] = {}
    try:
        content = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError no es un OSError: un .env guardado en latin-1, que
        # es lo que deja cualquier editor viejo en Windows, tumbaba el arranque
        # entero del stack en vez de quedarse sin esas variables.
        return {}

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        # El valor entrecomillado se corta en su comilla de cierre, no en el
        # final de la linea: exigir que TERMINE en comilla fallaba en cuanto
        # habia un comentario detras, y `TOKEN="abc" # el de prod` entregaba el
        # token con las comillas pegadas. Y como lo de adentro se toma tal cual,
        # un `#` dentro de las comillas sigue siendo parte del valor.
        if len(val) >= 2 and val[0] in "\"'" and val.find(val[0], 1) != -1:
            val = val[1 : val.find(val[0], 1)]
        elif " #" in val:
            val = val.split(" #", 1)[0].rstrip()
        env[key] = val
    return env


def _scripts(value: object) -> dict[str, tuple[str, ...]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError("'scripts' debe ser un mapa de nombre a comando o lista de comandos")

    scripts = {}
    for name, cmd in value.items():
        if not isinstance(name, str) or not name.strip():
            raise ConfigError("el nombre del script debe ser texto no vacio")
        if isinstance(cmd, str):
            if not cmd.strip():
                raise ConfigError(f"scripts.{name} no puede estar vacio")
            scripts[str(name)] = (cmd.strip(),)
        elif isinstance(cmd, list):
            if not cmd or not all(isinstance(c, str) and c.strip() for c in cmd):
                raise ConfigError(f"scripts.{name} debe ser una lista de comandos de texto no vacios")
            scripts[str(name)] = tuple(str(c).strip() for c in cmd)
        else:
            raise ConfigError(f"scripts.{name} debe ser un texto o lista de comandos")
    return scripts


