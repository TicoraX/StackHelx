"""Diagnostico: por que este proyecto no arranca, sin arrancarlo.

Vive en su propio modulo y no en `cli.py` porque cruza cuatro modulos y porque
la interfaz web va a querer los mismos chequeos. `run` devuelve datos; imprimir
es problema de quien llame.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config, detect, ports, registry

UI_PORT = 7666
DOCKER_TIMEOUT = 5.0


@dataclass(frozen=True)
class Check:
    """Un chequeo y su resultado.

    `fix` es un comando copiable o una accion de una linea. Un chequeo en rojo
    sin `fix` deja al usuario donde estaba: sabiendo que algo anda mal y no que
    hacer. Por eso todo lo que sale `fail` lo lleva.
    """

    name: str
    level: str  # "ok" | "warn" | "fail"
    detail: str
    fix: str = ""


def run(root: Path) -> list[Check]:
    """Entorno siempre, proyecto si hay algo que revisar."""
    return environment() + project(root)


def blocking(checks: list[Check]) -> bool:
    """Si algo impide arrancar. Los avisos no cuentan, o el codigo de salida
    daria 1 todo el dia y dejaria de significar algo."""
    return any(c.level == "fail" for c in checks)


# entorno ------------------------------------------------------------------


def environment() -> list[Check]:
    return [_token(), _ui_port(), *_registered()]


def _token() -> Check:
    try:
        registry.token()
    except Exception as exc:  # noqa: BLE001 - cualquier fallo aca es el mismo problema
        return Check(
            "token",
            "fail",
            f"no se pudo leer ni generar {registry.HOME / 'token'}: {exc}",
            "revisa los permisos de la carpeta, o defini STACKHELX_TOKEN",
        )
    return Check("token", "ok", str(registry.HOME / "token"))


def _ui_port() -> Check:
    status = ports.scan(UI_PORT)
    if status.free:
        return Check("puerto de la interfaz", "ok", f"{UI_PORT} libre")
    quien = status.name or "desconocido"
    return Check(
        "puerto de la interfaz",
        "warn",
        f"{UI_PORT} ocupado por {quien} (pid {status.pid})",
        f"si no es StackHelx: stackhelx free {UI_PORT}",
    )


def _registered() -> list[Check]:
    known = registry.paths()
    if not known:
        return [
            Check("proyectos registrados", "ok", "ninguno", "para registrar: stackhelx add .")
        ]
    perdidos = [p for p in known if not p.is_dir()]
    checks = [Check("proyectos registrados", "ok", f"{len(known)}")]
    for path in perdidos:
        checks.append(
            Check(
                "proyecto sin carpeta",
                "warn",
                str(path),
                f'stackhelx remove "{path}"',
            )
        )
    return checks


# proyecto -----------------------------------------------------------------


def project(root: Path) -> list[Check]:
    try:
        stack = detect.stack_for(root)
        services = stack.resolve()
    except config.ConfigError as exc:
        # Un stack.yaml roto es una falla; no tener ninguno, corriendo doctor en
        # una carpeta cualquiera, es lo normal recien instalado.
        if _archivo(root) is None:
            return [Check("proyecto", "ok", f"{root} no es un proyecto conocido")]
        return [
            Check(
                "stack",
                "fail",
                str(exc),
                "corregi el archivo (ver stack.example.yaml) o borralo para que se detecte",
            )
        ]

    origen = "detectado" if stack.detected else str(stack.path)
    checks = [Check("stack", "ok", f"{origen} ({len(services)} servicios)")]
    checks += _dotenv(root)
    checks += _executables(services)
    if any(_program(s.command) == "docker" for s in services):
        checks.append(_docker())
    checks += _ports(services)
    checks += _compartidos(stack.root, services)
    return checks


def _compartidos(root: Path, services: list[config.Service]) -> list[Check]:
    """Otros proyectos registrados que declaran los mismos puertos.

    Aviso y no falla: dos proyectos con el mismo puerto conviven mientras no
    corran a la vez. Lo que hoy no existe es enterarse antes, en vez de cuando
    el segundo no arranca y el error habla de un puerto ocupado sin decir por
    quien.
    """
    wanted = {s.port for s in services if s.port}
    if not wanted:
        return []
    try:
        mio = root.resolve()
    except OSError:
        mio = root

    duenos = registry.declared_ports()
    checks = []
    for port in sorted(wanted):
        otros = [p for p in duenos.get(port, []) if p != mio]
        if not otros:
            continue
        checks.append(
            Check(
                f"puerto {port} compartido",
                "warn",
                f"tambien lo declara {', '.join(str(p) for p in otros)}",
                "arrancalos de a uno, o cambia el port: en uno de los stack.yaml",
            )
        )
    return checks


def _parse_env_keys(path: Path) -> dict[str, str]:
    """Claves y valores de un .env. Lo que no entienda queda afuera.

    ponytail: parte en el primer `=`, saltea vacias y comentarios, y nada mas.
    Sin comillas, escapes, `export` ni valores multilinea. El techo: un archivo
    con un valor de varias lineas lee la segunda como una clave rara y la da
    por faltante. El dia que aparezca uno, hay librerias para esto.
    """
    result = {}
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return result
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            result[k.strip()] = v.strip()
    return result


_PLACEHOLDER_SECRETS = {
    "changeme",
    "change_me",
    "your_secret_here",
    "your_api_key",
    "your-secret-here",
    "your-api-key",
    "secret",
    "password",
    "admin",
    "123456",
    "todo",
}


def _dotenv(root: Path) -> list[Check]:
    examples = [p for p in (root / ".env.example", root / ".env.template") if p.is_file()]
    if not examples:
        return []
    example_path = examples[0]
    ejemplo = example_path.name
    env_path = root / ".env"

    if not env_path.is_file():
        return [
            Check(
                "variables de entorno",
                "warn",
                f"{ejemplo} existe pero falta .env local",
                f"cp {ejemplo} .env",
            )
        ]

    # Nombres de claves, nunca valores: esto sale por HTTP en /api/doctor.
    declaradas = _parse_env_keys(example_path)
    propias = _parse_env_keys(env_path)
    faltan = [k for k in declaradas if k not in propias]
    vacias = [k for k in declaradas if k in propias and not propias[k]]
    placeholders = [
        k
        for k, v in propias.items()
        if v.strip().strip("'\"").lower() in _PLACEHOLDER_SECRETS
        or v.strip().strip("'\"").lower().startswith(("your_", "your-"))
    ]

    # Las advertencias en `warn` y no en `fail`. En este comando `fail` esta reservado
    # para lo que no puede arrancar y se sabe con certeza: un binario que no
    # esta en el PATH, docker apagado, un stack.yaml invalido. Una clave que
    # falta en el .env es una inferencia: puede ser opcional, o venir del
    # entorno o del compose. Un rojo equivocado gasta el unico rojo que hay.
    checks = []
    if faltan:
        checks.append(
            Check(
                "variables de entorno",
                "warn",
                f"{ejemplo} declara claves que el .env no tiene: {', '.join(faltan)}",
                "agregalas al .env, o sacalas del ejemplo si ya no hacen falta",
            )
        )
    if vacias:
        checks.append(
            Check(
                "variables de entorno",
                "warn",
                f"sin valor en el .env: {', '.join(vacias)}",
                "completalas, o dejalas asi si el vacio es a proposito",
            )
        )
    if placeholders:
        checks.append(
            Check(
                "variables de entorno",
                "warn",
                f"valores de ejemplo/inseguros en .env: {', '.join(placeholders)}",
                "reemplaza los placeholders por credenciales reales y seguras",
            )
        )
    return checks or [Check("variables de entorno", "ok", ".env completo")]


def _executables(services: list[config.Service]) -> list[Check]:
    """El primer token de cada comando tiene que estar en el PATH.

    Cubre de una sola vez el gestor de paquetes del lockfile, docker, uvicorn y
    lo que sea que el usuario haya escrito en su stack.yaml. Un chequeo por
    tecnologia seria la misma pregunta escrita cuatro veces.
    """
    vistos: dict[str, list[str]] = {}
    for service in services:
        vistos.setdefault(_program(service.command), []).append(service.name)

    checks = []
    for programa, duenos in sorted(vistos.items()):
        if not programa:
            continue
        ruta = shutil.which(programa)
        if ruta:
            checks.append(Check(f"comando {programa}", "ok", ruta))
        else:
            checks.append(
                Check(
                    f"comando {programa}",
                    "fail",
                    f"no esta en el PATH, lo pide {', '.join(duenos)}",
                    f"instala {programa}, o corregi el comando en stack.yaml",
                )
            )
    return checks


def _docker() -> Check:
    """El daemon, no el binario. `docker` instalado con Docker Desktop cerrado
    es el caso que mas veces rompe un arranque, y el error que tira compose es
    una linea de 200 caracteres con un named pipe adentro."""
    try:
        done = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=DOCKER_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return Check("daemon de docker", "fail", "no contesto", "abri Docker Desktop")
    if done.returncode != 0:
        return Check("daemon de docker", "fail", "no esta en ejecucion", "abri Docker Desktop")
    return Check("daemon de docker", "ok", f"servidor {done.stdout.strip()}")


def _ports(services: list[config.Service]) -> list[Check]:
    """Solo los puertos declarados. El de un `npm run dev` no se sabe hasta que
    arranca, y adivinarlo seria peor que no decir nada."""
    wanted = [s.port for s in services if s.port]
    if not wanted:
        return [Check("puertos", "ok", "ninguno declarado, se descubren al arrancar")]

    scanned = ports.scan_many(wanted)
    checks = []
    for service in services:
        if not service.port:
            continue
        status = scanned.get(service.port)
        if status is None or status.free:
            checks.append(Check(f"puerto {service.port}", "ok", f"libre ({service.name})"))
            continue
        quien = status.name or "desconocido"
        checks.append(
            Check(
                f"puerto {service.port}",
                "warn",
                f"ocupado por {quien} (pid {status.pid}), lo pide {service.name}",
                f"stackhelx free {service.port}",
            )
        )
    return checks


def _archivo(root: Path) -> Path | None:
    try:
        return config.find(root)
    except config.ConfigError:
        return None


def _program(command: str) -> str:
    """Primer token del comando, sin comillas."""
    head = command.strip().split(maxsplit=1)
    return head[0].strip('"\'') if head else ""
