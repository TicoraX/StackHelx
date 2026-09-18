import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from stackhelx import config, detect, docker, ports, registry, server

TOKEN = "token-de-prueba-suficientemente-largo"
SERVER = (
    "import socket, time; "
    "s = socket.socket(); s.bind(('127.0.0.1', {port})); s.listen(); "
    "print('arriba', flush=True); time.sleep(60)"
)


@pytest.fixture(autouse=True)
def aislado(tmp_path, monkeypatch):
    """Registro propio y limiter limpio en cada test."""
    monkeypatch.setattr(registry, "HOME", tmp_path / "home")
    monkeypatch.setattr(registry, "PROJECTS", tmp_path / "home" / "projects.json")
    server.limiter._hits.clear()
    # El cache de la vista no puede cruzar tests: un proyecto detectado en uno
    # llegaria ya resuelto al siguiente, y el sintoma es el que `CLAUDE.md`
    # marca como estado compartido, un test que pasa solo y falla acompanado.
    with server._stack_lock:
        server._stack_seen.clear()
    with server.sessions_lock:
        server.sessions.clear()
        server.selected_profiles.clear()
    yield
    for session in list(server.sessions.values()):
        session.stop()
    server.sessions.clear()
    server.selected_profiles.clear()


@pytest.fixture
def client():
    app = server.create_app(TOKEN)
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        test_client.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield test_client


@pytest.fixture
def proyecto(tmp_path, free_ports):
    (port,) = free_ports(1)
    root = tmp_path / "proyecto"
    root.mkdir()
    (root / "stack.yaml").write_text(
        textwrap.dedent(f"""
        name: prueba
        services:
          srv:
            command: {sys.executable} -c "{SERVER.format(port=port)}"
            port: {port}
        profiles:
          solo: [srv]
        """),
        encoding="utf-8",
    )
    return registry.add(root), port


# seguridad ----------------------------------------------------------------


def test_sin_token_rechaza(client):
    respuesta = client.get("/api/state", headers={"Authorization": ""})
    assert respuesta.status_code == 401


def test_token_incorrecto_rechaza(client):
    respuesta = client.get("/api/state", headers={"Authorization": "Bearer otro"})
    assert respuesta.status_code == 401


def test_host_ajeno_rechaza(client):
    """Rebinding de DNS: un dominio cualquiera resolviendo a 127.0.0.1."""
    respuesta = client.get("/api/state", headers={"Host": "malicioso.example"})
    assert respuesta.status_code == 400


def test_headers_de_seguridad(client):
    headers = client.get("/api/state").headers
    assert "default-src 'self'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"


def test_los_estaticos_no_se_cachean(client):
    """El HTML pide app.js y app.css sin `?v=`, apoyado en este header. Si el
    mount de StaticFiles dejara de pasar por el middleware, el navegador se
    quedaria con la version vieja hasta un hard refresh y nadie se enteraria."""
    for asset in ("/static/app.js", "/static/app.css"):
        respuesta = client.get(asset)
        assert respuesta.status_code == 200
        assert respuesta.headers["cache-control"] == "no-store"


def test_version_sella_todos_los_estaticos(tmp_path, monkeypatch):
    """Cada archivo que sirve `web/` tiene que mover el sello del pie de pagina.

    Ese sello existe para contestar "estoy viendo la pagina nueva o una vieja de
    la cache". Con la lista de archivos escrita a mano se olvidaba tokens.css,
    que es donde viven los colores y los espaciados: un cambio de solo estilos
    no movia la fecha y el sello afirmaba que la pagina era mas vieja de lo que
    era, justo en el caso para el que se puso.

    Recorre el arbol en vez de nombrar los archivos, asi el dia que aparezca un
    quinto estatico el test lo cubre sin que nadie se acuerde.
    """
    web = tmp_path / "web"
    shutil.copytree(server.WEB, web)
    # `web/` es plano hoy, y el mount sirve el arbol entero: un estatico anidado
    # se descarga igual y con `iterdir` no contaba para el sello. El archivo lo
    # pone el test y no el paquete, para que la trampa quede cubierta antes de
    # que alguien cree el primer `web/img/`.
    anidado = web / "anidado" / "extra.css"
    anidado.parent.mkdir()
    anidado.write_text("/* un estatico en un subdirectorio */", encoding="utf-8")
    monkeypatch.setattr(server, "WEB", web)

    estaticos = sorted(f for f in web.rglob("*") if f.is_file())
    assert anidado in estaticos
    assert len(estaticos) >= 5, "index.html, app.js, app.css, tokens.css y el anidado"

    viejo = time.time() - 86400
    for archivo in estaticos:
        os.utime(archivo, (viejo, viejo))

    app = server.create_app(TOKEN)
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        test_client.headers.update({"Authorization": f"Bearer {TOKEN}"})
        # La premisa del sello: si el mount no sirviera esto, mirarlo no haria falta.
        assert test_client.get("/static/anidado/extra.css").status_code == 200
        for archivo in estaticos:
            nuevo = time.time()
            os.utime(archivo, (nuevo, nuevo))
            sello = test_client.get("/api/version").json()["assets"]
            esperado = time.strftime("%Y-%m-%d %H:%M", time.localtime(nuevo))
            assert sello == esperado, f"{archivo.name} no mueve el sello"
            os.utime(archivo, (viejo, viejo))


def test_rate_limit_en_kill(client):
    ruta = "/api/ports/1/kill"
    for _ in range(server.QUOTA_KILL):
        client.post(ruta)
    assert client.post(ruta).status_code == 429


def test_la_pagina_no_pide_token(client):
    """El HTML es publico; los datos no. El token viaja en la URL de arranque."""
    respuesta = client.get("/", headers={"Authorization": ""})
    assert respuesta.status_code == 200
    assert "StackHelx" in respuesta.text


# proyectos ----------------------------------------------------------------


def test_alta_exige_stack_yaml(client, tmp_path):
    vacio = tmp_path / "vacio"
    vacio.mkdir()
    respuesta = client.post("/api/projects", json={"path": str(vacio)})
    assert respuesta.status_code == 400
    assert "stack.yaml" in respuesta.json()["detail"]


def test_alta_y_listado(client, tmp_path, free_ports):
    (port,) = free_ports(1)
    root = tmp_path / "app"
    root.mkdir()
    (root / "stack.yaml").write_text(
        f"services:\n  web:\n    command: echo hola\n    port: {port}\n",
        encoding="utf-8",
    )

    assert client.post("/api/projects", json={"path": str(root)}).status_code == 200

    proyectos = client.get("/api/state").json()["projects"]
    assert len(proyectos) == 1
    assert proyectos[0]["state"] == "stopped"
    assert proyectos[0]["services"][0]["name"] == "web"
    assert proyectos[0]["services"][0]["port"] == port


def test_config_rota_no_tumba_el_listado(client, tmp_path):
    root = tmp_path / "roto"
    root.mkdir()
    (root / "stack.yaml").write_text("services:\n  a:\n    port: 1\n", encoding="utf-8")
    registry.add(root)

    proyecto = client.get("/api/state").json()["projects"][0]
    assert proyecto["state"] == "invalid"
    assert "command" in proyecto["error"]


def test_baja(client, proyecto):
    path, _ = proyecto
    pid = registry.project_id(path)
    assert client.delete(f"/api/projects/{pid}").status_code == 200
    assert client.get("/api/state").json()["projects"] == []


def test_proyecto_desconocido(client):
    assert client.post("/api/projects/nohay/up", json={}).status_code == 404


def test_perfil_invalido(client, proyecto):
    path, _ = proyecto
    pid = registry.project_id(path)
    respuesta = client.post(f"/api/projects/{pid}/up", json={"profile": "fantasma"})
    assert respuesta.status_code == 400
    assert "perfil desconocido" in respuesta.json()["detail"]


# busqueda y paginado ------------------------------------------------------


def registrar(tmp_path, *nombres):
    for nombre in nombres:
        root = tmp_path / nombre
        root.mkdir(parents=True)
        (root / "stack.yaml").write_text("services:\n  a:\n    command: echo a\n", encoding="utf-8")
        registry.add(root)


@pytest.fixture
def proxy_de_docker(free_ports):
    """Un proceso con el nombre del proxy de Docker, escuchando un puerto.

    Copiado al directorio del interprete porque un python suelto en otra
    carpeta no encuentra sus DLLs. Del interprete base y no del venv: en
    Windows el python.exe del venv relanza al real como hijo, y quien termina
    escuchando el puerto es ese hijo, con su nombre y no con el del impostor.
    """
    (port,) = free_ports(1)
    nombre = "wslrelay.exe" if os.name == "nt" else "docker-proxy"
    base = getattr(sys, "_base_executable", None) or sys.executable
    impostor = Path(base).with_name(nombre)
    try:
        shutil.copy2(base, impostor)
    except OSError:
        pytest.skip("directorio del interprete no escribible")

    proc = subprocess.Popen([str(impostor), "-c", SERVER.format(port=port)])
    try:
        deadline = time.time() + 10
        while time.time() < deadline and ports.is_free(port):
            time.sleep(0.1)
        # En macOS el interprete vive dentro de un bundle y el proceso se sigue
        # llamando "Python" por mas que el binario se copie con otro nombre, asi
        # que ahi no hay forma de hacerse pasar por el proxy.
        if (ports.scan(port).name or "").lower() != nombre:
            pytest.skip("el proceso no toma el nombre del ejecutable en esta plataforma")
        yield port
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        impostor.unlink(missing_ok=True)


def test_el_estado_delata_el_puerto_compartido(client, tmp_path, free_ports):
    """Dos proyectos que declaran el mismo puerto, sin que ninguno corra.

    Las carpetas se llaman distinto que los stacks a proposito. El aviso nombraba
    la carpeta y la ficha el `name:`: con `Fitness/` declarando `name: fittrack`,
    el mensaje mandaba a buscar un proyecto que en la interfaz no existe. Si
    carpeta y stack se llaman igual, revertir ese arreglo no se nota.
    """
    (port,) = free_ports(1)
    for carpeta, nombre in (("Blog", "blog"), ("Fitness", "fitness")):
        root = tmp_path / carpeta
        root.mkdir()
        (root / "stack.yaml").write_text(
            f"name: {nombre}\nservices:\n  web:\n    command: python web\n    port: {port}\n",
            encoding="utf-8",
        )
        registry.add(root)

    proyectos = client.get("/api/state").json()["projects"]
    compartidos = {p["name"]: p["services"][0]["shared_with"] for p in proyectos}
    assert compartidos == {"blog": ["fitness"], "fitness": ["blog"]}


def test_un_puerto_propio_no_figura_compartido(client, tmp_path, free_ports):
    uno, otro = free_ports(2)
    for nombre, port in (("blog", uno), ("fitness", otro)):
        root = tmp_path / nombre
        root.mkdir()
        (root / "stack.yaml").write_text(
            f"services:\n  web:\n    command: python web\n    port: {port}\n", encoding="utf-8"
        )
        registry.add(root)

    proyectos = client.get("/api/state").json()["projects"]
    assert all(p["services"][0]["shared_with"] == [] for p in proyectos)


def _proyecto_compose(tmp_path, port):
    root = tmp_path / "condocker"
    root.mkdir()
    (root / "stack.yaml").write_text(
        textwrap.dedent(f"""
        services:
          db:
            command: docker compose up -d db
            detached: true
            port: {port}
        """),
        encoding="utf-8",
    )
    registry.add(root)


def test_un_contenedor_arriba_no_es_un_intruso(client, tmp_path, proxy_de_docker):
    """Lo que publica el proxy de Docker es el propio stack, no un okupa."""
    _proyecto_compose(tmp_path, proxy_de_docker)

    intrusos = client.get("/api/ports/orphans").json()["orphans"]
    assert [o for o in intrusos if o["port"] == proxy_de_docker] == []


def test_un_contenedor_arriba_figura_corriendo(client, tmp_path, proxy_de_docker):
    """Un stack levantado desde la terminal no se ve detenido y en rojo."""
    _proyecto_compose(tmp_path, proxy_de_docker)

    proyecto = client.get("/api/state").json()["projects"][0]
    (db,) = proyecto["services"]
    assert db["state"] == "ready"
    assert db["occupant"] is None


def test_paginado(client, tmp_path):
    registrar(tmp_path, *(f"app{i:02d}" for i in range(11)))

    primera = client.get("/api/state").json()
    assert primera["total"] == 11
    esperadas = -(-11 // server.PAGE_SIZE)
    assert primera["pages"] == esperadas
    assert len(primera["projects"]) == server.PAGE_SIZE

    segunda = client.get("/api/state?page=2").json()
    assert len(segunda["projects"]) == server.PAGE_SIZE
    ids = {p["id"] for p in primera["projects"]} | {p["id"] for p in segunda["projects"]}
    assert len(ids) == server.PAGE_SIZE * 2, "ningun proyecto se repite ni se pierde entre paginas"


def test_una_pagina_de_mas_cae_en_la_ultima(client, tmp_path):
    registrar(tmp_path, "sola")
    datos = client.get("/api/state?page=99").json()
    assert datos["page"] == 1
    assert len(datos["projects"]) == 1


def test_busqueda_por_nombre_y_por_ruta(client, tmp_path):
    registrar(tmp_path, "tienda", "blog", "tienda-admin")

    por_nombre = client.get("/api/state?q=tienda").json()
    assert por_nombre["total"] == 2
    assert {p["path"].split("\\")[-1].split("/")[-1] for p in por_nombre["projects"]} == {
        "tienda",
        "tienda-admin",
    }

    assert por_nombre["registered"] == 3, "el total registrado no lo mueve el filtro"
    assert client.get("/api/state?q=TIENDA").json()["total"] == 2, "sin distinguir mayusculas"
    assert client.get("/api/state?q=nada-de-esto").json()["total"] == 0
    assert client.get(f"/api/state?q={tmp_path.name}").json()["total"] == 3, "tambien por ruta"


def test_la_busqueda_no_acepta_cualquier_cosa(client):
    assert client.get("/api/state?q=" + "x" * 500).status_code == 422
    assert client.get("/api/state?page=0").status_code == 422
    assert client.get("/api/state?size=999").status_code == 422


# ciclo de vida ------------------------------------------------------------


def esperar(condicion, segundos=20):
    limite = time.time() + segundos
    while time.time() < limite:
        if condicion():
            return True
        time.sleep(0.2)
    return False


def esperar_listo(client):
    """Espera a que el primer servicio quede listo, y si no llega dice por que.

    El motivo va en el mensaje porque este fallo aparecio dos veces en CI
    (ubuntu 3.10) como un "nunca quedo listo" pelado, que no distingue un
    arranque que reviento de uno que solo tardo mas que la ventana.
    """

    def listo():
        estado = client.get("/api/state").json()["projects"][0]
        return estado["services"][0]["state"] == "ready"

    if esperar(listo):
        return
    final = client.get("/api/state").json()["projects"][0]
    raise AssertionError(
        f"el servicio nunca quedo listo: proyecto {final['state']},"
        f" error {final.get('error')!r},"
        f" servicios {[(s['name'], s['state']) for s in final['services']]}"
    )


def test_arranque_y_apagado(client, proyecto):
    path, port = proyecto
    pid = registry.project_id(path)

    assert client.post(f"/api/projects/{pid}/up", json={}).status_code == 200
    esperar_listo(client)

    logs = client.get(f"/api/projects/{pid}/logs").json()
    assert any("arriba" in linea["text"] for linea in logs["lines"])

    assert client.post(f"/api/projects/{pid}/down").status_code == 200
    from stackhelx import ports

    assert esperar(lambda: ports.is_free(port)), "el puerto quedo tomado"


def test_apagar_mientras_arranca_no_deja_huerfanos(client, proyecto):
    """El apagado llega con el proceso arriba pero todavia no listo. Sin cortar
    el arranque, el hilo sigue detras del apagado y el servicio queda vivo."""
    from stackhelx import ports

    path, port = proyecto
    pid = registry.project_id(path)
    assert client.post(f"/api/projects/{pid}/up", json={}).status_code == 200

    sesion = server.sessions[pid]
    assert esperar(lambda: sesion.engine is not None and sesion.engine.procs)

    assert client.post(f"/api/projects/{pid}/down").status_code == 200
    assert esperar(lambda: ports.is_free(port)), "el arranque siguio detras del apagado"


def test_arranque_doble_choca(client, proyecto):
    path, _ = proyecto
    pid = registry.project_id(path)
    assert client.post(f"/api/projects/{pid}/up", json={}).status_code == 200
    assert client.post(f"/api/projects/{pid}/up", json={}).status_code == 409


def test_la_tarjeta_marca_el_puerto_que_ya_estaba_ocupado(client, tmp_path, free_ports):
    """El arranque por la web no libera puertos, asi que este caso es alcanzable."""
    (port,) = free_ports(1)
    root = tmp_path / "conintruso"
    root.mkdir()
    (root / "stack.yaml").write_text(
        textwrap.dedent(f"""
        services:
          srv:
            command: {sys.executable} -c "import time; time.sleep(60)"
            port: {port}
        """),
        encoding="utf-8",
    )
    registry.add(root)
    pid = client.get("/api/state").json()["projects"][0]["id"]

    # `closing` y no un try/finally con el socket abierto afuera: el `finally`
    # tocaba `pid`, que se asigna adentro, y un fallo temprano lo tapaba con un
    # NameError. El apagado del stack lo hace la fixture `aislado`.
    with contextlib.closing(socket.socket()) as intruso:
        intruso.bind(("127.0.0.1", port))
        intruso.listen()

        assert client.post(f"/api/projects/{pid}/up", json={}).status_code == 200
        esperar_listo(client)
        servicio = client.get("/api/state").json()["projects"][0]["services"][0]
        assert servicio["port_taken"] is True
        client.post(f"/api/projects/{pid}/down")


def test_un_puerto_libre_no_marca_la_tarjeta(client, proyecto):
    path, _ = proyecto
    pid = registry.project_id(path)
    try:
        assert client.post(f"/api/projects/{pid}/up", json={}).status_code == 200
        esperar_listo(client)
        servicio = client.get("/api/state").json()["projects"][0]["services"][0]
        assert servicio["port_taken"] is False
    finally:
        client.post(f"/api/projects/{pid}/down")


def test_apagar_contesta_sin_esperar_al_apagado(client, tmp_path):
    """`docker compose stop` tarda decenas de segundos: el request no los espera."""
    root = tmp_path / "lento"
    root.mkdir()
    (root / "stack.yaml").write_text(
        textwrap.dedent(f"""
        services:
          srv:
            command: {sys.executable} -c "import time; time.sleep(60)"
            stop: {sys.executable} -c "import time; time.sleep(3)"
        """),
        encoding="utf-8",
    )
    registry.add(root)
    pid = client.get("/api/state").json()["projects"][0]["id"]

    assert client.post(f"/api/projects/{pid}/up", json={}).status_code == 200
    assert esperar(lambda: server.sessions[pid].state == "running")

    empezo = time.monotonic()
    assert client.post(f"/api/projects/{pid}/down").status_code == 200
    assert time.monotonic() - empezo < 2, "el request espero al comando de apagado"

    # Un segundo despues el comando de apagado sigue corriendo: el stack tiene
    # que seguir diciendo "apagando" y no "detenido".
    time.sleep(1)
    proyecto = client.get("/api/state").json()["projects"][0]
    assert proyecto["state"] == "stopping"
    assert esperar(lambda: server.sessions[pid].state == "stopped")


def test_reiniciar_un_servicio(client, proyecto):
    path, port = proyecto
    pid = registry.project_id(path)
    client.post(f"/api/projects/{pid}/up", json={})

    sesion = server.sessions[pid]
    assert esperar(lambda: sesion.state == "running")
    viejo = sesion.engine.procs[0].popen.pid

    assert client.post(f"/api/projects/{pid}/services/srv/restart").status_code == 200
    assert esperar(lambda: sesion.engine.procs[0].popen.pid != viejo)
    assert esperar(lambda: sesion.engine.procs[0].ready)
    assert sesion.state == "running", "reiniciar un servicio no baja el stack"


def test_reiniciar_un_servicio_que_no_existe(client, proyecto):
    path, _ = proyecto
    pid = registry.project_id(path)
    client.post(f"/api/projects/{pid}/up", json={})
    assert esperar(lambda: server.sessions[pid].state == "running")

    respuesta = client.post(f"/api/projects/{pid}/services/../../etc/restart")
    assert respuesta.status_code == 404


def test_reiniciar_con_el_stack_apagado(client, proyecto):
    path, _ = proyecto
    pid = registry.project_id(path)
    assert client.post(f"/api/projects/{pid}/services/srv/restart").status_code == 404


def test_una_sesion_sin_procesos_no_ofrece_reiniciar(client, proyecto):
    """La sesion que sobrevive a un reinicio del servidor deriva sus estados del
    puerto y no tiene procesos: `restart` contesta 409, asi que la tarjeta no
    tiene que dibujar el boton. Es la forma exacta que arma `_load_sessions_state`."""
    path, port = proyecto
    pid = registry.project_id(path)
    with socket.socket() as ocupado:
        ocupado.bind(("127.0.0.1", port))
        ocupado.listen()
        recuperada = server.Session(detect.stack_for(path), None)
        recuperada.state = "running"
        server.sessions[pid] = recuperada
        try:
            vista = client.get("/api/state").json()["projects"]
            servicios = next(p for p in vista if p["id"] == pid)["services"]
            assert [s["state"] for s in servicios] == ["ready"]
            assert [s["managed"] for s in servicios] == [False]
            assert client.post(f"/api/projects/{pid}/services/srv/restart").status_code == 409
        finally:
            server.sessions.pop(pid, None)


def test_apagar_lo_que_no_arrancamos(client, tmp_path):
    """Un contenedor levantado por afuera se ve listo en la tarjeta: Apagar
    tiene que poder bajarlo. Sin sesion no hay proceso del que colgarse, asi que
    lo unico que lo apaga es el `stop:` del servicio."""
    root = tmp_path / "ajeno"
    root.mkdir()
    testigo = root / "apagado.txt"
    (root / "stack.yaml").write_text(
        textwrap.dedent(f"""
        name: ajeno
        services:
          db:
            command: {sys.executable} -c "pass"
            detached: true
            stop: echo ok > "{testigo}"
        """),
        encoding="utf-8",
    )
    pid = registry.project_id(registry.add(root))

    assert client.post(f"/api/projects/{pid}/down").status_code == 200
    assert esperar(testigo.exists)


# puertos ------------------------------------------------------------------


def test_kill_de_puerto_libre(client, free_ports):
    (port,) = free_ports(1)
    respuesta = client.post(f"/api/ports/{port}/kill")
    assert respuesta.status_code == 200
    assert respuesta.json()["detail"] == "ya estaba libre"


def test_kill_de_puerto_invalido(client):
    assert client.post("/api/ports/70000/kill").status_code == 400


def test_stack_solo_detached_queda_corriendo(client, tmp_path):
    """Un compose deja contenedores vivos fuera de nuestro arbol: el comando
    termina, pero el stack no esta detenido."""
    root = tmp_path / "contenedores"
    root.mkdir()
    (root / "stack.yaml").write_text(
        textwrap.dedent(f"""
        services:
          setup:
            command: {sys.executable} -c "print('arriba')"
            detached: true
        """),
        encoding="utf-8",
    )
    registry.add(root)
    pid = client.get("/api/state").json()["projects"][0]["id"]

    assert client.post(f"/api/projects/{pid}/up", json={}).status_code == 200
    for _ in range(50):
        proyecto = client.get("/api/state").json()["projects"][0]
        if proyecto["state"] == "running":
            break
        time.sleep(0.1)

    assert proyecto["state"] == "running"
    assert proyecto["services"][0]["state"] == "ready"
    assert proyecto["services"][0]["kind"] == "container"


# explorador ---------------------------------------------------------------


def test_browse_exige_token(client):
    client.headers.pop("Authorization")
    assert client.get("/api/browse").status_code == 401


def test_browse_sin_ruta_da_las_raices(client):
    cuerpo = client.get("/api/browse").json()
    assert cuerpo["path"] == ""
    assert cuerpo["parent"] is None, "la vista de raices no tiene a donde subir"
    assert cuerpo["entries"], "al menos la home del usuario"


def test_browse_sin_ruta_con_oserror_en_particiones_no_falla(client, monkeypatch):
    import psutil

    def fake_disk_partitions(all=False):
        raise OSError("Dispositivo no listo")

    monkeypatch.setattr(psutil, "disk_partitions", fake_disk_partitions)
    cuerpo = client.get("/api/browse").json()
    assert cuerpo["path"] == ""
    assert len(cuerpo["entries"]) >= 1
    assert cuerpo["entries"][0]["name"] == "Inicio"


def test_browse_lista_solo_carpetas(client, tmp_path):
    raiz = tmp_path / "arbol"
    (raiz / "proyecto").mkdir(parents=True)
    (raiz / "node_modules").mkdir()
    (raiz / ".oculta").mkdir()
    (raiz / "secreto.txt").write_text("no me listes", encoding="utf-8")
    (raiz / "proyecto" / "package.json").write_text("{}", encoding="utf-8")

    cuerpo = client.get("/api/browse", params={"path": str(raiz)}).json()

    assert [e["name"] for e in cuerpo["entries"]] == ["proyecto"]
    assert cuerpo["entries"][0]["markers"] == ["package.json"]
    assert cuerpo["parent"] == str(raiz.parent)


def test_browse_marca_los_proyectos(client, tmp_path):
    raiz = tmp_path / "arbol"
    (raiz / "app").mkdir(parents=True)
    (raiz / "app" / "compose.yaml").write_text("services: {}", encoding="utf-8")
    (raiz / "app" / "manage.py").write_text("", encoding="utf-8")
    (raiz / "rust_app").mkdir(parents=True)
    (raiz / "rust_app" / "Cargo.toml").write_text("[package]\nname='test'\n", encoding="utf-8")
    (raiz / "vacia").mkdir()

    entries = {e["name"]: e["markers"] for e in
               client.get("/api/browse", params={"path": str(raiz)}).json()["entries"]}

    assert entries["app"] == ["compose.yaml", "manage.py"]
    assert entries["rust_app"] == ["Cargo.toml"]
    assert entries["vacia"] == []


@pytest.mark.parametrize("ruta", ["relativa/mala", "no-existe-en-ningun-lado-12345"])
def test_browse_rechaza_rutas_invalidas(client, ruta):
    assert client.get("/api/browse", params={"path": ruta}).status_code == 400


def test_browse_rechaza_un_archivo(client, tmp_path):
    archivo = tmp_path / "archivo.txt"
    archivo.write_text("hola", encoding="utf-8")
    assert client.get("/api/browse", params={"path": str(archivo)}).status_code == 400


def test_browse_normaliza_los_puntos(client, tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    cuerpo = client.get("/api/browse", params={"path": str(tmp_path / "a" / "b" / "..")}).json()
    assert cuerpo["path"] == str((tmp_path / "a").resolve())


def test_busqueda_por_estado(client, tmp_path):
    registrar(tmp_path, "detenido1", "detenido2")
    detenidos = client.get("/api/state?status=stopped").json()
    assert detenidos["total"] == 2

    corriendo = client.get("/api/state?status=running").json()
    assert corriendo["total"] == 0


def test_sink_flush_linea_parcial():
    sink = server._Sink()
    sink.write("linea completa\nsin salto final")
    assert len(sink.lines) == 1
    assert sink.lines[0][1] == "linea completa"
    sink.flush()
    assert len(sink.lines) == 2
    assert sink.lines[1][1] == "sin salto final"


def test_un_servidor_que_contesta_tarde_igual_consigue_el_boton_abrir(
    client, tmp_path, free_ports
):
    """El caso de Next: el puerto acepta enseguida y el HTTP recien aparece
    cuando termina de compilar. El sondeo unico del arranque da falso negativo."""
    (port,) = free_ports(1)
    # Acepta conexiones ya (para que `ready: port` de listo) y recien despues de
    # unos segundos empieza a contestar HTTP de verdad.
    tarde = (
        "import socket, threading, time; "
        "s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); "
        f"s.bind(('127.0.0.1', {port})); s.listen(); "
        "time.sleep(4); s.close(); "
        "from http.server import HTTPServer, BaseHTTPRequestHandler; "
        f"HTTPServer(('127.0.0.1', {port}), BaseHTTPRequestHandler).serve_forever()"
    )
    root = tmp_path / "tardio"
    root.mkdir()
    (root / "stack.yaml").write_text(
        f'services:\n  web:\n    command: {sys.executable} -c "{tarde}"\n    port: {port}\n',
        encoding="utf-8",
    )
    pid = registry.project_id(registry.add(root))
    assert client.post(f"/api/projects/{pid}/up", json={}).status_code == 200

    def abrible():
        proyecto = client.get("/api/state").json()["projects"][0]
        return proyecto["services"][0]["openable"]

    # HTTP_RETRIES suma 37s, y el primer intento no arranca hasta que el stack
    # esta listo. Con 40 el ultimo re-sondeo caia justo fuera de la ventana.
    total = sum(server.HTTP_RETRIES) + 20
    assert esperar(abrible, total), "el re-sondeo nunca le devolvio el boton"


def test_un_puerto_mudo_no_frena_la_vista_de_estado(client, proyecto):
    """El re-sondeo vive en su propio hilo. Si corriera en `_project_view`, un
    puerto que acepta y no contesta bloquearia el request el timeout entero, y
    con el al apagado, que comparte el threadpool."""
    path, port = proyecto
    pid = registry.project_id(path)
    client.post(f"/api/projects/{pid}/up", json={})
    esperar_listo(client)

    # El socket pelado del fixture nunca contesta HTTP: es el peor caso.
    inicio = time.time()
    for _ in range(5):
        assert client.get("/api/state").status_code == 200
    assert time.time() - inicio < 2, "la vista de estado se llevo el timeout del sondeo"

    inicio = time.time()
    assert client.post(f"/api/projects/{pid}/down").status_code == 200
    assert time.time() - inicio < 2, "apagar quedo detras del sondeo"


def _con_compose(tmp_path, nombre="congelable"):
    root = tmp_path / nombre
    root.mkdir()
    (root / "compose.yaml").write_text(
        'services:\n  db:\n    image: postgres\n    ports: ["5432:5432"]\n', encoding="utf-8"
    )
    return root, registry.project_id(registry.add(root))


def test_congelar_escribe_un_stack_yaml_que_carga(client, tmp_path):
    root, pid = _con_compose(tmp_path)
    cuerpo = client.post(f"/api/projects/{pid}/freeze").json()

    escrito = Path(cuerpo["path"])
    assert escrito == root / "stack.yaml"
    # Lo escrito tiene que ser cargable: congelar algo que despues no abre seria peor
    # que no ofrecerlo.
    assert [s.name for s in config.load(escrito).resolve()] == ["db"]
    # Y deja de ser un proyecto detectado.
    assert client.get("/api/state").json()["projects"][0]["detected"] is False


def test_congelar_dos_veces_no_pisa_el_archivo(client, tmp_path):
    root, pid = _con_compose(tmp_path)
    client.post(f"/api/projects/{pid}/freeze")
    original = (root / "stack.yaml").read_text(encoding="utf-8")

    (root / "stack.yaml").write_text(original + "\n# editado a mano\n", encoding="utf-8")
    respuesta = client.post(f"/api/projects/{pid}/freeze")

    assert respuesta.status_code == 409
    assert "editado a mano" in (root / "stack.yaml").read_text(encoding="utf-8")


def test_health_ve_todas_las_sesiones_y_no_solo_la_pagina(client, tmp_path, free_ports):
    """Alimentar el aviso de "algo se cayo" desde /api/state seria mentira apenas
    hay una segunda pagina."""
    puertos = free_ports(server.PAGE_SIZE + 1)
    ids = []
    for i, port in enumerate(puertos):
        root = tmp_path / f"app{i}"
        root.mkdir()
        (root / "stack.yaml").write_text(
            f'services:\n  srv:\n    command: {sys.executable} -c "{SERVER.format(port=port)}"\n'
            f"    port: {port}\n",
            encoding="utf-8",
        )
        ids.append(registry.project_id(registry.add(root)))

    for pid in ids:
        assert client.post(f"/api/projects/{pid}/up", json={}).status_code == 200
    assert esperar(lambda: client.get("/api/health").json()["running"] == len(ids), 40)

    # El ultimo cae fuera de la primera pagina.
    primera = {p["id"] for p in client.get("/api/state").json()["projects"]}
    assert ids[-1] not in primera

    salud = client.get("/api/health").json()
    assert salud["running"] == len(ids)
    assert salud["fallen"] == []


def test_health_delata_al_servicio_que_se_murio_solo(client, tmp_path, free_ports):
    (port,) = free_ports(1)
    root = tmp_path / "efimero"
    root.mkdir()
    corto = (
        "import socket, time; s = socket.socket(); "
        f"s.bind(('127.0.0.1', {port})); s.listen(); "
        "print('arriba', flush=True); time.sleep(2)"
    )
    (root / "stack.yaml").write_text(
        f'services:\n  srv:\n    command: {sys.executable} -c "{corto}"\n    port: {port}\n',
        encoding="utf-8",
    )
    pid = registry.project_id(registry.add(root))
    client.post(f"/api/projects/{pid}/up", json={})

    def caido():
        return len(client.get("/api/health").json()["fallen"]) == 1

    assert esperar(caido, 30), "el servicio se murio y health no lo dice"
    assert client.get("/api/health").json()["fallen"][0]["project"] == pid


def test_apagar_a_mano_no_es_una_caida(client, proyecto):
    """Apagar y morirse llegan al mismo estado por caminos distintos. Solo uno
    de los dos es noticia."""
    path, _ = proyecto
    pid = registry.project_id(path)
    client.post(f"/api/projects/{pid}/up", json={})
    assert esperar(lambda: client.get("/api/health").json()["running"] == 1)

    client.post(f"/api/projects/{pid}/down")
    assert esperar(lambda: client.get("/api/health").json()["running"] == 0)
    assert client.get("/api/health").json()["fallen"] == []


def test_reiniciar_no_cuenta_como_caida(client, proyecto):
    """Sin esto, el aviso se dispararia con cada Reiniciar: entre matar el
    proceso viejo y arrancar el nuevo no hay nadie vivo."""
    path, _ = proyecto
    pid = registry.project_id(path)
    client.post(f"/api/projects/{pid}/up", json={})
    assert esperar(lambda: client.get("/api/health").json()["running"] == 1)

    client.post(f"/api/projects/{pid}/services/srv/restart")
    for _ in range(30):
        assert client.get("/api/health").json()["fallen"] == [], "aviso falso al reiniciar"
        time.sleep(0.1)


def test_persistencia_de_sesiones_al_reiniciar_servidor(client, proyecto):
    path, port = proyecto
    pid = registry.project_id(path)
    client.post(f"/api/projects/{pid}/up", json={})
    assert esperar(lambda: client.get("/api/state").json()["projects"][0]["state"] == "running")

    # Simulamos reinicio del servidor limpiando la memoria de sessions
    server._save_sessions_state()
    assert server._sessions_file().is_file()

    server.sessions.clear()
    server._load_sessions_state()

    # La sesion debe haber sido restaurada en estado running
    assert pid in server.sessions
    assert server.sessions[pid].state == "running"


def test_browse_autocompletado_de_rutas(client, tmp_path):
    root = tmp_path / "proyectos"
    (root / "app_alpha").mkdir(parents=True)
    (root / "app_beta").mkdir(parents=True)

    res = client.get(f"/api/browse?path={root}")
    assert res.status_code == 200
    nombres = [e["name"] for e in res.json()["entries"]]
    assert "app_alpha" in nombres
    assert "app_beta" in nombres


def test_export_e_import_api(client, tmp_path):
    root = tmp_path / "app_api"
    root.mkdir()
    (root / "stack.yaml").write_text("services:\n  web:\n    command: python web\n", encoding="utf-8")
    registry.add(root)

    res_exp = client.get("/api/projects/export")
    assert res_exp.status_code == 200
    assert str(root) in res_exp.json()

    pid = registry.project_id(root)
    registry.remove(pid)

    res_imp = client.post("/api/projects/import", json=[str(root)])
    assert res_imp.status_code == 200
    assert res_imp.json()["count"] == 1


def test_la_pagina_no_regala_el_token(client):
    """GET / es publico, pero contestaba con el token de verdad en un Set-Cookie
    aunque no lo trajeras: cualquier proceso local conseguia la llave con un
    curl, y con ella corre comandos."""
    respuesta = client.get("/", headers={"Authorization": ""})
    assert respuesta.status_code == 200
    assert "stackhelx_token" not in respuesta.headers.get("set-cookie", "")
    assert TOKEN not in respuesta.headers.get("set-cookie", "")


def test_la_cookie_se_entrega_a_quien_ya_tiene_el_token(client):
    respuesta = client.get(f"/?token={TOKEN}", headers={"Authorization": ""})
    assert TOKEN in respuesta.headers.get("set-cookie", "")


def test_un_token_inventado_no_consigue_cookie(client):
    respuesta = client.get("/?token=no-es-el-token-pero-es-largo", headers={"Authorization": ""})
    assert "stackhelx_token" not in respuesta.headers.get("set-cookie", "")


def test_la_cookie_autentica_la_api(client):
    """El <a download> de exportar no manda el header Authorization: sin cookie
    ese boton no existiria."""
    client.get(f"/?token={TOKEN}", headers={"Authorization": ""})
    respuesta = client.get("/api/projects/export", headers={"Authorization": ""})
    assert respuesta.status_code == 200


def test_el_chequeo_de_docker_no_corre_en_cada_request(client, tmp_path, monkeypatch):
    """Corria una vez por proyecto y por request, con la interfaz sondeando cada
    2.5s. Tres proyectos con contenedores se llevaban un segundo de cada
    /api/state y saturaban el threadpool que comparte con apagar y matar."""
    veces = []

    def contado():
        veces.append(1)
        return server.doctor.Check("daemon de docker", "fail", "de prueba")

    monkeypatch.setattr(server.doctor, "_docker", contado)
    monkeypatch.setattr(server, "_docker_seen", (0.0, False))

    for nombre in ("a", "b", "c"):
        root = tmp_path / nombre
        root.mkdir()
        (root / "stack.yaml").write_text(
            "services:\n  c:\n    command: docker compose up -d\n    detached: true\n",
            encoding="utf-8",
        )
        registry.add(root)

    for _ in range(4):
        assert client.get("/api/state").status_code == 200

    assert len(veces) == 1, f"el daemon se chequeo {len(veces)} veces en 12 vistas de proyecto"
    assert client.get("/api/state").json()["projects"][0]["docker_down"] is True


def _sesion_recuperada(tmp_path, port):
    """Simula un reinicio de `stackhelx serve` con el proceso todavia vivo."""
    import json

    root = tmp_path / "sobreviviente"
    root.mkdir()
    (root / "stack.yaml").write_text(
        f"services:\n  srv:\n    command: echo hola\n    port: {port}\n", encoding="utf-8"
    )
    pid = registry.project_id(registry.add(root))
    registry.HOME.mkdir(parents=True, exist_ok=True)
    server._sessions_file().write_text(
        json.dumps({pid: {"path": str(root), "profile": None, "state": "running"}}),
        encoding="utf-8",
    )
    server.sessions.clear()
    server._load_sessions_state()
    return pid


def test_una_sesion_recuperada_muestra_sus_servicios(client, tmp_path, monkeypatch, free_ports):
    """Devolvia {} y la tarjeta decia "corriendo" con la lista vacia."""
    import socket

    (port,) = free_ports(1)
    vivo = socket.socket()
    vivo.bind(("127.0.0.1", port))
    vivo.listen()
    try:
        pid = _sesion_recuperada(tmp_path, port)
        estados = server.sessions[pid].service_states()
        assert estados == {"srv": "ready"}
    finally:
        vivo.close()


def test_reiniciar_una_sesion_recuperada_lo_dice_en_vez_de_reventar(
    client, tmp_path, monkeypatch, free_ports
):
    """Sin motor, restart_async moria con un AssertionError en un hilo daemon:
    el usuario apretaba el boton y no pasaba nada."""
    import socket

    (port,) = free_ports(1)
    vivo = socket.socket()
    vivo.bind(("127.0.0.1", port))
    vivo.listen()
    try:
        pid = _sesion_recuperada(tmp_path, port)
        respuesta = client.post(f"/api/projects/{pid}/services/srv/restart")
        assert respuesta.status_code == 409
        assert "reinicio del servidor" in respuesta.json()["detail"]
    finally:
        vivo.close()


@pytest.fixture
def intruso(tmp_path, free_ports):
    """Un proyecto registrado con su puerto tomado por un proceso ajeno."""
    (port,) = free_ports(1)
    root = tmp_path / "conintruso"
    root.mkdir()
    (root / "stack.yaml").write_text(
        f"services:\n  web:\n    command: python web\n    port: {port}\n", encoding="utf-8"
    )
    registry.add(root)

    proc = subprocess.Popen([sys.executable, "-c", SERVER.format(port=port)])
    assert esperar(lambda: not ports.is_free(port)), "el intruso nunca tomo el puerto"
    yield port
    if proc.poll() is None:
        proc.kill()
        proc.wait()


def test_kill_all_cierra_los_puertos_pedidos(client, intruso):
    assert any(o["port"] == intruso for o in client.get("/api/ports/orphans").json()["orphans"])

    res = client.post("/api/ports/kill-all", json={"ports": [intruso]})
    assert res.status_code == 200
    assert [k["port"] for k in res.json()["killed"]] == [intruso]
    assert esperar(lambda: ports.is_free(intruso)), "el puerto siguio ocupado"


def test_kill_all_sin_lista_no_mata_nada(client, intruso):
    """El filtro es obligatorio a proposito.

    Con un campo opcional, un body mal formado se leia como "sin filtro" y el
    endpoint pasaba de cerrar lo que el usuario nombro a cerrar todo lo que
    encontrara. Un endpoint que mata procesos falla cerrado.
    """
    assert client.post("/api/ports/kill-all", json={}).status_code == 422
    assert client.post("/api/ports/kill-all", json={"ports": None}).status_code == 422
    assert not ports.is_free(intruso), "el intruso murio con un body invalido"


def test_kill_all_solo_toca_lo_que_le_pidieron(client, intruso, free_ports):
    """Un puerto ajeno a la lista no se cierra aunque sea intruso."""
    res = client.post("/api/ports/kill-all", json={"ports": [free_ports(1)[0]]})
    assert res.status_code == 200
    assert res.json()["killed"] == []
    assert not ports.is_free(intruso)


def test_kill_all_exceder_max_targets_devuelve_422(client):
    """Una lista mayor a MAX_TARGETS (200) debe fallar con 422 Unprocessable Entity."""
    puertos = list(range(1000, 1201))  # 201 puertos
    assert client.post("/api/ports/kill-all", json={"ports": puertos}).status_code == 422


# docker ------------------------------------------------------------------

# Parchean `docker.ACTIONS` por procesos reales, y no por mocks: lo que hay que
# probar es que se mira el resultado, y para eso hace falta un proceso que
# devuelva un codigo de verdad. Sin el parche, cada corrida de la suite
# arrancaria Docker Desktop en la maquina de quien la corre y en los cinco
# runners del CI.


def _acciones(comando: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    return {"start": comando, "restart": comando}


def test_docker_dice_que_no_cuando_el_comando_no_existe(monkeypatch):
    monkeypatch.setattr(docker, "ACTIONS", _acciones(("no-existe-este-binario-12345",)))
    ok, detail = docker.run("start")
    assert ok is False
    assert "PATH" in detail


def test_docker_dice_que_no_cuando_el_comando_falla(monkeypatch):
    """El caso de Linux sin el plugin: docker existe y el subcomando no."""
    guion = "import sys; print('desktop is not a docker command', file=sys.stderr); sys.exit(1)"
    monkeypatch.setattr(docker, "ACTIONS", _acciones((sys.executable, "-c", guion)))
    ok, detail = docker.run("start")
    assert ok is False
    assert detail == "desktop is not a docker command", "el motivo tiene que llegar al usuario"


@pytest.mark.parametrize("accion", ["start", "restart"])
def test_docker_dice_que_si_cuando_el_comando_sale_bien(client, monkeypatch, accion):
    monkeypatch.setattr(docker, "ACTIONS", _acciones((sys.executable, "-c", "pass")))
    respuesta = client.post(f"/api/docker/{accion}")
    assert respuesta.status_code == 200
    assert respuesta.json()["ok"] is True


def test_docker_rechaza_una_accion_inventada(client, monkeypatch):
    """La ruta es la unica entrada, y el `Literal` la cierra antes de ejecutar nada."""
    monkeypatch.setattr(docker, "ACTIONS", _acciones((sys.executable, "-c", "pass")))
    for inventada in ("stop", "borrar-todo", "start;rm"):
        respuesta = client.post(f"/api/docker/{inventada}")
        assert respuesta.status_code == 422, inventada
        assert "start" in respuesta.json()["detail"][0]["msg"], "el error dice que se acepta"


def test_docker_exige_token(client):
    client.headers.pop("Authorization")
    assert client.post("/api/docker/start").status_code == 401


def test_docker_clean_endpoint(client, monkeypatch):
    pedidos = []
    monkeypatch.setattr(
        docker, "prune", lambda targets: pedidos.append(list(targets)) or (True, "cache: 10MB")
    )
    res = client.post("/api/docker/clean", json={"targets": ["cache", "images"]})
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert "10MB" in res.json()["detail"]
    assert pedidos == [["cache", "images"]], "el servidor limpio otra cosa que la pedida"


def test_docker_clean_sin_lista_no_borra_nada(client, monkeypatch):
    """Falla cerrado, como kill-all.

    Con un campo opcional, un body mal formado se leia como "limpia todo", y
    este endpoint borra datos que no vuelven.
    """
    llamadas = []
    monkeypatch.setattr(docker, "prune", lambda targets: llamadas.append(targets) or (True, "ok"))

    assert client.post("/api/docker/clean").status_code == 422
    assert client.post("/api/docker/clean", json={}).status_code == 422
    assert client.post("/api/docker/clean", json={"targets": []}).status_code == 422
    assert client.post("/api/docker/clean", json={"targets": ["borrar-todo"]}).status_code == 422
    assert llamadas == [], "algo llego a ejecutarse con un cuerpo invalido"


def test_share_endpoint(client, monkeypatch):
    monkeypatch.setattr(
        server.tunnel,
        "start_tunnel",
        lambda port, provider=None: server.tunnel.Tunnel(
            provider="cloudflared",
            port=port,
            url="https://test-tunnel.trycloudflare.com",
            proc=subprocess.Popen("echo ok", shell=True),
        ),
    )
    res = client.post("/api/share?port=3000")
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert res.json()["url"] == "https://test-tunnel.trycloudflare.com"

    res_del = client.delete("/api/share/3000")
    assert res_del.status_code == 200
    assert res_del.json()["ok"] is True



# interfaz -----------------------------------------------------------------


def test_la_interfaz_arranca_sola():
    """app.js se poblaba solo al tocar algo: el bloque de arranque se perdio en
    a252013 al reescribir el final del archivo, y la pagina cargaba en blanco.
    Todas las demas llamadas a `refresh` viven adentro de un handler."""
    fuente = (server.WEB / "app.js").read_text(encoding="utf-8")
    assert "setInterval(refresh" in fuente, "no hay sondeo periodico"
    # La llamada inicial, la que puebla la pagina antes del primer intervalo.
    assert "\nrefresh();" in fuente, "no hay una llamada a refresh fuera de un handler"


def test_cada_id_del_html_lo_usa_el_js():
    """Un id que el JS no toca es un control muerto, y no se nota mirando."""
    import re

    html = (server.WEB / "index.html").read_text(encoding="utf-8")
    js = (server.WEB / "app.js").read_text(encoding="utf-8")
    huerfanos = []
    for ident in re.findall(r'id="([^"]+)"', html):
        camel = re.sub(r"-([a-z])", lambda m: m.group(1).upper(), ident)
        # `btn-export` es un <a href> puro: no necesita JS.
        if ident in ("btn-export",):
            continue
        if ident not in js and camel not in js:
            huerfanos.append(ident)
    assert not huerfanos, f"ids del HTML que el JS nunca toca: {huerfanos}"


def _tunel_falso(monkeypatch, proc):
    """Un `Tunnel` con un proceso de verdad detras, para poder matarlo y verlo."""
    monkeypatch.setattr(
        server.tunnel,
        "start_tunnel",
        lambda port, provider=None: server.tunnel.Tunnel(
            provider="cloudflared",
            port=port,
            url=f"https://prueba-{port}.trycloudflare.com",
            proc=proc,
        ),
    )


def test_los_tuneles_se_cierran_al_apagar_el_servidor(monkeypatch, free_ports):
    """`stackhelx serve` terminaba y el cliente de tuneles seguia vivo.

    El puerto quedaba expuesto a internet, sin nada en pantalla que lo dijera y
    sin forma de cerrarlo salvo matar el proceso a mano.
    """
    (port,) = free_ports(1)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    _tunel_falso(monkeypatch, proc)
    try:
        app = server.create_app(TOKEN)
        with TestClient(app, base_url="http://127.0.0.1") as cliente:
            cliente.headers.update({"Authorization": f"Bearer {TOKEN}"})
            assert cliente.post(f"/api/share?port={port}").json()["ok"] is True
            assert proc.poll() is None, "el tunel tiene que seguir vivo mientras el servidor corre"

        # Salir del `with` dispara el apagado de la aplicacion.
        assert esperar(lambda: proc.poll() is not None), "el tunel sobrevivio al apagado"
        assert server._active_tunnels == {}
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_un_segundo_tunel_para_el_mismo_puerto_se_rechaza(monkeypatch, free_ports):
    """Reemplazar la entrada dejaba al primer proceso fuera del registro.

    Y fuera del registro no lo cierra ni el apagado ni el boton: es la misma
    fuga, disparada con dos clicks seguidos.
    """
    (port,) = free_ports(1)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    _tunel_falso(monkeypatch, proc)
    try:
        app = server.create_app(TOKEN)
        with TestClient(app, base_url="http://127.0.0.1") as cliente:
            cliente.headers.update({"Authorization": f"Bearer {TOKEN}"})
            assert cliente.post(f"/api/share?port={port}").json()["ok"] is True

            segundo = cliente.post(f"/api/share?port={port}").json()
            assert segundo["ok"] is False
            assert "ya hay un tunel" in segundo["detail"]

            assert cliente.delete(f"/api/share/{port}").json()["ok"] is True
            assert esperar(lambda: proc.poll() is not None), "cerrar el tunel no mato el proceso"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_el_estado_lista_los_tuneles_abiertos(monkeypatch, free_ports):
    """Fuera del paginado: un puerto expuesto a internet no puede quedar en la
    pagina 2, que es donde lo dejaria colgarlo de un proyecto."""
    (port,) = free_ports(1)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    _tunel_falso(monkeypatch, proc)
    try:
        app = server.create_app(TOKEN)
        with TestClient(app, base_url="http://127.0.0.1") as cliente:
            cliente.headers.update({"Authorization": f"Bearer {TOKEN}"})
            assert cliente.get("/api/state").json()["tunnels"] == []

            cliente.post(f"/api/share?port={port}")
            (activo,) = cliente.get("/api/state").json()["tunnels"]
            assert activo["port"] == port
            assert activo["provider"] == "cloudflared"
            assert activo["url"].startswith("https://")

            cliente.delete(f"/api/share/{port}")
            assert cliente.get("/api/state").json()["tunnels"] == []
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_un_tunel_que_se_murio_solo_deja_de_figurar(monkeypatch, free_ports):
    """El cliente de tuneles se puede caer por su cuenta.

    Un puerto que figura expuesto sin estarlo es una mentira justo en el panel
    que existe para no mentir sobre eso.
    """
    (port,) = free_ports(1)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    _tunel_falso(monkeypatch, proc)
    try:
        app = server.create_app(TOKEN)
        with TestClient(app, base_url="http://127.0.0.1") as cliente:
            cliente.headers.update({"Authorization": f"Bearer {TOKEN}"})
            cliente.post(f"/api/share?port={port}")
            assert len(cliente.get("/api/state").json()["tunnels"]) == 1

            # El cloudflared se cae solo, sin que nadie apriete Cerrar.
            proc.kill()
            proc.wait()

            assert cliente.get("/api/state").json()["tunnels"] == []
            # Y el puerto queda libre para volver a compartirse.
            assert cliente.post(f"/api/share?port={port}").json()["ok"] is True
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_el_estado_de_docker_no_depende_de_la_pagina(client, tmp_path):
    """Apretar "Siguiente" apagaba la fila de Docker entera.

    Salia de los cuatro proyectos de la pagina, asi que bastaba una segunda
    pagina sin contenedores para que el estado y los dos botones desaparecieran.
    Una fila que se va no distingue "esta en orden" de "esto dejo de funcionar",
    que es el mismo motivo por el que /api/health no pagina.
    """
    condocker = tmp_path / "condocker"
    condocker.mkdir()
    (condocker / "stack.yaml").write_text(
        "services:\n  db:\n    command: docker compose up -d db\n    detached: true\n",
        encoding="utf-8",
    )
    registry.add(condocker)
    # Los demas sin contenedores, suficientes para empujar una segunda pagina.
    for i in range(server.PAGE_SIZE):
        root = tmp_path / f"simple{i}"
        root.mkdir()
        (root / "stack.yaml").write_text(
            "services:\n  web:\n    command: echo hola\n", encoding="utf-8"
        )
        registry.add(root)

    primera = client.get("/api/state?page=1").json()
    assert primera["pages"] > 1, "hacen falta dos paginas para que el bug sea alcanzable"
    ultima = client.get(f"/api/state?page={primera['pages']}").json()

    assert primera["docker"]["needed"] is True
    assert ultima["docker"]["needed"] is True, "la fila de Docker se apago al pasar de pagina"
    assert primera["docker"]["down"] == ultima["docker"]["down"]


def test_un_proyecto_invalido_devuelve_el_contrato_completo(client, tmp_path):
    """El `return` temprano se salteaba la mitad de las claves.

    En JS eso es `undefined`, o sea falso silencioso: el proximo que lea
    `p.needs_docker` esperando un booleano se come la trampa.
    """
    roto = tmp_path / "roto"
    roto.mkdir()
    (roto / "stack.yaml").write_text("services:\n  a:\n    port: 1\n", encoding="utf-8")
    registry.add(roto)

    (proyecto,) = client.get("/api/state").json()["projects"]
    assert proyecto["state"] == "invalid"
    for clave in ("detected", "needs_docker", "docker_down", "services", "profiles", "error"):
        assert clave in proyecto, f"falta {clave} en un proyecto invalido"


def test_un_servicio_sin_puerto_declarado_no_es_intruso_de_nadie(client, tmp_path, free_ports):
    """El caso de un Next detectado: `ready: listen`, sin `port:` en el stack.

    El puerto lo elige el servicio al arrancar, asi que no estaba en la lista de
    "esto es nuestro", que solo miraba los declarados. Otro proyecto registrado
    que si declarara ese numero lo veia ocupado y acusaba de intruso al proceso
    que acababamos de levantar nosotros: cerrarlo mataba el servicio propio, y
    "Liberar todos" lo hacia de un click.
    """
    (port,) = free_ports(1)

    # Lo levanta sin declarar el puerto: lo abre y el runner lo descubre.
    sin_declarar = tmp_path / "sindeclarar"
    sin_declarar.mkdir()
    (sin_declarar / "stack.yaml").write_text(
        f'services:\n  web:\n    command: {sys.executable} -c "{SERVER.format(port=port)}"\n'
        "    ready: listen\n",
        encoding="utf-8",
    )
    pid = registry.project_id(registry.add(sin_declarar))

    # Y otro que si declara ese mismo numero, que es quien lo acusaba.
    declarante = tmp_path / "declarante"
    declarante.mkdir()
    (declarante / "stack.yaml").write_text(
        f"services:\n  web:\n    command: echo hola\n    port: {port}\n", encoding="utf-8"
    )
    registry.add(declarante)

    assert client.post(f"/api/projects/{pid}/up", json={}).status_code == 200
    assert esperar(lambda: server.sessions[pid].service_ports().get("web") == port), (
        "el runner nunca descubrio el puerto"
    )

    intrusos = client.get("/api/ports/orphans").json()["orphans"]
    assert [o for o in intrusos if o["port"] == port] == [], (
        "el servicio propio figura como intruso de otro proyecto"
    )


def test_un_puerto_que_dos_proyectos_declaran_es_una_sola_fila(client, tmp_path, free_ports):
    """Salia una fila por proyecto: el mismo pid y la misma linea de comando dos
    veces, que ademas sugeria que habia dos procesos. El puerto es uno y el
    proceso es uno; lo que hay de a varios son los proyectos que lo reclaman."""
    (port,) = free_ports(1)
    for nombre in ("blog", "tienda"):
        root = tmp_path / nombre
        root.mkdir()
        (root / "stack.yaml").write_text(
            f"services:\n  web:\n    command: echo hola\n    port: {port}\n", encoding="utf-8"
        )
        registry.add(root)

    proc = subprocess.Popen([sys.executable, "-c", SERVER.format(port=port)])
    try:
        assert esperar(lambda: not ports.is_free(port)), "el intruso nunca tomo el puerto"

        intrusos = [o for o in client.get("/api/ports/orphans").json()["orphans"]
                    if o["port"] == port]

        assert len(intrusos) == 1, f"una fila por puerto, llegaron {len(intrusos)}"
        assert intrusos[0]["projects"] == ["blog", "tienda"], "los dos que lo declaran, ordenados"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_el_endpoint_de_contenedores_lista_los_que_corren(client, monkeypatch):
    """Reiniciar el motor los baja a todos, incluidos los de proyectos que no
    estas mirando. No se puede a medias, asi que lo unico que queda es nombrarlos
    antes: "los contenedores" no dice si son dos o nueve."""
    monkeypatch.setattr(server.docker, "running", lambda: ["web", "db"])
    assert client.get("/api/docker/containers").json()["running"] == ["web", "db"]


def test_los_contenedores_con_docker_caido_no_rompen_la_confirmacion(client, monkeypatch):
    """Sin motor, la lista vacia y la interfaz se queda con la frase generica.

    Un 500 acá dejaría el botón armado sin decir nada, que es peor que decir de
    más: la advertencia importa incluso cuando no se puede detallar.
    """
    monkeypatch.setattr(server.docker, "running", lambda: [])
    res = client.get("/api/docker/containers")
    assert res.status_code == 200
    assert res.json()["running"] == []


def _proyecto_http_con_url(tmp_path, port, url):
    """Un proyecto cuyo servicio contesta HTTP de verdad, con `url:` declarada."""
    servidor = (
        "from http.server import HTTPServer, SimpleHTTPRequestHandler; "
        f"HTTPServer(('127.0.0.1', {port}), SimpleHTTPRequestHandler).serve_forever()"
    )
    root = tmp_path / "conurl"
    root.mkdir()
    (root / "stack.yaml").write_text(
        f'services:\n'
        f'  web:\n'
        f'    command: {sys.executable} -c "{servidor}"\n'
        f'    port: {port}\n'
        f'    url: {url}\n',
        encoding="utf-8",
    )
    return root


def test_el_estado_trae_la_url_declarada_con_la_variable_expandida(
    client, tmp_path, free_ports
):
    """El caso de ORQUESTER: el boton tiene que llevar al `?token=`, no a la raiz
    del puerto, que carga una pagina que se ve bien y falla en cada llamada.
    """
    (port,) = free_ports(1)
    root = _proyecto_http_con_url(
        tmp_path, port, "http://127.0.0.1:8765/?token=${PM_TOKEN_DE_PRUEBA}"
    )
    (root / "stack.yaml").write_text(
        (root / "stack.yaml").read_text(encoding="utf-8")
        + "    env:\n      PM_TOKEN_DE_PRUEBA: secreto-expandido\n",
        encoding="utf-8",
    )
    pid = registry.project_id(registry.add(root))
    assert client.post(f"/api/projects/{pid}/up", json={}).status_code == 200

    def con_url():
        servicio = client.get("/api/state").json()["projects"][0]["services"][0]
        return servicio["openable"] and servicio["url"]

    assert esperar(con_url, sum(server.HTTP_RETRIES) + 20), "nunca llego la url"
    servicio = client.get("/api/state").json()["projects"][0]["services"][0]
    assert servicio["url"] == "http://127.0.0.1:8765/?token=secreto-expandido"


def test_un_servicio_detenido_no_trae_url_aunque_la_declare(client, tmp_path, free_ports):
    """La expansion lee `env.global` y cada `env_file` de disco, y esto se sondea
    cada 2.5s para cada servicio de cada proyecto. Con el stack apagado el boton
    ni se dibuja, asi que preguntarla seria pagar disco por nada.

    Este test se pone rojo si alguien saca la compuerta de `openable` y vuelve a
    poner la expansion en el camino caliente.
    """
    (port,) = free_ports(1)
    root = _proyecto_http_con_url(tmp_path, port, "http://localhost:3000/admin")
    registry.add(root)

    servicio = client.get("/api/state").json()["projects"][0]["services"][0]
    assert servicio["openable"] is False
    assert servicio["url"] is None


def test_un_servicio_sin_url_declarada_la_trae_en_none(client, proyecto):
    """La interfaz arma el default con el puerto. Que el servidor mande
    `http://localhost:<port>` seria un cuarto lugar donde vive ese literal.
    """
    path, _ = proyecto
    pid = registry.project_id(path)
    client.post(f"/api/projects/{pid}/up", json={})
    esperar_listo(client)

    servicio = client.get("/api/state").json()["projects"][0]["services"][0]
    assert servicio["url"] is None


def test_share_rechaza_un_puerto_fuera_de_rango(client):
    """`kill` validaba gratis porque llama a `ports.scan`; `share` no tiene scan.

    Sin la validacion propia, el 0, el -5 y el 99999 contestaban 200 y llegaban
    hasta el cliente de tuneles. Y la reserva en `_active_tunnels` se hacia antes,
    asi que un puerto imposible dejaba una entrada a medias.
    """
    for port in (0, -5, 70000, 99999):
        res = client.post(f"/api/share?port={port}")
        assert res.status_code == 400, port
        assert "rango" in res.json()["detail"]


def test_get_history_endpoint(client, tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "HOME", tmp_path)
    pid = "serverhistorytest"
    from stackhelx import history
    history.append(pid, {"duration_s": 2.5, "result": "running", "profile": "prod"})

    res = client.get(f"/api/projects/{pid}/history")
    assert res.status_code == 200
    data = res.json()
    assert "history" in data
    assert len(data["history"]) >= 1
    assert data["history"][0]["duration_s"] == 2.5


def test_get_metrics_endpoint(client, proyecto):
    path, _ = proyecto
    pid = registry.project_id(path)
    # Sin arrancar, retorna métricas vacías
    res = client.get(f"/api/projects/{pid}/metrics")
    assert res.status_code == 200
    assert res.json() == {"metrics": {}}

    # Arrancado, retorna métricas del stack
    client.post(f"/api/projects/{pid}/up", json={})
    esperar_listo(client)
    try:
        res = client.get(f"/api/projects/{pid}/metrics")
        assert res.status_code == 200
        metrics = res.json().get("metrics", {})
        assert "srv" in metrics
        assert "memory_mb" in metrics["srv"]
        assert "cpu_percent" in metrics["srv"]

        # También debe venir incluido en /api/state sin requests adicionales
        state_res = client.get("/api/state")
        assert state_res.status_code == 200
        proj = next(p for p in state_res.json()["projects"] if p["id"] == pid)
        assert "metrics" in proj
        assert "srv" in proj["metrics"]
    finally:
        client.post(f"/api/projects/{pid}/down")


def test_sink_subscription_and_dispatch():
    sink = server._Sink()
    sub_q = sink.subscribe()
    sink.write("primera linea\nsegunda linea\n")

    item1 = sub_q.get(timeout=1.0)
    assert item1 == (1, "primera linea")
    item2 = sub_q.get(timeout=1.0)
    assert item2 == (2, "segunda linea")

    sink.unsubscribe(sub_q)
    sink.write("tercera linea\n")
    import queue
    with pytest.raises(queue.Empty):
        sub_q.get(timeout=0.1)


def test_logs_stream_endpoint(client, proyecto):
    path, _ = proyecto
    pid = registry.project_id(path)

    # Con proyecto apagado, el endpoint responde 404
    res = client.get(f"/api/projects/{pid}/logs/stream")
    assert res.status_code == 404

    client.post(f"/api/projects/{pid}/up", json={})
    esperar_listo(client)
    try:
        # Escribir en el sink para tener una línea conocida
        sesion = server.sessions[pid]
        sesion.sink.write("evento de prueba en vivo\n")

        # Petición SSE con stream (follow=False para terminar de leer el lote de logs existentes)
        with client.stream("GET", f"/api/projects/{pid}/logs/stream?follow=false") as response:
            assert response.status_code == 200
            assert "text/event-stream" in response.headers.get("content-type", "")

            lines = [chunk for chunk in response.iter_lines() if chunk.startswith("data: ")]
            assert any("evento de prueba en vivo" in line for line in lines)
    finally:
        client.post(f"/api/projects/{pid}/down")


def test_logs_stream_dynamic_realtime(client, proyecto):
    path, _ = proyecto
    pid = registry.project_id(path)

    client.post(f"/api/projects/{pid}/up", json={})
    esperar_listo(client)
    try:
        sesion = server.sessions[pid]
        current_seq = sesion.sink.seq

        import threading

        def emit_later():
            time.sleep(0.05)
            sesion.sink.write("linea dinamica en vuelo\n")

        t = threading.Thread(target=emit_later)
        t.start()

        with client.stream(
            "GET", f"/api/projects/{pid}/logs/stream?since={current_seq}&max_duration=0.6"
        ) as response:
            assert response.status_code == 200
            lines = [chunk for chunk in response.iter_lines() if chunk.startswith("data: ")]
            assert any("linea dinamica en vuelo" in line for line in lines)

        t.join()
    finally:
        client.post(f"/api/projects/{pid}/down")


def test_project_view_includes_dependency_graph(client, proyecto):
    path, _ = proyecto
    pid = registry.project_id(path)

    res = client.get("/api/state")
    assert res.status_code == 200
    proj = next(p for p in res.json()["projects"] if p["id"] == pid)
    assert "graph" in proj
    assert "nodes" in proj["graph"]
    assert "edges" in proj["graph"]
    assert "levels" in proj["graph"]
    assert len(proj["graph"]["nodes"]) >= 1
    assert proj["graph"]["nodes"][0]["name"] == "srv"


def test_env_audit_endpoint(client, tmp_path):
    root = tmp_path / "env_proj"
    root.mkdir()
    (root / "stack.yaml").write_text("name: env_proj\nservices: {}\n", encoding="utf-8")
    (root / ".env.example").write_text(
        "API_KEY=\nDB_URL=\nSECRET_KEY=\nEMPTY_KEY=\n", encoding="utf-8"
    )
    (root / ".env").write_text(
        "API_KEY=your_secret_here\nDB_URL=postgres://super_secret_user:super_secret_pass@127.0.0.1/db\nEMPTY_KEY=\n",
        encoding="utf-8",
    )
    pid = registry.project_id(registry.add(root))

    res = client.get(f"/api/projects/{pid}/env-audit")
    assert res.status_code == 200
    data = res.json()
    assert data["has_env"] is True
    assert data["has_example"] is True
    assert data["example_file"] == ".env.example"
    assert data["ok"] is False
    assert "SECRET_KEY" in data["missing_keys"]
    assert "API_KEY" in data["placeholder_keys"]
    assert "EMPTY_KEY" in data["empty_keys"]
    # Verificar que NINGÚN secreto se filtra en la respuesta JSON ni en texto
    assert "super_secret_user" not in res.text
    assert "super_secret_pass" not in res.text

    # Arreglar .env y verificar que ok pasa a True
    (root / ".env").write_text(
        "API_KEY=valid-prod-token-12345\nDB_URL=sqlite:///app.db\nSECRET_KEY=custom-key-999\nEMPTY_KEY=value\n",
        encoding="utf-8",
    )
    res2 = client.get(f"/api/projects/{pid}/env-audit")
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["ok"] is True
    assert data2["missing_keys"] == []
    assert data2["placeholder_keys"] == []
    assert data2["empty_keys"] == []


def test_switch_profile_endpoint(client, tmp_path, free_ports):
    ports = free_ports(2)
    p1, p2 = ports[0], ports[1]
    root = tmp_path / "switch_proj"
    root.mkdir()
    (root / "stack.yaml").write_text(
        textwrap.dedent(f"""
        name: switch_proj
        services:
          web:
            command: {sys.executable} -c "{SERVER.format(port=p1)}"
            port: {p1}
          api:
            command: {sys.executable} -c "{SERVER.format(port=p2)}"
            port: {p2}
        profiles:
          front: [web]
          back: [api]
        """),
        encoding="utf-8",
    )
    pid = registry.project_id(registry.add(root))

    # 1. Proyecto detenido: rechaza perfil desconocido con 400
    res_err = client.post(f"/api/projects/{pid}/switch-profile", json={"profile": "desconocido"})
    assert res_err.status_code == 400

    # 2. Proyecto detenido: cambia perfil configurado a "front"
    res = client.post(f"/api/projects/{pid}/switch-profile", json={"profile": "front"})
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert res.json()["profile"] == "front"

    # Verificar en /api/state que el perfil activo ahora es "front"
    state = client.get("/api/state").json()
    proj = next(p for p in state["projects"] if p["id"] == pid)
    assert proj["profile"] == "front"

    # 3. Arrancar con perfil "front"
    assert client.post(f"/api/projects/{pid}/up", json={"profile": "front"}).status_code == 200
    esperar_listo(client)
    from stackhelx import ports as ports_mod

    assert not ports_mod.is_free(p1), "web debio arrancar"
    assert ports_mod.is_free(p2), "api no debio arrancar"

    # 4. Conmutar en caliente a perfil "back"
    res_switch = client.post(f"/api/projects/{pid}/switch-profile", json={"profile": "back"})
    assert res_switch.status_code == 200
    assert res_switch.json()["ok"] is True
    assert res_switch.json()["profile"] == "back"

    # Esperar que web se detenga y api arranque
    assert esperar(lambda: not ports_mod.is_free(p2) and ports_mod.is_free(p1), segundos=15)

    # Verificar en /api/state que el proyecto sigue corriendo con perfil "back"
    assert esperar(
        lambda: any(
            p["id"] == pid and p["state"] == "running" and p["profile"] == "back"
            for p in client.get("/api/state").json()["projects"]
        ),
        segundos=15,
    )
    state2 = client.get("/api/state").json()
    proj2 = next(p for p in state2["projects"] if p["id"] == pid)
    assert proj2["profile"] == "back"
    assert proj2["state"] == "running"

    # 5. Apagar
    assert client.post(f"/api/projects/{pid}/down").status_code == 200
    assert esperar(lambda: ports_mod.is_free(p2))


def test_suggest_port_endpoint(client, free_ports):
    (p,) = free_ports(1)
    # 1. Puerto libre
    res_free = client.get(f"/api/ports/{p}/suggest")
    assert res_free.status_code == 200
    data_free = res_free.json()
    assert data_free["port"] == p
    assert data_free["free"] is True
    assert data_free["suggested"] == p

    # 2. Puerto ocupado
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", p))
    sock.listen()
    try:
        res_busy = client.get(f"/api/ports/{p}/suggest")
        assert res_busy.status_code == 200
        data_busy = res_busy.json()
        assert data_busy["port"] == p
        assert data_busy["free"] is False
        assert data_busy["suggested"] != p
        assert data_busy["occupant"] is not None
        assert data_busy["occupant"]["pid"] == os.getpid()
    finally:
        sock.close()


def test_project_conflicts_endpoint(client, tmp_path, free_ports):
    (p,) = free_ports(1)
    root = tmp_path / "conflict_proj"
    root.mkdir()
    (root / "stack.yaml").write_text(
        textwrap.dedent(f"""
        name: conflict_proj
        services:
          web:
            command: {sys.executable} -c "{SERVER.format(port=p)}"
            port: {p}
        """),
        encoding="utf-8",
    )
    pid = registry.project_id(registry.add(root))

    # Sin conflicto cuando el puerto está libre
    res_ok = client.get(f"/api/projects/{pid}/conflicts")
    assert res_ok.status_code == 200
    assert res_ok.json()["has_conflicts"] is False
    assert res_ok.json()["conflicts"] == []

    # Ocupamos el puerto con un socket ajeno
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", p))
    sock.listen()
    try:
        res_conf = client.get(f"/api/projects/{pid}/conflicts")
        assert res_conf.status_code == 200
        data = res_conf.json()
        assert data["has_conflicts"] is True
        assert len(data["conflicts"]) == 1
        c = data["conflicts"][0]
        assert c["service"] == "web"
        assert c["port"] == p
        assert c["occupant"]["pid"] == os.getpid()
        assert c["suggested_port"] != p

        # Verificar que /api/state también incluye suggested_port en la vista del servicio
        state = client.get("/api/state").json()
        proj = next(proj for proj in state["projects"] if proj["id"] == pid)
        web_svc = proj["services"][0]
        assert web_svc["port_taken"] is False or web_svc["occupant"] is not None
        assert web_svc["suggested_port"] != p
    finally:
        sock.close()


def test_mcp_activity_endpoint(client):
    from stackhelx import mcp

    mcp.clear_telemetry()
    mcp.record_tool_call("stackhelx_doctor", 45.2, "ok", "ok (512 bytes)")
    res = client.get("/api/mcp/activity")
    assert res.status_code == 200
    data = res.json()
    assert data["total_calls"] == 1
    assert data["by_tool"].get("stackhelx_doctor") == 1
    assert data["rate_limit_max"] == 30
    assert len(data["recent_events"]) == 1
    ev = data["recent_events"][0]
    assert ev["tool"] == "stackhelx_doctor"
    assert ev["duration_ms"] == 45.2
    assert ev["status"] == "ok"


def test_sink_concurrencia_escribe_sin_perder_seq():
    import threading

    from stackhelx.server import _Sink

    sink = _Sink()
    hilos = 5
    lineas_por_hilo = 50

    def escritor(hilo_id: int):
        for i in range(lineas_por_hilo):
            sink.write(f"hilo-{hilo_id} linea-{i}\n")
        sink.flush()

    threads = [threading.Thread(target=escritor, args=(h,)) for h in range(hilos)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    total_esperado = hilos * lineas_por_hilo
    assert sink.seq == total_esperado
    assert len(sink.lines) == total_esperado
    seqs = [item[0] for item in sink.lines]
    assert seqs == list(range(1, total_esperado + 1))


def test_open_folder_exitoso(client, tmp_path, monkeypatch):
    carpeta = tmp_path / "mi_carpeta"
    carpeta.mkdir()

    abierto = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: abierto.append(cmd))

    res = client.post("/api/open-folder", json={"path": str(carpeta)})
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert len(abierto) == 1



def test_open_folder_rechaza_invalido_o_no_existente(client, tmp_path):
    res_relativa = client.post("/api/open-folder", json={"path": "carpeta/relativa"})
    assert res_relativa.status_code == 400

    no_existe = tmp_path / "fantasma"
    res_no_existe = client.post("/api/open-folder", json={"path": str(no_existe)})
    assert res_no_existe.status_code == 400


def test_open_editor_con_editor_detectado(client, tmp_path, monkeypatch):
    carpeta = tmp_path / "proyecto_editor"
    carpeta.mkdir()

    monkeypatch.setenv("STACKHELX_EDITOR", "dummy_editor")
    monkeypatch.setattr(shutil, "which", lambda cmd: "/usr/bin/dummy_editor" if cmd == "dummy_editor" else None)

    lanzado = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: lanzado.append(cmd))

    res = client.post("/api/open-editor", json={"path": str(carpeta)})
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert len(lanzado) == 1


def test_open_editor_sin_editor_en_sistema(client, tmp_path, monkeypatch):
    carpeta = tmp_path / "proyecto_sin_editor"
    carpeta.mkdir()

    monkeypatch.delenv("STACKHELX_EDITOR", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.setattr(shutil, "which", lambda cmd: None)

    res = client.post("/api/open-editor", json={"path": str(carpeta)})
    assert res.status_code == 404


def test_list_editors_detecta_instalados(client, monkeypatch):
    def fake_which(cmd):
        if "cursor" in cmd:
            return "/usr/bin/cursor"
        return None

    monkeypatch.setattr(shutil, "which", fake_which)
    res = client.get("/api/editors")
    assert res.status_code == 200
    ids = [e["id"] for e in res.json()["editors"]]
    assert "cursor" in ids


def test_open_editor_con_seleccion_especifica(client, tmp_path, monkeypatch):
    carpeta = tmp_path / "proyecto_cursor"
    carpeta.mkdir()

    def fake_which(cmd):
        if "cursor" in cmd:
            return "C:\\bin\\cursor.cmd"
        return None

    monkeypatch.setattr(shutil, "which", fake_which)
    lanzado = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: lanzado.append(cmd))

    res = client.post("/api/open-editor", json={"path": str(carpeta), "editor": "cursor"})
    assert res.status_code == 200
    assert res.json()["editor"] == "Cursor"
    assert len(lanzado) == 1


def test_browse_frecuentes(client, tmp_path, monkeypatch):

    base1 = tmp_path / "dev"
    base1.mkdir()
    p1 = base1 / "app1"
    p1.mkdir()
    p2 = base1 / "app2"
    p2.mkdir()

    monkeypatch.setattr(registry, "paths", lambda: [p1, p2])

    res = client.get("/api/browse/frecuentes")
    assert res.status_code == 200
    roots = res.json()["roots"]
    assert str(base1) in roots











def test_share_rechaza_el_puerto_propio(client, monkeypatch):
    """StackHelx no se publica a si mismo.

    Detras de ese puerto esta la API que corre los comandos de stack.yaml, o
    sea ejecucion arbitraria: el token pasa a ser lo unico entre internet y tu
    consola, y el usuario cree que compartio su proyecto.

    El puerto sale del scope ASGI (el socket que se bindeo), no del header
    Host, que lo escribe quien llama y serviria para esquivar la guarda. Con
    TestClient el scope dice 80, asi que ese es el puerto propio aca.
    """
    llamadas = []
    monkeypatch.setattr(
        server.tunnel,
        "start_tunnel",
        lambda port, provider=None: llamadas.append(port),
    )

    res = client.post("/api/share?port=80")
    assert res.status_code == 400
    assert llamadas == [], "se abrio un tunel hacia el puerto de StackHelx"


def test_share_no_confia_en_el_header_host(client, monkeypatch):
    """Un Host mentido no mueve la guarda ni la abre ni la cierra.

    `request.url.port` sale del header. Si la guarda leyera de ahi, un
    `Host: 127.0.0.1:3000` haria pasar el 80 (el puerto real) y frenaria el
    3000 (uno legitimo). Las dos mitades se afirman.
    """
    abiertos = []
    monkeypatch.setattr(
        server.tunnel,
        "start_tunnel",
        lambda port, provider=None: abiertos.append(port)
        or server.tunnel.Tunnel(
            provider="cloudflared",
            port=port,
            url="https://test-tunnel.trycloudflare.com",
            proc=subprocess.Popen("echo ok", shell=True),
        ),
    )
    cabecera = {"Host": "127.0.0.1:3000"}

    assert client.post("/api/share?port=80", headers=cabecera).status_code == 400
    assert client.post("/api/share?port=3000", headers=cabecera).status_code == 200
    assert abiertos == [3000]
    client.delete("/api/share/3000")


# cache de la vista ---------------------------------------------------------


def test_los_sondeos_siguientes_no_tocan_el_disco(client, proyecto, monkeypatch):
    """La interfaz sondea cada 2.5s y `detect` relee todo el proyecto.

    Medido sobre un proyecto poliglota: 6.7ms por llamada, o sea 242ms de disco
    en cada `/api/state` con doce proyectos y tres pestanas, releyendo archivos
    que casi nunca cambian.

    Se afirma el efecto y no la forma: que la cuenta de lecturas **no crezca
    con la cantidad de sondeos**. Cuantas hace el primero es asunto del
    servidor (hoy son tres, porque ademas del stack de la fila se resuelven el
    mapa de puertos compartidos y el de Docker, los dos ya cacheados en
    `registry`); lo que este test protege es que los siguientes sean gratis.
    """
    lecturas = []
    real = server.detect.stack_for
    monkeypatch.setattr(
        server.detect, "stack_for", lambda p: lecturas.append(str(p)) or real(p)
    )

    for _ in range(4):
        assert client.get("/api/state").status_code == 200
    tras_cuatro = len(lecturas)

    for _ in range(8):
        client.get("/api/state")

    assert len(lecturas) == tras_cuatro, (
        f"doce sondeos costaron {len(lecturas)} lecturas de disco y cuatro costaron "
        f"{tras_cuatro}: el costo sigue atado a la cantidad de sondeos"
    )


def test_arrancar_no_usa_la_version_cacheada(client, proyecto, monkeypatch):
    """`up` lee fresco siempre, y esto no es un detalle.

    Arrancar con un stack cacheado correria los comandos viejos despues de que
    el usuario edito su `stack.yaml`, que es exactamente lo que nadie espera.
    La vista puede estar diez segundos vieja; el arranque no puede estarlo.
    """
    ruta, _puerto = proyecto
    pid = registry.project_id(ruta)
    client.get("/api/state")  # deja el proyecto en el cache de la vista

    lecturas = []
    real = server.detect.stack_for
    monkeypatch.setattr(
        server.detect, "stack_for", lambda p: lecturas.append(str(p)) or real(p)
    )

    assert client.post(f"/api/projects/{pid}/up", json={"profile": None}).status_code == 200
    assert str(ruta) in lecturas, "up sirvio el stack del cache en vez de releer el stack.yaml"
    client.post(f"/api/projects/{pid}/down")


def test_congelar_refresca_la_vista_en_el_acto(client, tmp_path, monkeypatch):
    """Sin invalidar, apretabas "Congelar" y la fila no se enteraba por 10s."""
    raiz = tmp_path / "proyecto-node"
    raiz.mkdir()
    (raiz / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite"}, "dependencies": {"vite": "^5"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(registry, "paths", lambda: [raiz])
    pid = registry.project_id(raiz)

    def vista():
        return client.get("/api/state").json()["projects"][0]

    assert vista()["detected"] is True, "el proyecto tendria que salir detectado"
    assert client.post(f"/api/projects/{pid}/freeze").status_code == 200
    assert vista()["detected"] is False, "la fila siguio sirviendo la version cacheada"


def test_el_cache_de_la_vista_es_por_proyecto(client, tmp_path, monkeypatch):
    """Dos proyectos no comparten entrada: la clave es la ruta.

    Sin esto, el primero en resolverse contestaria por los dos y la interfaz
    mostraria los servicios de un proyecto en la fila del otro.
    """
    raices = []
    for nombre in ("uno", "dos"):
        raiz = tmp_path / nombre
        raiz.mkdir()
        (raiz / "package.json").write_text(
            json.dumps({"scripts": {"dev": "vite"}, "dependencies": {"vite": "^5"}}),
            encoding="utf-8",
        )
        raices.append(raiz)
    monkeypatch.setattr(registry, "paths", lambda: raices)

    vistas = {v["name"]: v for v in client.get("/api/state").json()["projects"]}
    client.get("/api/state")
    de_nuevo = {v["name"]: v for v in client.get("/api/state").json()["projects"]}

    assert set(vistas) == {"uno", "dos"}
    assert de_nuevo["uno"]["path"] == str(raices[0])
    assert de_nuevo["dos"]["path"] == str(raices[1])


def test_drop_project_invalida_el_cache_de_la_vista(client, tmp_path, monkeypatch):
    raiz = tmp_path / "a_borrar"
    raiz.mkdir()
    (raiz / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite"}, "dependencies": {"vite": "^5"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(registry, "paths", lambda: [raiz])
    pid = registry.project_id(raiz)

    client.get("/api/state")
    with server._stack_lock:
        assert str(raiz) in server._stack_seen

    assert client.delete(f"/api/projects/{pid}").status_code == 200
    with server._stack_lock:
        assert str(raiz) not in server._stack_seen


def test_cache_invalidation_race_no_reinyecta_viejo(tmp_path):
    raiz = tmp_path / "concurrente"
    raiz.mkdir()
    (raiz / "Cargo.toml").write_text('[package]\nname = "c"\n[dependencies]\naxum = "0.7"\n', encoding="utf-8")
    (raiz / "src").mkdir()
    (raiz / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")

    server._olvidar_stack(raiz)
    with server._stack_lock:
        server._stack_invalidations[str(raiz)] = 100.0

    clave = str(raiz)
    inicio = 50.0
    stack = detect.stack_for(raiz)
    with server._stack_lock:
        if server._stack_invalidations.get(clave, 0.0) <= inicio:
            server._stack_seen[clave] = (105.0, stack)

    with server._stack_lock:
        assert clave not in server._stack_seen


def test_metrics_badge_has_tooltip_and_cursor_help():
    """El badge de consumo debe proveer tooltip explicativo (title) y cursor help."""
    js = (server.WEB / "app.js").read_text(encoding="utf-8")
    assert "badge.title = `${cpuInfo}\\n${memInfo}`" in js
    assert "núcleos en paralelo" in js
    assert "memoria física residente RSS" in js

    css = (server.WEB / "app.css").read_text(encoding="utf-8")
    assert ".service__metrics" in css
    assert "cursor: help" in css

