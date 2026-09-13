"""Deteccion sobre directorios reales, y descubrimiento del puerto sobre un
proceso real que escucha. Lo mismo que el resto de la suite: nada de mocks."""

import builtins
import io
import json
import os
import sys
import textwrap

import pytest
from rich.console import Console

from stackhelx import config, detect, ports, runner


def write(root, name, body=""):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_importa_tomli_si_falta_tomllib(monkeypatch):
    class FakeTomli:
        class TOMLDecodeError(ValueError):
            pass

        @staticmethod
        def loads(_text):
            return {}

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "tomllib":
            raise ModuleNotFoundError("No module named 'tomllib'")
        if name == "tomli":
            return FakeTomli
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    source = open(detect.__file__, encoding="utf-8").read()
    namespace = {"__name__": "stackhelx._detect_import_probe", "__package__": "stackhelx", "__file__": detect.__file__}
    exec(compile(source, detect.__file__, "exec"), namespace)

    assert namespace["tomllib"] is FakeTomli


def test_sin_nada_conocido_no_detecta(tmp_path):
    write(tmp_path, "README.md", "hola")
    assert detect.detect(tmp_path) is None


def test_node_usa_dev_y_el_gestor_del_lockfile(tmp_path):
    write(tmp_path, "package.json", json.dumps({"scripts": {"dev": "vite", "start": "x"}}))
    write(tmp_path, "pnpm-lock.yaml", "lockfileVersion: 9")

    stack = detect.detect(tmp_path)
    assert stack.detected
    assert list(stack.services) == ["web"]
    web = stack.services["web"]
    assert web.command == "pnpm run dev"
    assert web.port is None
    assert web.ready == "listen"


def test_node_cae_a_start_y_a_npm(tmp_path):
    write(tmp_path, "package.json", json.dumps({"scripts": {"start": "next start"}}))
    assert detect.detect(tmp_path).services["web"].command == "npm run start"


def test_package_json_roto_no_revienta(tmp_path):
    write(tmp_path, "package.json", "{ esto no es json")
    assert detect.detect(tmp_path) is None


@pytest.mark.parametrize(
    "entrada, esperado",
    [
        ('["8080:80"]', 8080),
        ('["127.0.0.1:8080:80"]', 8080),
        ('[{"published": 8080, "target": 80}]', 8080),
        ('[{"published": "8080", "target": 80}]', 8080),
        ('["80"]', None),  # host aleatorio, no hay nada que liberar
        ('["8000-8010:80"]', None),  # rango
        ("[]", None),
    ],
)
def test_puerto_publicado_del_compose(tmp_path, entrada, esperado):
    write(tmp_path, "compose.yaml", f'services:\n  db:\n    image: postgres\n    ports: {entrada}\n')
    assert detect.detect(tmp_path).services["db"].port == esperado


def test_un_servicio_por_contenedor(tmp_path):
    write(
        tmp_path,
        "docker-compose.yml",
        """
        services:
          db:
            image: postgres
            ports: ['5433:5432']
          api:
            build: ./api
            ports: ['3100:3100']
            depends_on:
              db:
                condition: service_healthy
          web:
            build: ./web
            ports: ['8080:80']
            depends_on: [api]
        """,
    )

    stack = detect.detect(tmp_path)

    assert [s.name for s in stack.resolve()] == ["db", "api", "web"]
    assert stack.services["web"].command == "docker compose up -d web"
    assert stack.services["web"].port == 8080
    assert stack.services["api"].needs == ("db",)
    assert stack.services["web"].needs == ("api",)
    assert all(s.detached for s in stack.services.values())


def test_cada_contenedor_trae_su_apagado(tmp_path):
    write(tmp_path, "compose.yaml", "services:\n  db:\n    image: postgres\n")
    assert detect.detect(tmp_path).services["db"].stop == "docker compose stop db"


def test_compose_sin_servicios_apaga_todo(tmp_path):
    write(tmp_path, "docker-compose.yml", "name: solo-un-nombre\n")
    assert detect.detect(tmp_path).services["docker"].stop == "docker compose stop"


def test_un_proceso_local_no_tiene_apagado(tmp_path):
    write(tmp_path, "manage.py", "import django")
    assert detect.detect(tmp_path).services["api"].stop is None


def test_contenedor_con_puerto_espera_a_que_acepte(tmp_path):
    write(
        tmp_path,
        "docker-compose.yml",
        """
        services:
          db:
            image: postgres
            ports: ['5433:5432']
          cron:
            image: alpine
        """,
    )
    stack = detect.detect(tmp_path)
    assert stack.services["db"].ready == "port"
    assert stack.services["cron"].ready == "none", "sin puerto no hay nada que sondear"


def test_puerto_desde_variable_con_default(tmp_path):
    write(
        tmp_path,
        "compose.yaml",
        "services:\n  web:\n    image: nginx\n    ports: ['${WEB_PORT:-8080}:80']\n",
    )
    assert detect.detect(tmp_path).services["web"].port == 8080


def test_el_dotenv_le_gana_al_default(tmp_path):
    write(
        tmp_path,
        "compose.yaml",
        "services:\n  web:\n    image: nginx\n    ports: ['${WEB_PORT:-8080}:80']\n",
    )
    write(tmp_path, ".env", "# comentario\nWEB_PORT=9090\n")
    assert detect.detect(tmp_path).services["web"].port == 9090


def test_el_entorno_le_gana_al_dotenv(tmp_path, monkeypatch):
    write(
        tmp_path,
        "compose.yaml",
        "services:\n  web:\n    image: nginx\n    ports: ['${WEB_PORT:-8080}:80']\n",
    )
    write(tmp_path, ".env", "WEB_PORT=9090")
    monkeypatch.setenv("WEB_PORT", "7070")
    assert detect.detect(tmp_path).services["web"].port == 7070


def test_el_contenedor_le_gana_el_nombre_al_local(tmp_path):
    write(tmp_path, "compose.yaml", "services:\n  web:\n    image: nginx\n    ports: ['8080:80']\n")
    write(tmp_path, "package.json", json.dumps({"scripts": {"dev": "vite"}}))

    stack = detect.detect(tmp_path)

    assert list(stack.services) == ["web"]
    assert stack.services["web"].command == "docker compose up -d web"


def test_depends_on_a_un_servicio_inexistente_se_ignora(tmp_path):
    write(
        tmp_path,
        "compose.yaml",
        "services:\n  api:\n    image: node\n    depends_on: [fantasma]\n",
    )
    assert detect.detect(tmp_path).services["api"].needs == ()


def test_compose_sin_servicios_arranca_entero(tmp_path):
    write(tmp_path, "docker-compose.yml", "name: solo-un-nombre\n")
    docker = detect.detect(tmp_path).services["docker"]
    assert docker.command == "docker compose up -d"
    assert docker.detached
    assert docker.ready == "none"


def test_compose_invalido_detecta_sin_puerto(tmp_path):
    write(tmp_path, "compose.yaml", "esto: [no cierra")
    assert detect.detect(tmp_path).services["docker"].port is None


def test_django_por_manage_py(tmp_path):
    write(tmp_path, "manage.py", "import django")
    assert detect.detect(tmp_path).services["api"].command == "python manage.py runserver"


def test_uvicorn_necesita_el_modulo_con_app(tmp_path):
    write(tmp_path, "requirements.txt", "fastapi\nuvicorn\n")
    assert detect.detect(tmp_path) is None, "sin modulo con app no se adivina el comando"

    write(tmp_path, "src/main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    assert detect.detect(tmp_path).services["api"].command == "uvicorn src.main:app --reload"


def test_backend_python_en_subcarpeta(tmp_path):
    write(tmp_path, "backend/requirements.txt", "fastapi\nuvicorn\n")
    write(tmp_path, "backend/main.py", "from fastapi import FastAPI\napp = FastAPI()\n")

    servicio = detect.detect(tmp_path).services["backend"]
    assert servicio.command == "uvicorn main:app --reload"
    assert servicio.cwd == tmp_path / "backend"


def test_backend_python_en_workspace(tmp_path):
    write(tmp_path, "services/api/manage.py", "import django")
    assert detect.detect(tmp_path).services["api"].cwd == tmp_path / "services" / "api"


def test_la_raiz_python_gana_y_no_baja(tmp_path):
    """Igual que en Node: si la raiz es el proyecto, los hijos no entran."""
    write(tmp_path, "manage.py", "import django")
    write(tmp_path, "backend/manage.py", "import django")

    stack = detect.detect(tmp_path)
    assert list(stack.services) == ["api"]
    assert stack.services["api"].cwd == tmp_path


def test_orden_y_dependencias(tmp_path):
    write(tmp_path, "compose.yaml", "services:\n  db:\n    image: postgres\n")
    write(tmp_path, "manage.py", "import django")
    write(tmp_path, "package.json", json.dumps({"scripts": {"dev": "vite"}}))

    stack = detect.detect(tmp_path)
    assert [s.name for s in stack.resolve()] == ["db", "api", "web"]
    assert stack.services["api"].needs == ("db",), "el backend local espera al contenedor"
    assert stack.services["web"].needs == ("api",)


def test_frontend_en_subcarpeta_de_workspace(tmp_path):
    write(
        tmp_path,
        "apps/web/package.json",
        json.dumps({"scripts": {"dev": "vite"}, "devDependencies": {"vite": "^5"}}),
    )
    write(tmp_path, "pnpm-lock.yaml", "lockfileVersion: 9")

    stack = detect.detect(tmp_path)
    web = stack.services["web"]

    assert web.command == "pnpm run dev", "el lockfile de la raiz vale para el hijo"
    assert web.cwd == tmp_path / "apps" / "web"
    assert web.ready == "listen"


def test_frontend_en_carpeta_con_nombre_conocido(tmp_path):
    write(
        tmp_path,
        "frontend/package.json",
        json.dumps({"scripts": {"dev": "next dev"}, "dependencies": {"next": "^15"}}),
    )
    assert detect.detect(tmp_path).services["frontend"].command == "npm run dev"


def test_nest_usa_start_dev(tmp_path):
    write(
        tmp_path,
        "apps/api/package.json",
        json.dumps(
            {
                "scripts": {"start": "nest start", "start:dev": "nest start --watch"},
                "devDependencies": {"@nestjs/cli": "^10"},
            }
        ),
    )
    assert detect.detect(tmp_path).services["api"].command == "npm run start:dev"


def test_una_libreria_del_workspace_no_es_un_servicio(tmp_path):
    write(
        tmp_path,
        "packages/ui/package.json",
        json.dumps({"scripts": {"dev": "tsc --watch"}, "devDependencies": {"typescript": "^5"}}),
    )
    assert detect.detect(tmp_path) is None, "un tsc --watch nunca abre un puerto"


def test_la_raiz_le_gana_a_las_subcarpetas(tmp_path):
    write(tmp_path, "package.json", json.dumps({"scripts": {"dev": "turbo dev"}}))
    write(
        tmp_path,
        "apps/web/package.json",
        json.dumps({"scripts": {"dev": "vite"}, "devDependencies": {"vite": "^5"}}),
    )

    stack = detect.detect(tmp_path)

    assert list(stack.services) == ["web"]
    assert stack.services["web"].command == "npm run dev"
    assert stack.services["web"].cwd == tmp_path, "el orquestador de la raiz arranca todo"


def test_varios_frontends_del_workspace(tmp_path):
    for name in ("admin", "tienda"):
        write(
            tmp_path,
            f"apps/{name}/package.json",
            json.dumps({"scripts": {"dev": "vite"}, "devDependencies": {"vite": "^5"}}),
        )
    assert sorted(detect.detect(tmp_path).services) == ["admin", "tienda"]


def test_el_contenedor_le_gana_a_la_subcarpeta(tmp_path):
    write(tmp_path, "compose.yaml", "services:\n  web:\n    image: nginx\n    ports: ['8080:80']\n")
    write(
        tmp_path,
        "apps/web/package.json",
        json.dumps({"scripts": {"dev": "vite"}, "devDependencies": {"vite": "^5"}}),
    )

    stack = detect.detect(tmp_path)

    assert list(stack.services) == ["web"]
    assert stack.services["web"].command == "docker compose up -d web"


def test_el_archivo_gana_sobre_la_deteccion(tmp_path):
    write(tmp_path, "package.json", json.dumps({"scripts": {"dev": "vite"}}))
    write(tmp_path, "stack.yaml", "services:\n  solo:\n    command: echo hola\n")

    stack = detect.stack_for(tmp_path)
    assert not stack.detected
    assert list(stack.services) == ["solo"]


def test_stack_yaml_invalido_no_cae_en_la_deteccion(tmp_path):
    write(tmp_path, "package.json", json.dumps({"scripts": {"dev": "vite"}}))
    write(tmp_path, "stack.yaml", "services: {}\n")

    with pytest.raises(config.ConfigError):
        detect.stack_for(tmp_path)


def test_to_yaml_vuelve_a_cargar_igual(tmp_path):
    write(tmp_path, "compose.yaml", "services:\n  db:\n    image: postgres\n    ports: ['5433:5432']\n")
    write(tmp_path, "package.json", json.dumps({"scripts": {"dev": "vite"}}))
    detected = detect.detect(tmp_path)

    write(tmp_path, "stack.yaml", detect.to_yaml(detected))
    reloaded = config.load(tmp_path / "stack.yaml")

    assert {n: (s.command, s.port, s.ready, s.needs, s.detached)
            for n, s in reloaded.services.items()} == {
        n: (s.command, s.port, s.ready, s.needs, s.detached)
        for n, s in detected.services.items()
    }


# ready: listen ------------------------------------------------------------

SERVER = (
    "import socket, time; "
    "s = socket.socket(); s.bind(('127.0.0.1', {port})); s.listen(); "
    "time.sleep(120)"
)


def test_listen_descubre_el_puerto_del_proceso(tmp_path, free_ports):
    (port,) = free_ports(1)
    stack = _stack(
        tmp_path,
        f"""
        services:
          web:
            command: {sys.executable} -c "{SERVER.format(port=port)}"
            ready: listen
        """,
    )

    engine = runner.Runner(stack, console=Console(file=io.StringIO()), timeout=20.0)
    try:
        engine.up()
        proc = engine.procs[0]
        assert proc.ready
        assert proc.port == port, "el puerto sale del proceso, no de la config"
        assert proc.known_port == port
    finally:
        engine.down()


def test_listen_con_puerto_declarado_es_error(tmp_path):
    with pytest.raises(config.ConfigError, match="listen"):
        _stack(
            tmp_path,
            """
            services:
              web:
                command: echo hola
                port: 3000
                ready: listen
            """,
        )


def test_listening_sin_proceso_devuelve_none():
    assert ports.listening(2**31 - 1) is None


def test_detect_package_manager_field(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"scripts": {"dev": "vite"}, "packageManager": "pnpm@9.0.0"}', encoding="utf-8"
    )
    stack = detect.detect(tmp_path)
    assert stack is not None
    assert stack.services["web"].command == "pnpm run dev"


def _stack(tmp_path, body):
    path = tmp_path / "stack.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return config.load(path)


COMPOSE_CON_PERFILES = """
    services:
      db:
        image: postgres
        ports: ["5432:5432"]
      api:
        image: api
        ports: ["8080:8080"]
        depends_on: [db]
      seed:
        image: seed
        profiles: [tools]
      mailhog:
        image: mailhog
        profiles: ["dev"]
    """


def test_un_contenedor_con_perfil_no_arranca_por_defecto(tmp_path):
    """En compose, `profiles:` excluye. En PortMaster un perfil es una lista de
    lo que se arranca. Traducirlos al reves arranca lo que compose apaga."""
    write(tmp_path, "compose.yaml", COMPOSE_CON_PERFILES)
    stack = detect.detect(tmp_path)

    por_defecto = [s.name for s in stack.resolve()]
    assert por_defecto == ["db", "api"]
    assert "seed" not in por_defecto
    assert "mailhog" not in por_defecto


def test_cada_perfil_del_compose_es_un_perfil(tmp_path):
    write(tmp_path, "compose.yaml", COMPOSE_CON_PERFILES)
    stack = detect.detect(tmp_path)

    assert sorted(stack.profiles) == ["dev", "tools"]
    # Un perfil arranca lo de siempre mas lo suyo, igual que `--profile` en compose.
    assert [s.name for s in stack.resolve("tools")] == ["db", "api", "seed"]
    assert [s.name for s in stack.resolve("dev")] == ["db", "api", "mailhog"]


def test_un_perfil_como_string_suelto_tambien_cuenta(tmp_path):
    write(
        tmp_path,
        "compose.yaml",
        """
        services:
          db:
            image: postgres
          seed:
            image: seed
            profiles: tools
        """,
    )
    stack = detect.detect(tmp_path)
    assert [s.name for s in stack.resolve()] == ["db"]
    assert "tools" in stack.profiles


def test_el_frontend_no_espera_a_un_contenedor_opcional(tmp_path):
    """Si heredara la cadena entera, el orden topologico arrastraria el
    contenedor con perfil de vuelta al arranque por defecto."""
    write(tmp_path, "compose.yaml", COMPOSE_CON_PERFILES)
    write(tmp_path, "package.json", json.dumps({"scripts": {"dev": "vite"}, "dependencies": {"vite": "5"}}))

    stack = detect.detect(tmp_path)
    assert "seed" not in stack.services["web"].needs
    assert "mailhog" not in stack.services["web"].needs
    assert [s.name for s in stack.resolve()] == ["db", "api", "web"]


def test_un_compose_sin_perfiles_arranca_todo(tmp_path):
    write(
        tmp_path,
        "compose.yaml",
        """
        services:
          db:
            image: postgres
          api:
            image: api
        """,
    )
    stack = detect.detect(tmp_path)
    assert stack.profiles == {}
    assert stack.default is None, "sin perfiles, el default sigue siendo todo"
    assert len(stack.resolve()) == 2


def test_congelar_un_compose_con_perfiles_no_cambia_lo_que_arranca(tmp_path):
    """`portmaster init` tiene que ser fiel: el archivo congelado arranca lo
    mismo que la deteccion, ni un contenedor mas."""
    write(tmp_path, "compose.yaml", COMPOSE_CON_PERFILES)
    detectado = detect.detect(tmp_path)

    write(tmp_path, "stack.yaml", detect.to_yaml(detectado))
    cargado = config.load(tmp_path / "stack.yaml")

    assert [s.name for s in cargado.resolve()] == [s.name for s in detectado.resolve()]
    assert cargado.profiles == detectado.profiles
    assert [s.name for s in cargado.resolve("tools")] == ["db", "api", "seed"]


# go y rust ----------------------------------------------------------------


def test_go_con_framework(tmp_path):
    write(tmp_path, "go.mod", "module ejemplo\n\nrequire github.com/gin-gonic/gin v1.9.1\n")
    write(tmp_path, "main.go", "package main\n\nfunc main() {}\n")

    stack = detect.detect(tmp_path)
    assert stack.services["api"].command == "go run ."
    assert stack.services["api"].ready == "listen"


def test_go_con_solo_la_stdlib(tmp_path):
    """net/http no aparece en go.mod: la llamada en el fuente es la unica senal."""
    write(tmp_path, "go.mod", "module ejemplo\n\ngo 1.22\n")
    write(
        tmp_path,
        "main.go",
        """
        package main

        import "net/http"

        func main() { http.ListenAndServe(":8080", nil) }
        """,
    )
    assert detect.detect(tmp_path).services["api"].command == "go run ."


def test_go_que_no_sirve_nada_no_se_detecta(tmp_path):
    """Una herramienta de linea de comandos: arrancarla esperaria un puerto que
    nunca abre, hasta el timeout."""
    write(tmp_path, "go.mod", "module herramienta\n\ngo 1.22\n")
    write(tmp_path, "main.go", 'package main\n\nfunc main() { println("hola") }\n')
    assert detect.detect(tmp_path) is None


def test_go_en_cmd(tmp_path):
    write(tmp_path, "go.mod", "module ejemplo\n\nrequire github.com/go-chi/chi/v5 v5.0.0\n")
    write(tmp_path, "cmd/server/main.go", "package main\n\nfunc main() {}\n")
    assert detect.detect(tmp_path).services["api"].command == "go run ./cmd/server"


def test_go_en_subcarpeta_de_backend(tmp_path):
    write(tmp_path, "backend/go.mod", "module api\n\nrequire github.com/gin-gonic/gin v1.9.1\n")
    write(tmp_path, "backend/main.go", "package main\n\nfunc main() {}\n")

    servicio = detect.detect(tmp_path).services["backend"]
    assert servicio.command == "go run ."
    assert servicio.cwd == tmp_path.resolve() / "backend"


def test_rust_con_framework(tmp_path):
    write(tmp_path, "Cargo.toml", '[dependencies]\naxum = "0.7"\n')
    write(tmp_path, "src/main.rs", "fn main() {}\n")

    stack = detect.detect(tmp_path)
    assert stack.services["api"].command == "cargo run"
    assert stack.services["api"].ready == "listen"


def test_rust_sin_framework_se_detecta_con_ready_none(tmp_path):
    write(tmp_path, "Cargo.toml", '[dependencies]\nclap = "4"\n')
    write(tmp_path, "src/main.rs", "fn main() {}\n")
    stack = detect.detect(tmp_path)
    assert stack is not None
    servicio = stack.services["api"]
    assert servicio.command == "cargo run"
    assert servicio.ready == "none"
    assert servicio.port is None


def test_rust_libreria_no_se_detecta(tmp_path):
    """Sin src/main.rs no hay binario que arrancar, aunque dependa de un framework."""
    write(tmp_path, "Cargo.toml", '[dependencies]\naxum = "0.7"\n')
    write(tmp_path, "src/lib.rs", "pub fn nada() {}\n")
    assert detect.detect(tmp_path) is None


def test_rust_con_hyper_se_detecta_con_ready_none(tmp_path):
    """hyper como cliente HTTP no activa ready: listen, pero el binario ejecutable
    se detecta con ready: none sin esperar un puerto inexistente."""
    write(tmp_path, "Cargo.toml", '[dependencies]\nhyper = "1"\n')
    write(tmp_path, "src/main.rs", 'use hyper::Client;\nfn main() { descargar(); }\n')
    stack = detect.detect(tmp_path)
    assert stack is not None
    assert stack.services["api"].command == "cargo run"
    assert stack.services["api"].ready == "none"


def test_rust_con_bin_en_src_bin(tmp_path):
    """Detecta binarios alternativos en src/bin/*.rs cuando no hay src/main.rs."""
    write(tmp_path, "Cargo.toml", '[dependencies]\naxum = "0.7"\n')
    write(tmp_path, "src/bin/server.rs", "fn main() {}\n")

    stack = detect.detect(tmp_path)
    assert stack is not None
    assert stack.services["api"].command == "cargo run --bin server"
    assert stack.services["api"].ready == "listen"


def test_rust_con_bin_declarado_en_cargo_toml(tmp_path):
    """Detecta binario declarado explicitamente con [[bin]] en Cargo.toml."""
    write(
        tmp_path,
        "Cargo.toml",
        '[package]\nname = "miapp"\n\n[[bin]]\nname = "mi-servidor"\npath = "src/app.rs"\n\n[dependencies]\nrocket = "0.5"\n',
    )
    write(tmp_path, "src/app.rs", "fn main() {}\n")

    stack = detect.detect(tmp_path)
    assert stack is not None
    assert stack.services["api"].command == "cargo run --bin mi-servidor"


def test_rust_cargo_workspace(tmp_path):
    """Detecta miembros de un Cargo workspace en crates/* o services/*."""
    write(
        tmp_path,
        "Cargo.toml",
        '[workspace]\nmembers = [\n    "crates/api",\n    "crates/core",\n]\n',
    )
    # Miembro api con framework servidor
    write(
        tmp_path,
        "crates/api/Cargo.toml",
        '[package]\nname = "api"\n\n[dependencies]\ntonic = "0.12"\n',
    )
    write(tmp_path, "crates/api/src/main.rs", "fn main() {}\n")

    # Miembro core es solo libreria
    write(
        tmp_path,
        "crates/core/Cargo.toml",
        '[package]\nname = "core"\n',
    )
    write(tmp_path, "crates/core/src/lib.rs", "pub fn helper() {}\n")

    stack = detect.detect(tmp_path)
    assert stack is not None
    assert "api" in stack.services
    assert stack.services["api"].command == "cargo run"
    assert "core" not in stack.services


def test_rust_nuevos_frameworks(tmp_path):
    """Verifica deteccion con tonic, trillium, gotham y volo-http."""
    for framework in ("tonic", "trillium", "gotham", "volo-http"):
        carpeta = tmp_path / framework
        write(carpeta, "Cargo.toml", f'[dependencies]\n{framework} = "1.0"\n')
        write(carpeta, "src/main.rs", "fn main() {}\n")
        stack = detect.detect(carpeta)
        assert stack is not None, f"debio detectar {framework}"
        assert stack.services["api"].command == "cargo run"


def test_rust_cli_tipo_rtok_se_detecta_con_ready_none(tmp_path):
    """Un CLI o TUI de terminal (como rtok, con ratatui y ureq) se detecta con ready: none."""
    write(
        tmp_path,
        "Cargo.toml",
        '[package]\nname = "rtok"\n\n[dependencies]\nratatui = "0.30"\nureq = "3.4"\narboard = "3.6"\n',
    )
    write(tmp_path, "src/main.rs", "fn main() {}\n")
    stack = detect.detect(tmp_path)
    assert stack is not None
    servicio = stack.services["api"]
    assert servicio.command == "cargo run"
    assert servicio.ready == "none"
    assert servicio.port is None


def test_el_frontend_espera_al_backend_de_go(tmp_path):
    write(tmp_path, "go.mod", "module ejemplo\n\nrequire github.com/gin-gonic/gin v1.9.1\n")
    write(tmp_path, "main.go", "package main\n\nfunc main() {}\n")
    write(
        tmp_path,
        "frontend/package.json",
        json.dumps({"scripts": {"dev": "vite"}, "devDependencies": {"vite": "^5"}}),
    )

    stack = detect.detect(tmp_path)
    assert stack.services["frontend"].needs == ("api",)


# rails y laravel ----------------------------------------------------------


def test_rails(tmp_path):
    write(tmp_path, "Gemfile", 'source "https://rubygems.org"\ngem "rails"\n')
    write(tmp_path, "config/application.rb", "module App\nend\n")

    stack = detect.detect(tmp_path)
    assert stack.services["api"].command == "bundle exec rails server"
    assert stack.services["api"].ready == "listen"


def test_una_gema_no_es_una_app_de_rails(tmp_path):
    """Gemfile lo tiene cualquier proyecto Ruby. config/application.rb no."""
    write(tmp_path, "Gemfile", 'source "https://rubygems.org"\ngem "rails"\n')
    write(tmp_path, "lib/mi_gema.rb", "module MiGema\nend\n")
    assert detect.detect(tmp_path) is None


def test_laravel(tmp_path):
    write(tmp_path, "artisan", "#!/usr/bin/env php\n")
    write(tmp_path, "composer.json", json.dumps({"require": {"laravel/framework": "^11"}}))

    stack = detect.detect(tmp_path)
    assert stack.services["api"].command == "php artisan serve"
    assert stack.services["api"].ready == "listen"


def test_composer_sin_artisan_no_es_laravel(tmp_path):
    write(tmp_path, "composer.json", json.dumps({"require": {"monolog/monolog": "^3"}}))
    assert detect.detect(tmp_path) is None


def test_laravel_con_su_frontend(tmp_path):
    """El stack tipico de Laravel: artisan y vite en la misma raiz."""
    write(tmp_path, "artisan", "#!/usr/bin/env php\n")
    write(tmp_path, "composer.json", json.dumps({"require": {"laravel/framework": "^11"}}))
    write(
        tmp_path,
        "package.json",
        json.dumps({"scripts": {"dev": "vite"}, "devDependencies": {"vite": "^5"}}),
    )

    stack = detect.detect(tmp_path)
    assert stack.services["api"].command == "php artisan serve"
    assert stack.services["web"].needs == ("api",)


def test_rails_en_subcarpeta_de_backend(tmp_path):
    write(tmp_path, "backend/Gemfile", 'source "https://rubygems.org"\n')
    write(tmp_path, "backend/config/application.rb", "module App\nend\n")

    servicio = detect.detect(tmp_path).services["backend"]
    assert servicio.command == "bundle exec rails server"
    assert servicio.cwd == tmp_path.resolve() / "backend"


# .net ---------------------------------------------------------------------

CSPROJ_WEB = """
<Project Sdk="Microsoft.NET.Sdk.Web">
  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>
</Project>
"""

CSPROJ_LIB = """
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>
</Project>
"""

CSPROJ_CONSOLA = """
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
</Project>
"""


def test_dotnet_web(tmp_path):
    write(tmp_path, "Api.csproj", CSPROJ_WEB)

    stack = detect.detect(tmp_path)
    assert stack.services["api"].command == "dotnet watch run"
    assert stack.services["api"].ready == "listen"


def test_una_libreria_de_dotnet_no_se_detecta(tmp_path):
    write(tmp_path, "Dominio.csproj", CSPROJ_LIB)
    assert detect.detect(tmp_path) is None


def test_una_consola_de_dotnet_no_se_detecta(tmp_path):
    """OutputType Exe, pero no sirve nada por un puerto: el Sdk lo delata."""
    write(tmp_path, "Herramienta.csproj", CSPROJ_CONSOLA)
    assert detect.detect(tmp_path) is None


def test_dotnet_con_varios_csproj_apunta_al_web(tmp_path):
    """`dotnet run` con dos csproj en la carpeta falla pidiendo que le digan cual."""
    write(tmp_path, "Dominio.csproj", CSPROJ_LIB)
    write(tmp_path, "Api.csproj", CSPROJ_WEB)

    comando = detect.detect(tmp_path).services["api"].command
    assert comando == 'dotnet watch run --project "Api.csproj"'


def test_dotnet_en_subcarpeta_de_backend(tmp_path):
    write(tmp_path, "server/Server.csproj", CSPROJ_WEB)

    servicio = detect.detect(tmp_path).services["server"]
    assert servicio.command == "dotnet watch run"
    assert servicio.cwd == tmp_path.resolve() / "server"


def test_python_fastapi_con_uv_lock(tmp_path):
    write(tmp_path, "main.py", "app = FastAPI()\n")
    write(tmp_path, "pyproject.toml", '[project]\ndependencies = ["fastapi", "uvicorn"]\n')
    write(tmp_path, "uv.lock", "version = 1\n")

    stack = detect.detect(tmp_path)
    assert stack.services["api"].command == "uv run uvicorn main:app --reload"


def test_python_django_con_uv_lock(tmp_path):
    write(tmp_path, "manage.py", "# django manage.py\n")
    write(tmp_path, "uv.lock", "version = 1\n")

    stack = detect.detect(tmp_path)
    assert stack.services["api"].command == "uv run python manage.py runserver"


@pytest.mark.parametrize("framework", ["hono", "fastify", "express", "nitro", "astro"])
def test_un_framework_moderno_declara_un_servidor(tmp_path, framework):
    """Uno por dependencia y no uno solo con nombre de varios.

    El test se llamaba `astro_hono` y solo declaraba `hono`: los otros tres que
    se agregaron a DEV_SERVERS nunca se probaron.
    """
    write(
        tmp_path,
        "package.json",
        json.dumps({
            "dependencies": {framework: "^4.0.0"},
            "scripts": {"dev": "tsx watch src/index.ts"},
        }),
    )
    stack = detect.detect(tmp_path)
    assert stack.services["web"].command == "npm run dev"


def test_deno_con_tasks(tmp_path):
    write(
        tmp_path,
        "deno.json",
        json.dumps({
            "tasks": {"dev": "deno run --watch main.ts"}
        }),
    )
    stack = detect.detect(tmp_path)
    assert stack is not None
    assert stack.services["web"].command == "deno task dev"
    assert stack.services["web"].ready == "listen"


def test_deno_con_entrypoint(tmp_path):
    write(tmp_path, "deno.jsonc", "{\n  // config\n}\n")
    write(tmp_path, "server.ts", "Deno.serve((_req) => new Response('Hello'));\n")
    stack = detect.detect(tmp_path)
    assert stack is not None
    assert stack.services["web"].command == "deno run --allow-net server.ts"


def test_deno_no_secuestra_proyecto_node(tmp_path):
    write(tmp_path, "deno.json", "{\n  \"fmt\": {\"indentWidth\": 2}\n}\n")
    write(tmp_path, "server.ts", "import express from 'express';\n")
    write(tmp_path, "package.json", json.dumps({"scripts": {"dev": "vite"}}))
    stack = detect.detect(tmp_path)
    assert stack is not None
    assert stack.services["web"].command == "npm run dev"


def test_deno_json_no_dict_no_revienta(tmp_path):
    write(tmp_path, "deno.json", "[\"array\", \"invalido\"]")
    write(tmp_path, "server.ts", "console.log('hola');")
    stack = detect.detect(tmp_path)
    assert stack is not None
    assert stack.services["web"].command == "deno run --allow-net server.ts"


def test_python_con_tool_uvicorn_no_se_detecta_como_uv(tmp_path):
    write(
        tmp_path,
        "pyproject.toml",
        """
[project]
name = "api"
dependencies = ["fastapi", "uvicorn"]

[tool.uvicorn]
workers = 4
""",
    )
    write(tmp_path, "main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    stack = detect.detect(tmp_path)
    assert stack is not None
    assert "uv run" not in stack.services["api"].command
    assert stack.services["api"].command.startswith("uvicorn main:app")


def test_deno_jsonc_con_comentarios_en_tasks(tmp_path):
    write(
        tmp_path,
        "deno.jsonc",
        """{
  // Comentario de configuración
  "tasks": {
    // Tarea de desarrollo
    "dev": "deno run --allow-net main.ts"
  }
}""",
    )
    stack = detect.detect(tmp_path)
    assert stack is not None
    assert stack.services["web"].command == "deno task dev"






# bun ----------------------------------------------------------------------


def test_bun_no_se_come_un_proyecto_node(tmp_path):
    """El orden de la tupla de detectores, que es lo mas fragil de este cambio.

    Un proyecto con `package.json` y `bun.lock` ya funcionaba antes de que
    existiera `_bun`: sale por `_node`, que es el unico que sabe leer los
    scripts, y `_manager` devuelve `bun` por el lockfile. `_bun_at` solo tiene
    que atrapar lo que `_node` deja pasar.

    En `detect()` el primero gana, asi que esto depende de una linea: `_bun`
    va despues de `_node` en la tupla. Es una dependencia entre dos posiciones
    de una lista, invisible para cualquier otro test, y si alguien las
    reordena este proyecto pasa a arrancar con el comando equivocado sin que
    nada se ponga rojo.
    """
    write(tmp_path, "package.json", json.dumps({
        "scripts": {"dev": "vite"},
        "dependencies": {"vite": "^5.0.0"},
    }))
    write(tmp_path, "bun.lock", "{}")
    write(tmp_path, "index.ts", "Bun.serve({ fetch() {} })")

    stack = detect.detect(tmp_path)
    assert stack.services["web"].command == "bun run dev"


def test_bun_como_runtime_sin_scripts(tmp_path):
    """Lo que `_node` deja pasar: no hay script que correr.

    `_package` pide un `scripts` con `dev`, `start:dev`, `serve` o `start`, y
    sin eso devuelve None. Un proyecto Bun tipico no tiene ninguno: se corre el
    archivo directo.
    """
    write(tmp_path, "bunfig.toml", "[install]\n")
    write(tmp_path, "index.ts", 'Bun.serve({ port: 3000, fetch: () => new Response("ok") })')

    stack = detect.detect(tmp_path)
    assert stack.services["web"].command == "bun run index.ts"
    assert stack.services["web"].ready == "listen"


def test_bun_con_framework_en_las_dependencias(tmp_path):
    """`Bun.serve` es la API nativa y no aparece en ninguna dependencia.

    Con Hono o Elysia el fuente no la nombra, asi que la dependencia es la
    unica senal. Es el mismo par que usa `_go_at`: el framework en el manifiesto
    o la llamada en el codigo.
    """
    write(tmp_path, "bun.lockb", "")
    write(tmp_path, "package.json", json.dumps({"dependencies": {"hono": "^4.0.0"}}))
    write(tmp_path, "src/index.ts", "import { Hono } from 'hono'\nexport default new Hono()")

    stack = detect.detect(tmp_path)
    assert stack.services["web"].command == "bun run src/index.ts"


def test_bun_no_detecta_una_cli(tmp_path):
    """Sin framework y sin `Bun.serve`, es una herramienta de linea de comandos.

    Es la misma decision que `_go_at` con las CLIs y `_rust_at` con las
    librerias: arrancarla dejaria al runner esperando un puerto que nunca abre
    hasta que se acabe el timeout. No detectar es mejor.

    Este es el test que atrapa el bug caro. Un detector que devuelve algo
    siempre pasa los otros tres.
    """
    write(tmp_path, "bunfig.toml", "[install]\n")
    write(tmp_path, "index.ts", 'console.log("hola desde una cli")')

    assert detect.detect(tmp_path) is None


def test_bun_sin_marca_de_bun_no_es_de_bun(tmp_path):
    """Un `index.ts` suelto no alcanza: podria ser de Deno, de Node o de nadie."""
    write(tmp_path, "index.ts", 'console.log("hola")')

    assert detect.detect(tmp_path) is None


def test_bun_en_una_subcarpeta_de_backend(tmp_path):
    """Bun sirve un frontend o una API, asi que se busca en los dos lados.

    Por eso no puede usar `_backend_at` como Go o PHP: mirar solo BACKEND_DIRS
    dejaria afuera un `frontend/` servido con Bun.
    """
    write(tmp_path, "api/bunfig.toml", "[install]\n")
    write(tmp_path, "api/server.ts", "Bun.serve({ fetch() {} })")

    stack = detect.detect(tmp_path)
    assert stack.services["api"].command == "bun run server.ts"


# elixir -------------------------------------------------------------------


def test_elixir_phoenix_por_la_dependencia(tmp_path):
    write(tmp_path, "mix.exs", """
        defmodule MiApp.MixProject do
          use Mix.Project
          defp deps do
            [
              {:phoenix, "~> 1.7.10"},
              {:ecto_sql, "~> 3.10"}
            ]
          end
        end
    """)

    stack = detect.detect(tmp_path)
    assert stack.services["api"].command == "mix phx.server"
    assert stack.services["api"].ready == "listen"


def test_elixir_phoenix_por_la_carpeta_web(tmp_path):
    """`lib/<algo>_web/` la genera Phoenix siempre.

    Es la senal estructural, el analogo de `config/application.rb` en Rails: si
    esta, hay una aplicacion que sirve. Sirve para el proyecto que trae las
    dependencias de otro archivo o de un umbrella.
    """
    write(tmp_path, "mix.exs", "defmodule MiApp.MixProject do\nend\n")
    write(tmp_path, "lib/mi_app_web/router.ex", "defmodule MiAppWeb.Router do\nend\n")

    stack = detect.detect(tmp_path)
    assert stack.services["api"].command == "mix phx.server"


def test_elixir_una_libreria_no_se_detecta(tmp_path):
    """Un `mix.exs` a secas es una libreria o una app OTP sin puerto.

    Arrancarla dejaria al runner esperando un socket que nunca abre hasta que
    se acabe el timeout. Es la misma decision que `_go_at` con las CLIs.

    Este es el test que atrapa el bug caro: un detector que devuelve algo
    siempre pasa los otros.
    """
    write(tmp_path, "mix.exs", """
        defmodule MiLibreria.MixProject do
          use Mix.Project
          defp deps do
            [{:jason, "~> 1.4"}, {:telemetry, "~> 1.2"}]
          end
        end
    """)

    assert detect.detect(tmp_path) is None


def test_elixir_una_libreria_de_componentes_phoenix_no_se_detecta(tmp_path):
    """`{:phoenix_html, ...}` no es `{:phoenix, ...}`, y la diferencia importa.

    Una libreria de componentes declara `phoenix_html` o `phoenix_live_view`
    sin ser una aplicacion: no tiene endpoint, y `mix phx.server` ahi falla.
    Un `"phoenix" in texto` las tomaria a todas, que es la forma facil y
    equivocada de escribir este detector.
    """
    write(tmp_path, "mix.exs", """
        defmodule MisComponentes.MixProject do
          use Mix.Project
          defp deps do
            [{:phoenix_html, "~> 4.0"}, {:phoenix_live_view, "~> 0.20"}]
          end
        end
    """)

    assert detect.detect(tmp_path) is None


def test_elixir_en_una_subcarpeta_de_backend(tmp_path):
    write(tmp_path, "backend/mix.exs", '[{:phoenix, "~> 1.7"}]')

    stack = detect.detect(tmp_path)
    assert stack.services["backend"].command == "mix phx.server"


def test_elixir_sin_mix_no_es_elixir(tmp_path):
    write(tmp_path, "lib/mi_app_web/router.ex", "defmodule MiAppWeb.Router do\nend\n")

    assert detect.detect(tmp_path) is None


# jvm ----------------------------------------------------------------------

POM = """
<project>
  <dependencies>{deps}</dependencies>
  <build><plugins>{plugins}</plugins></build>
</project>
"""

SPRING_WEB = "<dependency><artifactId>spring-boot-starter-web</artifactId></dependency>"


@pytest.fixture(autouse=True)
def _sin_cache_de_path():
    """`_en_el_path` esta cacheado, y el cache no puede cruzar tests.

    Un test que parchea `shutil.which` deja la respuesta guardada para el
    siguiente, y el sintoma es el peor de todos: pasa solo y falla acompanado,
    o al reves. `CLAUDE.md` lo nombra como estado compartido entre tests y dice
    que nunca es ruido del runner.
    """
    detect._en_el_path.cache_clear()
    yield
    detect._en_el_path.cache_clear()


def sin_binarios(monkeypatch, *disponibles):
    """El PATH visto por el detector: solo lo que se nombre existe."""
    monkeypatch.setattr(
        detect.shutil, "which", lambda b: f"/usr/bin/{b}" if b in disponibles else None
    )


def test_jvm_spring_maven_con_mvn_en_el_path(tmp_path, monkeypatch):
    """El caso que hace portable el stack.yaml congelado.

    `mvn spring-boot:run` es igual en Linux, macOS y Windows. El wrapper no:
    `./mvnw` no corre en cmd.exe y `mvnw.cmd` no corre en bash, asi que un
    stack.yaml congelado con el wrapper se rompe al compartirlo con un equipo
    mixto. Por eso el binario del PATH gana cuando esta.
    """
    write(tmp_path, "pom.xml", POM.format(deps=SPRING_WEB, plugins=""))
    write(tmp_path, "mvnw", "#!/bin/sh")
    sin_binarios(monkeypatch, "mvn")

    stack = detect.detect(tmp_path)
    assert stack.services["api"].command == "mvn spring-boot:run"
    assert stack.services["api"].ready == "listen"


def test_jvm_spring_maven_cae_al_wrapper_de_la_plataforma(tmp_path, monkeypatch):
    """Sin `mvn` en el PATH queda el wrapper, y el wrapper es por plataforma."""
    write(tmp_path, "pom.xml", POM.format(deps=SPRING_WEB, plugins=""))
    write(tmp_path, "mvnw", "#!/bin/sh")
    write(tmp_path, "mvnw.cmd", "@echo off")
    sin_binarios(monkeypatch)

    esperado = "mvnw.cmd" if os.name == "nt" else "./mvnw"
    assert detect.detect(tmp_path).services["api"].command == f"{esperado} spring-boot:run"


def test_jvm_spring_gradle_usa_bootrun(tmp_path, monkeypatch):
    write(tmp_path, "build.gradle", "dependencies { implementation 'spring-boot-starter-web' }")
    sin_binarios(monkeypatch, "gradle")

    assert detect.detect(tmp_path).services["api"].command == "gradle bootRun"


def test_jvm_ktor_en_kotlin_dsl(tmp_path, monkeypatch):
    """`build.gradle.kts` es Gradle igual: Kotlin no es un detector aparte."""
    write(tmp_path, "build.gradle.kts", 'implementation("io.ktor:ktor-server-netty:2.3.0")')
    sin_binarios(monkeypatch, "gradle")

    assert detect.detect(tmp_path).services["api"].command == "gradle run"


def test_jvm_quarkus_maven(tmp_path, monkeypatch):
    write(tmp_path, "pom.xml", POM.format(
        deps="", plugins="<plugin><artifactId>quarkus-maven-plugin</artifactId></plugin>"
    ))
    sin_binarios(monkeypatch, "mvn")

    assert detect.detect(tmp_path).services["api"].command == "mvn quarkus:dev"


def test_jvm_una_libreria_no_se_detecta(tmp_path, monkeypatch):
    """Un `pom.xml` sin framework web es una libreria o una app de consola.

    Este es el test que atrapa el bug caro: un detector que devuelve algo
    siempre pasa todos los demas.
    """
    write(tmp_path, "pom.xml", POM.format(
        deps="<dependency><artifactId>guava</artifactId></dependency>", plugins=""
    ))
    sin_binarios(monkeypatch, "mvn")

    assert detect.detect(tmp_path) is None


def test_jvm_spring_sin_web_no_sirve_por_un_puerto(tmp_path, monkeypatch):
    """`spring-boot-starter` a secas no levanta un servidor.

    Es una app de Spring sin servlet container: una tarea batch, un consumidor
    de colas, un CLI. `bootRun` corre y termina, o se queda sin abrir ningun
    puerto. Es el mismo filo que `phoenix_html` contra `phoenix` en Elixir, y
    la unica forma de tomar las dos es buscar la palabra suelta.
    """
    write(tmp_path, "pom.xml", POM.format(
        deps="<dependency><artifactId>spring-boot-starter</artifactId></dependency>", plugins=""
    ))
    sin_binarios(monkeypatch, "mvn")

    assert detect.detect(tmp_path) is None


def test_jvm_en_una_subcarpeta_de_backend(tmp_path, monkeypatch):
    write(tmp_path, "backend/pom.xml", POM.format(deps=SPRING_WEB, plugins=""))
    sin_binarios(monkeypatch, "mvn")

    assert detect.detect(tmp_path).services["backend"].command == "mvn spring-boot:run"


def test_jvm_el_cache_del_path_no_se_pega_entre_proyectos(tmp_path, monkeypatch):
    """El cache es por binario, no por proyecto ni por PATH.

    Es la contracara del lru_cache: acelera el sondeo, y a cambio no puede
    distinguir dos PATH distintos dentro del mismo proceso. Se afirma para que
    quede escrito que es la decision y no un descuido, y para que el dia que
    alguien necesite lo contrario encuentre el test en vez del sintoma.
    """
    write(tmp_path, "pom.xml", POM.format(deps=SPRING_WEB, plugins=""))
    # Los dos wrappers: con solo el de POSIX, en Windows no hay wrapper usable
    # y el detector cae al binario pelado, que es correcto pero tapa lo que
    # este test quiere mirar.
    write(tmp_path, "mvnw", "#!/bin/sh")
    write(tmp_path, "mvnw.cmd", "@echo off")
    sin_binarios(monkeypatch, "mvn")
    assert detect.detect(tmp_path).services["api"].command == "mvn spring-boot:run"

    sin_binarios(monkeypatch)  # ahora mvn "no esta", pero ya se pregunto
    assert detect.detect(tmp_path).services["api"].command == "mvn spring-boot:run"

    detect._en_el_path.cache_clear()
    esperado = "mvnw.cmd" if os.name == "nt" else "./mvnw"
    assert detect.detect(tmp_path).services["api"].command == f"{esperado} spring-boot:run"


def test_bun_servidor_con_export_default_fetch(tmp_path):
    write(tmp_path, "bunfig.toml", "")
    write(tmp_path, "index.ts", "export default { port: 3000, fetch(req) { return new Response('ok'); } };\n")
    stack = detect.detect(tmp_path)
    assert stack is not None
    assert stack.services["web"].command == "bun run index.ts"


def test_bun_con_main_ts(tmp_path):
    write(tmp_path, "bunfig.toml", "")
    write(tmp_path, "main.ts", "Bun.serve({ fetch() { return new Response('hola'); } });\n")
    stack = detect.detect(tmp_path)
    assert stack is not None
    assert stack.services["web"].command == "bun run main.ts"


def test_bun_subproyecto_en_monorepo(tmp_path):
    write(tmp_path, "bun.lock", "")
    write(tmp_path, "backend/index.ts", "Bun.serve({ fetch() { return new Response('api'); } });\n")
    stack = detect.detect(tmp_path)
    assert stack is not None
    assert "backend" in stack.services
    assert stack.services["backend"].command == "bun run index.ts"


def test_jvm_monorepo_con_wrapper_en_raiz(tmp_path, monkeypatch):
    sin_binarios(monkeypatch)
    detect._en_el_path.cache_clear()
    write(tmp_path, "mvnw", "#!/bin/sh")
    write(tmp_path, "mvnw.cmd", "@echo off")
    write(tmp_path, "backend/pom.xml", POM.format(deps=SPRING_WEB, plugins=""))
    stack = detect.detect(tmp_path)
    assert stack is not None
    esperado = "..\\mvnw.cmd" if os.name == "nt" else "../mvnw"
    assert stack.services["backend"].command == f"{esperado} spring-boot:run"
