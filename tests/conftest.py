import os
import random
import socket

import pytest

# Debajo del rango efimero de los tres sistemas: Linux reparte desde 32768,
# macOS y Windows desde 49152. Pedir `bind(port 0)` devuelve uno de ahi arriba,
# que es justo el que el SO le esta dando a todo lo demas: entre que la fixture
# lo suelta y el test lo usa, se lo lleva cualquiera. Aca abajo nadie asigna
# nada por su cuenta, asi que la ventana solo la disputan los tests entre si.
#
# No es teorico. Costo tres rojos de CI: un mes buscando un intermitente que
# resulto ser next_free escaneando dentro del rango efimero, y dos "nunca quedo
# listo" en ubuntu 3.10 con el servidor de prueba sin poder bindear.
BANDA = (20000, 30000)

# Los intentos que hace `reservar` antes de rendirse. Una franja mas corta que
# eso deja al azar sin donde caer.
INTENTOS = 200
ANCHO_MINIMO = INTENTOS + 1


def banda():
    """La franja de la banda que le toca a este worker.

    Bindear para ver si el puerto esta libre y despues soltarlo deja una
    ventana: entre soltarlo y usarlo, se lo lleva cualquiera. Corriendo de a
    uno esa ventana la disputan los tests entre si, que es lo que dice el
    comentario de arriba. Con `pytest -n`, cada worker es un proceso aparte y
    la disputan todos a la vez.

    Darle a cada worker su franja hace que la carrera no exista, en vez de
    hacerla menos probable. Sin xdist, o si la variable trae algo que no se
    puede leer, la banda entera: mejor eso que una franja inventada.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER", "")
    cuenta = os.environ.get("PYTEST_XDIST_WORKER_COUNT", "")
    if not worker.startswith("gw") or not worker[2:].isdigit() or not cuenta.strip().isdigit():
        return BANDA
    indice = int(worker[2:])
    total = int(cuenta)
    if total < 1 or indice >= total:
        return BANDA
    ancho = (BANDA[1] - BANDA[0] + 1) // total
    # Con muchos workers la franja se vuelve mas corta que los intentos que hace
    # la reserva, y en el extremo `ancho` llega a 0 y la franja sale invertida:
    # `randint(inicio, inicio - 1)` revienta. La banda entera es peor reparto
    # pero sigue funcionando, y nadie corre con esa cantidad de workers.
    if ancho < ANCHO_MINIMO:
        return BANDA
    inicio = BANDA[0] + indice * ancho
    return (inicio, inicio + ancho - 1)


@pytest.fixture
def free_ports():
    """Reserva n puertos libres y los suelta justo antes de devolverlos."""

    def reservar(n):
        mi_banda = banda()
        socks = []
        numeros = []
        intentos = 0
        while len(numeros) < n:
            intentos += 1
            if intentos > INTENTOS:
                raise RuntimeError(f"sin puertos libres en {mi_banda} despues de 200 intentos")
            candidato = random.randint(*mi_banda)
            if candidato in numeros:
                continue
            sock = socket.socket()
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", candidato))
            except OSError:
                sock.close()
                continue
            socks.append(sock)
            numeros.append(candidato)
        for sock in socks:
            sock.close()
        return tuple(numeros)

    return reservar


# Parche defensivo para entornos Windows con Python 3.14 donde el cleanup de symlinks/junctions lanza PermissionError
if os.name == "nt":
    import contextlib

    import _pytest.pathlib

    _orig_cleanup = _pytest.pathlib.cleanup_dead_symlinks

    def _safe_cleanup(root):
        with contextlib.suppress(PermissionError, OSError):
            _orig_cleanup(root)

    _pytest.pathlib.cleanup_dead_symlinks = _safe_cleanup
