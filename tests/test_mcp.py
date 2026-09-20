import json
import subprocess
import sys
import textwrap
import time

import pytest

from stackhelx import mcp, ports


@pytest.fixture(autouse=True)
def reset_mcp_state():
    with mcp._action_lock:
        mcp._action_timestamps.clear()
    mcp.clear_telemetry()
    yield
    with mcp._action_lock:
        mcp._action_timestamps.clear()
    mcp.clear_telemetry()


def test_mcp_initialize():
    req = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    res = mcp.handle_request(req)
    assert res["id"] == 1
    assert res["result"]["serverInfo"]["name"] == "stackhelx"
    assert "tools" in res["result"]["capabilities"]


def test_mcp_tools_list():
    req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    res = mcp.handle_request(req)
    tool_names = [t["name"] for t in res["result"]["tools"]]
    assert "stackhelx_status" in tool_names
    assert "stackhelx_doctor" in tool_names
    assert "stackhelx_ports" in tool_names
    assert "stackhelx_free_port" in tool_names
    assert "stackhelx_share" in tool_names
    assert "stackhelx_run" in tool_names
    assert "stackhelx_clean" in tool_names
    assert "stackhelx_history" in tool_names
    assert "stackhelx_init" in tool_names


def _pedir_liberar(port):
    return mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {"name": "stackhelx_free_port", "arguments": {"port": port}},
        }
    )


def test_mcp_free_port_cierra_al_dueno_del_puerto(free_ports):
    """Un proceso real, como el resto de la suite.

    El mock que habia aca nombraba el parametro `port` y devolvia True, y la
    firma de verdad es `kill(pid, create_time, ...)` y no devuelve nada. O sea
    que el test afirmaba un mensaje que el codigo real no podia producir, y
    tapaba que se le pasaba el numero de puerto en el lugar del pid: pedir
    liberar el 3000 mataba al proceso 3000.
    """
    (port,) = free_ports(1)
    code = (
        "import socket, time; s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); "
        f"s.bind(('127.0.0.1', {port})); s.listen(); time.sleep(60)"
    )
    proc = subprocess.Popen([sys.executable, "-c", code])
    try:
        limite = time.time() + 10
        while time.time() < limite and ports.is_free(port):
            time.sleep(0.1)
        dueno = ports.scan(port)
        assert dueno.pid is not None, "el proceso nunca tomo el puerto"

        res = _pedir_liberar(port)

        assert res["result"].get("isError") is not True, res["result"]
        assert "liberado" in res["result"]["content"][0]["text"]
        assert str(dueno.pid) in res["result"]["content"][0]["text"]

        limite = time.time() + 10
        while time.time() < limite and not ports.is_free(port):
            time.sleep(0.1)
        assert ports.is_free(port), "el puerto siguio ocupado"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_mcp_free_port_con_el_puerto_libre_no_mata_nada(free_ports, monkeypatch):
    """Nadie escucha: no hay a quien cerrar, y `kill` no se llama."""
    (port,) = free_ports(1)
    llamadas = []
    monkeypatch.setattr(mcp.ports, "kill", lambda *a, **k: llamadas.append((a, k)))

    res = _pedir_liberar(port)

    assert "ya esta libre" in res["result"]["content"][0]["text"]
    assert llamadas == []


def test_mcp_tool_call_share(tmp_path, monkeypatch):
    (tmp_path / "stack.yaml").write_text("services:\n  web:\n    command: echo web\n    port: 3000\n", encoding="utf-8")
    monkeypatch.setattr(
        mcp.tunnel,
        "start_tunnel",
        lambda port, provider=None: mcp.tunnel.Tunnel(
            provider="cloudflared",
            port=port,
            url="https://ai-tunnel.trycloudflare.com",
            proc=None,
        ),
    )
    req = {
        "jsonrpc": "2.0",
        "id": 11,
        "method": "tools/call",
        "params": {
            "name": "stackhelx_share",
            "arguments": {"port": 3000, "path": str(tmp_path)},
        },
    }
    res = mcp.handle_request(req)
    assert res["result"].get("isError") is not True, res["result"]
    assert "https://ai-tunnel.trycloudflare.com" in res["result"]["content"][0]["text"]


def test_mcp_unknown_method():
    req = {"jsonrpc": "2.0", "id": 5, "method": "unsupported", "params": {}}
    res = mcp.handle_request(req)
    assert res["error"]["code"] == -32601


def test_mcp_tool_call_status(tmp_path):
    body = """
    name: mcp-test
    services:
      web:
        command: echo web
        port: 3000
    scripts:
      test: echo ok
    """
    (tmp_path / "stack.yaml").write_text(textwrap.dedent(body), encoding="utf-8")

    req = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "stackhelx_status",
            "arguments": {"path": str(tmp_path)},
        },
    }
    res = mcp.handle_request(req)
    assert res["id"] == 3
    assert res["result"].get("isError") is not True, res["result"]
    content = json.loads(res["result"]["content"][0]["text"])
    assert content["project_name"] == "mcp-test"
    assert content["services"][0]["name"] == "web"
    assert content["services"][0]["port"] == 3000
    assert "test" in content["scripts"]


def test_mcp_tool_call_run(tmp_path):
    flag = tmp_path / "mcp_flag.txt"
    body = f"""
    name: mcp-run-test
    services:
      web:
        command: echo web
    scripts:
      touch: python -c "import pathlib; pathlib.Path(r'{flag}').write_text('mcp_ok')"
    """
    (tmp_path / "stack.yaml").write_text(textwrap.dedent(body), encoding="utf-8")

    req = {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {
            "name": "stackhelx_run",
            "arguments": {"script": "touch", "path": str(tmp_path)},
        },
    }
    res = mcp.handle_request(req)
    assert res["result"].get("isError") is not True, res["result"]
    assert "finalizado con código de salida 0" in res["result"]["content"][0]["text"]
    assert flag.exists()
    assert flag.read_text() == "mcp_ok"



def test_un_script_ruidoso_no_ensucia_el_protocolo(tmp_path):
    """Un `echo` adentro de un script se colaba entre dos respuestas JSON-RPC.

    Proceso real y no una llamada a `handle_request`: el problema vive en el
    descriptor 1, que el subproceso del script hereda, y eso no se ve desde el
    proceso del test.
    """
    (tmp_path / "stack.yaml").write_text(
        "services:\n  web:\n    command: echo web\n"
        "scripts:\n  ruidoso: echo RUIDO-EN-STDOUT\n",
        encoding="utf-8",
    )
    peticiones = (
        "\n".join(
            json.dumps(r)
            for r in (
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "stackhelx_run",
                        "arguments": {"script": "ruidoso", "path": str(tmp_path)},
                    },
                },
            )
        )
        + "\n"
    )

    done = subprocess.run(
        [sys.executable, "-m", "stackhelx.cli", "mcp"],
        input=peticiones,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=60,
        cwd=tmp_path,
    )

    lineas = [line for line in done.stdout.splitlines() if line.strip()]
    for linea in lineas:
        json.loads(linea)  # cada linea de stdout tiene que ser una respuesta valida
    assert [json.loads(line)["id"] for line in lineas] == [1, 2]
    assert "RUIDO-EN-STDOUT" not in done.stdout, "la salida del script llego al protocolo"
    assert "RUIDO-EN-STDOUT" in done.stderr, "la salida del script tiene que ir a stderr"


def _pedir_clean(argumentos):
    return mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 20,
            "method": "tools/call",
            "params": {"name": "stackhelx_clean", "arguments": argumentos},
        }
    )


def test_mcp_clean_se_niega_a_borrar_volumenes(monkeypatch):
    """El CLI y la interfaz preguntan antes; el MCP no tiene donde preguntar.

    El resto del prune se regenera solo. Un volumen tiene la base de datos del
    proyecto adentro y no vuelve, asi que un agente no lo borra: lo hace el
    usuario, con el comando que pregunta.
    """
    llamadas = []
    monkeypatch.setattr(mcp.docker, "prune", lambda targets: llamadas.append(list(targets)) or (True, "ok"))

    res = _pedir_clean({"volumes": True})

    assert res["result"]["isError"] is True
    assert "no se hace desde un agente" in res["result"]["content"][0]["text"]
    assert llamadas == [], "llego a llamar al prune con volumes"


def test_mcp_clean_sin_volumenes_limpia(monkeypatch):
    llamadas = []
    monkeypatch.setattr(mcp.docker, "prune", lambda targets: llamadas.append(list(targets)) or (True, "ok"))

    res = _pedir_clean({})

    assert res["result"].get("isError") is not True
    assert llamadas == [list(mcp.docker.DEFAULT_TARGETS)]


def test_mcp_clean_no_le_ofrece_volumes_al_agente():
    """El esquema es lo que el agente lee para decidir que puede pedir."""
    tools = mcp.handle_request(
        {"jsonrpc": "2.0", "id": 21, "method": "tools/list", "params": {}}
    )["result"]["tools"]
    clean = next(t for t in tools if t["name"] == "stackhelx_clean")
    assert "volumes" not in clean["inputSchema"].get("properties", {})


def test_mcp_no_contesta_ninguna_notificacion():
    """JSON-RPC 2.0: una notificacion es una peticion sin `id`, y no se contesta.

    Se reconocia una sola por nombre (`notifications/initialized`), asi que las
    demas caian al final y se llevaban una respuesta de error con `id: null`,
    que es justo lo que el protocolo prohibe. Un cliente que espera silencio
    puede tratar esa respuesta huerfana como un error de sesion.
    """
    for method in (
        "notifications/initialized",
        "notifications/cancelled",
        "notifications/progress",
        "notifications/lo/que/venga",
    ):
        req = {"jsonrpc": "2.0", "method": method, "params": {}}
        assert mcp.handle_request(req) is None, method


def test_mcp_una_peticion_con_id_si_se_contesta():
    """La otra mitad: sin esto, `if "id" not in req` podria callar todo."""
    res = mcp.handle_request({"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
    assert res is not None
    assert res["id"] == 7


def test_mcp_tool_call_doctor(tmp_path):
    """Prueba que stackhelx_doctor ejecuta los checks sin crash."""
    req = {
        "jsonrpc": "2.0",
        "id": 30,
        "method": "tools/call",
        "params": {"name": "stackhelx_doctor", "arguments": {"path": str(tmp_path)}},
    }
    res = mcp.handle_request(req)
    assert res["result"].get("isError") is not True, res["result"]
    text = res["result"]["content"][0]["text"]
    assert "[OK]" in text or "[WARN]" in text or "[FAIL]" in text


def test_mcp_tool_call_history(tmp_path, monkeypatch):
    """Prueba que stackhelx_history lee la telemetria."""
    monkeypatch.setattr(mcp.registry, "HOME", tmp_path)
    pid = mcp.registry.project_id(tmp_path)
    mcp.history.append(pid, {"event": "test", "duration_s": 1.5})

    req = {
        "jsonrpc": "2.0",
        "id": 31,
        "method": "tools/call",
        "params": {"name": "stackhelx_history", "arguments": {"path": str(tmp_path)}},
    }
    res = mcp.handle_request(req)
    assert res["result"].get("isError") is not True, res["result"]
    data = json.loads(res["result"]["content"][0]["text"])
    assert data["project_id"] == pid
    assert len(data["entries"]) >= 1
    assert data["entries"][0]["event"] == "test"


def test_mcp_tool_call_ports(free_ports):
    """Prueba que stackhelx_ports retorna lista de puertos formateada."""
    (port,) = free_ports(1)
    req = {
        "jsonrpc": "2.0",
        "id": 32,
        "method": "tools/call",
        "params": {"name": "stackhelx_ports", "arguments": {"ports": [port]}},
    }
    res = mcp.handle_request(req)
    assert res["result"].get("isError") is not True, res["result"]
    data = json.loads(res["result"]["content"][0]["text"])
    assert len(data) == 1
    assert data[0]["port"] == port
    assert data[0]["free"] is True


def test_mcp_share_rechaza_puerto_ajeno(tmp_path):
    (tmp_path / "stack.yaml").write_text("services:\n  web:\n    command: echo web\n    port: 3000\n", encoding="utf-8")
    req = {
        "jsonrpc": "2.0",
        "id": 33,
        "method": "tools/call",
        "params": {"name": "stackhelx_share", "arguments": {"port": 5432, "path": str(tmp_path)}},
    }
    res = mcp.handle_request(req)
    assert res["result"].get("isError") is True
    assert "no pertenece a los puertos declarados" in res["result"]["content"][0]["text"]


def test_mcp_action_budget_limit(monkeypatch):
    with mcp._action_lock:
        mcp._action_timestamps.clear()

    # Simular que se consumieron todas las acciones de la ventana
    now = time.monotonic()
    with mcp._action_lock:
        mcp._action_timestamps.extend([now] * mcp._MAX_ACTIONS_PER_WINDOW)

    req = {
        "jsonrpc": "2.0",
        "id": 34,
        "method": "tools/call",
        "params": {"name": "stackhelx_ports", "arguments": {"ports": [8080]}},
    }
    res = mcp.handle_request(req)
    assert res["result"].get("isError") is True
    assert "Límite de acciones MCP excedido" in res["result"]["content"][0]["text"]

    # Limpiar estado
    with mcp._action_lock:
        mcp._action_timestamps.clear()


def test_mcp_telemetry_records_success(tmp_path):
    mcp.clear_telemetry()
    (tmp_path / "stack.yaml").write_text("services:\n  api:\n    command: echo api\n", encoding="utf-8")
    req = {
        "jsonrpc": "2.0",
        "id": 101,
        "method": "tools/call",
        "params": {"name": "stackhelx_status", "arguments": {"path": str(tmp_path)}},
    }
    res = mcp.handle_request(req)
    assert res["result"].get("isError") is not True

    data = mcp.get_telemetry()
    assert data["total_calls"] == 1
    assert data["by_tool"].get("stackhelx_status") == 1
    assert len(data["recent_events"]) == 1
    event = data["recent_events"][0]
    assert event["tool"] == "stackhelx_status"
    assert event["status"] == "ok"
    assert event["duration_ms"] >= 0
    assert "timestamp" in event


def test_mcp_telemetry_records_error_and_rate_limit():
    mcp.clear_telemetry()
    # 1. Error de herramienta desconocida
    req_err = {
        "jsonrpc": "2.0",
        "id": 102,
        "method": "tools/call",
        "params": {"name": "non_existent_tool", "arguments": {}},
    }
    res_err = mcp.handle_request(req_err)
    assert res_err["result"].get("isError") is True

    data = mcp.get_telemetry()
    assert data["total_calls"] == 1
    assert data["recent_events"][0]["status"] == "error"

    # 2. Rate limit
    with mcp._action_lock:
        mcp._action_timestamps.extend([time.monotonic()] * mcp._MAX_ACTIONS_PER_WINDOW)

    req_limit = {
        "jsonrpc": "2.0",
        "id": 103,
        "method": "tools/call",
        "params": {"name": "stackhelx_ports", "arguments": {"ports": [80]}},
    }
    res_limit = mcp.handle_request(req_limit)
    assert res_limit["result"].get("isError") is True

    data_limit = mcp.get_telemetry()
    assert data_limit["total_calls"] == 2
    assert data_limit["recent_events"][0]["status"] == "rate_limited"

    with mcp._action_lock:
        mcp._action_timestamps.clear()



