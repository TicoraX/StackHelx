"""Logica pura de la interfaz (`web/logic.js`), ejecutada con node de verdad.

No hay navegador en la suite: el JS con decisiones (que error es cada fallo,
que puertos chocan, que dice la confirmacion de matar) vive en `logic.js` sin
DOM justamente para poder afirmarlo aca. Sin node se salta, no se finge.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

LOGIC = Path(__file__).resolve().parent.parent / "stackhelx" / "web" / "logic.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node no esta instalado")


def node_eval(expr: str):
    script = f"const L = require({json.dumps(str(LOGIC))}); console.log(JSON.stringify({expr}));"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(out.stdout)


def test_un_500_no_se_presenta_como_sin_conexion():
    """El bug: `offline · boom` para una respuesta 500 y el cuerpo en "Cargando"."""
    kinds = node_eval(
        "[{status:500}, {status:503}, {status:401}, {status:404}, new TypeError('x'), new SyntaxError('y')]"
        ".map(L.classifyLoadError)"
    )
    assert kinds == ["server", "server", "auth", "request", "offline", "unexpected"]

    server = node_eval("L.loadErrorText('server', 'boom', 'en')")
    offline = node_eval("L.loadErrorText('offline', '', 'en')")
    assert "boom" in server["body"]
    assert "offline" not in server["head"]
    assert offline["head"] == "offline"
    assert node_eval("L.loadErrorText('offline', '', 'es')")["head"] == "sin conexión"


def test_choque_de_puertos_solo_entre_proyectos_distintos():
    """El 3000 de Decepticon y de WineSpa chocan; dos servicios del mismo proyecto no."""
    chocan = node_eval(
        "[...L.portConflicts(["
        "{port:3000, project:'Decepticon'}, {port:3000, project:'WineSpa'},"
        "{port:4000, project:'Decepticon'}, {port:4000, project:'Decepticon'},"
        "{port:5173, project:'a'}, {port:9, project:null}])]"
    )
    assert chocan == [3000]


def test_la_confirmacion_de_matar_nombra_proceso_y_puerto():
    en = node_eval("L.killConfirmText({name:'Orca.exe', pid:20352, port:6768}, 'en')")
    es = node_eval("L.killConfirmText({name:'node.exe', pid:null, port:4391}, 'es')")
    assert "Orca.exe" in en and "20352" in en and "6768" in en
    assert "node.exe" in es and "4391" in es and "pid" not in es
