"""API local que consume la interfaz web.

Este servidor ejecuta comandos arbitrarios definidos en stack.yaml. Se trata
como superficie sensible aunque solo escuche en loopback:

- bind exclusivo a 127.0.0.1
- token obligatorio en Authorization: Bearer, comparado en tiempo constante
- validacion del header Host contra rebinding de DNS
- rate limit en todas las rutas
- headers de seguridad y CSP estricta
- logging de cierres de procesos, rechazos de auth y excesos de rate limit
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import queue
import secrets
import shutil
import subprocess
import sys
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import psutil
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from rich.console import Console

from . import (
    __version__,
    config,
    detect,
    docker,
    doctor,
    history,
    mcp,
    ports,
    registry,
    runner,
    tunnel,
)

# `browse_module` porque el endpoint de /api/browse ya se llama browse.
from . import browse as browse_module

log = logging.getLogger("stackhelx.server")

WEB = Path(__file__).parent / "web"
LOG_LINES = 500
PAGE_SIZE = 4
# Esperas entre re-sondeos HTTP, en segundos. Cuatro intentos y se abandona: un
# servicio que no habla HTTP no va a empezar a hacerlo, y lo que se busca cabe
# en el primer medio minuto de vida del servicio.
HTTP_RETRIES = (2, 5, 10, 20)

# Cuotas por ventana de 15 minutos. La interfaz sondea el estado cada 2s, que da
# unas 450 peticiones por ventana: un limite global de 100 la romperia en el uso
# normal. Las rutas que ejecutan o matan procesos si van cortas.
WINDOW = 900
QUOTA_READ = 1800
QUOTA_WRITE = 60
QUOTA_KILL = 30

# Techo de puertos por cierre en lote. La lista real la limita la cantidad de
# servicios registrados; esto solo evita que un body enorme llegue a recorrerse.
MAX_TARGETS = 200


class RateLimit:
    """Ventana deslizante en memoria.

    ponytail: un proceso, un usuario, sin persistencia. Si alguna vez hay mas de
    un worker, esto se cambia por un limiter con almacen compartido.
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, quota: int, window: int = WINDOW) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] <= now - window:
                hits.popleft()
            if len(hits) >= quota:
                return False
            hits.append(now)
            return True


class _Sink:
    """Recibe lo que escribe Rich y lo parte en lineas numeradas."""

    def __init__(self) -> None:
        self.lines: deque[tuple[int, str]] = deque(maxlen=LOG_LINES)
        self.seq = 0
        self._partial = ""
        self._subscribers: set[queue.Queue[tuple[int, str]]] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue[tuple[int, str]]:
        q: queue.Queue[tuple[int, str]] = queue.Queue(maxsize=1000)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue[tuple[int, str]]) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def write(self, text: str) -> int:
        items = []
        with self._lock:
            self._partial += text
            *complete, self._partial = self._partial.split("\n")
            for line in complete:
                self.seq += 1
                item = (self.seq, line.rstrip())
                self.lines.append(item)
                items.append(item)
        for item in items:
            self._dispatch(item)
        return len(text)

    def flush(self) -> None:
        item = None
        with self._lock:
            if self._partial:
                self.seq += 1
                item = (self.seq, self._partial.rstrip())
                self.lines.append(item)
                self._partial = ""
        if item is not None:
            self._dispatch(item)

    def _dispatch(self, item: tuple[int, str]) -> None:
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(item)
            except queue.Full:
                pass


@dataclass
class Session:
    """Un stack corriendo, arrancado desde la interfaz."""

    stack: config.Stack
    profile: str | None
    sink: _Sink = field(default_factory=_Sink)
    state: str = "starting"
    error: str | None = None
    engine: runner.Runner | None = None
    thread: threading.Thread | None = None
    # Servicios que no contestaron HTTP al quedar listos y si al re-sondearlos.
    # Vive aca y no en `Proc.http` para que el hilo del sondeo no le escriba
    # estado al runner por atras.
    late_http: set[str] = field(default_factory=set)
    # Un stack detenido porque el usuario apreto Apagar y uno que se murio solo
    # llegan al mismo estado por caminos distintos. Solo el segundo es noticia.
    stopped_by_user: bool = False

    def start(self) -> None:
        # force_terminal=False no es redundante con no_color: Rich mira FORCE_COLOR
        # del entorno y decide que el sink es una terminal, y no_color saca los
        # colores pero deja el dim, que llega al navegador como "[2m|[0m".
        console = Console(
            file=self.sink, force_terminal=False, no_color=True, width=160, soft_wrap=True
        )
        self.engine = runner.Runner(self.stack, console=console)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        assert self.engine is not None
        pid = registry.project_id(self.stack.root)
        t0 = time.monotonic()
        try:
            self.engine.up(self.profile)
            dur_total = time.monotonic() - t0
            
            # Registrar el éxito del arranque
            history.append(pid, {
                "project": self.stack.name,
                "profile": self.profile,
                "duration_s": round(dur_total, 2),
                "result": "running",
                # Duraciones por servicio se obtendrian si runner guardara .start_time.
                # Como es complejo refactorizar runner, guardamos las cuotas simples.
                "services": [p.service.name for p in self.engine.procs if p.ready],
            })
            
            self.state = "running"
            threading.Thread(target=self._probe_late_http, daemon=True).start()
            if all(proc.service.detached for proc in self.engine.procs):
                return
            self.engine.follow()
            if self.state != "stopping":
                self.state = "stopped"
            _save_sessions_state()
        except Exception as exc:  # el hilo no debe morir en silencio
            dur_total = time.monotonic() - t0
            self.error = runner.clean_error_message(str(exc))
            self.state = "error"
            history.append(pid, {
                "project": self.stack.name,
                "profile": self.profile,
                "duration_s": round(dur_total, 2),
                "result": "error",
                "error": self.error,
            })
            _save_sessions_state()
            log.warning("stack %s fallo: %s", self.stack.name, exc)

    def stop_async(self) -> None:
        """Apaga sin hacer esperar al request.

        `docker compose stop` tarda decenas de segundos con varios contenedores.
        La interfaz sondea el estado igual que en el arranque, asi que lo unico
        que hace falta es un estado que sepa mostrar mientras tanto.
        """
        self.state = "stopping"
        threading.Thread(target=self.stop, daemon=True).start()

    def restart_async(self, name: str) -> None:
        """Reinicia un servicio. Como el apagado, no hace esperar al request."""
        threading.Thread(target=self._restart, args=(name,), daemon=True).start()

    def _restart(self, name: str) -> None:
        assert self.engine is not None
        # El servicio vuelve con un `Proc` nuevo y su propio sondeo: lo que
        # sabiamos de la vida anterior deja de valer.
        self.late_http.discard(name)
        try:
            self.engine.restart(name)
        except Exception as exc:  # el hilo no debe morir en silencio
            self.error = runner.clean_error_message(str(exc))
            self.state = "error"
            log.warning("reinicio de %s fallo: %s", name, exc)

    def switch_profile_async(self, new_profile: str | None) -> None:
        """Conmuta el perfil en caliente apagando servicios y arrancando el nuevo perfil."""
        self.state = "stopping"
        threading.Thread(target=self._switch_profile, args=(new_profile,), daemon=True).start()

    def _switch_profile(self, new_profile: str | None) -> None:
        self.stop()
        self.profile = new_profile
        self.error = None
        self.late_http.clear()
        self.stopped_by_user = False
        self.state = "starting"
        self.sink.write(
            f"\n[stackhelx] Conmutando al perfil '{new_profile or 'default'}'...\n"
        )
        console = Console(
            file=self.sink, force_terminal=False, no_color=True, width=160, soft_wrap=True
        )
        self.engine = runner.Runner(self.stack, console=console)
        self.thread = threading.current_thread()
        self._run()

    def stop(self) -> None:
        self.stopped_by_user = True
        if self.engine is not None:
            # Apagar mientras arranca: el hilo de arranque es el que sabe que
            # levanto, asi que se le pide que corte y se lo espera. Despues su
            # `down` ya corrio y este no hace nada.
            self.engine.cancel()
            if (
                self.thread is not None
                and self.thread.is_alive()
                and self.thread != threading.current_thread()
            ):
                # ponytail: un detached en curso (`docker compose up -d`
                # construyendo) no se puede cortar, se espera a que termine. Si
                # alguna vez molesta, matar el arbol del comando detached.
                self.thread.join(runner.STOP_TIMEOUT)
            self.engine.down()
        else:
            # Sin engine: la sesion sobrevivio a un reinicio del servidor, o
            # nadie arranco esto desde aca y solo hay que bajar lo que este vivo.
            # Un contenedor no es hijo nuestro y no se apaga matando el pid del
            # puerto: ese pid es el proxy de Docker, y matarlo deja el
            # contenedor corriendo sin publicar. El `stop:` del servicio es lo
            # unico que lo baja de verdad.
            sueltos = []
            for service in reversed(list(self.stack.services.values())):
                if service.stop:
                    runner.run_stop(service)
                elif service.port:
                    sueltos.append(service.port)
            scanned = ports.scan_many(sueltos)
            for status in scanned.values():
                if not status.free and status.pid is not None:
                    if ports.proxy_owner(status) is not None:
                        # El pid es el proxy de Docker: matarlo deja el
                        # contenedor corriendo sin publicar.
                        continue
                    try:
                        ports.kill(status.pid, status.create_time, port=status.port)
                    except Exception:
                        pass
        self.state = "stopped"
        _save_sessions_state()

    def _probe_late_http(self) -> None:
        """Vuelve a preguntarle a los puertos que no contestaron al arrancar.

        Un dev server de Next compila recien en la primera peticion: el sondeo
        unico de `runner.speaks_http` da falso negativo y el proyecto se queda
        sin boton Abrir hasta el proximo arranque.

        Corre en su propio hilo y no en `_project_view` a proposito. La vista
        de estado corre una vez por proyecto y por request, cada 2.5s y por
        pestana abierta, en el threadpool que FastAPI comparte con apagar y con
        matar procesos: un puerto que acepta y no contesta lo bloquea el timeout
        entero, y saturarlo dejaria a la herramienta sin poder apagar nada justo
        cuando algo anda mal.

        Los intentos son finitos por la misma razon. Un servicio que no habla
        HTTP no va a empezar a hacerlo, y sondearlo para siempre le manda un GET
        a un dev server cada tantos segundos, que en Next dispara una
        compilacion.
        """
        for espera in HTTP_RETRIES:
            time.sleep(espera)
            if self.state != "running" or self.engine is None:
                return
            pendientes = [
                (p.service.name, p.known_port)
                for p in self.engine.procs
                if p.ready and p.known_port and not p.http and p.service.name not in self.late_http
            ]
            if not pendientes:
                return
            for name, port in pendientes:
                if runner.speaks_http(port):
                    self.late_http.add(name)
                    log.info("%s contesto HTTP al re-sondear el puerto %d", name, port)

    def service_ports(self) -> dict[str, int]:
        """Puertos descubiertos al arrancar, para los servicios que no los declaran."""
        if self.engine is None:
            return {}
        return {p.service.name: p.port for p in self.engine.procs if p.port}

    def service_http(self) -> set[str]:
        """Servicios cuyo puerto contesta HTTP: los unicos que tiene sentido abrir."""
        if self.engine is None:
            return set()
        return {p.service.name for p in self.engine.procs if p.http} | self.late_http

    def service_taken(self) -> set[str]:
        """Servicios cuyo puerto ya estaba ocupado antes de arrancar.

        El `listo` de esos puede ser de un proceso ajeno. Ver runner._spawn_proc.
        """
        if self.engine is None:
            return set()
        return {p.service.name for p in self.engine.procs if p.port_taken}

    def service_states(self) -> dict[str, str]:
        if self.state in ("stopped", "error"):
            return {}
        if self.engine is None:
            # Sesion recuperada tras reiniciar el servidor: no tenemos los
            # procesos, tenemos los puertos. Devolver {} dejaba la tarjeta
            # diciendo "corriendo" con la lista de servicios vacia.
            declarados = {n: s.port for n, s in self.stack.services.items() if s.port}
            scanned = ports.scan_many(list(declarados.values()))
            return {
                name: "ready" if not scanned[port].free else "stopped"
                for name, port in declarados.items()
            }
        states = {}
        for proc in self.engine.procs:
            # Durante un reinicio el proceso viejo ya murio y el nuevo todavia no
            # existe. Sin esto, "se cayo solo" se dispararia con cada Reiniciar.
            if self.engine.restarting:
                states[proc.service.name] = "starting"
            elif proc.popen.poll() is not None and not proc.service.detached:
                states[proc.service.name] = "stopped"
            else:
                states[proc.service.name] = "ready" if proc.ready else "starting"
        return states


sessions: dict[str, Session] = {}
selected_profiles: dict[str, str | None] = {}
sessions_lock = threading.Lock()
limiter = RateLimit()


def _current_profile(pid: str, session: Session | None) -> str | None:
    if pid in selected_profiles:
        return selected_profiles[pid]
    if session is not None:
        return session.profile
    return None

# Cuanto vale un chequeo del daemon de docker antes de repetirlo.
DOCKER_TTL = 10.0
_docker_seen: tuple[float, bool] = (0.0, False)
_docker_lock = threading.Lock()


def _docker_is_down() -> bool:
    """Si el daemon no contesta, con cache.

    `doctor._docker` lanza un subproceso y tarda cientos de milisegundos.
    `_project_view` corre una vez por proyecto y por request, y la interfaz
    sondea cada 2.5s por pestana abierta: sin cache, tres proyectos con
    contenedores se llevaban un segundo entero de cada `/api/state` y saturaban
    el threadpool que FastAPI comparte con apagar y con matar procesos. Medido:
    `/api/health` pasaba de 2ms a 9s con la vista de estado bajo carga.

    El chequeo corre adentro del lock a proposito. Afuera, una tanda de
    requests concurrentes lanza un subproceso cada uno; adentro, el primero lo
    paga y los demas esperan ese mismo resultado.
    """
    global _docker_seen
    with _docker_lock:
        cuando, valor = _docker_seen
        if time.monotonic() - cuando < DOCKER_TTL:
            return valor
        caido = doctor._docker().level == "fail"
        _docker_seen = (time.monotonic(), caido)
        return caido
# Cuanto vale una deteccion antes de repetirla. Entre los dos TTL que ya hay, y
# a proposito: `registry.PORTS_TTL` son 30s porque alimenta un aviso de puertos
# compartidos, donde llegar tarde no molesta. Esto alimenta la fila principal
# (el estado y la lista de servicios), asi que se parece mas al `DOCKER_TTL`.
STACK_TTL = 10.0
_stack_seen: dict[str, tuple[float, config.Stack]] = {}
_stack_invalidations: dict[str, float] = {}
_stack_lock = threading.Lock()


def _stack_para_la_vista(path: Path) -> config.Stack:
    """El stack del proyecto para la vista de estado, con cache.

    `detect.stack_for` relee y reparsea `pom.xml`, `package.json`,
    `compose.yaml` y todo lo demas en cada llamada, y `_project_view` corre una
    vez por proyecto y por request con la interfaz sondeando cada 2.5s por
    pestana. Medido sobre un proyecto poliglota: 6.7ms por llamada, o sea 242ms
    de lectura de disco en cada `/api/state` con doce proyectos y tres
    pestanas, releyendo archivos que casi nunca cambian.

    Cierra un hueco en vez de inventar un patron: los otros dos caminos que
    resuelven stacks en el mismo request ya estaban cacheados hace rato
    (`registry.declared_ports` y `registry.any_uses_docker`, las dos con
    `max_age=PORTS_TTL`). El de `_project_view` era el unico que quedaba
    releyendo en cada sondeo.

    **Solo para la vista.** `up`, `switch_profile` y `down` siguen llamando a
    `detect.stack_for` directo, y eso no es una omision: arrancar un stack con
    una version cacheada correria los comandos viejos despues de que el usuario
    edito su `stack.yaml`, que es exactamente lo que nadie espera. La vista
    puede estar diez segundos vieja; el arranque no puede estarlo nunca.

    `Stack` y `Service` son `frozen=True`, asi que compartir la instancia entre
    requests es seguro.
    """
    ahora = time.monotonic()
    clave = str(path)
    with _stack_lock:
        visto = _stack_seen.get(clave)
        if visto is not None and ahora - visto[0] < STACK_TTL:
            return visto[1]
    # Afuera del lock: la deteccion toca disco, y con doce proyectos adentro
    # del lock la vista se serializa entera contra el proyecto mas lento.
    inicio = time.monotonic()
    stack = detect.stack_for(path)
    with _stack_lock:
        # Si fue invalidado mientras leiamos el disco, no reinyectamos el resultado obsoleto.
        if _stack_invalidations.get(clave, 0.0) <= inicio:
            _stack_seen[clave] = (time.monotonic(), stack)
    return stack


def _olvidar_stack(path: Path) -> None:
    """Saca el proyecto del cache de la vista.

    Lo llama `freeze` y `drop_project`: sin esto el usuario apretaba "Congelar"
    o borraba el proyecto y la fila no se enteraba hasta diez segundos despues.
    """
    clave = str(path)
    with _stack_lock:
        _stack_seen.pop(clave, None)
        _stack_invalidations[clave] = time.monotonic()


def _sessions_file() -> Path:
    """Resuelto al usarlo y no al importar.

    Como constante quedaba fijada al `registry.HOME` del momento del import, y
    entonces `mkdir` creaba un directorio y `write_text` escribia en otro. En
    los tests eso mandaba el estado al home real del usuario, y con
    `PORTMASTER_TOKEN` en el entorno, donde nadie llega a crear `~/.portmaster`,
    la persistencia fallaba con un warning que no lee nadie.
    """
    return registry.HOME / "sessions.json"


def _save_sessions_state() -> None:
    try:
        data = {}
        with sessions_lock:
            for pid, session in sessions.items():
                if session.state in ("starting", "running"):
                    data[pid] = {
                        "path": str(session.stack.path),
                        "profile": session.profile,
                        "state": session.state,
                    }
        registry.HOME.mkdir(parents=True, exist_ok=True)
        _sessions_file().write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning("no se pudo guardar estado de sesiones: %s", exc)


def _load_sessions_state() -> None:
    archivo = _sessions_file()
    if not archivo.is_file():
        return
    import json
    try:
        raw = json.loads(archivo.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return
        known_paths = {registry.project_id(p): p for p in registry.paths()}
        with sessions_lock:
            for pid, item in raw.items():
                path = known_paths.get(pid)
                if not path or not path.is_dir():
                    continue
                try:
                    stack = detect.stack_for(path)
                except config.ConfigError:
                    continue
                scanned = ports.scan_many([s.port for s in stack.services.values() if s.port])
                any_busy = any(not status.free for status in scanned.values())
                if any_busy and pid not in sessions:
                    session = Session(stack, item.get("profile"))
                    session.state = "running"
                    sessions[pid] = session
    except Exception as exc:
        log.warning("no se pudo cargar estado de sesiones: %s", exc)


# seguridad ----------------------------------------------------------------


def require_token(request: Request) -> None:
    header = request.headers.get("authorization", "")
    supplied = header[7:] if header.lower().startswith("bearer ") else ""
    if not supplied:
        supplied = request.cookies.get("stackhelx_token") or request.cookies.get("portmaster_token", "")
    if not supplied:
        supplied = request.query_params.get("token", "")
    if not secrets.compare_digest(supplied, request.app.state.token):
        log.warning("auth rechazada en %s", request.url.path)
        raise HTTPException(401, "token invalido")


def quota(name: str, limit: int):
    def dependency(request: Request) -> None:
        client = request.client.host if request.client else "desconocido"
        if not limiter.allow(f"{name}:{client}", limit):
            log.warning("rate limit excedido en %s desde %s", name, client)
            raise HTTPException(429, "Demasiadas peticiones. Espera un momento.")

    return Depends(dependency)


# modelos ------------------------------------------------------------------


class AddProject(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class PathRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    editor: str | None = Field(default=None, max_length=50)




class UpRequest(BaseModel):
    profile: str | None = Field(default=None, max_length=100)


class SwitchProfileRequest(BaseModel):
    profile: str | None = Field(default=None, max_length=100)

    @field_validator("profile", mode="before")
    def _empty_to_none(cls, v: object) -> object:
        if v == "":
            return None
        return v


class KillAllRequest(BaseModel):
    """Los puertos que el cliente vio en pantalla, como filtro.

    Obligatorio a proposito. Con un campo opcional, un body mal formado (la
    clave escrita distinto, un null) se leia como "sin filtro" y el endpoint
    pasaba de cerrar lo que el usuario nombro a cerrar todo lo que encontrara.
    Un endpoint que mata procesos falla cerrado: sin lista valida, 422 y nada
    muere. Los puertos son solo un filtro; el pid y el create_time los saca el
    servidor de su propio escaneo.
    """

    ports: list[int] = Field(max_length=MAX_TARGETS)


class CleanRequest(BaseModel):
    """Que categorias de Docker limpiar.

    Obligatorio y sin default, por lo mismo que en KillAllRequest: un body mal
    formado no puede leerse como "limpia todo". El `Literal` es la validacion,
    y el comando de cada categoria sale de `docker.TARGETS`, nunca de la red.
    """

    targets: list[Literal["containers", "images", "networks", "cache", "volumes"]] = Field(
        min_length=1, max_length=5
    )


# app ----------------------------------------------------------------------


# Tuneles abiertos desde la interfaz. `None` marca un puerto reservado mientras
# el cliente de tuneles arranca, que tarda segundos: sin la reserva, dos pedidos
# a la vez dejaban uno de los dos procesos fuera del registro y por lo tanto
# imposible de cerrar.
_active_tunnels: dict[int, tunnel.Tunnel | None] = {}
_tunnels_lock = threading.Lock()


def _docker_view() -> dict:
    """Estado de Docker sobre todos los proyectos registrados, no sobre la pagina."""
    usan = registry.any_uses_docker(max_age=registry.PORTS_TTL)
    return {"needed": usan, "down": _docker_is_down() if usan else False}


def tunnels_view() -> list[dict]:
    """Los tuneles abiertos, para que la interfaz pueda mostrarlos y cerrarlos.

    De paso saca del registro los que se murieron por su cuenta: el cliente de
    tuneles se puede caer solo, y un puerto que figura expuesto sin estarlo es
    una mentira justo en el panel que existe para no mentir sobre eso.
    """
    with _tunnels_lock:
        for port, tun in list(_active_tunnels.items()):
            if tun is not None and tun.proc.poll() is not None:
                del _active_tunnels[port]
                log.info("el tunel del puerto %d se cerro solo", port)
        return [
            {"port": port, "url": tun.url, "provider": tun.provider}
            for port, tun in sorted(_active_tunnels.items())
            if tun is not None
        ]


def _cerrar_tuneles() -> None:
    """Cierra todo tunel que siga abierto."""
    with _tunnels_lock:
        vivos = [t for t in _active_tunnels.values() if t is not None]
        _active_tunnels.clear()
    for tun in vivos:
        try:
            tun.stop()
        except Exception as exc:  # un tunel roto no puede frenar el apagado
            log.warning("no se pudo cerrar el tunel del puerto %d: %s", tun.port, exc)


@contextlib.asynccontextmanager
async def _ciclo_de_vida(app: FastAPI):
    """Al apagar, cierra los tuneles.

    Sin esto `portmaster serve` terminaba y el cliente de tuneles seguia vivo,
    con el puerto expuesto a internet, sin nada en pantalla que lo dijera y sin
    forma de cerrarlo que no fuera matarlo a mano.
    """
    yield
    _cerrar_tuneles()


def create_app(token: str | None = None) -> FastAPI:
    _load_sessions_state()
    token = token or registry.token()
    app = FastAPI(
        title="StackHelx",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_ciclo_de_vida,
    )
    app.state.token = token
    app.state.allowed_hosts = {"127.0.0.1", "localhost", "[::1]"}

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # Rebinding de DNS: un sitio cualquiera puede resolver su dominio a
        # 127.0.0.1 y hablarle a este servidor desde el navegador de la victima.
        # El token ya lo frena, pero rechazar por Host cierra la puerta antes.
        host = request.headers.get("host", "").rsplit(":", 1)[0]
        if host not in app.state.allowed_hosts:
            log.warning("host rechazado: %r", host)
            return JSONResponse({"detail": "host no permitido"}, status_code=400)

        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        # Sin Strict-Transport-Security: el servidor es http en loopback y el
        # header obligaria a https a todo 127.0.0.1, rompiendo otros proyectos.
        return response

    @app.get("/", include_in_schema=False)
    def index(request: Request, token: str = Query("")) -> FileResponse:
        response = FileResponse(WEB / "index.html")
        # Solo lo que trajo quien pide. El fallback a `app.state.token` que habia
        # aca hacia que un GET / pelado se contestara con el token de verdad en
        # un Set-Cookie: cualquier proceso local conseguia la llave con un curl,
        # y con ella corre comandos, porque stack.yaml es ejecutable por diseno.
        tok = token or request.cookies.get("stackhelx_token") or request.cookies.get("portmaster_token", "")
        if tok and secrets.compare_digest(tok, request.app.state.token):
            response.set_cookie(
                "stackhelx_token",
                tok,
                httponly=False,
                samesite="lax",
                path="/",
            )
        return response

    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/api/state", dependencies=[quota("state", QUOTA_READ), Depends(require_token)])
    def state(
        q: str = Query("", max_length=200),
        status: str = Query("", max_length=20),
        page: int = Query(1, ge=1),
        size: int = Query(PAGE_SIZE, ge=1, le=50),
    ) -> dict:
        known = registry.paths()
        paths = _matching(known, q)
        if status:
            paths = [p for p in paths if _status_match(p, status)]
        total = len(paths)
        pages = max(1, -(-total // size))
        page = min(page, pages)
        # Solo se arma la vista de la pagina pedida: cada una lee la config del
        # proyecto y le escanea los puertos, y eso corre cada 2.5 segundos.
        window = paths[(page - 1) * size : page * size]
        return {
            "projects": [_project_view(path) for path in window],
            "total": total,
            # Registrados en total, filtro aparte: es lo que dice el encabezado, y
            # no tiene por que bajar cuando alguien escribe en el buscador.
            "registered": len(known),
            "page": page,
            "pages": pages,
            # Fuera del paginado a proposito: un tunel expone un puerto a
            # internet y no puede quedar escondido en la pagina 2.
            "tunnels": tunnels_view(),
            # Lo mismo con Docker. La interfaz lo sacaba de los cuatro proyectos
            # de la pagina, asi que pasar a la segunda apagaba la fila entera si
            # ahi no habia ninguno con contenedores. Un control que desaparece
            # no distingue "esta en orden" de "esto dejo de funcionar".
            "docker": _docker_view(),
        }

    @app.get("/api/browse", dependencies=[quota("state", QUOTA_READ), Depends(require_token)])
    def browse(path: str = "") -> dict:
        try:
            return browse_module.listing(path)
        except ValueError as exc:
            log.info("browse rechazado: %s", exc)
            raise HTTPException(400, str(exc))

    @app.get("/api/browse/frecuentes", dependencies=[quota("state", QUOTA_READ), Depends(require_token)])
    def browse_frecuentes() -> dict:
        return {"roots": browse_module.frequent_roots(registry.paths())}

    @app.post("/api/open-folder", dependencies=[quota("write", QUOTA_WRITE), Depends(require_token)])
    def open_folder(req: PathRequest) -> dict:
        p = Path(req.path).expanduser()
        if not p.is_absolute():
            raise HTTPException(400, f"la ruta debe ser absoluta: {req.path}")
        try:
            resolved = p.resolve(strict=True)
        except OSError:
            raise HTTPException(400, f"la carpeta no existe: {req.path}")
        if not resolved.is_dir():
            raise HTTPException(400, f"no es un directorio: {req.path}")

        p_str = str(resolved)
        try:
            if sys.platform == "win32":
                # explorer.exe fuerza a Windows a crear una ventana en primer plano
                # enfocando la carpeta solicitada en vez de un startfile pasivo.
                subprocess.Popen(["explorer.exe", p_str])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", p_str])
            else:
                subprocess.Popen(["xdg-open", p_str])
            return {"ok": True, "path": p_str}
        except Exception as exc:
            log.warning("fallo al abrir carpeta %s: %s", p_str, exc)
            raise HTTPException(500, f"fallo al abrir el explorador: {exc}")

    @app.get("/api/editors", dependencies=[quota("state", QUOTA_READ), Depends(require_token)])
    def list_editors() -> dict:
        """Detecta que editores estan instalados en el sistema."""
        available = []
        # VS Code
        if shutil.which("code.cmd") or shutil.which("code"):
            available.append({"id": "code", "name": "VS Code"})
        # Cursor
        if shutil.which("cursor.cmd") or shutil.which("cursor"):
            available.append({"id": "cursor", "name": "Cursor"})
        # Custom
        env_editor = os.environ.get("STACKHELX_EDITOR") or os.environ.get("PORTMASTER_EDITOR") or os.environ.get("EDITOR")
        if env_editor and shutil.which(env_editor):
            available.append({"id": "env", "name": f"Sistema ({env_editor})"})
        return {"editors": available}

    @app.post("/api/open-editor", dependencies=[quota("write", QUOTA_WRITE), Depends(require_token)])
    def open_editor(req: PathRequest) -> dict:
        p = Path(req.path).expanduser()
        if not p.is_absolute():
            raise HTTPException(400, f"la ruta debe ser absoluta: {req.path}")
        try:
            resolved = p.resolve(strict=True)
        except OSError:
            raise HTTPException(400, f"la carpeta no existe: {req.path}")
        if not resolved.is_dir():
            raise HTTPException(400, f"no es un directorio: {req.path}")

        # Si el usuario eligio un editor especifico del dropdown
        target = req.editor.lower().strip() if req.editor else ""
        found_editor = None
        editor_display = "Editor"

        if target == "cursor":
            found_editor = shutil.which("cursor.cmd") or shutil.which("cursor")
            editor_display = "Cursor"
        elif target == "code" or target == "vscode":
            found_editor = shutil.which("code.cmd") or shutil.which("code")
            editor_display = "VS Code"
        elif target == "env":
            cand = os.environ.get("STACKHELX_EDITOR") or os.environ.get("PORTMASTER_EDITOR") or os.environ.get("EDITOR")
            if cand and shutil.which(cand):
                found_editor = cand
                editor_display = cand

        if not found_editor:
            # Fallback a autodeteccion
            candidates = [
                os.environ.get("STACKHELX_EDITOR"),
                os.environ.get("PORTMASTER_EDITOR"),
                os.environ.get("EDITOR"),
                "cursor.cmd",
                "cursor",
                "code.cmd",
                "code",
            ]
            for cand in candidates:
                if cand and shutil.which(cand):
                    found_editor = cand
                    editor_display = "Cursor" if "cursor" in cand.lower() else ("VS Code" if "code" in cand.lower() else cand)
                    break

        if not found_editor:
            raise HTTPException(404, "No se detectó el editor solicitado en el sistema.")

        p_str = str(resolved)
        try:
            # Sin shell=True: en Windows metia la ruta por `cmd.exe /c`, y ahi
            # `list2cmdline` sola no alcanza (ver `scripts._entrecomillar`).
            # `shutil.which` devuelve el .cmd completo y CreateProcess lo corre
            # igual, asi que la capa de shell no aportaba nada y solo abria
            # superficie a los metacaracteres de cmd en el nombre de la carpeta.
            subprocess.Popen([found_editor, p_str])
            return {"ok": True, "editor": editor_display, "path": p_str}
        except Exception as exc:
            log.warning("fallo al abrir editor %s en %s: %s", found_editor, p_str, exc)
            raise HTTPException(500, f"fallo al lanzar el editor: {exc}")



    @app.get("/api/projects/{pid}/history", dependencies=[quota("state", QUOTA_READ), Depends(require_token)])
    def get_history(pid: str) -> dict:
        return {"history": history.read(pid)}

    @app.get("/api/projects/{pid}/metrics", dependencies=[quota("state", QUOTA_READ), Depends(require_token)])
    def get_metrics(pid: str) -> dict:
        with sessions_lock:
            session = sessions.get(pid)
        if session is None or session.engine is None:
            return {"metrics": {}}
        return {"metrics": session.engine.resource_stats()}

    @app.get("/api/projects/{pid}/env-audit", dependencies=[quota("state", QUOTA_READ), Depends(require_token)])
    def get_env_audit(pid: str) -> dict:
        path = _lookup(pid)
        examples = [p for p in (path / ".env.example", path / ".env.template") if p.is_file()]
        env_path = path / ".env"
        has_env = env_path.is_file()
        has_example = len(examples) > 0
        example_name = examples[0].name if has_example else None

        declaradas = doctor._parse_env_keys(examples[0]) if has_example else {}
        propias = doctor._parse_env_keys(env_path) if has_env else {}

        faltan = [k for k in declaradas if k not in propias]
        vacias = [k for k in declaradas if k in propias and not propias[k]]
        placeholders = [
            k
            for k, v in propias.items()
            if v.strip().strip("'\"").lower() in doctor._PLACEHOLDER_SECRETS
            or v.strip().strip("'\"").lower().startswith(("your_", "your-"))
        ] if has_env else []

        if has_example:
            ok = has_env and len(faltan) == 0 and len(placeholders) == 0
        else:
            ok = (not has_env) or (len(placeholders) == 0)

        # Cero secretos: sólo nombres de variables y flags booleanos.
        return {
            "ok": ok,
            "has_env": has_env,
            "has_example": has_example,
            "example_file": example_name,
            "missing_keys": faltan,
            "empty_keys": vacias,
            "placeholder_keys": placeholders,
        }

    @app.get(
        "/api/projects/{pid}/conflicts",
        dependencies=[quota("state", QUOTA_READ), Depends(require_token)],
    )
    def project_conflicts(pid: str) -> dict:
        path = _lookup(pid)
        try:
            stack = detect.stack_for(path)
        except config.ConfigError as exc:
            raise HTTPException(400, str(exc))

        with sessions_lock:
            session = sessions.get(pid)
        is_ours = session is not None and session.state in ("starting", "running")

        declared = registry.declared_ports()
        all_declared = set(declared.keys())

        conflicts = []
        wanted_ports = [s.port for s in stack.services.values() if s.port]
        scanned = ports.scan_many(wanted_ports)

        for s in stack.services.values():
            if not s.port:
                continue
            status = scanned.get(s.port)
            if status and not status.free and not is_ours:
                occ = _occupant(status, False)
                if occ is not None:
                    suggested = ports.suggest_alternative(s.port, exclude=all_declared)
                    can_kill = (
                        status.pid is not None
                        and status.pid not in ports.PROTECTED_PIDS
                        and occ.get("proxy") is None
                    )
                    conflicts.append(
                        {
                            "service": s.name,
                            "port": s.port,
                            "occupant": occ,
                            "can_kill": can_kill,
                            "suggested_port": suggested,
                        }
                    )

        return {
            "has_conflicts": len(conflicts) > 0,
            "conflicts": conflicts,
        }

    @app.post("/api/projects", dependencies=[quota("write", QUOTA_WRITE), Depends(require_token)])
    def add_project(body: AddProject) -> dict:
        try:
            path = registry.add(body.path)
        except registry.RegistryError as exc:
            log.info("alta de proyecto rechazada: %s", exc)
            raise HTTPException(400, str(exc))
        return _project_view(path)

    @app.get("/api/projects/export", dependencies=[quota("state", QUOTA_READ), Depends(require_token)])
    def export_projects() -> list[str]:
        return registry.export_data()

    @app.post("/api/projects/import", dependencies=[quota("write", QUOTA_WRITE), Depends(require_token)])
    def import_projects(paths_list: list[str]) -> dict:
        imported = registry.import_data(paths_list)
        return {"imported": imported, "count": len(imported)}

    @app.delete(
        "/api/projects/{pid}",
        dependencies=[quota("write", QUOTA_WRITE), Depends(require_token)],
    )
    def drop_project(pid: str) -> dict:
        path = _lookup(pid)
        with sessions_lock:
            session = sessions.pop(pid, None)
        if session is not None:
            session.stop()
        if not registry.remove(pid):
            raise HTTPException(404, "proyecto desconocido")
        _olvidar_stack(path)
        return {"ok": True}

    @app.post(
        "/api/projects/{pid}/freeze",
        dependencies=[quota("write", QUOTA_WRITE), Depends(require_token)],
    )
    def freeze(pid: str) -> dict:
        """Congela lo detectado en un stack.yaml editable.

        La ruta sale del registro, nunca del cuerpo del request, y el contenido
        lo genera `detect.to_yaml`. Un stack.yaml es codigo ejecutable por
        diseno (ver `shell=True` en CLAUDE.md): un endpoint que aceptara
        cualquiera de las dos cosas del cliente seria ejecucion remota local
        detras del token, en vez de un boton que hace lo que dice.
        """
        path = _lookup(pid)
        try:
            target = detect.freeze(path)
        except config.ConfigError as exc:
            log.info("congelado rechazado para %s: %s", pid, exc)
            # Ya existe, o no hay nada que congelar: en los dos casos el estado
            # del disco es el que manda, no el pedido.
            raise HTTPException(409, str(exc))

        # El stack.yaml recien escrito es lo que la vista tiene que leer ahora,
        # no dentro de diez segundos: sin esto se apretaba "Congelar" y la fila
        # seguia diciendo "detectado" hasta que venciera el cache.
        _olvidar_stack(path)
        log.info("stack congelado en %s", target)
        return {"ok": True, "path": str(target)}

    @app.post(
        "/api/projects/{pid}/up",
        dependencies=[quota("write", QUOTA_WRITE), Depends(require_token)],
    )
    def up(pid: str, body: UpRequest) -> dict:
        path = _lookup(pid)
        try:
            stack = detect.stack_for(path)
            stack.resolve(body.profile)
        except config.ConfigError as exc:
            log.info("arranque rechazado para %s: %s", pid, exc)
            raise HTTPException(400, str(exc))

        with sessions_lock:
            live = sessions.get(pid)
            if live is not None and live.state in ("starting", "running"):
                raise HTTPException(409, "el stack ya esta corriendo")
            if live is not None and live.state == "stopping":
                raise HTTPException(409, "el stack se esta apagando")
            selected_profiles[pid] = body.profile
            session = Session(stack, body.profile)
            sessions[pid] = session
        session.start()
        _save_sessions_state()
        return {"ok": True}

    @app.post(
        "/api/projects/{pid}/switch-profile",
        dependencies=[quota("write", QUOTA_WRITE), Depends(require_token)],
    )
    def switch_profile(pid: str, body: SwitchProfileRequest) -> dict:
        path = _lookup(pid)
        try:
            stack = detect.stack_for(path)
            stack.resolve(body.profile)
        except config.ConfigError as exc:
            log.info("cambio de perfil rechazado para %s: %s", pid, exc)
            raise HTTPException(400, str(exc))

        with sessions_lock:
            selected_profiles[pid] = body.profile
            session = sessions.get(pid)
            is_running = session is not None and session.state in ("starting", "running")
            if is_running:
                session.switch_profile_async(body.profile)

        _save_sessions_state()
        return {"ok": True, "profile": body.profile}

    @app.post(
        "/api/projects/{pid}/down",
        dependencies=[quota("write", QUOTA_WRITE), Depends(require_token)],
    )
    def down(pid: str) -> dict:
        path = _lookup(pid)
        with sessions_lock:
            session = sessions.get(pid)
            if session is None:
                # Los contenedores de un `docker compose up -d` desde la
                # terminal se ven "listos" en la tarjeta, y Apagar contestaba
                # 404: la interfaz mostraba algo vivo que no podia bajar.
                try:
                    stack = detect.stack_for(path)
                except config.ConfigError as exc:
                    raise HTTPException(400, str(exc))
                session = Session(stack, None)
                sessions[pid] = session
        if session.state != "stopping":
            session.stop_async()
        return {"ok": True}

    @app.post(
        "/api/projects/{pid}/services/{name}/restart",
        dependencies=[quota("write", QUOTA_WRITE), Depends(require_token)],
    )
    def restart(pid: str, name: str) -> dict:
        with sessions_lock:
            session = sessions.get(pid)
        if session is None:
            raise HTTPException(404, "ese stack no fue arrancado desde aca")
        if session.state != "running":
            raise HTTPException(409, "el stack no esta corriendo")
        if session.engine is None:
            # Sesion recuperada tras reiniciar el servidor: sabemos que los
            # puertos estan ocupados, no tenemos los procesos. Reiniciar reventaba
            # con un AssertionError adentro de un hilo daemon, o sea en silencio.
            raise HTTPException(
                409, "este stack sobrevivio a un reinicio del servidor: apagalo y volve a arrancarlo"
            )
        if name not in session.stack.services:
            log.info("reinicio rechazado en %s: servicio %r desconocido", pid, name)
            raise HTTPException(404, "ese servicio no esta en el stack")
        session.restart_async(name)
        return {"ok": True}

    @app.get(
        "/api/projects/{pid}/logs",
        dependencies=[quota("state", QUOTA_READ), Depends(require_token)],
    )
    def logs(pid: str, since: int = 0) -> dict:
        with sessions_lock:
            session = sessions.get(pid)
        if session is None:
            return {"lines": [], "seq": 0}
        pending = [
            {"seq": seq, "text": text}
            for seq, text in list(session.sink.lines)
            if seq > since
        ]
        return {"lines": pending, "seq": session.sink.seq}

    @app.get(
        "/api/projects/{pid}/logs/stream",
        dependencies=[quota("state", QUOTA_READ), Depends(require_token)],
    )
    async def stream_logs(
        pid: str,
        since: int = 0,
        follow: bool = True,
        max_duration: float | None = None,
    ) -> StreamingResponse:
        with sessions_lock:
            session = sessions.get(pid)
        if session is None:
            raise HTTPException(404, "el proyecto no esta corriendo")

        sub_q = session.sink.subscribe()

        async def event_generator():
            try:
                # 1. Emitir líneas pendientes del buffer
                pending = [
                    (seq, text)
                    for seq, text in list(session.sink.lines)
                    if seq > since
                ]
                for seq, text in pending:
                    payload = json.dumps({"seq": seq, "text": text})
                    yield f"data: {payload}\n\n"

                if not follow:
                    return

                start_time = time.monotonic()
                last_event_time = start_time
                effective_max = max_duration or 3600.0
                while True:
                    now = time.monotonic()
                    if (now - start_time) > effective_max:
                        break

                    had_event = False
                    while True:
                        try:
                            seq, text = sub_q.get_nowait()
                            payload = json.dumps({"seq": seq, "text": text})
                            yield f"data: {payload}\n\n"
                            had_event = True
                            last_event_time = now
                        except queue.Empty:
                            break

                    if not had_event and (now - last_event_time) >= 15.0:
                        yield ": keepalive\n\n"
                        last_event_time = now

                    await asyncio.sleep(0.05)
            except (asyncio.CancelledError, GeneratorExit):
                pass
            finally:
                session.sink.unsubscribe(sub_q)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post(
        "/api/ports/{port}/kill",
        dependencies=[quota("kill", QUOTA_KILL), Depends(require_token)],
    )
    def kill_port(port: int) -> dict:
        try:
            status = ports.scan(port)
        except ValueError as exc:
            log.info("puerto rechazado: %s", exc)
            raise HTTPException(400, str(exc))

        if status.free:
            return {"ok": True, "detail": "ya estaba libre"}
        if status.pid is None:
            raise HTTPException(403, "el proceso no es visible con estos permisos")

        try:
            ports.kill(status.pid, status.create_time, port=port)
        except ports.KillRefused as exc:
            log.warning("kill rechazado en %d: %s", port, exc)
            raise HTTPException(409, str(exc))
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            log.warning("kill sin permisos en %d (pid %d)", port, status.pid)
            raise HTTPException(403, "sin permisos para cerrar ese proceso")

        log.info("proceso cerrado: puerto=%d pid=%d nombre=%s", port, status.pid, status.name)
        return {"ok": True}

    @app.get(
        "/api/ports/{port}/suggest",
        dependencies=[quota("state", QUOTA_READ), Depends(require_token)],
    )
    def suggest_port(port: int) -> dict:
        try:
            ports.check_port(port)
        except ValueError as exc:
            log.info("puerto rechazado para sugerencia: %s", exc)
            raise HTTPException(400, str(exc))

        status = ports.scan(port)
        declared = registry.declared_ports()
        all_declared = set(declared.keys())
        suggested = ports.suggest_alternative(port, exclude=all_declared)
        return {
            "port": port,
            "free": status.free,
            "suggested": suggested,
            "occupant": _occupant(status, False),
        }

    @app.get(
        "/api/mcp/activity",
        dependencies=[quota("state", QUOTA_READ), Depends(require_token)],
    )
    def mcp_activity() -> dict:
        """Telemetría y registro de invocaciones de herramientas MCP para agentes IA."""
        return mcp.get_telemetry()

    @app.get("/api/version", dependencies=[quota("state", QUOTA_READ), Depends(require_token)])
    def version() -> dict:
        """Version y fecha de los estaticos que este servidor sirve.

        Existe para contestar "estoy viendo la pagina nueva o una vieja de la
        cache del navegador", que sin esto se responde adivinando.
        """
        # Todo lo que sirve el mount, y no una lista escrita a mano. La lista se
        # olvidaba tokens.css, que app.css importa y es donde viven los colores:
        # un cambio de solo estilos no movia la fecha y el sello quedaba
        # mintiendo justo en el caso para el que se puso.
        #
        # El arbol entero y no solo el primer nivel: StaticFiles sirve los
        # subdirectorios igual. Hoy `web/` es plano, asi que esto no arregla un
        # sintoma, desarma la trampa antes de que exista el primer `web/img/`.
        assets = max(f.stat().st_mtime for f in WEB.rglob("*") if f.is_file())
        return {
            "version": __version__,
            "assets": time.strftime("%Y-%m-%d %H:%M", time.localtime(assets)),
        }

    @app.get("/api/health", dependencies=[quota("state", QUOTA_READ), Depends(require_token)])
    def health() -> dict:
        """Salud de todo lo que arrancamos, sin paginar.

        `/api/state` devuelve una pagina de cuatro proyectos: alimentar de ahi un
        aviso de "algo se cayo" seria una mentira silenciosa apenas hay una
        segunda pagina. Aca no hay escaneo de puertos ni lectura de configs, solo
        un `poll()` por proceso vivo, que es lo que lo hace barato como para
        sondearlo al mismo ritmo que el estado.

        Solo mira las sesiones: un servicio que no arrancamos nosotros no es algo
        que se nos "haya caido".
        """
        with sessions_lock:
            live = list(sessions.items())

        corriendo, caidos = 0, []
        for pid, session in live:
            entrada = {"project": pid, "stack": session.stack.name}
            if session.state in ("stopped", "error") and not session.stopped_by_user:
                # El stack entero se murio. La tarjeta lo dice, pero puede estar
                # en otra pagina: es justo lo que este endpoint viene a resolver.
                caidos.append({**entrada, "service": None})
                continue
            if session.state not in ("starting", "running"):
                continue
            corriendo += 1
            for name, state in session.service_states().items():
                if state == "stopped":
                    caidos.append({**entrada, "service": name})
        return {"running": corriendo, "fallen": caidos}

    def _active_service_ports() -> set[int]:
        """Puertos que ocupa alguna sesion nuestra, de cualquier proyecto.

        Antes el descarte era por servicio y por proyecto. Con el conjunto de
        puertos, un puerto que corre en el proyecto A deja de figurar como
        intruso en el proyecto B que lo declara: quien lo tiene es nuestro, y
        el aviso de puerto compartido ya cubre ese caso en la tarjeta.
        """
        with sessions_lock:
            active_sessions = list(sessions.values())
        running_ports = set()
        for s in active_sessions:
            # Los que el servicio eligio al arrancar, ademas de los declarados.
            # Un proyecto con `ready: listen` no declara puerto (un Next, un
            # vite), asi que el suyo no entraba aca: otro proyecto que si
            # declarara ese numero lo veia ocupado y acusaba de intruso al
            # proceso que acabamos de levantar nosotros. Cerrarlo desde la
            # interfaz mataba el servicio propio, y "Liberar todos" lo hacia de
            # un click.
            descubiertos = s.service_ports()
            for name, state in s.service_states().items():
                if state == "stopped":
                    continue
                svc = s.stack.services.get(name)
                if svc and svc.port:
                    running_ports.add(svc.port)
                if name in descubiertos:
                    running_ports.add(descubiertos[name])
        return running_ports

    @app.get("/api/ports/orphans", dependencies=[quota("state", QUOTA_READ), Depends(require_token)])
    def orphans() -> dict:
        """Puertos de proyectos registrados ocupados por procesos que no lanzamos."""
        return {"orphans": registry.find_orphans(_active_service_ports())}

    @app.post(
        "/api/ports/kill-all",
        dependencies=[quota("kill", QUOTA_KILL), Depends(require_token)],
    )
    def kill_all_ports(body: KillAllRequest) -> dict:
        """Cierra los procesos intrusos que ocupan los puertos que pide el cliente.

        Del cliente viene una lista de puertos, y nada mas. El servidor vuelve a
        calcular quienes son intrusos ahora, y de ahi saca el pid y el
        create_time: un PID que llegara por la red se saltearia el escaneo, la
        exclusion del proxy de Docker y el recorte a los proyectos registrados.

        Contesta 200 aunque falle alguno. Cerrar seis procesos donde dos dan
        AccessDenied no es exito ni error, y un solo codigo HTTP no lo puede
        decir: el detalle va por item.
        """
        pedidos = set(body.ports)
        candidatos = [
            c for c in registry.find_orphans(_active_service_ports()) if c["port"] in pedidos
        ]

        killed, failed = [], []
        for candidato in candidatos:
            port, pid = candidato["port"], candidato["pid"]
            try:
                ports.kill(pid, candidato["create_time"], port=port)
            except ports.KillRefused as exc:
                failed.append({"port": port, "reason": str(exc)})
                log.warning("kill en lote rechazado en %d: %s", port, exc)
                continue
            except psutil.NoSuchProcess:
                pass  # se murio entre el escaneo y el kill: el puerto quedo libre igual
            except psutil.AccessDenied:
                failed.append({"port": port, "reason": "sin permisos para cerrar ese proceso"})
                log.warning("kill en lote sin permisos en %d (pid %d)", port, pid)
                continue
            except Exception as exc:  # un item roto no puede tirar el lote entero
                failed.append({"port": port, "reason": "no se pudo cerrar"})
                log.warning("kill en lote fallo en %d (pid %d): %s", port, pid, exc)
                continue
            killed.append({"port": port, "pid": pid, "name": candidato["name"]})
            log.info("intruso cerrado en lote: puerto %d, pid %d", port, pid)

        return {"ok": True, "killed": killed, "failed": failed}

    @app.post(
        "/api/docker/clean",
        dependencies=[quota("write", QUOTA_WRITE), Depends(require_token)],
    )
    def docker_clean(body: CleanRequest) -> dict:
        """Limpia las categorias de Docker que pidio el usuario."""
        ok, detail = docker.prune(body.targets)
        log.info("docker clean pedido %s: ok=%s (%s)", body.targets, ok, detail)
        return {"ok": ok, "detail": detail}

    @app.get(
        "/api/docker/containers",
        dependencies=[quota("state", QUOTA_READ), Depends(require_token)],
    )
    def docker_containers() -> dict:
        """Los contenedores corriendo, para que Reiniciar diga que se va a llevar.

        Reiniciar el motor los baja a todos, incluidos los de proyectos que no
        estas mirando, y eso no se puede hacer a medias. Nombrarlos antes es lo
        unico que se puede hacer por quien aprieta el boton.
        """
        return {"running": docker.running()}

    @app.get(
        "/api/docker/usage",
        dependencies=[quota("state", QUOTA_READ), Depends(require_token)],
    )
    def docker_usage() -> dict:
        """La tabla de `docker system df`, para elegir con un numero delante.

        "Limpiar Docker?" sin decir cuanto hay no es una pregunta que se pueda
        contestar. Sale tal cual la formatea Docker: parsearla seria atarnos a
        su salida a cambio de nada.
        """
        return {"table": docker.usage() or ""}

    @app.post(
        "/api/docker/{action}",
        dependencies=[quota("write", QUOTA_WRITE), Depends(require_token)],
    )
    def docker_action(action: Literal["start", "restart"]) -> dict:
        """Arranca o reinicia Docker Desktop. Si esta arriba lo dice `doctor`.

        El `Literal` es la validacion: cualquier otra cosa en la ruta la rechaza
        FastAPI con un 422 antes de llegar aca, y el comando sale igual de
        `docker.ACTIONS` y nunca de lo que vino por la red.

        200 tambien cuando no se pudo: que Docker no este instalado no es un
        error del request. El motivo va en el cuerpo, que es donde la interfaz
        lo puede mostrar.
        """
        ok, detail = docker.run(action)
        log.info("docker %s pedido: ok=%s (%s)", action, ok, detail)
        return {"ok": ok, "detail": detail}

    @app.post(
        "/api/share",
        dependencies=[quota("write", QUOTA_WRITE), Depends(require_token)],
    )
    def share_port(request: Request, port: int, provider: str | None = None) -> dict:
        """Inicia un tunel efimero para compartir un puerto."""
        # Antes del candado y de la reserva: un puerto que no existe no puede
        # dejar una entrada a medias en `_active_tunnels`. `kill` valida gratis
        # porque llama a `ports.scan`; aca no hay ningun scan que lo traiga, y
        # sin esto el 0, el -5 y el 99999 contestaban 200.
        try:
            ports.check_port(port)
        except ValueError as exc:
            log.info("puerto rechazado: %s", exc)
            raise HTTPException(400, str(exc))

        # StackHelx no se publica a si mismo. Detras de este puerto esta la API
        # que arranca stack.yaml, o sea ejecucion de comandos: exponerla deja al
        # token como unica puerta contra internet entero. La validacion de Host
        # del middleware ya rechaza al cliente de tuneles, asi que hoy el efecto
        # es una URL publica que solo sabe contestar 400; el usuario cree que
        # compartio algo y comparte la consola. Se corta aca y se dice por que.
        #
        # El puerto sale del scope ASGI, que uvicorn llena con el socket que
        # bindeo de verdad. `request.url.port` sale del header Host, y un header
        # lo escribe quien llama: serviria para esquivar justo esta guarda.
        propio = (request.scope.get("server") or (None, None))[1]
        if propio is not None and port == propio:
            raise HTTPException(
                400,
                f"el puerto {port} es el de StackHelx. Publicarlo expone la API "
                "que ejecuta los comandos de tu stack.yaml, no tu proyecto.",
            )

        with _tunnels_lock:
            # Un tunel que se murio solo no puede bloquear el puerto hasta que
            # pase el sondeo de estado: la limpieza vivia en `tunnels_view`, o
            # sea que reabrir dependia de que la interfaz hubiera refrescado.
            previo = _active_tunnels.get(port)
            if previo is not None and previo.proc.poll() is not None:
                del _active_tunnels[port]
                log.info("el tunel del puerto %d se cerro solo", port)
            if port in _active_tunnels:
                return {"ok": False, "detail": f"ya hay un tunel para el puerto {port}"}
            _active_tunnels[port] = None  # reserva, ver _active_tunnels

        try:
            tun = tunnel.start_tunnel(port, provider=provider)
        except Exception as exc:
            with _tunnels_lock:
                _active_tunnels.pop(port, None)
            if not isinstance(exc, tunnel.TunnelError):
                raise
            return {"ok": False, "detail": str(exc)}

        with _tunnels_lock:
            _active_tunnels[port] = tun
        log.info("tunel abierto en el puerto %d via %s", port, tun.provider)
        return {"ok": True, "url": tun.url, "provider": tun.provider, "port": port}

    @app.delete(
        "/api/share/{port}",
        dependencies=[quota("write", QUOTA_WRITE), Depends(require_token)],
    )
    def stop_share_port(port: int) -> dict:
        """Detiene un tunel activo."""
        with _tunnels_lock:
            # Solo un tunel ya arrancado. Sacar una reserva liberaria el puerto
            # mientras su proceso todavia esta naciendo, y ese quedaria afuera.
            tun = _active_tunnels.get(port)
            if tun is not None:
                del _active_tunnels[port]
        if tun is None:
            return {"ok": False, "detail": f"No hay tunel activo para el puerto {port}."}
        tun.stop()
        log.info("tunel del puerto %d cerrado", port)
        return {"ok": True, "detail": f"Tunel para puerto {port} cerrado."}

    return app


# vistas -------------------------------------------------------------------


def _lookup(pid: str) -> Path:
    for path in registry.paths():
        if registry.project_id(path) == pid:
            return path
    raise HTTPException(404, "proyecto desconocido")


def _matching(paths: list[Path], needle: str) -> list[Path]:
    """Filtra por nombre de carpeta o ruta.

    El nombre del stack no entra: sale de leer la config de cada proyecto, que es
    justo el trabajo que el filtro existe para no hacer.
    """
    needle = needle.strip().lower()
    if not needle:
        return paths
    return [p for p in paths if needle in p.name.lower() or needle in str(p).lower()]


def _status_match(path: Path, wanted: str) -> bool:
    """Filtra por estado general del proyecto."""
    pid = registry.project_id(path)
    with sessions_lock:
        session = sessions.get(pid)
    state = session.state if session else "stopped"
    if wanted == "running":
        return state in ("starting", "running")
    if wanted == "stopped":
        return state == "stopped"
    if wanted == "error":
        return state in ("error", "invalid")
    return True


def _project_view(path: Path) -> dict:
    pid = registry.project_id(path)
    base = {"id": pid, "path": str(path), "name": path.name}

    try:
        stack = _stack_para_la_vista(path)
    except config.ConfigError as exc:
        # Con el contrato completo: este `return` se salteaba `detected`,
        # `needs_docker` y `docker_down`, y un proyecto con la config rota
        # llegaba a la interfaz con la mitad de las claves. En JS eso es
        # `undefined`, o sea falso silencioso, y el proximo que lea
        # `p.needs_docker` esperando un booleano se come la trampa.
        return {
            **base,
            "state": "invalid",
            "error": str(exc),
            "services": [],
            "profiles": [],
            "profile": None,
            "default": [],
            "detected": False,
            "metrics": {},
            "needs_docker": False,
            "docker_down": False,
            "graph": {"levels": [], "nodes": [], "edges": []},
        }

    with sessions_lock:
        session = sessions.get(pid)
    # El estado del proyecto se lee antes que el de sus servicios, y no despues.
    # `_run` pasa a "running" recien cuando todos quedaron listos: leyendolo al
    # final, un arranque que termina entre las dos lecturas devuelve el proyecto
    # "corriendo" con los servicios todavia "arrancando".
    estado = session.state if session else "stopped"
    running = session.service_states() if session else {}
    # Los que ademas tienen un `Proc` vivo detras. None cuando no hay de donde.
    manejados = running if session and session.engine else None
    discovered = session.service_ports() if session else {}
    openable = session.service_http() if session else set()
    tomados = session.service_taken() if session else set()

    scanned = ports.scan_many([s.port for s in stack.services.values() if s.port])
    # Cacheado: resolver el stack de cada proyecto registrado cuesta de 1 a 92ms
    # segun tenga stack.yaml o haya que detectarlo, y esto corre por proyecto y
    # por request cada 2.5s. Ver registry.declared_ports.
    compartidos = registry.declared_ports(max_age=registry.PORTS_TTL)
    services = []
    for service in stack.services.values():
        status = scanned.get(service.port) if service.port else None
        state = running.get(service.name, "stopped")
        # Un detached vive fuera de nuestro arbol: si alguien lo bajo por afuera
        # (docker stop), el puerto libre es la verdad y el proceso no dice nada.
        if state == "ready" and service.detached and status is not None and status.free:
            state = "stopped"
        # Y al reves: el puerto lo publica el proxy de Docker, asi que el
        # contenedor esta arriba lo haya arrancado esta interfaz o no. Sin esto
        # un stack levantado desde la terminal se ve "detenido" con los tres
        # puertos en rojo.
        #
        # ponytail: asume que el contenedor de ese puerto es el de este
        # proyecto, la misma apuesta que hace `ready: port` en el runner.
        # Preguntarle a `docker ps` por cada puerto costaria un subproceso, y
        # esto se sondea cada 2.5s.
        if state == "stopped" and service.detached and _published(status):
            state = "ready"
        # Solo lo que contesta HTTP se ofrece para abrir: un postgres listo tiene
        # puerto y no es algo que mandar al navegador.
        abrible = state != "stopped" and service.name in openable
        occ = _occupant(status, state != "stopped")
        suggested_alt = (
            ports.suggest_alternative(service.port, set(compartidos.keys()))
            if (status is not None and not status.free and (occ is not None or state == "stopped"))
            else None
        )
        services.append(
            {
                "name": service.name,
                # Contenedor o proceso local: es lo unico que se sabe de verdad
                # del rol de un servicio. Adivinar "backend" por el nombre no.
                "kind": "container" if service.detached else "local",
                "port": service.port or discovered.get(service.name),
                "openable": abrible,
                # Adonde lleva "Abrir", si el stack.yaml lo declara. None deja
                # que la interfaz arme el default con el puerto.
                #
                # Se pregunta SOLO si ya es abrible: `runner.service_url` lee
                # `env.global` y cada `env_file` de disco, y esto corre cada 2.5s
                # para cada servicio de cada proyecto. Con los stacks apagados,
                # que es el caso normal, el boton ni se dibuja y no se toca disco.
                "url": runner.service_url(service) if abrible else None,
                "needs": list(service.needs),
                "state": state,
                "occupant": occ,
                "suggested_port": suggested_alt,
                # Otros proyectos registrados que declaran este mismo puerto.
                # Conviven mientras no corran a la vez, y saberlo antes evita
                # el "puerto ocupado" que no dice por quien.
                "shared_with": _shared_with(compartidos, service.port, path),
                # El puerto ya estaba ocupado cuando arrancamos: el verde puede
                # ser de otro proceso. Solo mientras corra la sesion que lo vio.
                "port_taken": state != "stopped" and service.name in tomados,
                # Lo arranco esta interfaz y hay un proceso del que colgarse.
                # Sin esto la tarjeta ofrecia "Reiniciar" sobre un contenedor
                # ajeno, y el boton contestaba 404. El engine hace falta aparte
                # de la sesion: una que sobrevivio a un reinicio del servidor
                # deriva sus estados del puerto y no tiene procesos, y ahi
                # `restart` contesta 409.
                "managed": manejados is not None and service.name in manejados,
            }
        )

    has_docker = any(doctor._program(s.command) == "docker" for s in stack.services.values())
    docker_down = _docker_is_down() if has_docker else False
    metrics = session.engine.resource_stats() if session and session.engine else {}

    return {
        **base,
        "name": stack.name,
        "state": estado,
        "error": session.error if session else None,
        "detected": stack.detected,
        "profiles": sorted(stack.profiles),
        "profile": _current_profile(pid, session),
        # Que levanta Arrancar sin perfil. Vacio = todos.
        "default": list(stack.default or ()),
        "services": services,
        "metrics": metrics,
        # Los dos: `docker_down` en False quiere decir "el daemon contesta" y
        # tambien "este proyecto no usa Docker", y la interfaz necesita
        # distinguirlos para poder decir "corriendo" en vez de callarse.
        "needs_docker": has_docker,
        "docker_down": docker_down,
        "graph": runner.dependency_graph(stack),
    }


def _shared_with(mapa: dict[int, list[Path]], port: int | None, mio: Path) -> list[str]:
    """Nombres de los otros proyectos que declaran `port`. Vacio si no hay."""
    if not port:
        return []
    return [registry.name_of(otro) for otro in mapa.get(port, []) if otro != mio]


def _published(status: ports.PortStatus | None) -> bool:
    """El puerto lo tiene el proxy de Docker, o sea que hay un contenedor arriba."""
    return status is not None and not status.free and ports.proxy_owner(status) is not None


def _occupant(status: ports.PortStatus | None, ours: bool) -> dict | None:
    """Quien ocupa el puerto, solo cuando no somos nosotros."""
    if status is None or status.free or ours:
        return None
    # El dueno del puerto puede ser el proxy de Docker, y entonces lo que escucha
    # ahi es un contenedor: matar ese pid apaga el backend de Docker Desktop
    # entero y deja el contenedor corriendo sin publicar. `find_orphans` ya lo
    # sabe y lo saltea, por eso la tarjeta decia "ocupado" mientras el panel de
    # intrusos decia "ninguno".
    return {
        "pid": status.pid,
        "name": status.name or "desconocido",
        "proxy": ports.proxy_owner(status),
    }
