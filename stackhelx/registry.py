"""Proyectos que la interfaz conoce.

El CLI trabaja sobre el directorio actual y no necesita registro. La interfaz
si: su razon de existir es ver todos los proyectos a la vez sin importar en que
carpeta estas parado.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import time
from pathlib import Path

from . import config, detect, ports


def _default_home() -> Path:
    env = os.environ.get("STACKHELX_HOME") or os.environ.get("PORTMASTER_HOME")
    if env:
        return Path(env)
    new_home = Path.home() / ".stackhelx"
    old_home = Path.home() / ".portmaster"
    if not new_home.exists() and old_home.exists():
        return old_home
    return new_home


HOME = _default_home()
PROJECTS = HOME / "projects.json"


class RegistryError(Exception):
    """La ruta no sirve como proyecto."""


def project_id(path: Path) -> str:
    """Identificador estable y seguro para URLs, derivado de la ruta."""
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]


def paths() -> list[Path]:
    try:
        data = json.loads(PROJECTS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [Path(item) for item in data if isinstance(item, str)]


def add(raw: str | Path) -> Path:
    """Registra un proyecto: con stack.yaml propio, o detectable."""
    path = Path(raw).expanduser()
    if not path.is_dir():
        raise RegistryError(f"no es un directorio: {path}")
    path = path.resolve()

    known = any((path / name).is_file() for name in config.CONFIG_NAMES)
    if not known and detect.detect(path) is None:
        raise RegistryError(
            f"{path} no tiene {config.CONFIG_NAMES[0]} y no se detecto nada conocido"
        )

    current = paths()
    if path not in current:
        _save(current + [path])
    return path


def remove(pid: str) -> bool:
    current = paths()
    kept = [p for p in current if project_id(p) != pid]
    if len(kept) == len(current):
        return False
    _save(kept)
    return True


def export_data() -> list[str]:
    """Exporta las rutas de los proyectos registrados como lista JSON."""
    return [str(p) for p in paths()]


def import_data(data: list[str]) -> list[str]:
    """Importa masivamente rutas de proyectos desde una lista."""
    if not isinstance(data, list):
        raise RegistryError("el formato debe ser una lista de rutas de proyectos")
    imported = []
    for item in data:
        if isinstance(item, str):
            try:
                path = add(item)
                imported.append(str(path))
            except RegistryError:
                pass
    return imported


# Cuanto vale reusar el mapa antes de recalcularlo, para quien lo pida cacheado.
PORTS_TTL = 30.0
_cache_lock = threading.Lock()
# Sube con cada cambio del registro. Un escaneo que empezo antes de un `add`
# no puede escribir su resultado despues: seria el estado viejo pisando al
# nuevo, con 30s de vida por delante.
_revision = 0
# Los puertos y los nombres salen del mismo recorrido: un solo cache, que es
# una cosa menos que olvidarse de invalidar en `_save`.
_ports_cache: tuple[float, dict[int, list[Path]], dict[Path, str]] | None = None
_docker_cache: tuple[float, bool] | None = None


def declared_ports(max_age: float = 0.0) -> dict[int, list[Path]]:
    """Puerto declarado -> proyectos registrados que lo piden.

    Es el unico dato que StackHelx tiene y las herramientas de un proyecto solo
    no pueden tener: cada compose se conoce a si mismo y ninguno sabe del de al
    lado. Sin esto, que dos proyectos peleen por el 3000 se descubre cuando el
    segundo no arranca.

    Resolver el stack de cada proyecto medido en esta maquina: 1.3ms con
    stack.yaml, 10 a 92ms detectado. Con tres proyectos son 114ms, y escala
    lineal. Para un comando que corre una vez no es nada, y por eso el default
    es recalcular siempre: un doctor que miente no sirve. La vista de estado,
    que sondea cada 2.5s en el threadpool que comparte con /down y /kill, pide
    `max_age=PORTS_TTL`.

    ponytail: el cache vence por tiempo y no por mtime. Editar un stack.yaml
    tarda hasta PORTS_TTL en verse en la interfaz, que para un aviso esta bien.
    Si alguna vez molesta, la salida es sumarle el mtime del archivo de config,
    aunque para un proyecto detectado eso tampoco alcanza: la deteccion lee
    archivos de subcarpetas que el mtime de la raiz no delata.
    """
    global _ports_cache
    now = time.monotonic()
    with _cache_lock:
        if max_age > 0 and _ports_cache is not None and now - _ports_cache[0] < max_age:
            return _ports_cache[1]
        revision = _revision

    found: dict[int, list[Path]] = {}
    names: dict[Path, str] = {}
    for path in paths():
        names[path], puertos = _stack_of(path)
        for port in sorted(puertos):
            found.setdefault(port, []).append(path)
    with _cache_lock:
        if revision == _revision:
            _ports_cache = (now, found, names)
    return found


def name_of(path: Path, max_age: float = PORTS_TTL) -> str:
    """Como se llama un proyecto registrado, igual que en su ficha.

    El nombre lo declara `stack.yaml` y puede no coincidir con el de la carpeta:
    `apps/Fitness` se llama `fittrack`. Nombrar la carpeta en un aviso manda al
    usuario a buscar un proyecto que en la interfaz no existe.
    """
    declared_ports(max_age=max_age)
    with _cache_lock:
        cache = _ports_cache
    return cache[2].get(path, path.name) if cache else path.name


def any_uses_docker(max_age: float = 0.0) -> bool:
    """Si algun proyecto registrado levanta contenedores.

    La interfaz decide con esto si muestra la fila de Docker, y tiene que salir
    de todos los proyectos y no de la pagina que estas mirando: colgado de la
    pagina, pasar a la segunda apagaba la fila entera cuando ahi no habia
    ninguno con contenedores. Es el mismo motivo por el que `/api/health` no
    pagina y por el que los tuneles van fuera del paginado.

    ponytail: cache propio, o sea un segundo recorrido de todos los proyectos
    ademas del de `declared_ports`. Los dos vencen a los 30s, asi que la vista
    de estado paga uno de cada doce sondeos. Si alguna vez pesa, los dos salen
    de un unico recorrido que resuelva cada stack una sola vez.
    """
    global _docker_cache
    now = time.monotonic()
    with _cache_lock:
        if max_age > 0 and _docker_cache is not None and now - _docker_cache[0] < max_age:
            return _docker_cache[1]
        revision = _revision

    usa = any(_uses_docker(path) for path in paths())

    with _cache_lock:
        # Solo si el registro no cambio mientras se recorria. El escaneo tarda
        # decenas de milisegundos y `/api/state` corre en un threadpool: sin
        # esto, un `add` que caia justo en el medio limpiaba el cache y despues
        # este escaneo, que arranco antes, lo repoblaba con el valor viejo. La
        # fila de Docker volvia a tardar 30s en aparecer, que es el bug que ya
        # arreglamos una vez.
        if revision == _revision:
            _docker_cache = (now, usa)
    return usa


def _uses_docker(path: Path) -> bool:
    """Un proyecto roto no cuenta, y no es motivo para no revisar el resto."""
    # Adentro de la funcion: `doctor` importa este modulo, y arriba seria un
    # ciclo. Es el mismo `_program` que usa la vista de estado, y compartirlo
    # importa: si algun dia cambia como se saca el nombre del programa, los dos
    # lados tienen que cambiar juntos o la fila aparece cuando no debe.
    from . import doctor

    try:
        stack = detect.stack_for(path)
    except (config.ConfigError, OSError):
        return False
    return any(doctor._program(s.command) == "docker" for s in stack.services.values())


def _stack_of(path: Path) -> tuple[str, set[int]]:
    """Nombre y puertos declarados por un proyecto. Vacio si no se puede leer.

    Un proyecto roto o borrado no es motivo para que el resto no se revise:
    tiene su propio chequeo en `doctor`. Ahi el nombre cae en el de la carpeta,
    que es lo unico que se sabe de un proyecto que no se puede leer.
    """
    try:
        stack = detect.stack_for(path)
        return stack.name, {s.port for s in stack.resolve() if s.port}
    except (config.ConfigError, OSError):
        return path.name, set()


def _save(items: list[Path]) -> None:
    global _ports_cache, _docker_cache, _revision
    # Agregar o quitar un proyecto cambia el mapa ya mismo: esperar el TTL
    # dejaria la interfaz media hora sin ver el proyecto recien registrado.
    # Los dos caches salen del mismo recorrido del registro y vencen juntos: el
    # de Docker se sumo despues y quedo afuera de esta linea, y el sintoma fue
    # registrar un proyecto con contenedores y que la fila tardara 30s en salir.
    with _cache_lock:
        _ports_cache = None
        _docker_cache = None
        _revision += 1
    HOME.mkdir(parents=True, exist_ok=True)
    ordered = sorted({str(p) for p in items})
    tmp = PROJECTS.with_suffix(".tmp")
    tmp.write_text(json.dumps(ordered, indent=2), encoding="utf-8")
    tmp.replace(PROJECTS)


def token() -> str:
    """Token de la API local.

    Prioriza STACKHELX_TOKEN (con fallback a PORTMASTER_TOKEN). Si no esta, usa uno generado en el directorio del
    usuario. Ver la desviacion documentada en CLAUDE.md: una herramienta que se
    instala con pipx no puede traer un .env, y un token generado con permisos
    0600 fuera del repo es mas seguro que uno que el usuario copia a mano.
    """
    from secrets import token_urlsafe

    from_env = os.environ.get("STACKHELX_TOKEN") or os.environ.get("PORTMASTER_TOKEN")
    if from_env:
        if len(from_env) < 16:
            raise RegistryError("STACKHELX_TOKEN es demasiado corto (minimo 16)")
        return from_env

    path = HOME / "token"
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if len(existing) >= 16:
            return existing
    except OSError:
        pass

    HOME.mkdir(parents=True, exist_ok=True)
    fresh = token_urlsafe(32)
    # Permisos restrictivos (0600) en el open, y fchmod previo si ya existia.
    def _opener(p, flags):
        fd = os.open(p, flags, 0o600)
        if hasattr(os, "fchmod"):
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
        return fd

    try:
        with open(path, "w", encoding="utf-8", opener=_opener) as f:
            f.write(fresh)
        try:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    except OSError:
        path.write_text(fresh, encoding="utf-8")  # sistemas sin permisos POSIX
    return fresh


def find_orphans(running_ports: frozenset[int] | set[int] = frozenset()) -> list[dict]:
    """Puertos de proyectos registrados ocupados por procesos que no lanzamos.

    Un proceso es intruso si su puerto aparece en el stack de algun proyecto
    registrado, no esta en running_ports (lo que corre en nuestras sesiones), y
    hay un proceso externo escuchando ahi que no es un proxy de Docker.

    El default vacio es lo correcto para el CLI, que no tiene sesiones y por eso
    no puede distinguir un intruso de un stack que vos mismo levantaste en otra
    terminal. Quien llame con esa suposicion tiene que mostrar la lista antes de
    cerrar nada.
    """
    # Una fila por puerto, con todos los proyectos que lo reclaman. Antes salia
    # una por proyecto, asi que un puerto que dos declaran aparecia dos veces
    # con el mismo pid y la misma linea de comando: informacion repetida que
    # ademas sugeria que habia dos procesos.
    por_puerto: dict[int, dict] = {}
    for path in paths():
        try:
            stack = detect.stack_for(path)
        except (config.ConfigError, OSError):
            continue

        scanned = ports.scan_many([s.port for s in stack.services.values() if s.port])

        for svc in stack.services.values():
            if not svc.port or svc.port in running_ports:
                continue
            status = scanned.get(svc.port)
            if status is None or status.free or status.pid is None:
                continue
            if ports.proxy_owner(status):
                continue
            nombre = stack.name or path.name
            fila = por_puerto.get(svc.port)
            if fila is None:
                por_puerto[svc.port] = {
                    "port": svc.port,
                    "projects": [nombre],
                    "pid": status.pid,
                    "name": status.name or "desconocido",
                    "cmd": (status.cmdline or "")[:120] or None,
                    "create_time": status.create_time,
                }
            elif nombre not in fila["projects"]:
                fila["projects"].append(nombre)

    for fila in por_puerto.values():
        fila["projects"].sort()
    return [por_puerto[port] for port in sorted(por_puerto)]


def find_collisions(max_age: float = 0.0) -> dict[int, list[Path]]:
    """Devuelve los puertos disputados por dos o mas proyectos registrados."""
    ports_map = declared_ports(max_age=max_age)
    return {port: projs for port, projs in ports_map.items() if len(projs) > 1}


