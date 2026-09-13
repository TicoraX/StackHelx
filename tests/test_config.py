import textwrap

import pytest

from stackhelx import config

VALID = """
name: demo

services:
  db:
    command: docker compose up -d postgres
    port: 5432
    detached: true
  api:
    command: npm run dev
    cwd: backend
    port: 8080
    needs: [db]
    env:
      DATABASE_URL: postgres://localhost:5432/app
      WORKERS: 4
  web:
    command: npm run dev
    port: 3000
    needs: [api]

profiles:
  backend: [api]
  db-only: [db]
"""


def write(tmp_path, body, **dirs):
    (tmp_path / "backend").mkdir(exist_ok=True)
    for name in dirs:
        (tmp_path / name).mkdir(exist_ok=True)
    path = tmp_path / "stack.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_carga_completa(tmp_path):
    stack = config.load(write(tmp_path, VALID))

    assert stack.name == "demo"
    assert set(stack.services) == {"db", "api", "web"}
    assert stack.ports() == [5432, 8080, 3000]

    api = stack.services["api"]
    assert api.cwd == (tmp_path / "backend").resolve()
    assert api.needs == ("db",)
    assert api.env == {"DATABASE_URL": "postgres://localhost:5432/app", "WORKERS": "4"}
    assert api.ready == "port"       # default cuando hay puerto
    assert stack.services["db"].detached is True


def test_orden_topologico(tmp_path):
    stack = config.load(write(tmp_path, VALID))
    assert [s.name for s in stack.resolve()] == ["db", "api", "web"]


def test_perfil_arrastra_dependencias(tmp_path):
    stack = config.load(write(tmp_path, VALID))
    # El perfil lista solo 'api', pero sin 'db' arriba no sirve de nada.
    assert [s.name for s in stack.resolve("backend")] == ["db", "api"]
    assert [s.name for s in stack.resolve("db-only")] == ["db"]


def test_perfil_desconocido(tmp_path):
    stack = config.load(write(tmp_path, VALID))
    with pytest.raises(config.ConfigError, match="perfil desconocido"):
        stack.resolve("noexiste")


def test_ciclo_falla_al_cargar(tmp_path):
    body = """
    services:
      a:
        command: echo a
        needs: [b]
      b:
        command: echo b
        needs: [a]
    """
    with pytest.raises(config.ConfigError, match="circular"):
        config.load(write(tmp_path, body))


def test_cwd_no_puede_escapar_de_la_raiz(tmp_path):
    body = """
    services:
      api:
        command: echo hola
        cwd: ../../etc
    """
    with pytest.raises(config.ConfigError, match="sale de la raiz"):
        config.load(write(tmp_path, body))


def test_cwd_absoluto_rechazado(tmp_path):
    body = f"""
    services:
      api:
        command: echo hola
        cwd: {tmp_path.as_posix()}
    """
    with pytest.raises(config.ConfigError, match="relativa"):
        config.load(write(tmp_path, body))


def test_cwd_inexistente(tmp_path):
    body = """
    services:
      api:
        command: echo hola
        cwd: no-existe
    """
    with pytest.raises(config.ConfigError, match="no existe"):
        config.load(write(tmp_path, body))


@pytest.mark.parametrize(
    "body, mensaje",
    [
        ("services: {}", "services"),
        ("nombre: sin-servicios", "services"),
        ("services:\n  api:\n    port: 8080", "command"),
        ("services:\n  api:\n    command: ''", "command"),
        ("services:\n  api:\n    command: x\n    port: 70000", "port"),
        ("services:\n  api:\n    command: x\n    ready: port", "no hay 'port'"),
        ("services:\n  api:\n    command: x\n    ready: raro", "ready"),
        ("services:\n  api:\n    command: x\n    needs: [fantasma]", "no existe"),
        ("services:\n  api:\n    command: x\n    needs: [api]", "a si mismo"),
        ("services:\n  api:\n    command: x\n    detached: quizas", "detached"),
        ("services:\n  api:\n    command: x\n    stop: ''", "stop"),
        ("services:\n  api:\n    command: x\n    stop: 3", "stop"),
        ("services:\n  api:\n    command: x\n    typo: 1", "desconocidos"),
        ("services:\n  api:\n    command: x\n    env:\n      A: [1, 2]", "valor simple"),
        ("services:\n  api:\n    command: x\nprofiles:\n  p: [fantasma]", "no existe"),
        ("services:\n  api:\n    command: x\nprofiles:\n  p: []", "no vacia"),
        ("- solo\n- una\n- lista", "mapa"),
    ],
)
def test_configuraciones_invalidas(tmp_path, body, mensaje):
    with pytest.raises(config.ConfigError, match=mensaje):
        config.load(write(tmp_path, body))


def test_stop_es_opcional(tmp_path):
    stack = config.load(
        write(
            tmp_path,
            """
            services:
              db:
                command: docker compose up -d db
                detached: true
                stop: docker compose stop db
              api:
                command: npm run dev
            """,
        )
    )
    assert stack.services["db"].stop == "docker compose stop db"
    assert stack.services["api"].stop is None


def test_yaml_roto(tmp_path):
    with pytest.raises(config.ConfigError, match="no se pudo leer"):
        config.load(write(tmp_path, "services: [a: ]]"))


def test_find_sube_directorios(tmp_path):
    write(tmp_path, VALID)
    hondo = tmp_path / "backend" / "src" / "app"
    hondo.mkdir(parents=True)
    assert config.find(hondo) == (tmp_path / "stack.yaml").resolve()


def test_find_sin_archivo(tmp_path):
    with pytest.raises(config.ConfigError, match="no se encontro"):
        config.find(tmp_path)


def test_el_ejemplo_del_repo_es_valido(tmp_path):
    """stack.example.yaml es la especificacion publicada: tiene que cargar."""
    from pathlib import Path

    ejemplo = Path(__file__).parent.parent / "stack.example.yaml"
    (tmp_path / "backend").mkdir()
    (tmp_path / "frontend").mkdir()
    destino = tmp_path / "stack.yaml"
    destino.write_text(ejemplo.read_text(encoding="utf-8"), encoding="utf-8")

    stack = config.load(destino)
    assert [s.name for s in stack.resolve("fullstack")] == ["db", "api", "web"]


def test_env_file_y_hooks(tmp_path):
    (tmp_path / ".env").write_text("DB_PASS=secret\nPORT=5432\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text("DEBUG=true\n", encoding="utf-8")

    body = """
    services:
      api:
        command: npm run dev
        env_file: [.env, .env.local]
        pre_start: npm run build
        post_start: npm run seed
      worker:
        command: python worker.py
        env_file: .env
    """
    stack = config.load(write(tmp_path, body))
    api = stack.services["api"]
    worker = stack.services["worker"]

    assert api.env_file == ((tmp_path / ".env").resolve(), (tmp_path / ".env.local").resolve())
    assert api.pre_start == "npm run build"
    assert api.post_start == "npm run seed"

    assert worker.env_file == ((tmp_path / ".env").resolve(),)
    assert worker.pre_start is None
    assert worker.post_start is None


def test_parse_env_file(tmp_path):
    env_file = tmp_path / "test.env"
    env_file.write_text(
        textwrap.dedent("""
        # Comentario
        API_KEY=12345
        export DB_HOST="localhost"
        SECRET_TOKEN='super-secret'
        WITH_INLINE=valor # comentario inline
        EMPTY=
        """),
        encoding="utf-8",
    )
    parsed = config.parse_env_file(env_file)
    assert parsed["API_KEY"] == "12345"
    assert parsed["DB_HOST"] == "localhost"
    assert parsed["SECRET_TOKEN"] == "super-secret"
    assert parsed["WITH_INLINE"] == "valor"
    assert parsed["EMPTY"] == ""
    assert "# Comentario" not in parsed


def test_env_file_fuera_de_raiz(tmp_path):
    body = """
    services:
      api:
        command: echo hola
        env_file: ../../.env
    """
    with pytest.raises(config.ConfigError, match="sale de la raiz"):
        config.load(write(tmp_path, body))


def test_includes_combinar_servicios(tmp_path):
    sub = tmp_path / "subproject"
    sub.mkdir()
    sub_body = """
    services:
      db:
        command: echo db
        port: 5432
    """
    write(sub, sub_body)

    main_body = """
    includes:
      - subproject
    services:
      api:
        command: echo api
        port: 8080
        needs: [db]
    """
    stack = config.load(write(tmp_path, main_body))
    assert "db" in stack.services
    assert "api" in stack.services
    assert stack.services["api"].needs == ("db",)
    resolved = stack.resolve()
    assert [s.name for s in resolved] == ["db", "api"]


def test_includes_ciclo_detectado(tmp_path):
    p1 = tmp_path / "proj1"
    p2 = tmp_path / "proj2"
    p1.mkdir()
    p2.mkdir()

    write(p1, "includes: [../proj2]\nservices:\n  s1:\n    command: echo s1")
    write(p2, "includes: [../proj1]\nservices:\n  s2:\n    command: echo s2")

    with pytest.raises(config.ConfigError, match="ciclo de inclusion"):
        config.load(p1 / "stack.yaml")


def test_includes_conflicto_nombre_servicio(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    write(sub, "services:\n  api:\n    command: echo api2")
    main = write(tmp_path, "includes: [sub]\nservices:\n  api:\n    command: echo api1")

    with pytest.raises(config.ConfigError, match="conflicto de servicio"):
        config.load(main)




def test_un_env_con_bytes_invalidos_no_tumba_el_arranque(tmp_path):
    """UnicodeDecodeError no es un OSError, y solo se atrapaba OSError.

    Un .env guardado en latin-1, que es lo que deja cualquier editor viejo en
    Windows, hacia reventar el arranque entero del stack en vez de quedarse sin
    esas variables.
    """
    archivo = tmp_path / ".env"
    archivo.write_bytes(b"CLAVE=valor\nACENTO=caf\xe9\n")
    assert config.parse_env_file(archivo) == {}


def _con_url(tmp_path, valor):
    (tmp_path / "stack.yaml").write_text(
        textwrap.dedent(f"""
            name: demo
            services:
              web:
                command: echo hola
                port: 3000
                url: {valor}
            """),
        encoding="utf-8",
    )
    return config.load(tmp_path / "stack.yaml")


def test_url_se_carga_y_llega_al_servicio(tmp_path):
    stack = _con_url(tmp_path, "http://localhost:3000/admin")
    assert stack.services["web"].url == "http://localhost:3000/admin"


def test_sin_url_el_servicio_queda_en_none(tmp_path):
    (tmp_path / "stack.yaml").write_text(
        "name: d\nservices:\n  web:\n    command: echo\n    port: 3000\n", encoding="utf-8"
    )
    assert config.load(tmp_path / "stack.yaml").services["web"].url is None


def test_una_url_con_esquema_javascript_no_carga(tmp_path):
    """El `href` termina en el origen de la interfaz, que tiene el token en una
    cookie. No es la barrera principal —un stack.yaml ya ejecuta comandos— pero
    un esquema que no sea http no tiene ningun uso legitimo aca.
    """
    with pytest.raises(config.ConfigError, match="http://"):
        _con_url(tmp_path, "'javascript:fetch(1)'")


def test_una_url_sin_esquema_no_carga(tmp_path):
    """`127.0.0.1:8765` es el error de tipeo mas probable y el navegador lo
    interpreta como ruta relativa: falla lejos de la causa. Que falle al cargar.
    """
    with pytest.raises(config.ConfigError, match="http://"):
        _con_url(tmp_path, "127.0.0.1:8765")


def test_una_url_vacia_no_carga(tmp_path):
    with pytest.raises(config.ConfigError, match="texto no vacio"):
        _con_url(tmp_path, "'   '")


def test_env_file_desentrecomilla_aunque_haya_comentario_detras(tmp_path):
    """Se intentaba quitar las comillas antes de sacar el comentario.

    Con un comentario detras, el valor ya no TERMINA en comilla, asi que la rama
    de desentrecomillado no corria y `TOKEN="abc" # el de prod` entregaba el
    token con las comillas pegadas. Un token asi no falla al arrancar: falla en
    la primera peticion autenticada, que es donde cuesta encontrarlo.
    """
    archivo = tmp_path / ".env"
    archivo.write_text(
        'A="valor con espacio"  # comentario\n'
        "B='comilla simple' # otro\n"
        'C="tiene # adentro"\n'
        "D=pelado # comentario\n"
        'E="sin cerrar\n',
        encoding="utf-8",
    )

    valores = config.parse_env_file(archivo)
    assert valores["A"] == "valor con espacio"
    assert valores["B"] == "comilla simple"
    # El `#` de adentro es parte del valor: sacar el comentario primero lo rompe.
    assert valores["C"] == "tiene # adentro"
    assert valores["D"] == "pelado"
    # Un .env roto se deja como esta: no hay nada que adivinar.
    assert valores["E"] == '"sin cerrar'
