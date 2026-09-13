"""Exposicion de servicios locales via tuneles seguros (Cloudflare, ngrok, localtunnel, tailscale)."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from . import ports, runner

PROVIDERS = ("cloudflared", "ngrok", "lt", "tailscale")
TAILSCALE_URL = re.compile(r"https://[a-zA-Z0-9.-]+\.ts\.net(?:/\S*)?")

CLOUDFLARE_REGEX = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
NGROK_REGEX = re.compile(r"https://[a-zA-Z0-9-]+\.ngrok(?:-free)?\.app")
LOCALTUNNEL_REGEX = re.compile(r"https://[a-zA-Z0-9-]+\.loca\.lt")


class TunnelError(Exception):
    """Falla al iniciar o detectar un proveedor de tuneles."""


@dataclass
class Tunnel:
    provider: str
    port: int
    url: str
    proc: subprocess.Popen

    def stop(self) -> None:
        """Cierra el cliente de tuneles, y no solo el shell que lo lanzo.

        Con `shell=True` el hijo directo es el shell y el cliente es nieto:
        `self.proc.terminate()` mataba el `cmd` y dejaba el cloudflared vivo,
        con la URL publica funcionando. Todo lo que se hizo contra las fugas de
        tuneles (el cierre al apagar, el boton, el camino del timeout) cerraba
        el shell y no el tunel.

        `runner._terminate_tree` ya existia con este mismo problema resuelto y
        documentado, y ademas espera acotado: sin timeout, un cliente que ignora
        la señal colgaba el apagado del servidor para siempre.
        """
        if self.proc.poll() is None:
            runner._terminate_tree(self.proc)
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(timeout=3)


def detect_providers() -> list[str]:
    """Retorna los clientes de tuneles instalados en el sistema."""
    return [p for p in PROVIDERS if shutil.which(p) is not None]


def sirve_stackhelx(port: int) -> bool:
    """Si detras del puerto hay un `stackhelx serve` (o legacy `portmaster serve`).

    ponytail: es la linea de comando del dueno del puerto, o sea una heuristica.
    Lo exacto seria que `serve` dejara su puerto en un archivo, y eso es estado
    nuevo que sobrevive a un cierre feo. El costo de equivocarse es chico en las
    dos direcciones: un falso positivo pide otro puerto, un falso negativo deja
    el comportamiento que ya habia. El servidor ademas tiene su propia guarda,
    que sale del socket que bindeo y no de una cadena de texto.
    """
    try:
        estado = ports.scan(port)
    except (ValueError, OSError):
        return False
    if estado.pid == os.getpid():
        return True
    linea = (estado.cmdline or "").lower()
    return ("stackhelx" in linea or "portmaster" in linea) and "serve" in linea


sirve_portmaster = sirve_stackhelx


def start_tunnel(
    port: int,
    provider: str | None = None,
    timeout: float = 15.0,
) -> Tunnel:
    """Inicia un tunel efimero hacia el puerto especificado y extrae la URL publica."""
    # StackHelx no se publica a si mismo. Detras de ese puerto esta la API que
    # arranca stack.yaml, o sea ejecucion de comandos, y el token pasaria a ser
    # lo unico entre internet y la consola del usuario. Va aca y no en cada
    # comando porque este es el unico lugar por donde pasan todos los tuneles:
    # el boton de la interfaz, `stackhelx share` y lo que venga despues.
    if sirve_stackhelx(port):
        raise TunnelError(
            f"el puerto {port} es de un `stackhelx serve`. Publicarlo expone la "
            "API que ejecuta los comandos de tu stack.yaml, no tu proyecto."
        )

    available = detect_providers()
    if provider:
        if provider not in PROVIDERS:
            raise TunnelError(f"proveedor desconocido: {provider!r}. Soportados: {', '.join(PROVIDERS)}")
        if shutil.which(provider) is None:
            raise TunnelError(f"el binario '{provider}' no esta instalado o no se encuentra en el PATH")
        chosen = provider
    else:
        if not available:
            raise TunnelError(
                "no se encontro ningun cliente de tuneles en el PATH. "
                "Instala 'cloudflared' (recomendado), 'ngrok' o 'localtunnel'."
            )
        chosen = available[0]

    cmd, url_extractor = _provider_config(chosen, port)
    proc = subprocess.Popen(
        cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        errors="replace",
    )

    found_url: list[str] = []
    ready_event = threading.Event()
    # Las ultimas lineas, para que un fallo diga lo que dijo el cliente en vez de
    # "no se pudo": el motivo real suele estar ahi (una cuenta sin autenticar, un
    # puerto ya tomado del lado del proveedor).
    ultimas: deque[str] = deque(maxlen=3)

    def _reader():
        assert proc.stdout is not None
        for line in proc.stdout:
            if line.strip():
                ultimas.append(line.strip())
            url = url_extractor(line)
            if url and not found_url:
                found_url.append(url)
                ready_event.set()

    thread = threading.Thread(target=_reader, daemon=True)
    thread.start()

    # Mirando tambien si el proceso se murio, y no solo el reloj. Un cliente que
    # falla al arrancar (`ngrok` sin autenticar) se va en menos de un segundo, y
    # esperarle el plazo entero dejaba a `portmaster share` pareciendo colgado
    # antes de dar un error que ya se sabia.
    limite = time.monotonic() + timeout
    while not ready_event.wait(0.1):
        if proc.poll() is not None or time.monotonic() >= limite:
            break

    if not found_url:
        salida = proc.poll()
        # Por `stop` y no un `terminate` suelto: el proceso que no contesto en el
        # plazo puede estar por levantar igual, y si sobrevive al terminate queda
        # un tunel publico que nadie sabe que existe.
        Tunnel(provider=chosen, port=port, url="", proc=proc).stop()
        motivo = f": {ultimas[-1]}" if ultimas else ""
        if salida is not None:
            raise TunnelError(f"'{chosen}' termino con codigo {salida} sin publicar una URL{motivo}")
        raise TunnelError(
            f"no se pudo obtener la URL publica del tunel '{chosen}' en {timeout:.0f}s{motivo}"
        )

    return Tunnel(provider=chosen, port=port, url=found_url[0], proc=proc)


def _provider_config(
    provider: str, port: int
) -> tuple[str, Callable[[str], str | None]]:
    if provider == "cloudflared":
        cmd = f"cloudflared tunnel --url http://127.0.0.1:{port}"
        def extract(line: str) -> str | None:
            m = CLOUDFLARE_REGEX.search(line)
            return m.group(0) if m else None
        return cmd, extract

    if provider == "ngrok":
        cmd = f"ngrok http {port} --log stdout"
        def extract(line: str) -> str | None:
            m = NGROK_REGEX.search(line)
            return m.group(0) if m else None
        return cmd, extract

    if provider == "lt":
        cmd = f"lt --port {port}"
        def extract(line: str) -> str | None:
            m = LOCALTUNNEL_REGEX.search(line)
            return m.group(0) if m else None
        return cmd, extract

    if provider == "tailscale":
        cmd = f"tailscale funnel {port}"
        def extract(line: str) -> str | None:
            # Contra el host de tailscale y no contra "cualquier https": los
            # otros proveedores matchean su propio dominio, y aca un enlace a la
            # documentacion o a la pantalla de login en una linea del log se
            # reportaba como la URL del tunel.
            match = TAILSCALE_URL.search(line)
            return match.group(0) if match else None
        return cmd, extract

    raise TunnelError(f"configuracion no implementada para: {provider}")
