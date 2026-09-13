import os
import sys
import textwrap

import pytest

from stackhelx import config, scripts
from stackhelx.config import ConfigError


def test_scripts_parsing_simple_and_pipeline(tmp_path):
    body = """
    services:
      srv:
        command: echo srv
    scripts:
      test: pytest tests/
      lint: ruff check .
      check: [lint, test]
    """
    path = tmp_path / "stack.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    stack = config.load(path)

    assert stack.scripts["test"] == ("pytest tests/",)
    assert stack.scripts["lint"] == ("ruff check .",)
    assert stack.scripts["check"] == ("lint", "test")


def test_scripts_parsing_invalido(tmp_path):
    path = tmp_path / "stack.yaml"

    # scripts no es un mapa
    path.write_text("services:\n  srv:\n    command: echo\nscripts: [1, 2]", encoding="utf-8")
    with pytest.raises(ConfigError, match="'scripts' debe ser un mapa"):
        config.load(path)

    # script vacio
    path.write_text("services:\n  srv:\n    command: echo\nscripts:\n  vacio: ''", encoding="utf-8")
    with pytest.raises(ConfigError, match="no puede estar vacio"):
        config.load(path)


def test_run_script_exitoso(tmp_path):
    flag = tmp_path / "flag.txt"
    body = f"""
    services:
      srv:
        command: echo srv
    scripts:
      create: {sys.executable} -c "import pathlib; pathlib.Path(r'{flag}').write_text('ok')"
    """
    path = tmp_path / "stack.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    stack = config.load(path)

    code = scripts.run_script(stack, "create")
    assert code == 0
    assert flag.exists()
    assert flag.read_text() == "ok"


def test_run_script_con_extra_args(tmp_path):
    out = tmp_path / "args.txt"
    body = f"""
    services:
      srv:
        command: echo srv
    scripts:
      echo_args: {sys.executable} -c "import sys, pathlib; pathlib.Path(r'{out}').write_text(' '.join(sys.argv[1:]))"
    """
    path = tmp_path / "stack.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    stack = config.load(path)

    code = scripts.run_script(stack, "echo_args", extra_args=["--foo", "bar"])
    assert code == 0
    assert out.exists()
    assert out.read_text() == "--foo bar"


def test_run_script_falla_y_aborta_pipeline(tmp_path):
    step2_flag = tmp_path / "step2.txt"
    body = f"""
    services:
      srv:
        command: echo srv
    scripts:
      failing_pipeline:
        - {sys.executable} -c "raise SystemExit(3)"
        - {sys.executable} -c "import pathlib; pathlib.Path(r'{step2_flag}').write_text('reached')"
    """
    path = tmp_path / "stack.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    stack = config.load(path)

    code = scripts.run_script(stack, "failing_pipeline")
    assert code == 3
    assert not step2_flag.exists()


def test_run_script_desconocido(tmp_path):
    body = """
    services:
      srv:
        command: echo srv
    scripts:
      test: echo test
    """
    path = tmp_path / "stack.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    stack = config.load(path)

    with pytest.raises(ConfigError, match="script desconocido: 'inexistente'"):
        scripts.run_script(stack, "inexistente")


def _stack_con_scripts(tmp_path, bloque):
    # Sin dedent: el bloque llega multilinea y ya trae su propia sangria, que es
    # justo lo que dedent no sabe combinar con la del literal.
    (tmp_path / "stack.yaml").write_text(
        "services:\n  web:\n    command: echo web\nscripts:\n" + bloque + "\n",
        encoding="utf-8",
    )
    return config.load(tmp_path / "stack.yaml")


def test_un_pipeline_encadena_los_scripts_que_nombra(tmp_path):
    """`check: [lint, test]` es lo que documenta docs/stack-yaml.md.

    Corria `lint` como si fuera un binario del PATH y moria en el paso 1 con
    "no se reconoce como comando": cada item se trataba como comando literal y
    ninguno se resolvia contra los demas scripts.

    Se afirma sobre archivos y no sobre la salida capturada. `run_script`
    imprime el comando antes de correrlo (`$ echo LINT`), asi que buscar "LINT"
    en `capsys` daba verde con el eco del comando y sin que el subproceso
    hubiera corrido nunca. Y `capsys` ni siquiera ve la salida de un subproceso,
    que sale por el descriptor del sistema. Un archivo que aparece solo puede
    haberlo creado el proceso.
    """
    stack = _stack_con_scripts(
        tmp_path,
        "  lint: echo x > hizo_lint.txt\n"
        "  test: echo x > hizo_test.txt\n"
        "  check: [lint, test]",
    )
    assert scripts.resolve(stack, "check") == ["echo x > hizo_lint.txt", "echo x > hizo_test.txt"]
    assert scripts.run_script(stack, "check") == 0
    assert (tmp_path / "hizo_lint.txt").is_file(), "el paso 1 del pipeline no corrio"
    assert (tmp_path / "hizo_test.txt").is_file(), "el paso 2 del pipeline no corrio"


def test_un_pipeline_anidado_se_aplana(tmp_path):
    stack = _stack_con_scripts(
        tmp_path,
        "  a: echo A\n  b: echo B\n  ab: [a, b]\n  todo: [ab, a]",
    )
    assert scripts.resolve(stack, "todo") == ["echo A", "echo B", "echo A"]


def test_un_comando_literal_no_se_confunde_con_una_referencia(tmp_path):
    """`pytest -v` no es el nombre de ningun script y tiene que quedar tal cual."""
    stack = _stack_con_scripts(tmp_path, "  test: pytest -v\n  suite: [test]")
    assert scripts.resolve(stack, "suite") == ["pytest -v"]


def test_un_ciclo_entre_scripts_se_detecta(tmp_path):
    stack = _stack_con_scripts(tmp_path, "  a: [b]\n  b: [a]")
    with pytest.raises(config.ConfigError, match="ciclo entre scripts"):
        scripts.resolve(stack, "a")


def test_un_argumento_extra_no_ejecuta_un_segundo_comando(tmp_path):
    """Los extra_args iban con un `" ".join` y esto corre con `shell=True`.

    Desde el CLI el argumento lo escribe el usuario, pero `portmaster_run` los
    recibe de un agente de IA: un separador adentro de un argumento ejecutaba lo
    que viniera despues.

    El separador es el de cada shell. `cmd.exe` no parte con `;` sino con `&`, y
    tampoco respeta comillas simples, que es por lo que `shlex.join` a secas no
    alcanzaba en Windows.
    """
    stack = _stack_con_scripts(tmp_path, "  saluda: echo")
    separador = "&" if os.name == "nt" else ";"
    argumento = f"hola {separador} echo x > {tmp_path / 'inyectado.txt'}"

    # Por el directorio y no por un nombre puntual: con un entrecomillado a
    # medias la redireccion igual corre y crea el archivo con la comilla pegada
    # al nombre. Preguntar por `inyectado.txt` daba verde con la inyeccion hecha.
    antes = set(tmp_path.iterdir())
    assert scripts.run_script(stack, "saluda", extra_args=[argumento]) == 0
    nuevos = set(tmp_path.iterdir()) - antes

    assert not nuevos, f"el shell ejecuto lo que venia despues de '{separador}': {nuevos}"
