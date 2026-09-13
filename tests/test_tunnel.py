import os
import subprocess
import sys
import time

import psutil
import pytest

from stackhelx import tunnel
from stackhelx.tunnel import TunnelError


def test_provider_regex_extraction():
    _, cf_extract = tunnel._provider_config("cloudflared", 3000)
    assert cf_extract("INF +--------------------------------------------------------------------------------------------+") is None
    assert cf_extract("INF |  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |") is None
    assert cf_extract("INF |  https://my-temp-tunnel-123.trycloudflare.com                                             |") == "https://my-temp-tunnel-123.trycloudflare.com"

    _, ngrok_extract = tunnel._provider_config("ngrok", 8080)
    assert ngrok_extract("t=2026-08-14 msg=\"started tunnel\" url=https://abc-123.ngrok-free.app") == "https://abc-123.ngrok-free.app"

    _, lt_extract = tunnel._provider_config("lt", 5173)
    assert lt_extract("your url is: https://sweet-lion-42.loca.lt") == "https://sweet-lion-42.loca.lt"


def test_start_tunnel_proveedor_desconocido():
    with pytest.raises(TunnelError, match="proveedor desconocido"):
        tunnel.start_tunnel(3000, provider="invalido")


def test_start_tunnel_proveedor_no_instalado(monkeypatch):
    monkeypatch.setattr(tunnel.shutil, "which", lambda x: None)
    with pytest.raises(TunnelError, match="no se encontro ningun cliente de tuneles"):
        tunnel.start_tunnel(3000)


# El test que habia aca afirmaba `mock_proc.terminate.assert_called_once()`, o
# sea justo el comportamiento que resulto estar mal: terminar el shell y dar el
# tunel por cerrado. Un mock no podia notarlo, porque el proceso que sobrevivia
# es uno que el mock no tiene. Lo cubre
# `test_stop_cierra_al_cliente_y_no_solo_al_shell`, con procesos de verdad.


# El pipeline de start_tunnel: Popen, el hilo lector, el Event y el timeout. Las
# regex ya estan cubiertas arriba una por una; lo que no ejercitaba nadie es todo
# lo que pasa alrededor, que es donde vive el unico bug que puede colgar el
# comando. Un binario falso en el PATH lo recorre entero, sin red ni cuentas.
#
# Las muestras estan reconstruidas del formato documentado de cada cliente, no
# copiadas de una corrida real. Prueban que el codigo hace lo correcto con la
# salida que creemos que sale; que sea la que realmente sale hoy solo lo dice
# correr el binario de verdad.

MUESTRAS = {
    "cloudflared": (
        ["2026-08-15T21:00:00Z INF +------------------------------------+",
         "2026-08-15T21:00:00Z INF |  Your quick Tunnel has been created! |",
         "2026-08-15T21:00:00Z INF |  https://tired-fish-run-fast.trycloudflare.com |",
         "2026-08-15T21:00:00Z INF +------------------------------------+"],
        "https://tired-fish-run-fast.trycloudflare.com",
    ),
    "ngrok": (
        ['t=2026-08-15T21:00:00+0000 lvl=info msg="started tunnel" obj=tunnels '
         "name=command_line addr=http://localhost:3000 url=https://1a2b-3c4d.ngrok-free.app"],
        "https://1a2b-3c4d.ngrok-free.app",
    ),
    "lt": (["your url is: https://tidy-moon-42.loca.lt"], "https://tidy-moon-42.loca.lt"),
    "tailscale": (
        ["Available on the internet:", "https://mi-maquina.tailnet-1234.ts.net/"],
        "https://mi-maquina.tailnet-1234.ts.net/",
    ),
}


# Cada Popen que abrio start_tunnel en el test en curso. Cuando el arranque
# falla no devuelve el Tunnel, asi que sin esto no hay forma de comprobar que no
# quedo un cliente vivo, que es justo lo que hay que comprobar.
lanzados: list[subprocess.Popen] = []


@pytest.fixture
def proveedor_falso(tmp_path, monkeypatch):
    """Pone en el PATH un binario con el nombre de un proveedor.

    Un `.bat` en Windows y un script con shebang en el resto, los dos delegando
    en este mismo interprete: es la unica forma de que `shell=True` lo encuentre
    igual en las tres plataformas del CI.
    """
    lanzados.clear()
    original = subprocess.Popen

    def espiado(*args, **kwargs):
        proc = original(*args, **kwargs)
        lanzados.append(proc)
        return proc

    monkeypatch.setattr(tunnel.subprocess, "Popen", espiado)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))

    def crear(nombre, lineas, vivo=True):
        impl = tmp_path / f"{nombre}_impl.py"
        cuerpo = "import time\n" + "".join(f"print({linea!r}, flush=True)\n" for linea in lineas)
        impl.write_text(cuerpo + ("time.sleep(30)\n" if vivo else ""), encoding="utf-8")
        if os.name == "nt":
            shim = bin_dir / f"{nombre}.bat"
            shim.write_text(f'@echo off\r\n"{sys.executable}" "{impl}" %*\r\n', encoding="utf-8")
        else:
            shim = bin_dir / nombre
            # Sin `exec`: con el, el shell se reemplaza por el interprete y no
            # queda nieto. Y `sh -c "un solo comando"` hace lo mismo por
            # optimizacion, asi que toda la cadena colapsaba en un proceso y en
            # macOS y Linux el test se quedaba sin el caso que viene a cubrir.
            shim.write_text(f'#!/bin/sh\n"{sys.executable}" "{impl}" "$@"\n', encoding="utf-8")
            shim.chmod(0o755)
        return shim

    return crear


@pytest.mark.parametrize("nombre", sorted(MUESTRAS))
def test_start_tunnel_saca_la_url_de_la_salida_del_cliente(proveedor_falso, nombre):
    lineas, esperada = MUESTRAS[nombre]
    proveedor_falso(nombre, lineas)

    tun = tunnel.start_tunnel(3000, provider=nombre, timeout=20)
    try:
        assert tun.url == esperada
        assert tun.provider == nombre
        assert tun.port == 3000
        assert tun.proc.poll() is None, "el cliente tiene que seguir vivo"
    finally:
        tun.stop()
    assert tun.proc.poll() is not None, "stop no cerro el proceso"


def test_detect_providers_ve_lo_que_hay_en_el_path(proveedor_falso):
    proveedor_falso("lt", ["nada"])
    assert "lt" in tunnel.detect_providers()


def _arbol_muerto(pid, plazo=10.0):
    """El shell y sus descendientes, todos cerrados.

    Mirar solo el shell no alcanzaba: con `shell=True` el cliente de tuneles es
    nieto, y matar al padre lo dejaba vivo publicando el puerto.
    """
    try:
        padre = psutil.Process(pid)
        procesos = padre.children(recursive=True) + [padre]
    except psutil.NoSuchProcess:
        return True
    _, vivos = psutil.wait_procs(procesos, timeout=plazo)
    return not vivos


def test_start_tunnel_sin_url_en_la_salida_corta_y_no_deja_el_proceso_vivo(proveedor_falso):
    """El caso de un cliente que arranca y nunca publica nada.

    Lo que importa no es solo el error: es que no quede ningun proceso vivo. Un
    cliente de tuneles que sobrevive al fallo sigue publicando el puerto, y ahi
    nadie sabe que existe.
    """
    proveedor_falso("cloudflared", ["INF arrancando", "INF conectando"])

    with pytest.raises(TunnelError, match="no se pudo obtener la URL"):
        tunnel.start_tunnel(3000, provider="cloudflared", timeout=2)

    assert lanzados, "el shim nunca llego a arrancar"
    for proc in lanzados:
        assert _arbol_muerto(proc.pid), "quedo un cliente de tuneles vivo despues del fallo"


def test_stop_cierra_al_cliente_y_no_solo_al_shell(proveedor_falso):
    """Con `shell=True` el hijo directo es el shell y el cliente es nieto.

    `proc.terminate()` mataba el cmd y dejaba el cloudflared corriendo con la
    URL publica activa: todo lo que se hizo contra las fugas cerraba el shell y
    no el tunel. Es el mismo problema que `runner._terminate_tree` ya resolvia
    para los servicios, documentado en CLAUDE.md.
    """
    lineas, esperada = MUESTRAS["cloudflared"]
    proveedor_falso("cloudflared", lineas)

    tun = tunnel.start_tunnel(3000, provider="cloudflared", timeout=20)
    assert tun.url == esperada
    descendientes = psutil.Process(tun.proc.pid).children(recursive=True)
    assert descendientes, "el shim tiene que dejar un nieto, que es el caso a probar"

    tun.stop()

    _, vivos = psutil.wait_procs(descendientes, timeout=10)
    assert not vivos, f"el cliente sobrevivio al stop: {[p.pid for p in vivos]}"


def test_start_tunnel_no_espera_a_un_cliente_que_ya_murio(proveedor_falso):
    """`ngrok` sin autenticar imprime el error y se va en menos de un segundo.

    Esperar el timeout entero ahi es tiempo regalado: el proceso esta muerto y
    la URL no va a aparecer nunca. Con 15s por defecto, `portmaster share`
    parecia colgado antes de dar un error que ya se sabia.
    """
    proveedor_falso("ngrok", ["ERR authentication failed"], vivo=False)

    inicio = time.monotonic()
    with pytest.raises(TunnelError):
        tunnel.start_tunnel(3000, provider="ngrok", timeout=10)
    tardo = time.monotonic() - inicio

    assert tardo < 5, f"espero {tardo:.1f}s a un proceso que ya estaba muerto"


def test_tailscale_no_confunde_un_enlace_del_log_con_el_tunel():
    """Los otros proveedores matchean su propio dominio; este tomaba el primer
    token que empezara con https. Una linea del log con el enlace a la
    documentacion o a la pantalla de login se reportaba como la URL del tunel,
    y esa es la URL que el usuario copia y comparte.
    """
    _, extract = tunnel._provider_config("tailscale", 3000)

    assert extract("Available on the internet: https://maquina.tail1234.ts.net/") == (
        "https://maquina.tail1234.ts.net/"
    )
    assert extract("To use funnel, see https://tailscale.com/kb/1223/funnel") is None
    assert extract("Log in at https://login.tailscale.com/a/abc123") is None
    assert extract("una linea sin ninguna url") is None


def test_el_mcp_cierra_sus_tuneles_al_terminar_la_sesion(tmp_path, proveedor_falso):
    """`portmaster_share` abria el tunel y nadie lo cerraba nunca.

    Es el mismo agujero que `server._ciclo_de_vida` ya documenta para la
    interfaz: la sesion terminaba y el cliente seguia vivo, con el puerto
    expuesto a internet. Aca es peor, porque del otro lado hay un agente y no
    una persona mirando la pantalla.
    """
    from stackhelx import mcp

    lineas, esperada = MUESTRAS["cloudflared"]
    proveedor_falso("cloudflared", lineas)
    mcp.cerrar_tuneles()  # arrancar de cero: el registro es de modulo

    # `portmaster_share` solo publica puertos que el proyecto declara, asi que
    # el test necesita un proyecto. Antes corria contra el cwd, que es este
    # repo, que no tiene stack.yaml: la validacion nueva lo dejo en rojo y con
    # el se cayo la unica prueba de que la sesion no deja el cliente vivo.
    (tmp_path / "stack.yaml").write_text(
        "services:\n  web:\n    command: echo web\n    port: 3000\n", encoding="utf-8"
    )

    salida = mcp._execute_tool(
        "stackhelx_share",
        {"port": 3000, "provider": "cloudflared", "path": str(tmp_path)},
    )
    assert esperada in salida
    assert len(mcp._tuneles) == 1

    descendientes = psutil.Process(mcp._tuneles[0].proc.pid).children(recursive=True)
    assert descendientes, "el shim tiene que dejar un nieto, que es el caso a probar"

    mcp.cerrar_tuneles()

    assert mcp._tuneles == []
    _, vivos = psutil.wait_procs(descendientes, timeout=10)
    assert not vivos, f"el cliente sobrevivio a la sesion: {[p.pid for p in vivos]}"


def test_el_mcp_rechaza_un_puerto_fuera_de_rango():
    """`int(args["port"])` aceptaba cualquier entero y llegaba al cliente."""
    from stackhelx import mcp

    with pytest.raises(ValueError, match="rango"):
        mcp._execute_tool("stackhelx_share", {"port": 0})


def test_no_se_publica_un_portmaster_serve(monkeypatch):
    """El unico choke point de los tuneles se niega a exponer la propia API.

    Detras de ese puerto esta lo que corre los comandos de stack.yaml. Va aca y
    no en el comando porque por `start_tunnel` pasan los tres caminos: el boton
    de la interfaz, `portmaster share` y el CLI.

    Se afirma el efecto: que no se lance ningun cliente de tuneles.
    """
    lanzados = []
    monkeypatch.setattr(tunnel.subprocess, "Popen", lambda *a, **k: lanzados.append(a))
    monkeypatch.setattr(
        tunnel.ports,
        "scan",
        lambda port: tunnel.ports.PortStatus(
            port=port, free=False, pid=123, cmdline="python -m portmaster serve --port 7666"
        ),
    )

    with pytest.raises(tunnel.TunnelError, match="serve"):
        tunnel.start_tunnel(7666)
    assert lanzados == [], "se levanto un cliente de tuneles contra la propia API"


def test_un_puerto_ajeno_no_se_confunde_con_portmaster(monkeypatch):
    """La otra mitad: la guarda no puede frenar un tunel legitimo.

    Sin esto, un `return True` constante pasaria el test de arriba.
    """
    monkeypatch.setattr(
        tunnel.ports,
        "scan",
        lambda port: tunnel.ports.PortStatus(
            port=port, free=False, pid=456, cmdline="node /app/node_modules/.bin/vite serve"
        ),
    )
    assert tunnel.sirve_stackhelx(3000) is False


def test_sirve_stackhelx_detecta_propio_pid(monkeypatch):
    monkeypatch.setattr(
        tunnel.ports,
        "scan",
        lambda port: tunnel.ports.PortStatus(
            port=port, free=False, pid=os.getpid(), cmdline="custom_executable --flag"
        ),
    )
    assert tunnel.sirve_stackhelx(7666) is True
