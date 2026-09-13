"""Sockets y procesos reales, sin mocks: es justo lo que hay que verificar."""

import os
import pathlib
import shutil
import socket
import subprocess
import sys
import time

import psutil
import pytest

from stackhelx import ports


@pytest.fixture
def listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    yield sock, sock.getsockname()[1]
    sock.close()


@pytest.fixture
def child():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    yield proc
    if proc.poll() is None:
        proc.kill()
        proc.wait()


def test_scan_identifica_el_listener_propio(listener):
    _, port = listener
    status = ports.scan(port)
    assert status.free is False
    assert status.pid == os.getpid()
    assert status.create_time is not None


def test_scan_puerto_libre(listener):
    sock, port = listener
    sock.close()
    status = ports.scan(port)
    assert status.free is True
    assert status.pid is None


def test_accepts_distingue_bindeado_de_escuchando():
    """La ventana entre bind() y listen() que rompia el arranque en macOS.

    HTTPServer bindea, resuelve el FQDN con un DNS inverso, y recien despues
    llama a listen(). Durante esa ventana `is_free` ya dice ocupado, porque el
    bind falla, pero nadie acepta conexiones todavia. El arranque se declaraba
    listo ahi y el sondeo HTTP se comia un connection refused.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    try:
        assert ports.is_free(port) is False, "un puerto bindeado no esta libre"
        assert ports.accepts(port) is False, "bindeado sin listen() no acepta a nadie"
        sock.listen()
        assert ports.accepts(port) is True, "con listen() si acepta"
    finally:
        sock.close()


def test_accepts_ve_un_servicio_que_solo_escucha_en_ipv6():
    """El caso de vite en Windows, que costo un arranque entero de 60 segundos.

    Node moderno resuelve `localhost` a `::1`, asi que el dev server escucha
    solo en IPv6. Preguntando por 127.0.0.1, `ready: port` no lo veia nunca, y
    el error del timeout acusaba de intruso al node.exe que acababamos de
    arrancar nosotros, porque el escaneo si mira las dos familias.
    """
    if not socket.has_ipv6:
        pytest.skip("sin IPv6 en esta maquina")
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        sock.bind(("::1", 0))
    except OSError:
        sock.close()
        pytest.skip("no se pudo bindear ::1")
    port = sock.getsockname()[1]
    try:
        sock.listen()
        assert ports.accepts(port) is True, "solo pregunto por IPv4"
        assert ports.is_free(port) is False
    finally:
        sock.close()


def test_accepts_en_un_puerto_cerrado(free_ports):
    (port,) = free_ports(1)
    assert ports.accepts(port) is False


def test_next_free_salta_el_ocupado(listener):
    """La ventana va ancha a proposito. `listener` bindea al puerto 0, asi que
    arranca en el rango efimero, que es justo donde el SO esta repartiendo
    puertos al resto de la suite: con los 20 por defecto, un runner cargado se
    queda sin ninguno libre en la ventana y next_free levanta RuntimeError.
    En produccion se lo llama con un puerto declarado por el usuario, tipo 3000,
    donde 20 sobran."""
    _, port = listener
    assert ports.next_free(port, limit=500) != port


def test_puerto_fuera_de_rango():
    for value in (0, 65536, -1):
        with pytest.raises(ValueError):
            ports.scan(value)


def test_kill_rechaza_el_proceso_propio():
    with pytest.raises(ports.KillRefused):
        ports.kill(os.getpid())


def test_proxy_owner_reconoce_a_docker_y_deja_pasar_al_resto():
    # Es la linea que decide si `up` cancela o sigue con el stack a medio levantar.
    proxy = ports.PortStatus(5432, False, 4436, "wslrelay.exe")
    assert ports.proxy_owner(proxy) == "WSL"
    assert ports.proxy_owner(ports.PortStatus(3000, False, 99, "node.exe")) is None
    assert ports.proxy_owner(ports.PortStatus(3000, True)) is None


def test_kill_rechaza_un_pid_vacio():
    # psutil.Process(None) es el proceso actual: sin el guard, esto mata a pytest.
    with pytest.raises(ports.KillRefused):
        ports.kill(None)


def test_kill_rechaza_pids_protegidos():
    for pid in (0, 1, 4):
        with pytest.raises(ports.KillRefused):
            ports.kill(pid)


def test_kill_rechaza_un_ancestro():
    parent = psutil.Process().parent()
    if parent is None:
        pytest.skip("sin proceso padre visible")
    with pytest.raises(ports.KillRefused):
        ports.kill(parent.pid)


def test_kill_rechaza_pid_reciclado(child):
    with pytest.raises(ports.KillRefused, match="ya no es el proceso que se escaneo"):
        ports.kill(child.pid, create_time=1.0)
    assert child.poll() is None


def test_kill_rechaza_el_proxy_de_docker():
    """Un proceso llamado como el proxy de Docker o WSL no se mata.

    Ese proxy escucha los puertos de todos los contenedores a la vez: matarlo
    para liberar uno apaga el motor entero. La copia va al directorio del
    interprete porque un python suelto en otra carpeta no encuentra sus DLLs.
    """
    nombre = "wslrelay.exe" if os.name == "nt" else "docker-proxy"
    impostor = pathlib.Path(sys.executable).with_name(nombre)
    try:
        shutil.copy2(sys.executable, impostor)
    except OSError:
        pytest.skip("directorio del interprete no escribible")

    proc = subprocess.Popen([str(impostor), "-c", "import time; time.sleep(60)"])
    try:
        status = psutil.Process(proc.pid)
        assert status.name().lower() == nombre

        with pytest.raises(ports.KillRefused, match="proxy compartido"):
            ports.kill(proc.pid)
        assert proc.poll() is None, "el proxy sobrevivio al rechazo"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        impostor.unlink(missing_ok=True)


def test_kill_cierra_el_proceso(child):
    ports.kill(child.pid)
    assert child.poll() is not None


def test_kill_libera_el_puerto():
    code = (
        "import socket, time; "
        "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(); "
        "print(s.getsockname()[1], flush=True); time.sleep(60)"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        port = int(proc.stdout.readline().strip())
        status = ports.scan(port)
        # En Windows el python.exe del venv relanza el interprete como hijo,
        # asi que el listener puede ser un descendiente y no el PID de Popen.
        launcher = psutil.Process(proc.pid)
        assert status.pid in {proc.pid} | {c.pid for c in launcher.children(recursive=True)}

        ports.kill(status.pid, status.create_time)
        deadline = time.time() + 5
        while time.time() < deadline and not ports.is_free(port):
            time.sleep(0.1)
        assert ports.is_free(port)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def test_scan_many_usa_cache_de_procesos(listener):
    _, port = listener
    scanned = ports.scan_many([port])
    assert port in scanned
    assert scanned[port].free is False
    cache = ports._process_listeners_cache()
    assert isinstance(cache, dict)
    assert cache.get(port) == os.getpid()


def test_suggest_alternative_occupied_port(free_ports):
    (port,) = free_ports(1)
    sock = socket.socket()
    sock.bind(("127.0.0.1", port))
    sock.listen(1)
    try:
        suggested = ports.suggest_alternative(port)
        assert suggested != port
        assert ports.is_free(suggested)
    finally:
        sock.close()


def test_suggest_alternative_when_free(free_ports):
    (p,) = free_ports(1)
    # p está libre
    assert ports.suggest_alternative(p) == p


def test_suggest_alternative_excludes_ports(free_ports):
    ports_list = free_ports(3)
    p0, p1 = ports_list[0], ports_list[1]
    suggested = ports.suggest_alternative(p0, exclude={p0, p1})
    assert suggested not in {p0, p1}
    assert ports.is_free(suggested)
