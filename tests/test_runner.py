"""Procesos reales. Un orquestador probado con mocks no prueba nada."""

import dataclasses
import io
import os
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import psutil
import pytest
from rich.console import Console

from stackhelx import config, detect, ports, runner

# Servidor minimo que anuncia su arranque y se queda escuchando.
SERVER = (
    "import socket, time; "
    "s = socket.socket(); s.bind(('127.0.0.1', {port})); s.listen(); "
    "print('SERVIDOR ARRIBA', flush=True); "
    "time.sleep(120)"
)

# Contesta HTTP y nada mas. Escrito a mano en vez de usar `http.server` porque
# el suyo llama a socket.getfqdn() entre el bind y el listen: es una resolucion
# inversa de DNS, y en el runner de macOS se cuelga mas de 20 segundos. El test
# quedaba rojo por el DNS del CI y no por nada del runner.
HTTP_SERVER = """
import socket

s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", {port}))
s.listen()
print("SERVIDOR ARRIBA", flush=True)
while True:
    conn, _ = s.accept()
    conn.recv(4096)
    conn.sendall(b"HTTP/1.1 200 OK\\r\\nContent-Length: 2\\r\\nConnection: close\\r\\n\\r\\nok")
    conn.close()
"""


def http_server_script(tmp_path, port):
    """En archivo y no en `python -c`: el cuerpo tiene comillas y saltos, y
    pasarlo por un shell (los comandos corren con shell=True) los destroza."""
    path = tmp_path / "servidor_http.py"
    path.write_text(HTTP_SERVER.format(port=port), encoding="utf-8")
    return path


def stack_from(tmp_path, body):
    path = tmp_path / "stack.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return config.load(path)


def make_runner(stack, timeout=20.0):
    return runner.Runner(stack, console=Console(file=io.StringIO()), timeout=timeout)


def test_arranca_en_orden_y_espera_el_puerto(tmp_path, free_ports):
    a, b = free_ports(2)
    stack = stack_from(
        tmp_path,
        f"""
        services:
          uno:
            command: {sys.executable} -c "{SERVER.format(port=a)}"
            port: {a}
          dos:
            command: {sys.executable} -c "{SERVER.format(port=b)}"
            port: {b}
            needs: [uno]
        """,
    )
    engine = make_runner(stack)
    try:
        engine.up()
        assert [p.service.name for p in engine.procs] == ["uno", "dos"]
        assert all(p.ready for p in engine.procs)
    finally:
        engine.down()


def test_down_apaga_el_arbol_y_libera_el_puerto(tmp_path, free_ports):
    (port,) = free_ports(1)
    stack = stack_from(
        tmp_path,
        f"""
        services:
          srv:
            command: {sys.executable} -c "{SERVER.format(port=port)}"
            port: {port}
        """,
    )
    engine = make_runner(stack)
    engine.up()
    hijos = psutil.Process(engine.procs[0].popen.pid).children(recursive=True)

    engine.down()

    deadline = time.time() + 10
    while time.time() < deadline and not runner.ports.is_free(port):
        time.sleep(0.1)
    assert runner.ports.is_free(port)
    assert all(not h.is_running() for h in hijos)


def test_ready_por_log(tmp_path):
    stack = stack_from(
        tmp_path,
        f"""
        services:
          srv:
            command: {sys.executable} -c "print('LISTO PARA RECIBIR', flush=True); import time; time.sleep(120)"
            ready: "log:LISTO PARA RECIBIR"
        """,
    )
    engine = make_runner(stack)
    try:
        engine.up()
        assert engine.procs[0].ready
    finally:
        engine.down()


def test_detached_exitoso(tmp_path):
    stack = stack_from(
        tmp_path,
        f"""
        services:
          setup:
            command: {sys.executable} -c "print('hecho')"
            detached: true
        """,
    )
    engine = make_runner(stack)
    engine.up()
    assert engine.procs[0].ready


def test_un_puerto_que_habla_http_se_puede_abrir(tmp_path, free_ports):
    (port,) = free_ports(1)
    script = http_server_script(tmp_path, port)
    stack = stack_from(
        tmp_path,
        f"""
        services:
          web:
            command: {sys.executable} "{script}"
            port: {port}
        """,
    )
    engine = make_runner(stack)
    try:
        engine.up()
        # Lo que distingue a un frontend de una base de datos es que conteste
        # HTTP, no que sirva algo util en la raiz.
        assert engine.procs[0].http is True
    finally:
        engine.down()


def test_un_socket_pelado_no_se_ofrece_para_abrir(tmp_path, free_ports):
    (port,) = free_ports(1)
    stack = stack_from(
        tmp_path,
        f"""
        services:
          db:
            command: {sys.executable} -c "{SERVER.format(port=port)}"
            port: {port}
        """,
    )
    engine = make_runner(stack)
    try:
        engine.up()
        assert engine.procs[0].ready, "esta listo igual, solo que no es abrible"
        assert engine.procs[0].http is False
    finally:
        engine.down()


def test_avisa_si_el_puerto_ya_estaba_ocupado(tmp_path, free_ports):
    """Con `ready: port` y alguien ya escuchando, el listo puede ser de otro."""
    (port,) = free_ports(1)
    intruso = socket.socket()
    intruso.bind(("127.0.0.1", port))
    intruso.listen()
    stack = stack_from(
        tmp_path,
        f"""
        services:
          api:
            command: {sys.executable} -c "import time; time.sleep(120)"
            port: {port}
        """,
    )
    salida = io.StringIO()
    engine = runner.Runner(stack, console=Console(file=salida, width=200), timeout=20.0)
    try:
        engine.up()
        assert engine.procs[0].ready, "se declara listo por el intruso, ese es el punto"
        assert engine.procs[0].port_taken
        assert "ya estaba ocupado" in salida.getvalue()
    finally:
        engine.down()
        intruso.close()


def test_un_puerto_libre_al_arrancar_no_avisa_nada(tmp_path, free_ports):
    (port,) = free_ports(1)
    stack = stack_from(
        tmp_path,
        f"""
        services:
          api:
            command: {sys.executable} -c "{SERVER.format(port=port)}"
            port: {port}
        """,
    )
    salida = io.StringIO()
    engine = runner.Runner(stack, console=Console(file=salida, width=200), timeout=20.0)
    try:
        engine.up()
        assert engine.procs[0].port_taken is False
        assert "ya estaba ocupado" not in salida.getvalue()
    finally:
        engine.down()


def test_reiniciar_un_servicio_no_toca_al_resto(tmp_path, free_ports):
    a, b = free_ports(2)
    stack = stack_from(
        tmp_path,
        f"""
        services:
          uno:
            command: {sys.executable} -c "{SERVER.format(port=a)}"
            port: {a}
          dos:
            command: {sys.executable} -c "{SERVER.format(port=b)}"
            port: {b}
            needs: [uno]
        """,
    )
    engine = make_runner(stack)
    try:
        engine.up()
        intacto = engine.procs[0].popen.pid
        viejo = engine.procs[1].popen.pid

        engine.restart("dos")

        assert engine.procs[1].popen.pid != viejo, "no se reinicio"
        assert engine.procs[1].ready
        assert engine.procs[0].popen.pid == intacto, "el otro servicio se movio"
        assert [p.service.name for p in engine.procs] == ["uno", "dos"], "cambio el orden"
        assert not psutil.pid_exists(viejo) or not psutil.Process(viejo).is_running()
    finally:
        engine.down()


def test_reiniciar_algo_que_no_esta(tmp_path):
    stack = stack_from(
        tmp_path,
        f"""
        services:
          srv:
            command: {sys.executable} -c "print('hola')"
            detached: true
        """,
    )
    engine = make_runner(stack)
    engine.up()
    with pytest.raises(runner.StartupError, match="no esta corriendo"):
        engine.restart("fantasma")


def test_el_comando_de_apagado_corre_al_bajar(tmp_path):
    """Un detached deja algo vivo fuera de nuestro arbol: matar al hijo no
    alcanza, tiene que correr su propio comando de apagado."""
    marca = tmp_path / "apagado.txt"
    stack = stack_from(
        tmp_path,
        f"""
        services:
          contenedores:
            command: {sys.executable} -c "print('arriba')"
            detached: true
            stop: {sys.executable} -c "open(r'{marca}', 'w').write('bajado')"
        """,
    )

    engine = make_runner(stack)
    engine.up()
    assert not marca.exists(), "el apagado no corre al arrancar"

    engine.down()
    assert marca.read_text(encoding="utf-8") == "bajado"


def test_un_apagado_que_falla_no_revienta(tmp_path):
    stack = stack_from(
        tmp_path,
        f"""
        services:
          contenedores:
            command: {sys.executable} -c "print('arriba')"
            detached: true
            stop: {sys.executable} -c "raise SystemExit(2)"
        """,
    )
    engine = make_runner(stack)
    engine.up()
    engine.down()  # no propaga: apagar es lo ultimo que se hace


def test_el_error_dice_la_causa_no_solo_el_codigo(tmp_path):
    stack = stack_from(
        tmp_path,
        f"""
        services:
          contenedores:
            command: {sys.executable} -c "print('no encuentro el daemon'); raise SystemExit(1)"
            detached: true
        """,
    )
    engine = make_runner(stack)
    with pytest.raises(runner.StartupError, match="no encuentro el daemon"):
        engine.up()


def test_clean_error_message_docker_daemon():
    raw = 'error during connect: Get "http://%2F%2F.%2Fpipe%2Fdocker_engine/v1.24/containers/json": open //./pipe/docker_engine: The system cannot find the file specified.'
    assert runner.clean_error_message(raw) == "Docker no está en ejecución (abrí Docker Desktop)"


def test_los_avisos_no_tapan_la_causa(tmp_path):
    """compose escupe un level=warning por cada variable sin definir, despues del
    error de verdad. La ultima linea a secas seria el aviso."""
    salida = (
        "print('la causa real'); "
        "print('time=x level=warning msg=\\\"variable sin definir\\\"'); "
        "raise SystemExit(1)"
    )
    stack = stack_from(
        tmp_path,
        f"""
        services:
          contenedores:
            command: {sys.executable} -c "{salida}"
            detached: true
        """,
    )
    engine = make_runner(stack)
    with pytest.raises(runner.StartupError, match="la causa real"):
        engine.up()


def test_detached_que_falla_aborta(tmp_path):
    stack = stack_from(
        tmp_path,
        f"""
        services:
          setup:
            command: {sys.executable} -c "raise SystemExit(3)"
            detached: true
        """,
    )
    engine = make_runner(stack)
    with pytest.raises(runner.StartupError, match="codigo 3"):
        engine.up()


def test_servicio_que_muere_antes_de_estar_listo(tmp_path, free_ports):
    (port,) = free_ports(1)
    stack = stack_from(
        tmp_path,
        f"""
        services:
          srv:
            command: {sys.executable} -c "raise SystemExit(1)"
            port: {port}
        """,
    )
    engine = make_runner(stack)
    with pytest.raises(runner.StartupError, match="antes de estar listo"):
        engine.up()


def test_timeout_de_healthcheck(tmp_path, free_ports):
    (port,) = free_ports(1)
    stack = stack_from(
        tmp_path,
        f"""
        services:
          srv:
            command: {sys.executable} -c "import time; time.sleep(120)"
            port: {port}
        """,
    )
    engine = make_runner(stack, timeout=2.0)
    with pytest.raises(runner.StartupError, match="no estuvo listo"):
        engine.up()


def test_el_timeout_avisa_si_abrio_otro_puerto(tmp_path, free_ports):
    """El caso vite: declaras 5177, el proceso se corre a 5178 y no falla."""
    declarado, real = free_ports(2)
    stack = stack_from(
        tmp_path,
        f"""
        services:
          srv:
            command: {sys.executable} -c "{SERVER.format(port=real)}"
            port: {declarado}
        """,
    )
    engine = make_runner(stack, timeout=2.0)
    with pytest.raises(runner.StartupError, match=f"pero abrio {real}"):
        engine.up()


def test_el_timeout_avisa_quien_tiene_el_puerto(tmp_path, free_ports):
    """El puerto declarado lo tiene un intruso y el servicio nunca abre nada."""
    (declarado,) = free_ports(1)
    intruso = socket.socket()
    intruso.bind(("127.0.0.1", declarado))
    intruso.listen()
    try:
        stack = stack_from(
            tmp_path,
            f"""
            services:
              srv:
                command: {sys.executable} -c "import time; time.sleep(120)"
                port: {declarado}
                ready: "log:NUNCA APARECE"
            """,
        )
        engine = make_runner(stack, timeout=2.0)
        with pytest.raises(runner.StartupError, match="ya lo tenia"):
            engine.up()
    finally:
        intruso.close()


def test_un_fallo_apaga_lo_ya_levantado(tmp_path, free_ports):
    a, b = free_ports(2)
    stack = stack_from(
        tmp_path,
        f"""
        services:
          bueno:
            command: {sys.executable} -c "{SERVER.format(port=a)}"
            port: {a}
          malo:
            command: {sys.executable} -c "raise SystemExit(1)"
            port: {b}
            needs: [bueno]
        """,
    )
    engine = make_runner(stack, timeout=5.0)
    with pytest.raises(runner.StartupError):
        engine.up()

    deadline = time.time() + 10
    while time.time() < deadline and not runner.ports.is_free(a):
        time.sleep(0.1)
    assert runner.ports.is_free(a), "el servicio bueno quedo huerfano"


def test_env_llega_al_proceso(tmp_path):
    stack = stack_from(
        tmp_path,
        f"""
        services:
          srv:
            command: {sys.executable} -c "import os; print('VALOR=' + os.environ['SALUDO'])"
            detached: true
            env:
              SALUDO: hola
        """,
    )
    salida = io.StringIO()
    engine = runner.Runner(stack, console=Console(file=salida), timeout=20.0)
    engine.up()
    assert "VALOR=hola" in salida.getvalue()


class _Cosechado:
    """Un `Popen` que ya murio y fue cosechado, con el pid de otro proceso vivo.

    Es exactamente la forma del reciclado de PID: el sistema le dio ese numero
    a alguien nuevo despues de que el nuestro terminara. Un objeto de verdad no
    sirve porque los pids no se pueden reasignar a mano.
    """

    def __init__(self, pid: int, codigo: int = 0):
        self.pid = pid
        self._codigo = codigo

    def poll(self):
        return self._codigo


def test_terminate_tree_no_mata_un_pid_reciclado():
    """El pid de un proceso ya muerto no es nuestro, y no se toca.

    `psutil` verifica el reciclado contra la identidad que capturo al construir
    el `Process`; si el pid ya se habia reciclado antes de esa linea, adopta al
    intruso y lo mata. La guarda es el `poll()` del `Popen`: mientras da None,
    el hijo esta vivo y sin cosechar y el sistema no puede reasignar su pid.

    Se afirma el efecto, no la forma: que el proceso ajeno siga vivo.
    """
    inocente = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        runner._terminate_tree(_Cosechado(inocente.pid))
        time.sleep(0.3)
        assert inocente.poll() is None, "se mato un proceso ajeno con un pid reciclado"
    finally:
        inocente.kill()
        inocente.wait(timeout=5)


def test_terminate_tree_pid_inexistente():
    # Un pid que no existe no deberia lanzar psutil.NoSuchProcess. Va vivo
    # (poll None) para pasar la guarda y llegar al psutil.Process de adentro.
    runner._terminate_tree(_Cosechado(999999, codigo=None))


LENTO = (
    "import socket, time; "
    "time.sleep(2); "
    "s = socket.socket(); s.bind(('127.0.0.1', {port})); s.listen(); "
    "time.sleep(120)"
)


def test_los_servicios_sin_dependencias_arrancan_juntos(tmp_path, free_ports):
    """Solapamiento, no duracion total: medir segundos es intermitente en tres
    sistemas operativos, y lo que importa es que los intervalos se pisen."""
    a, b, c = free_ports(3)
    stack = stack_from(
        tmp_path,
        f"""
        services:
          uno:
            command: {sys.executable} -c "{LENTO.format(port=a)}"
            port: {a}
          dos:
            command: {sys.executable} -c "{LENTO.format(port=b)}"
            port: {b}
          tres:
            command: {sys.executable} -c "{LENTO.format(port=c)}"
            port: {c}
            needs: [uno, dos]
        """,
    )
    engine = make_runner(stack)
    # Cada nivel contra si mismo, no contra un numero de segundos: el arranque
    # del interprete pesa distinto en cada sistema operativo y un umbral fijo se
    # vuelve intermitente en CI. `tres` arranca solo y da la unidad de medida.
    tiempos = {}
    original = engine._start_level

    def medido(level, colors):
        inicio = time.monotonic()
        original(level, colors)
        tiempos[tuple(s.name for s in level)] = time.monotonic() - inicio

    engine._start_level = medido
    try:
        engine.up()

        juntos = tiempos[("uno", "dos")]
        uno_solo = tiempos[("tres",)]
        assert juntos < uno_solo * 1.6, (
            f"dos servicios tardaron {juntos:.1f}s y uno solo {uno_solo:.1f}s: "
            "arrancaron en serie"
        )
        assert all(p.ready for p in engine.procs), "alguno quedo sin su healthcheck"
        assert {p.service.name for p in engine.procs} == {"uno", "dos", "tres"}
    finally:
        engine.down()


def test_cada_servicio_recibe_su_propio_healthcheck(tmp_path, free_ports):
    """`up` esperaba a `procs[-1]`, que con dos hilos en el mismo nivel es el
    servicio del otro hilo."""
    a, b = free_ports(2)
    stack = stack_from(
        tmp_path,
        f"""
        services:
          uno:
            command: {sys.executable} -c "{SERVER.format(port=a)}"
            port: {a}
          dos:
            command: {sys.executable} -c "{SERVER.format(port=b)}"
            port: {b}
        """,
    )
    engine = make_runner(stack)
    try:
        engine.up()
        assert all(p.ready for p in engine.procs)
        assert len({p.color for p in engine.procs}) == 2, "dos servicios, un solo color"
    finally:
        engine.down()


def test_un_fallo_en_el_nivel_no_deja_hermanos_vivos(tmp_path, free_ports):
    a, b = free_ports(2)
    stack = stack_from(
        tmp_path,
        f"""
        services:
          bueno:
            command: {sys.executable} -c "{LENTO.format(port=a)}"
            port: {a}
          malo:
            command: {sys.executable} -c "import time; time.sleep(0.2); raise SystemExit(1)"
            port: {b}
        """,
    )
    engine = make_runner(stack, timeout=10.0)
    with pytest.raises(runner.StartupError):
        engine.up()

    deadline = time.time() + 15
    while time.time() < deadline and not runner.ports.is_free(a):
        time.sleep(0.1)
    assert runner.ports.is_free(a), "el hermano del que fallo quedo huerfano"


def test_niveles_de_una_cadena_lineal():
    from stackhelx.config import Service

    def svc(name, needs=()):
        return Service(name, "echo", None, None, "none", tuple(needs), {}, False)

    a, b, c = svc("a"), svc("b", ["a"]), svc("c", ["b"])
    assert [[s.name for s in nivel] for nivel in runner._levels([a, b, c])] == [["a"], ["b"], ["c"]]

    d = svc("d")
    assert [[s.name for s in nivel] for nivel in runner._levels([a, d, b])] == [["a", "d"], ["b"]]


def test_env_file_inyecta_variables(tmp_path, free_ports):
    (port,) = free_ports(1)
    (tmp_path / ".env").write_text("CUSTOM_VAR=stackhelx_rocks\n", encoding="utf-8")
    flag = tmp_path / "env_result.txt"

    stack = stack_from(
        tmp_path,
        f"""
        services:
          srv:
            command: {sys.executable} -c "import os, pathlib, time, socket; pathlib.Path(r'{flag}').write_text(os.environ.get('CUSTOM_VAR', '')); s = socket.socket(); s.bind(('127.0.0.1', {port})); s.listen(); time.sleep(120)"
            port: {port}
            env_file: .env
        """,
    )
    engine = make_runner(stack)
    try:
        engine.up()
        assert flag.exists()
        assert flag.read_text(encoding="utf-8") == "stackhelx_rocks"
    finally:
        engine.down()


def test_pre_start_y_post_start(tmp_path, free_ports):
    (port,) = free_ports(1)
    pre_flag = tmp_path / "pre.txt"
    post_flag = tmp_path / "post.txt"

    stack = stack_from(
        tmp_path,
        f"""
        services:
          srv:
            command: {sys.executable} -c "{SERVER.format(port=port)}"
            port: {port}
            pre_start: {sys.executable} -c "import pathlib; pathlib.Path(r'{pre_flag}').write_text('pre_done')"
            post_start: {sys.executable} -c "import pathlib; pathlib.Path(r'{post_flag}').write_text('post_done')"
        """,
    )
    engine = make_runner(stack)
    try:
        engine.up()
        assert pre_flag.exists()
        assert pre_flag.read_text() == "pre_done"
        assert post_flag.exists()
        assert post_flag.read_text() == "post_done"
    finally:
        engine.down()


def test_pre_start_fallo_aborta_arranque(tmp_path, free_ports):
    (port,) = free_ports(1)
    stack = stack_from(
        tmp_path,
        f"""
        services:
          srv:
            command: {sys.executable} -c "{SERVER.format(port=port)}"
            port: {port}
            pre_start: {sys.executable} -c "raise SystemExit(42)"
        """,
    )
    engine = make_runner(stack)
    with pytest.raises(runner.StartupError, match="pre_start fallo con codigo 42"):
        engine.up()



@pytest.fixture
def sin_env_global(tmp_path, monkeypatch):
    """`build_env` lee `~/.stackhelx/env.global` (o `~/.portmaster/env.global`), o sea el disco de quien corre.

    Las reglas de precedencia dejan las aserciones de abajo a salvo hoy, pero un
    env.global que defina una de las variables que estos tests dan por ausentes
    las vuelve rojas en una maquina y verdes en otra. La suite no puede depender
    de que nadie haya usado la boveda global.
    """
    hogar = tmp_path / "hogar-de-prueba"
    hogar.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: hogar))
    return hogar


def _servicio_con_url(tmp_path, url, env=None, env_file=()):
    return config.Service(
        name="studio",
        command="echo hola",
        cwd=tmp_path,
        port=8765,
        ready="port",
        needs=(),
        env=env or {},
        detached=False,
        env_file=env_file,
        url=url,
    )


def test_la_url_expande_la_variable_desde_un_env_de_verdad(tmp_path, sin_env_global):
    """El caso que motivo el campo: un Studio que sirve la cascara sin token y
    exige `?token=` en toda su API. La variable sale del mismo entorno con el
    que corre el servicio, no de uno paralelo.
    """
    dotenv = tmp_path / ".env"
    dotenv.write_text("ORQUESTER_TOKEN=abc123\n", encoding="utf-8")

    service = _servicio_con_url(
        tmp_path,
        "http://127.0.0.1:8765/?token=${ORQUESTER_TOKEN}",
        env_file=(dotenv,),
    )
    assert runner.service_url(service) == "http://127.0.0.1:8765/?token=abc123"


def test_el_env_declarado_le_gana_al_env_file(tmp_path):
    """La precedencia es la que documenta `build_env`, no una segunda inventada
    para las URLs: si hubiera dos ordenes distintos, uno de los dos seria el bug.
    """
    dotenv = tmp_path / ".env"
    dotenv.write_text("TOK=delarchivo\n", encoding="utf-8")

    service = _servicio_con_url(
        tmp_path, "http://h/?t=${TOK}", env={"TOK": "declarado"}, env_file=(dotenv,)
    )
    assert runner.service_url(service) == "http://h/?t=declarado"


def test_una_variable_sin_valor_deja_el_servicio_sin_url(tmp_path, sin_env_global):
    """Abrir `?token=${TOK}` con el literal adentro es peor que no ofrecer el
    boton: la pagina carga, falla por dentro, y parece que funciono.
    """
    service = _servicio_con_url(tmp_path, "http://h/?t=${NO_EXISTE_EN_NINGUN_LADO}")
    assert runner.service_url(service) is None


def test_una_variable_sin_valor_pero_con_default_si_expande(tmp_path, sin_env_global):
    service = _servicio_con_url(tmp_path, "http://h/?t=${NO_EXISTE_TAMPOCO:-vacio}")
    assert runner.service_url(service) == "http://h/?t=vacio"


def test_sin_url_declarada_no_hay_url(tmp_path, sin_env_global):
    """El que llama arma el default con el puerto. Que `service_url` invente
    `http://localhost:<port>` seria un tercer lugar donde vive ese literal.
    """
    service = _servicio_con_url(tmp_path, None)
    assert runner.service_url(service) is None


def test_un_pre_start_colgado_falla_como_arranque_y_no_como_traceback(tmp_path, monkeypatch):
    """`subprocess.run(timeout=...)` levanta TimeoutExpired, que nadie atrapaba.

    Un `npm run build` colgado rompia el arranque con un traceback crudo en vez
    de decir que servicio y que hook se quedaron esperando, que es lo unico que
    hace falta para saber donde mirar.

    ponytail: el timeout avisa, no acota. Con `shell=True` y la salida por un
    pipe, `subprocess.run` mata al shell al vencer y vuelve a esperar la salida
    SIN timeout; el nieto todavia tiene el pipe heredado, asi que la espera dura
    lo que dure el comando colgado. Medido: `timeout=1` sobre un `sleep(20)`
    tarda 20.1s en levantar. No deja huerfanos, pero el numero no limita nada.
    Acotarlo de verdad es Popen + `_terminate_tree`, como el resto del modulo.
    Por eso este test usa un sleep corto: mide el mensaje, no el limite.
    """
    monkeypatch.setattr(runner, "DETACHED_TIMEOUT", 1)
    stack = stack_from(
        tmp_path,
        f"""
        services:
          web:
            command: echo arriba
            pre_start: {sys.executable} -c "import time; time.sleep(3)"
        """,
    )
    engine = make_runner(stack)
    try:
        with pytest.raises(runner.StartupError) as exc:
            engine.up()
        assert "web" in str(exc.value)
        assert "pre_start" in str(exc.value)
    finally:
        engine.down()


def test_service_auto_restart_on_failure(tmp_path, free_ports):
    (port,) = free_ports(1)
    flag = tmp_path / "runs.txt"
    script = tmp_path / "flaky.py"
    script.write_text(
        "import socket, time, sys, pathlib\n"
        f"p = pathlib.Path(r'{flag}')\n"
        "count = len(p.read_text().splitlines()) if p.exists() else 0\n"
        "p.write_text((p.read_text() if p.exists() else '') + f'{count+1}\\n')\n"
        "s = socket.socket()\n"
        f"s.bind(('127.0.0.1', {port}))\n"
        "s.listen()\n"
        "time.sleep(0.8 if count == 0 else 60)\n"
        "if count == 0:\n"
        "    sys.exit(1)\n",
        encoding="utf-8",
    )
    stack = stack_from(
        tmp_path,
        f"""
        services:
          flaky:
            command: {sys.executable} flaky.py
            port: {port}
            restart: on-failure
            max_retries: 2
        """,
    )
    engine = make_runner(stack)
    try:
        engine.up()
        import threading
        t = threading.Thread(target=engine.follow, daemon=True)
        t.start()
        limite = time.monotonic() + 20
        while time.monotonic() < limite:
            if flag.exists() and len(flag.read_text().splitlines()) >= 2:
                break
            time.sleep(0.1)
        assert flag.exists()
        assert len(flag.read_text().splitlines()) >= 2, "el watchdog no reinicio el servicio"
    finally:
        engine.down()


def test_runner_resource_stats(tmp_path, free_ports, monkeypatch):
    """Mide, no solo devuelve las claves.

    El test de antes preguntaba `"cpu_percent" in stats["srv"]`, o sea la forma.
    Estaba verde mientras el numero era 0.0 para siempre, porque `cpu_percent`
    resta contra la lectura anterior del mismo objeto Process y el codigo lo
    recreaba en cada llamada. El servicio de abajo quema CPU a proposito: si la
    linea base se pierde, esto se pone en 0.0 y el test falla.
    """
    (port,) = free_ports(1)
    stack = stack_from(
        tmp_path,
        f"""
        services:
          srv:
            command: {sys.executable} -c "import socket, itertools; s = socket.socket(); s.bind(('127.0.0.1', {port})); s.listen(); any(False for _ in itertools.count())"
            port: {port}
        """,
    )
    engine = make_runner(stack)
    try:
        engine.up()
        stats = engine.resource_stats()
        assert "srv" in stats
        assert stats["srv"]["memory_mb"] > 0
        assert stats["srv"]["pid"] is not None
        assert stats["srv"]["cpu_percent"] > 0, (
            f"un bucle ocupado tiene que consumir CPU, y midio {stats['srv']['cpu_percent']}"
        )

        # Segunda lectura: el camino que recorre el sondeo de la interfaz cada
        # 2.5s. Tiene que seguir midiendo y ademas no pagar el respiro, que es
        # para lo unico que sirve conservar los Process entre llamadas. Sin
        # contar los sleep, quitar el cache dejaba este test en verde.
        # El delta se toma contra la lectura anterior, asi que hay que dejar
        # pasar tiempo de verdad, como hace el sondeo cada 2.5s.
        time.sleep(0.3)
        dormidas = []
        monkeypatch.setattr(runner.time, "sleep", lambda s: dormidas.append(s))
        assert engine.resource_stats()["srv"]["cpu_percent"] > 0
        assert dormidas == [], f"la segunda lectura no deberia esperar, y espero {dormidas}"
    finally:
        engine.down()


def test_apagar_no_espera_al_pre_start_de_un_reinicio(tmp_path, free_ports):
    """`restart` corria `_spawn_proc` con `_procs_lock` tomado.

    `_spawn_proc` ejecuta `pre_start`, y su presupuesto es DETACHED_TIMEOUT:
    900s. `down` necesita ese mismo lock para copiar la lista de procesos, asi
    que apagar mientras un reinicio automatico estaba en su hook se quedaba
    esperando el hook entero. Aca el `pre_start` dura mas que el margen que se
    le da al apagado: si el lock vuelve, esto se cuelga y falla.
    """
    (port,) = free_ports(1)
    marca = tmp_path / "pre_start_arranco"
    lento = (
        f"{sys.executable} -c \"import pathlib, time; "
        f"pathlib.Path(r'{marca}').write_text('x'); time.sleep(20)\""
    )
    stack = stack_from(
        tmp_path,
        f"""
        services:
          srv:
            command: {sys.executable} -c "{SERVER.format(port=port)}"
            port: {port}
        """,
    )
    engine = make_runner(stack)
    hilo = None
    try:
        engine.up()
        # El hook se agrega despues del arranque: interesa el reinicio, no el
        # `up`, y asi el test no paga los 20s dos veces.
        original = engine.procs[0].service
        engine.procs[0].service = dataclasses.replace(original, pre_start=lento)

        fallos = []

        def reiniciar():
            try:
                engine.restart("srv")
            except Exception as exc:  # el apagado lo aborta, y eso es correcto
                fallos.append(exc)

        hilo = threading.Thread(target=reiniciar, daemon=True)
        hilo.start()

        limite = time.monotonic() + 15
        while not marca.exists() and time.monotonic() < limite:
            time.sleep(0.05)
        assert marca.exists(), "el pre_start del reinicio nunca arranco"

        t0 = time.monotonic()
        engine.down()
        tardanza = time.monotonic() - t0
        assert tardanza < 8, (
            f"apagar espero {tardanza:.1f}s al pre_start del reinicio, "
            "o sea que el lock volvio a cubrir el hook"
        )
    finally:
        engine.down()
        if hilo is not None:
            hilo.join(timeout=30)

    # El proceso que el reinicio alcanzo a levantar no puede quedar vivo: si
    # `down` ya paso por la lista, lo baja el que lo arranco.
    assert ports.is_free(port), "el reinicio dejo el servicio publicando el puerto"


def test_runner_follow_down_clean_shutdown(tmp_path, free_ports):
    (port,) = free_ports(1)
    stack = stack_from(
        tmp_path,
        f"""
        services:
          srv:
            command: {sys.executable} -c "import socket, time; s = socket.socket(); s.bind(('127.0.0.1', {port})); s.listen(); time.sleep(60)"
            port: {port}
        """,
    )
    engine = make_runner(stack)
    engine.up()
    t = threading.Thread(target=engine.follow, daemon=True)
    t.start()
    time.sleep(0.3)
    engine.down()
    t.join(timeout=2.0)
    assert not t.is_alive()
    for p in engine.procs:
        assert p.popen.poll() is not None


def test_un_stop_que_limpia_su_cache_no_queda_bloqueado(tmp_path, free_ports):
    """La lista de patrones destructivos vetaba comandos legitimos.

    `rm -rf ~/.cache/loquesea` y `del /s /q <dir>` son lo que hace media docena
    de hooks de limpieza reales, y quedaban bloqueados antes de ejecutarse. El
    modelo de confianza del proyecto dice que `stack.yaml` ya es codigo
    ejecutable por diseno: quien escribe ese comando ya tiene ejecucion
    arbitraria, asi que la lista no defendia de nadie y si le rompia el archivo
    al dueno del proyecto.

    El comando es el real de cada plataforma, no uno equivalente que el patron
    no mire: con un `python -c shutil.rmtree` este test pasaria en verde con y
    sin el bloqueo, que es no cubrir nada. En POSIX el `~` sale del HOME que se
    le declara al servicio, asi que borra dentro de tmp_path y no en el hogar
    de verdad.
    """
    (port,) = free_ports(1)
    hogar = tmp_path / "hogar"
    basura = hogar / ".cache" / "mi-proyecto"
    basura.mkdir(parents=True)
    rastro = basura / "build.log"
    rastro.write_text("x", encoding="utf-8")

    if os.name == "nt":
        limpiar = f"del /s /q {basura}"
        env = ""
    else:
        limpiar = "rm -rf ~/.cache/mi-proyecto"
        env = f"\n            env:\n              HOME: {hogar}"

    stack = stack_from(
        tmp_path,
        f"""
        services:
          srv:
            command: {sys.executable} -c "{SERVER.format(port=port)}"
            port: {port}
            stop: {limpiar}{env}
        """,
    )
    engine = make_runner(stack)
    engine.up()
    assert rastro.is_file(), "el hook todavia no corrio"

    engine.down()

    assert not rastro.exists(), (
        "el stop no llego a ejecutarse: lo bloquearon por parecerse a un "
        "comando destructivo"
    )


def test_apagar_mata_el_pre_start_que_estaba_corriendo(tmp_path, free_ports):
    """Un hook en curso sobrevivia al apagado.

    `pre_start` corria con `subprocess.run` y nadie guardaba el proceso, asi que
    `down` no tenia a quien matar: volvia en el acto y el hook seguia hasta su
    presupuesto de DETACHED_TIMEOUT, o sea 900s. Es el mismo agujero que ya
    costo caro con los tuneles, y va contra lo que el CLAUDE.md fija: cualquier
    cosa lanzada con `shell=True` se baja con `_terminate_tree`.

    El hook de abajo reescribe un contador cada 0.5s. Si sigue vivo despues del
    apagado, el archivo cambia; si murio, se queda quieto. Se afirma el efecto y
    no que se haya llamado a nadie.
    """
    (port,) = free_ports(1)
    marca = tmp_path / "contador"
    hook = (
        f'{sys.executable} -c "import pathlib,time; p=pathlib.Path(r\'{marca}\'); '
        "[(p.write_text(str(i)), time.sleep(0.5)) for i in range(60)]\""
    )
    stack = stack_from(
        tmp_path,
        f"""
        services:
          srv:
            command: {sys.executable} -c "{SERVER.format(port=port)}"
            port: {port}
            pre_start: {hook}
        """,
    )
    engine = make_runner(stack)
    arranque = threading.Thread(target=lambda: _tragar(engine.up), daemon=True)
    arranque.start()

    limite = time.monotonic() + 20
    while not marca.exists() and time.monotonic() < limite:
        time.sleep(0.05)
    assert marca.exists(), "el pre_start nunca arranco"

    engine.down()

    antes = marca.read_text()
    time.sleep(2)
    assert marca.read_text() == antes, (
        "el pre_start siguio corriendo despues del apagado: quedo un proceso "
        "huerfano que nadie va a bajar"
    )
    arranque.join(timeout=30)


def _tragar(fn):
    """`up` aborta con StartupError cuando el apagado lo corta, y esta bien."""
    try:
        fn()
    except Exception:
        pass


def test_el_stop_de_un_servicio_no_corre_dos_veces(tmp_path, free_ports):
    """`restart` y `down` podian apagar el mismo proceso a la vez.

    `restart` baja el viejo con el lock suelto, y en esa ventana `down` copia la
    lista y lo encuentra todavia ahi: los dos corrian el `stop:` del servicio.
    Con un `docker compose stop` de por medio es ruido, pero el comando lo
    escribe el usuario y no tiene por que ser idempotente.

    El `stop` de abajo cuenta sus corridas agregando una linea al archivo.
    """
    (port,) = free_ports(1)
    cuenta = tmp_path / "veces"
    contar = (
        f"{sys.executable} -c \"import pathlib; p=pathlib.Path(r'{cuenta}'); "
        "p.write_text(p.read_text() + 'x' if p.exists() else 'x')\""
    )
    stack = stack_from(
        tmp_path,
        f"""
        services:
          srv:
            command: {sys.executable} -c "{SERVER.format(port=port)}"
            port: {port}
            stop: {contar}
        """,
    )
    engine = make_runner(stack)
    engine.up()
    viejo = engine.procs[0]

    # Los dos apagados del mismo proceso, a la vez, que es la carrera real.
    hilos = [threading.Thread(target=lambda: engine._stop_one(viejo)) for _ in range(4)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=30)

    assert cuenta.read_text() == "x", (
        f"el stop corrio {len(cuenta.read_text())} veces en vez de una"
    )
    engine.down()


def test_dependency_graph(tmp_path):
    stack = stack_from(
        tmp_path,
        """
        services:
          db:
            command: echo db
            port: 5432
          api:
            command: echo api
            port: 8000
            needs: [db]
          web:
            command: echo web
            port: 3000
            needs: [api]
          worker:
            command: echo worker
            needs: [db]
        profiles:
          backend_only: [db, api]
        """,
    )
    # Grafo completo
    graph = runner.dependency_graph(stack)
    assert graph["levels"] == [["db"], ["api", "worker"], ["web"]]
    assert len(graph["nodes"]) == 4

    node_map = {n["name"]: n for n in graph["nodes"]}
    assert node_map["db"]["level"] == 0
    assert node_map["db"]["needs"] == []
    assert node_map["api"]["level"] == 1
    assert node_map["api"]["needs"] == ["db"]
    assert node_map["worker"]["level"] == 1
    assert node_map["worker"]["needs"] == ["db"]
    assert node_map["web"]["level"] == 2
    assert node_map["web"]["needs"] == ["api"]

    assert {"from": "db", "to": "api"} in graph["edges"]
    assert {"from": "db", "to": "worker"} in graph["edges"]
    assert {"from": "api", "to": "web"} in graph["edges"]

    # Grafo filtrado por perfil
    profile_graph = runner.dependency_graph(stack, profile="backend_only")
    assert profile_graph["levels"] == [["db"], ["api"]]
    assert len(profile_graph["nodes"]) == 2
    assert profile_graph["edges"] == [{"from": "db", "to": "api"}]



# bun ----------------------------------------------------------------------

BUN_SERVER = """
Bun.serve({{
  port: {port},
  fetch: () => new Response("ok"),
}});
console.log("SERVIDOR ARRIBA");
"""


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun no esta instalado")
def test_bun_detectado_arranca_y_abre_el_puerto(tmp_path, free_ports):
    """Deteccion y arranque de un proyecto Bun de punta a punta, con Bun de verdad.

    Los tests de `detect` afirman el string del comando y nada mas, que alcanza
    porque la deteccion es inspeccion de archivos. Lo que no prueban es el
    eslabon siguiente: que ese string efectivamente arranque algo y que el
    puerto aparezca. Un comando bien armado contra una API que no existe se ve
    igual de verde en `test_detect`.

    De los tres lenguajes del plan, Bun es el unico con toolchain instalado en
    la maquina de desarrollo, asi que es el unico que puede cerrar el circulo.
    Se saltea solo si `bun` no esta, para no romperle la CI a nadie.

    El puerto va en el fuente y `ready` queda en `listen`, que es lo que `_served`
    produce: se afirma que el runner lo descubre solo, sin que el stack.yaml se
    lo diga.
    """
    (port,) = free_ports(1)
    (tmp_path / "bunfig.toml").write_text("[install]\n", encoding="utf-8")
    (tmp_path / "index.ts").write_text(BUN_SERVER.format(port=port), encoding="utf-8")

    stack = detect.detect(tmp_path)
    assert stack is not None, "no se detecto el proyecto Bun"
    assert stack.services["web"].command == "bun run index.ts"

    engine = make_runner(stack, timeout=60.0)
    try:
        engine.up()
        assert engine.procs[0].ready
        # El puerto no estaba en el stack: lo descubrio el runner del socket.
        assert engine.procs[0].port == port
        assert not ports.is_free(port)
    finally:
        engine.down()

    deadline = time.time() + 10
    while time.time() < deadline and not ports.is_free(port):
        time.sleep(0.1)
    assert ports.is_free(port), "el puerto quedo tomado despues de down()"


def test_build_env_compatibilidad_stackhelx_y_portmaster(tmp_path, monkeypatch):
    """build_env lee ~/.stackhelx/env.global y usa ~/.portmaster/env.global como fallback."""
    hogar = tmp_path / "home"
    hogar.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: hogar))

    srv = config.Service(
        name="srv",
        command="echo ok",
        cwd=tmp_path,
        port=8080,
        ready="port",
        needs=(),
        env={},
        detached=False,
    )

    # 1. Sin archivo
    env = runner.build_env(srv)
    assert "MI_GLOBAL" not in env

    # 2. Solo portmaster legacy
    dir_portmaster = hogar / ".portmaster"
    dir_portmaster.mkdir()
    (dir_portmaster / "env.global").write_text("MI_GLOBAL=desde_portmaster\nOTRA_VAR=1\n", encoding="utf-8")
    env = runner.build_env(srv)
    assert env.get("MI_GLOBAL") == "desde_portmaster"
    assert env.get("OTRA_VAR") == "1"

    # 3. StackHelx sobrescribe portmaster legacy
    dir_stackhelx = hogar / ".stackhelx"
    dir_stackhelx.mkdir()
    (dir_stackhelx / "env.global").write_text("MI_GLOBAL=desde_stackhelx\n", encoding="utf-8")
    env = runner.build_env(srv)
    assert env.get("MI_GLOBAL") == "desde_stackhelx"

