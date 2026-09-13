"""Comandos del CLI. Servidores reales, igual que el resto del suite."""

import json
import os
import re
import socket
import subprocess
import sys
import textwrap
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from typer.testing import CliRunner

import stackhelx
from stackhelx import cli, registry

runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def sin_color(texto: str) -> str:
    """La salida sin los codigos de color de Rich.

    Rich pinta los numeros, asi que `f"El puerto {port} ya esta ocupado"` llega
    partido por un `\x1b[1;36m` en el medio y un `in` plano no lo encuentra. Solo
    aparece cuando el entorno trae `FORCE_COLOR` (la terminal del desarrollador,
    o un `portmaster up` que se lo pone a sus hijos): sin eso Rich ve que no hay
    tty y no pinta nada, que es por lo que en CI el test daba verde.

    Afirmar sobre el texto pelado, y no sobre como se ve, es lo correcto igual:
    lo que el test quiere probar es que el mensaje nombra el puerto y el comando
    que lo arregla, no de que color salen.
    """
    return _ANSI.sub("", texto)


@pytest.fixture(autouse=True)
def aislado_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "HOME", tmp_path / "home")
    monkeypatch.setattr(registry, "PROJECTS", tmp_path / "home" / "projects.json")


@pytest.fixture
def servidor_http(free_ports):
    """Un servidor HTTP de verdad, para distinguirlo de un socket pelado."""
    (port,) = free_ports(1)
    server = HTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield port
    # Las dos, y en este orden: `shutdown` corta el bucle de atencion y deja el
    # socket bindeado. Sin `server_close`, cada test que usa este fixture se
    # queda con un puerto de la banda hasta que termina la sesion entera, y
    # `free_ports` los busca al azar ahi y se rinde a los 200 intentos.
    server.shutdown()
    server.server_close()


@pytest.fixture
def abierto(monkeypatch):
    """Lo que se hubiera abierto en el navegador."""
    urls = []
    monkeypatch.setattr(webbrowser, "open", lambda url: urls.append(url) or True)
    return urls


def test_open_elige_el_puerto_que_contesta_http(
    tmp_path, monkeypatch, free_ports, servidor_http, abierto
):
    """El primer servicio del stack es una base de datos: no es lo que se abre."""
    (mudo,) = free_ports(1)
    (tmp_path / "stack.yaml").write_text(
        textwrap.dedent(f"""
        services:
          db:
            command: echo db
            port: {mudo}
          web:
            command: echo web
            port: {servidor_http}
            needs: [db]
        """),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    resultado = runner.invoke(cli.app, ["open"])
    assert resultado.exit_code == 0
    assert abierto == [f"http://localhost:{servidor_http}"]


def test_open_con_puerto_explicito(tmp_path, monkeypatch, servidor_http, abierto):
    monkeypatch.chdir(tmp_path)
    resultado = runner.invoke(cli.app, ["open", str(servidor_http)])
    assert resultado.exit_code == 0
    assert abierto == [f"http://localhost:{servidor_http}"]


def _sin_saltos(salida: str) -> str:
    """Rich parte las lineas al ancho de la consola, que en CI es 80 y en una
    terminal cualquiera es otro. Lo que se afirma es el mensaje, no el layout."""
    return " ".join(salida.split())


def test_doctor_sin_proyecto_no_revienta(tmp_path, monkeypatch):
    """Recien instalado, en una carpeta cualquiera, es el primer comando que
    alguien corre: no puede salir con ConfigError."""
    monkeypatch.chdir(tmp_path)
    resultado = runner.invoke(cli.app, ["doctor"])
    assert resultado.exit_code == 0
    assert "no es un proyecto conocido" in _sin_saltos(resultado.output)


def test_doctor_delata_el_puerto_ocupado_y_dice_como_liberarlo(
    tmp_path, monkeypatch, servidor_http
):
    (tmp_path / "stack.yaml").write_text(
        f"services:\n  web:\n    command: python web\n    port: {servidor_http}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    resultado = runner.invoke(cli.app, ["doctor"])
    assert f"stackhelx free {servidor_http}" in resultado.output
    # Un puerto ocupado es aviso, no falla: `up` ofrece liberarlo.
    assert resultado.exit_code == 0


def test_doctor_falla_si_el_comando_no_esta_en_el_path(tmp_path, monkeypatch):
    (tmp_path / "stack.yaml").write_text(
        "services:\n  web:\n    command: noexistecomandoasi --dev\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    resultado = runner.invoke(cli.app, ["doctor"])
    assert resultado.exit_code == 1
    assert "noexistecomandoasi" in resultado.output


def test_doctor_falla_con_un_stack_roto(tmp_path, monkeypatch):
    (tmp_path / "stack.yaml").write_text("services:\n  web:\n    puerto: 3000\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    resultado = runner.invoke(cli.app, ["doctor"])
    assert resultado.exit_code == 1
    assert "stack.example.yaml" in resultado.output


def test_doctor_advierte_falta_de_dotenv(tmp_path, monkeypatch):
    (tmp_path / "stack.yaml").write_text(
        "services:\n  web:\n    command: python web\n", encoding="utf-8"
    )
    (tmp_path / ".env.example").write_text("FOO=bar\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    resultado = runner.invoke(cli.app, ["doctor"])
    assert "cp .env.example .env" in resultado.output
    assert resultado.exit_code == 0


def _proyecto_con_env(tmp_path, ejemplo: str, propio: str) -> None:
    (tmp_path / "stack.yaml").write_text(
        "services:\n  web:\n    command: python web\n", encoding="utf-8"
    )
    (tmp_path / ".env.example").write_text(ejemplo, encoding="utf-8")
    (tmp_path / ".env").write_text(propio, encoding="utf-8")


def test_doctor_detecta_claves_faltantes_en_dotenv(tmp_path, monkeypatch):
    _proyecto_con_env(tmp_path, "FOO=bar\nBAR=baz\n", "FOO=bar\n")
    monkeypatch.chdir(tmp_path)

    resultado = runner.invoke(cli.app, ["doctor"])
    assert "el .env no tiene: BAR" in _sin_saltos(resultado.output)
    # Aviso y no falla: la clave puede ser opcional, o venir del entorno.
    assert resultado.exit_code == 0


def test_doctor_detecta_claves_vacias_en_dotenv(tmp_path, monkeypatch):
    _proyecto_con_env(tmp_path, "FOO=bar\nBAR=baz\n", "FOO=bar\nBAR=\n")
    monkeypatch.chdir(tmp_path)

    resultado = runner.invoke(cli.app, ["doctor"])
    assert "sin valor en el .env: BAR" in _sin_saltos(resultado.output)
    assert resultado.exit_code == 0


def test_doctor_no_muestra_valores_del_dotenv(tmp_path, monkeypatch):
    """El nombre de la clave si, el valor nunca: esto sale por HTTP en /api/doctor."""
    _proyecto_con_env(tmp_path, "TOKEN=ejemplo\nOTRA=x\n", "TOKEN=secreto-de-verdad\n")
    monkeypatch.chdir(tmp_path)

    resultado = runner.invoke(cli.app, ["doctor"])
    assert "secreto-de-verdad" not in resultado.output
    assert "el .env no tiene: OTRA" in _sin_saltos(resultado.output)


def test_doctor_detecta_placeholders_en_dotenv(tmp_path, monkeypatch):
    _proyecto_con_env(tmp_path, "API_KEY=x\n", "API_KEY=CHANGEME\n")
    monkeypatch.chdir(tmp_path)

    resultado = runner.invoke(cli.app, ["doctor"])
    assert "valores de ejemplo/inseguros en .env: API_KEY" in _sin_saltos(resultado.output)
    assert resultado.exit_code == 0



def test_doctor_desde_subcarpeta_no_reporta_colision_consigo_mismo(tmp_path, monkeypatch):
    root = tmp_path / "mi_proyecto"
    root.mkdir()
    (root / "stack.yaml").write_text(
        "services:\n  web:\n    command: python -m http.server\n    port: 9876\n",
        encoding="utf-8",
    )
    registry.add(root)
    sub = root / "src"
    sub.mkdir()
    monkeypatch.chdir(sub)

    resultado = runner.invoke(cli.app, ["doctor"])
    assert resultado.exit_code == 0
    assert "compartido" not in resultado.output


def test_doctor_con_el_dotenv_completo_no_avisa(tmp_path, monkeypatch):
    _proyecto_con_env(tmp_path, "# comentario\nFOO=bar\n\n", "FOO=otro-valor\n")
    monkeypatch.chdir(tmp_path)

    resultado = runner.invoke(cli.app, ["doctor"])
    assert ".env completo" in _sin_saltos(resultado.output)
    assert resultado.exit_code == 0


def _proyecto_con_puerto(tmp_path, nombre, port):
    root = tmp_path / nombre
    root.mkdir()
    (root / "stack.yaml").write_text(
        f"services:\n  web:\n    command: python web\n    port: {port}\n", encoding="utf-8"
    )
    return registry.add(root)


def test_doctor_avisa_si_otro_proyecto_declara_el_mismo_puerto(tmp_path, monkeypatch, free_ports):
    """El dato que solo PortMaster tiene: ningun compose sabe del de al lado."""
    (port,) = free_ports(1)
    _proyecto_con_puerto(tmp_path, "blog", port)
    fitness = _proyecto_con_puerto(tmp_path, "fitness", port)
    monkeypatch.chdir(fitness)

    salida = _sin_saltos(runner.invoke(cli.app, ["doctor"]).output)
    assert f"puerto {port} compartido" in salida
    assert "blog" in salida
    # No se delata a si mismo.
    assert salida.count("fitness") <= 1


def test_doctor_no_inventa_colisiones(tmp_path, monkeypatch, free_ports):
    uno, otro = free_ports(2)
    _proyecto_con_puerto(tmp_path, "blog", otro)
    fitness = _proyecto_con_puerto(tmp_path, "fitness", uno)
    monkeypatch.chdir(fitness)

    assert "compartido" not in runner.invoke(cli.app, ["doctor"]).output


def test_down_corre_los_stop_en_orden_inverso(tmp_path, monkeypatch):
    """Un stack de compose puro sobrevive a la terminal: `down` es la unica forma
    de bajarlo, y el orden importa igual que al arrancar."""
    marca = tmp_path / "orden.txt"
    escribir = "import sys; open(r'{f}', 'a').write('{q}\\n')"
    (tmp_path / "stack.yaml").write_text(
        textwrap.dedent(f"""
        services:
          db:
            command: echo db
            detached: true
            stop: {sys.executable} -c "{escribir.format(f=marca, q='db')}"
          api:
            command: echo api
            detached: true
            needs: [db]
            stop: {sys.executable} -c "{escribir.format(f=marca, q='api')}"
        """),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    resultado = runner.invoke(cli.app, ["down"])
    assert resultado.exit_code == 0
    assert marca.read_text(encoding="utf-8").split() == ["api", "db"]


def test_down_sin_ningun_stop_lo_dice_y_no_falla(tmp_path, monkeypatch):
    (tmp_path / "stack.yaml").write_text(
        "services:\n  web:\n    command: echo web\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    resultado = runner.invoke(cli.app, ["down"])
    assert resultado.exit_code == 0
    assert "Ctrl-C" in resultado.output


def test_down_con_un_stop_que_falla_sale_1(tmp_path, monkeypatch):
    (tmp_path / "stack.yaml").write_text(
        textwrap.dedent(f"""
        services:
          db:
            command: echo db
            detached: true
            stop: {sys.executable} -c "raise SystemExit(2)"
        """),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(cli.app, ["down"]).exit_code == 1


def test_up_env_file_inexistente(tmp_path, monkeypatch):
    (tmp_path / "stack.yaml").write_text("services:\n  web:\n    command: echo web\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    resultado = runner.invoke(cli.app, ["up", "--env-file", "no_existe.env"])
    assert resultado.exit_code == 1
    assert "No se encontró el archivo env" in resultado.output



def _stack_con_puerto(tmp_path, port):
    (tmp_path / "stack.yaml").write_text(
        textwrap.dedent(f"""
        services:
          web:
            command: {sys.executable} -c "import time; time.sleep(30)"
            port: {port}
        """),
        encoding="utf-8",
    )


def test_up_cancela_si_no_puede_liberar_el_puerto(tmp_path, monkeypatch, free_ports):
    """El puerto lo tiene el propio pytest, que kill() se niega a matar."""
    (port,) = free_ports(1)
    ocupado = socket.socket()
    ocupado.bind(("127.0.0.1", port))
    ocupado.listen()
    try:
        _stack_con_puerto(tmp_path, port)
        monkeypatch.chdir(tmp_path)
        resultado = runner.invoke(cli.app, ["up", "--yes"])
        assert resultado.exit_code == 1
        assert "Cancelado" in resultado.output
    finally:
        ocupado.close()


def test_up_no_free_ni_lo_intenta(tmp_path, monkeypatch, free_ports):
    """Con --no-free el puerto ocupado no se toca: el arranque llega y falla solo."""
    (port,) = free_ports(1)
    ocupado = socket.socket()
    ocupado.bind(("127.0.0.1", port))
    ocupado.listen()
    try:
        _stack_con_puerto(tmp_path, port)
        monkeypatch.chdir(tmp_path)
        resultado = runner.invoke(cli.app, ["up", "--yes", "--no-free"])
        assert "Cancelado" not in resultado.output
        assert "ocupado por PID" not in resultado.output
    finally:
        ocupado.close()


def test_switch_baja_al_que_le_pisa_el_puerto_y_levanta_el_pedido(tmp_path, free_ports):
    """El caso que motiva el comando: dos proyectos declaran el mismo puerto."""
    (port,) = free_ports(1)
    marca = tmp_path / "bajado.txt"

    blog = tmp_path / "blog"
    blog.mkdir()
    (blog / "stack.yaml").write_text(
        textwrap.dedent(f"""
        services:
          web:
            command: echo blog
            detached: true
            port: {port}
            stop: {sys.executable} -c "open(r'{marca}', 'a').write('blog\\n')"
        """),
        encoding="utf-8",
    )
    registry.add(blog)

    fitness = tmp_path / "fitness"
    fitness.mkdir()
    (fitness / "stack.yaml").write_text(
        textwrap.dedent(f"""
        services:
          web:
            command: echo fitness
            detached: true
            port: {port}
            # El puerto se declara para que haya conflicto; abrirlo de verdad
            # necesitaria un proceso que sobreviva al comando, que es otro test.
            ready: none
        """),
        encoding="utf-8",
    )
    registry.add(fitness)

    resultado = runner.invoke(cli.app, ["switch", "fitness", "--yes"])
    assert resultado.exit_code == 0, resultado.output
    assert marca.read_text(encoding="utf-8").split() == ["blog"], "no bajo al rival"
    assert "fitness" in _sin_saltos(resultado.output)


def test_switch_no_toca_a_quien_no_le_disputa_nada(tmp_path, free_ports):
    uno, otro = free_ports(2)
    marca = tmp_path / "bajado.txt"

    blog = tmp_path / "blog"
    blog.mkdir()
    (blog / "stack.yaml").write_text(
        textwrap.dedent(f"""
        services:
          web:
            command: echo blog
            detached: true
            port: {otro}
            stop: {sys.executable} -c "open(r'{marca}', 'a').write('blog\\n')"
        """),
        encoding="utf-8",
    )
    registry.add(blog)

    fitness = tmp_path / "fitness"
    fitness.mkdir()
    (fitness / "stack.yaml").write_text(
        f"services:\n  web:\n    command: echo fitness\n"
        f"    detached: true\n    port: {uno}\n    ready: none\n",
        encoding="utf-8",
    )
    registry.add(fitness)

    assert runner.invoke(cli.app, ["switch", "fitness", "--yes"]).exit_code == 0
    assert not marca.exists(), "bajo un proyecto que no le disputaba ningun puerto"


def test_switch_con_un_nombre_que_no_existe(tmp_path):
    root = tmp_path / "blog"
    root.mkdir()
    (root / "stack.yaml").write_text("services:\n  web:\n    command: echo x\n", encoding="utf-8")
    registry.add(root)

    resultado = runner.invoke(cli.app, ["switch", "noexiste"])
    assert resultado.exit_code == 1
    salida = _sin_saltos(resultado.output)
    assert "no es un proyecto registrado" in salida
    assert "blog" in salida, "no dice cuales si conoce"


def test_export_e_import_de_proyectos_cli(tmp_path, monkeypatch):
    root = tmp_path / "proyecto_exportable"
    root.mkdir()
    (root / "stack.yaml").write_text("services:\n  web:\n    command: python web\n", encoding="utf-8")
    registry.add(root)

    export_file = tmp_path / "backup.json"
    res_export_file = runner.invoke(cli.app, ["export", str(export_file)])
    assert res_export_file.exit_code == 0
    assert export_file.is_file()

    exported_paths = json.loads(export_file.read_text(encoding="utf-8"))
    assert str(root) in exported_paths

    pid = registry.project_id(root)
    registry.remove(pid)
    assert not any(p == root for p in registry.paths())

    res_import = runner.invoke(cli.app, ["import", str(export_file)])
    assert res_import.exit_code == 0
    assert any(p == root for p in registry.paths())


def test_open_sin_nada_arriba_no_abre_nada(tmp_path, monkeypatch, free_ports, abierto):
    (mudo,) = free_ports(1)
    (tmp_path / "stack.yaml").write_text(
        f"services:\n  web:\n    command: echo web\n    port: {mudo}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    resultado = runner.invoke(cli.app, ["open"])
    assert resultado.exit_code == 1
    assert abierto == []


def test_free_all_libera_el_puerto(tmp_path, free_ports):
    from stackhelx import ports

    (port,) = free_ports(1)
    root = tmp_path / "cli_free_all"
    root.mkdir()
    (root / "stack.yaml").write_text(
        f"services:\n  web:\n    command: python web\n    port: {port}\n", encoding="utf-8"
    )
    registry.add(root)

    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import socket, time; s = socket.socket(); s.bind(('127.0.0.1', {port})); "
            "s.listen(); time.sleep(30)",
        ]
    )
    try:
        deadline = time.time() + 10
        while time.time() < deadline and ports.is_free(port):
            time.sleep(0.1)
        assert not ports.is_free(port), "el intruso nunca tomo el puerto"

        res = runner.invoke(cli.app, ["free", "--all", "--yes"])
        assert res.exit_code == 0, res.output
        # Lo que importa no es el mensaje: es que el puerto haya quedado libre.
        assert ports.is_free(port), res.output
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_free_all_saltea_el_puerto_que_cambio_de_dueno(free_ports):
    """Entre que se imprime la lista y el usuario confirma pasa tiempo indefinido.

    Si en ese rato el puerto cambio de proceso, cerrar al que este ahora seria
    matar a alguien que el usuario nunca vio en pantalla. `_release` compara
    contra la identidad capturada y se saltea el puerto en vez de liberarlo.
    """
    from stackhelx import ports

    (port,) = free_ports(1)
    sock = socket.socket()
    sock.bind(("127.0.0.1", port))
    sock.listen()
    try:
        dueno = ports.scan(port)
        assert dueno.pid is not None

        # El PID que el usuario vio ya no es el que tiene el puerto.
        assert cli._release(port, yes=True, force=False, expected_pid=dueno.pid + 100000) is False
        assert not ports.is_free(port), "cerro un proceso que no era el que se mostro"
    finally:
        sock.close()


def test_free_all_sin_nada_ocupado_no_falla(tmp_path, free_ports):
    (port,) = free_ports(1)
    root = tmp_path / "cli_free_all_limpio"
    root.mkdir()
    (root / "stack.yaml").write_text(
        f"services:\n  web:\n    command: python web\n    port: {port}\n", encoding="utf-8"
    )
    registry.add(root)

    res = runner.invoke(cli.app, ["free", "--all"])
    assert res.exit_code == 0
    assert "Ningun puerto" in _sin_saltos(res.output)


def test_version_por_flag_y_por_subcomando():
    """El flag es lo que prueba cualquiera recien instalado; el subcomando ya existia."""
    for args in (["--version"], ["version"]):
        res = runner.invoke(cli.app, args)
        assert res.exit_code == 0, args
        # Rich pinta los numeros: sin sacar los codigos, el 1.0.0 llega partido.
        assert stackhelx.__version__ in re.sub(r"\x1b\[[0-9;]*m", "", res.output), args


def test_cli_run_listar_y_ejecutar(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    flag = tmp_path / "cli_flag.txt"
    body = f"""
    name: cli-run-test
    services:
      srv:
        command: echo srv
    scripts:
      touch_flag: {sys.executable} -c "import pathlib; pathlib.Path(r'{flag}').write_text('flag_ok')"
      test: pytest tests/
    """
    (tmp_path / "stack.yaml").write_text(textwrap.dedent(body), encoding="utf-8")

    # Sin argumentos: lista los scripts
    res_list = runner.invoke(cli.app, ["run"])
    assert res_list.exit_code == 0
    assert "touch_flag" in res_list.output
    assert "test" in res_list.output

    # Ejecutar script existente
    res_run = runner.invoke(cli.app, ["run", "touch_flag"])
    assert res_run.exit_code == 0
    assert flag.exists()
    assert flag.read_text() == "flag_ok"


def test_cli_share_sin_proveedores(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    body = """
    name: share-test
    services:
      web:
        command: echo web
        port: 3000
    """
    (tmp_path / "stack.yaml").write_text(textwrap.dedent(body), encoding="utf-8")
    monkeypatch.setattr(cli.tunnel.shutil, "which", lambda x: None)

    res = runner.invoke(cli.app, ["share", "web"])
    assert res.exit_code == 1
    assert "no se encontro ningun cliente de tuneles" in res.output


def test_cli_clean_docker(monkeypatch):
    monkeypatch.setattr(cli.docker, "prune", lambda targets: (True, "Total reclaimed space: 0B"))
    # Con --yes: lo que este test afirma es que el resultado del prune llega a
    # la salida, y desde que `clean` pregunta, ese camino es el del flag.
    res = runner.invoke(cli.app, ["clean", "--yes"])
    assert res.exit_code == 0
    assert "Total reclaimed space: 0B" in res.output





def _prune_espia(monkeypatch):
    """Registra si `clean` llego a ejecutar el prune, sin correr docker."""
    llamadas = []
    monkeypatch.setattr(
        cli.docker, "prune", lambda targets: llamadas.append(list(targets)) or (True, "ok")
    )
    monkeypatch.setattr(cli.docker, "usage", lambda: None)
    return llamadas


def test_clean_no_borra_nada_si_decis_que_no(monkeypatch):
    """Es el unico comando que borra datos en vez de cerrar procesos, y era el
    unico que no preguntaba. El boton de la interfaz ya pedia dos clicks."""
    llamadas = _prune_espia(monkeypatch)
    res = runner.invoke(cli.app, ["clean"], input="n\n")
    assert res.exit_code == 0
    assert "Cancelado" in res.output
    assert llamadas == [], "borro con la respuesta en no"


def test_clean_pregunta_con_no_por_defecto(monkeypatch):
    """Enter pelado no puede borrar 17 GB."""
    llamadas = _prune_espia(monkeypatch)
    runner.invoke(cli.app, ["clean"], input="\n")
    assert llamadas == [], "el default de la confirmacion no es 'no'"


def test_clean_avisa_de_los_volumenes_aparte(monkeypatch):
    """Los volumenes tienen datos adentro: no es lo mismo que un cache."""
    _prune_espia(monkeypatch)
    res = runner.invoke(cli.app, ["clean", "--volumes"], input="n\n")
    assert "volumenes anonimos huerfanos" in _sin_saltos(res.output)


def test_clean_con_yes_no_pregunta(monkeypatch):
    """Para scripts, igual que `free --yes`."""
    llamadas = _prune_espia(monkeypatch)
    res = runner.invoke(cli.app, ["clean", "--yes"])
    assert res.exit_code == 0
    assert llamadas == [list(cli.docker.DEFAULT_TARGETS)], "sin --solo van las cuatro"


def test_docker_running_sin_motor_no_revienta(monkeypatch):
    """`docker ps` con el daemon caido sale con codigo != 0. Lista vacia, no error:
    quien lo llama es una confirmacion que tiene que seguir funcionando igual."""
    guion = "import sys; print('cannot connect to the docker daemon', file=sys.stderr); sys.exit(1)"
    original = cli.docker.subprocess.run

    def falso(cmd, **kwargs):
        return original([sys.executable, "-c", guion], **kwargs)

    monkeypatch.setattr(cli.docker.subprocess, "run", falso)
    assert cli.docker.running() == []


def test_open_usa_la_url_declarada_en_vez_de_la_raiz_del_puerto(
    tmp_path, monkeypatch, servidor_http, abierto
):
    """El caso de ORQUESTER: la raiz del puerto carga una cascara que despues
    falla en cada llamada, y el `?token=` es lo que hace que sirva.
    """
    (tmp_path / "stack.yaml").write_text(
        textwrap.dedent(f"""
        services:
          web:
            command: echo web
            port: {servidor_http}
            url: http://127.0.0.1:{servidor_http}/?token=${{TOK}}
            env:
              TOK: abc123
        """),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    resultado = runner.invoke(cli.app, ["open"])
    assert resultado.exit_code == 0
    assert abierto == [f"http://127.0.0.1:{servidor_http}/?token=abc123"]


def test_open_con_url_y_sin_puerto_abre_sin_sondear(tmp_path, monkeypatch, abierto):
    """Sin `port:` no hay que sondear: no hay puerto que preguntar. Una pestaña
    muerta es lo mismo que pasa hoy escribiendo la URL a mano, y es preferible a
    no poder abrirla nunca.
    """
    (tmp_path / "stack.yaml").write_text(
        textwrap.dedent("""
        services:
          studio:
            command: echo studio
            url: http://127.0.0.1:8765/estudio
        """),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    resultado = runner.invoke(cli.app, ["open"])
    assert resultado.exit_code == 0
    assert abierto == ["http://127.0.0.1:8765/estudio"]


def test_open_ignora_una_url_con_una_variable_sin_valor(
    tmp_path, monkeypatch, servidor_http, abierto
):
    """Cae al default del puerto. Abrir la URL con el `${TOK}` literal adentro
    seria peor: la pagina carga y falla por dentro.
    """
    (tmp_path / "stack.yaml").write_text(
        textwrap.dedent(f"""
        services:
          web:
            command: echo web
            port: {servidor_http}
            url: http://127.0.0.1:{servidor_http}/?token=${{NO_EXISTE_EN_NINGUN_LADO}}
        """),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    resultado = runner.invoke(cli.app, ["open"])
    assert resultado.exit_code == 0
    assert abierto == [f"http://localhost:{servidor_http}"]


def test_open_sin_puerto_y_con_una_variable_sin_valor_no_revienta(
    tmp_path, monkeypatch, abierto
):
    """Sin `port:` no hay default al que caer, y `_abrir(None)` reventaba.

    `os.startfile(None)` levanta TypeError, y `webbrowser.open` solo atrapa
    OSError: el traceback salia crudo a la terminal despues de imprimir
    "Abriendo None". Es la forma exacta del caso que motivo el campo `url:`, un
    Studio con `ready: listen` y el token en una variable, el dia que falta el
    .env.
    """
    (tmp_path / "stack.yaml").write_text(
        textwrap.dedent("""
        services:
          studio:
            command: echo studio
            url: http://127.0.0.1:8765/?token=${NO_EXISTE_EN_NINGUN_LADO}
        """),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    resultado = runner.invoke(cli.app, ["open"])
    assert resultado.exit_code == 1
    assert abierto == []
    assert "Ningun puerto del stack contesta HTTP" in resultado.output


def test_open_sin_puerto_no_tapa_al_servicio_que_si_contesta(
    tmp_path, monkeypatch, servidor_http, abierto
):
    """El candidato sin URL resoluble se saltea, no corta el recorrido.

    Antes cualquier servicio sin `port:` cortocircuitaba con `return` y los que
    venian despues no se miraban nunca.
    """
    (tmp_path / "stack.yaml").write_text(
        textwrap.dedent(f"""
        services:
          api:
            command: echo api
            port: {servidor_http}
          studio:
            command: echo studio
            needs: [api]
            url: http://127.0.0.1:8765/?token=${{NO_EXISTE_EN_NINGUN_LADO}}
        """),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    resultado = runner.invoke(cli.app, ["open"])
    assert resultado.exit_code == 0
    assert abierto == [f"http://localhost:{servidor_http}"]


def test_share_rechaza_un_puerto_fuera_de_rango(tmp_path, monkeypatch):
    """`target` es texto porque tambien acepta el nombre de un servicio, asi que
    se pierde el min/max que traen los demas comandos. Sin la validacion a mano,
    `portmaster share 0` levantaba el cliente de tuneles contra 127.0.0.1:0.
    """
    monkeypatch.chdir(tmp_path)
    for target in ("0", "70000", "99999"):
        resultado = runner.invoke(cli.app, ["share", target])
        assert resultado.exit_code == 1, target
        assert "rango" in resultado.output


def test_test_stack_valido(tmp_path, monkeypatch):
    (tmp_path / "stack.yaml").write_text(
        "services:\n  db:\n    command: echo db\n    port: 5432\n  api:\n    command: echo api\n    port: 8000\n    needs: [db]\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    resultado = runner.invoke(cli.app, ["test-stack"])
    assert resultado.exit_code == 0
    assert "Stack validado con éxito" in resultado.output
    assert "db" in resultado.output
    assert "api" in resultado.output


def test_test_stack_invalido(tmp_path, monkeypatch):
    (tmp_path / "stack.yaml").write_text(
        "services:\n  a:\n    command: echo a\n    needs: [b]\n  b:\n    command: echo b\n    needs: [a]\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    resultado = runner.invoke(cli.app, ["test-stack"])
    assert resultado.exit_code == 1
    assert "circular" in resultado.output


def test_cli_history_vacio(tmp_path, monkeypatch):
    (tmp_path / "stack.yaml").write_text("services:\n  web:\n    command: echo web\n", encoding="utf-8")
    monkeypatch.setattr(cli.registry, "HOME", tmp_path)
    monkeypatch.chdir(tmp_path)
    resultado = runner.invoke(cli.app, ["history"])
    assert resultado.exit_code == 0
    assert "No hay historial" in resultado.output


def test_cli_history_con_entradas(tmp_path, monkeypatch):
    (tmp_path / "stack.yaml").write_text("services:\n  web:\n    command: echo web\n", encoding="utf-8")
    monkeypatch.setattr(cli.registry, "HOME", tmp_path)
    monkeypatch.chdir(tmp_path)
    pid = cli.registry.project_id(tmp_path)
    cli.history.append(pid, {"duration_s": 3.4, "result": "running", "profile": "dev"})
    resultado = runner.invoke(cli.app, ["history"])
    assert resultado.exit_code == 0
    assert "Historial de arranques" in resultado.output
    assert "3.4s" in resultado.output


def test_cli_logs_sin_servidor(tmp_path, monkeypatch):
    (tmp_path / "stack.yaml").write_text("services:\n  web:\n    command: echo web\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    resultado = runner.invoke(cli.app, ["logs", "--port", "59999"])
    assert resultado.exit_code == 1
    assert "No se pudo conectar" in resultado.output


def test_cli_stats_sin_servidor(tmp_path, monkeypatch):
    (tmp_path / "stack.yaml").write_text("services:\n  web:\n    command: echo web\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    resultado = runner.invoke(cli.app, ["stats", "--port", "59999"])
    assert resultado.exit_code == 1
    assert "No se pudo conectar" in resultado.output


def test_up_env_file_unwraps_quotes(tmp_path, monkeypatch):
    (tmp_path / "stack.yaml").write_text("services:\n  web:\n    command: echo $APP_PORT\n", encoding="utf-8")
    (tmp_path / ".env.custom").write_text('APP_PORT="8080"\nSECRET=\'my-secret\'\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    llamado = {}

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            self.procs = []
            llamado["extra_env"] = kwargs.get("extra_env", {})
        def up(self, services):
            return self
        def follow(self):
            pass
        def down(self):
            pass

    monkeypatch.setattr(cli.runner, "Runner", FakeRunner)
    resultado = runner.invoke(cli.app, ["up", "--env-file", ".env.custom"])
    assert resultado.exit_code == 0
    assert llamado.get("extra_env", {}).get("APP_PORT") == "8080"
    assert llamado.get("extra_env", {}).get("SECRET") == "my-secret"


def test_clean_solo_rejects_volumes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    resultado = runner.invoke(cli.app, ["clean", "--solo", "volumes"])
    assert resultado.exit_code == 1
    assert "Categoria desconocida" in resultado.output


def test_test_stack_cycle_dependency_error(tmp_path, monkeypatch):
    (tmp_path / "stack.yaml").write_text(
        "services:\n"
        "  s1:\n    command: echo 1\n    needs: [s2]\n"
        "  s2:\n    command: echo 2\n    needs: [s1]\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    resultado = runner.invoke(cli.app, ["test-stack"])
    assert resultado.exit_code == 1
    assert "Configuración inválida" in resultado.output






def test_test_stack_no_revienta_en_una_consola_cp1252(tmp_path, monkeypatch):
    """El comando terminaba en traceback despues de validar bien.

    Imprimia un `✓`, que no existe en cp1252, o sea la pagina de codigos
    con la que sale la consola de Windows. `CliRunner` captura a un buffer
    UTF-8 y no lo hubiera visto nunca: hace falta un proceso de verdad con la
    codificacion de salida forzada, que es donde el usuario lo encontro.
    """
    (tmp_path / "stack.yaml").write_text(
        "services:\n  api:\n    command: echo hola\n    ready: none\n", encoding="utf-8"
    )
    entorno = dict(os.environ, PYTHONIOENCODING="cp1252")
    res = subprocess.run(
        [sys.executable, "-m", "stackhelx", "test-stack"],
        cwd=tmp_path,
        env=entorno,
        capture_output=True,
        text=True,
        encoding="cp1252",
        errors="replace",
        timeout=60,
    )
    assert "UnicodeEncodeError" not in (res.stdout + res.stderr), res.stdout + res.stderr
    assert res.returncode == 0, res.stdout + res.stderr


def test_serve_port_occupied_suggests_alternative(free_ports):
    """Si el puerto de serve está ocupado, sugiere el comando free y el puerto alternativo."""
    (port,) = free_ports(1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", port))
    sock.listen()
    try:
        resultado = runner.invoke(cli.app, ["serve", "--port", str(port), "--no-open"])
        salida = sin_color(resultado.output)
        assert resultado.exit_code == 1
        assert f"El puerto {port} ya esta ocupado" in salida
        assert f"stackhelx free {port}" in salida
        assert "arranca con: --port" in salida
    finally:
        sock.close()


def test_mcp_status_cli():
    """El comando mcp-status muestra estadísticas de llamadas MCP sin reventar."""
    from stackhelx import mcp

    mcp.clear_telemetry()
    mcp.record_tool_call("stackhelx_status", 15.0, "ok")
    resultado = runner.invoke(cli.app, ["mcp-status"])
    assert resultado.exit_code == 0
    assert "Servidor MCP StackHelx" in resultado.output
    assert "stackhelx_status" in resultado.output


