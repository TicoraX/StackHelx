"""CLI de StackHelx."""

from __future__ import annotations

import json
from pathlib import Path

import psutil
import typer
from rich.console import Console
from rich.table import Table

from . import (
    __version__,
    config,
    detect,
    docker,
    doctor,
    history,
    mcp,
    ports,
    registry,
    runner,
    scripts,
    tunnel,
)

app = typer.Typer(
    help="Orquestador de entornos de desarrollo locales.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err = Console(stderr=True)

PortArg = typer.Argument(..., min=1, max=65535)


def _row(status: ports.PortStatus, cmd_width: int) -> tuple[str, ...]:
    if status.free:
        return (str(status.port), "[green]libre[/]", "-", "-", "-")
    if status.owner_unknown:
        return (str(status.port), "[red]ocupado[/]", "?", "[dim]sin permisos[/]", "-")
    cmd = status.cmdline or "-"
    if len(cmd) > cmd_width:
        cmd = cmd[: cmd_width - 1] + "…"
    return (str(status.port), "[red]ocupado[/]", str(status.pid), status.name or "?", cmd)


@app.command("ports")
def ports_cmd(
    port: list[int] = typer.Argument(None, min=1, max=65535),
) -> None:
    """Revisa puertos. Sin argumentos, usa los del stack (declarados o detectados)."""
    if not port:
        try:
            stack = detect.stack_for(Path.cwd())
        except config.ConfigError as exc:
            err.print(f"{exc}\nPasa los puertos como argumento: stackhelx ports 3000 8080")
            raise typer.Exit(1)
        port = stack.ports()
        if not port:
            err.print(
                f"{stack.path} no declara ningun puerto. Los servicios con "
                "'ready: listen' recien lo tienen cuando arrancan."
            )
            raise typer.Exit(1)

    table = Table(box=None, pad_edge=False)
    for column in ("PUERTO", "ESTADO", "PID", "PROCESO", "COMANDO"):
        table.add_column(column)
    # Presupuesto para el comando: lo que sobra tras las cuatro columnas fijas.
    cmd_width = max(20, console.width - 34)
    scanned = ports.scan_many(port)
    for value in port:
        table.add_row(*_row(scanned[value], cmd_width))
    console.print(table)


def _release(
    port: int,
    yes: bool,
    force: bool,
    expected_pid: int | None = None,
    expected_create_time: float | None = None,
) -> bool:
    """Libera un puerto ocupado. Si se pasan expected_pid y expected_create_time,
    verifica que el proceso no haya cambiado entre la confirmacion y el cierre.
    """
    status = ports.scan(port)
    if status.free:
        console.print(f"Puerto {port} ya estaba libre.")
        return True

    if status.pid is None:
        err.print(
            f"Puerto {port} ocupado, pero el proceso no es visible con estos "
            "permisos. Proba desde una terminal con privilegios."
        )
        return False

    if expected_pid is not None and status.pid != expected_pid:
        err.print(
            f"El puerto {port} cambio de proceso (ahora es PID {status.pid} [{status.name}]). "
            "Salteado por seguridad."
        )
        return False

    target_pid = expected_pid if expected_pid is not None else status.pid
    target_create_time = (
        expected_create_time if expected_create_time is not None else status.create_time
    )

    if expected_pid is None:
        console.print(f"Puerto {port} ocupado por PID {status.pid} ([bold]{status.name}[/])")
        if status.cmdline:
            console.print(f"  [dim]{status.cmdline}[/]")

        if not yes and not typer.confirm(f"Cerrar el PID {status.pid}?"):
            return False

    try:
        ports.kill(target_pid, target_create_time, force=force, port=port)
    except ports.KillRefused as exc:
        err.print(f"Rechazado: {exc}")
        return False
    except psutil.NoSuchProcess:
        pass
    except psutil.AccessDenied:
        err.print(
            f"Sin permisos para cerrar el PID {target_pid}. "
            "Proba desde una terminal con privilegios."
        )
        return False

    console.print(f"PID {target_pid} cerrado. Puerto {port} [green]libre[/].")
    return True


@app.command("free")
def free_cmd(
    port: int | None = typer.Argument(None, help="Puerto a liberar (ej: 8080)."),
    all_ports: bool = typer.Option(
        False, "--all", "-a", help="Liberar todos los puertos intrusos de los proyectos registrados."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="No preguntar antes de matar."),
    force: bool = typer.Option(False, "--force", help="kill() si ignora terminate()."),
) -> None:
    """Libera un puerto ocupado o todos los puertos intrusos de proyectos registrados."""
    if not all_ports and port is None:
        err.print("Especifica un puerto (ej: stackhelx free 8080) o el flag --all")
        raise typer.Exit(1)

    if all_ports:
        return _free_all(yes, force)

    if ports.is_free(port):
        console.print(f"Puerto {port} [green]libre[/].")
        return
    if not _release(port, yes, force):
        console.print(f"Siguiente puerto libre: [bold]{ports.next_free(port)}[/]")


def _free_all(yes: bool, force: bool) -> None:
    """Cierra lo que ocupa los puertos declarados por los proyectos registrados.

    A diferencia de la interfaz, el CLI no tiene sesiones: no sabe cuales de
    esos procesos los arranco StackHelx en otra terminal. Por eso lista todo
    antes de tocar nada, y el texto no promete que sean ajenos.
    """
    encontrados = registry.find_orphans()
    if not encontrados:
        console.print("Ningun puerto de tus proyectos registrados esta ocupado.")
        return

    table = Table(box=None, pad_edge=False, show_header=False)
    for item in encontrados:
        # Los proyectos que reclaman ese puerto, no el dueño del proceso: el
        # proceso es un desconocido y por eso esta en esta lista.
        reclaman = ", ".join(item["projects"])
        table.add_row(
            f"  [bold]:{item['port']}[/]",
            f"{item['name']} (pid {item['pid']})",
            f"[dim]lo declara {reclaman}[/]",
        )
    console.print(f"Ocupando puertos de tus proyectos ({len(encontrados)}):")
    console.print(table)
    console.print("[dim]Si alguno lo arrancaste vos desde otra terminal, tambien se cierra.[/]")
    if not yes and not typer.confirm("Cerrarlos?", default=False):
        console.print("Cancelado.")
        return

    liberados = sum(
        1
        for item in encontrados
        if _release(
            item["port"],
            yes=True,
            force=force,
            expected_pid=item["pid"],
            expected_create_time=item["create_time"],
        )
    )
    color = "green" if liberados == len(encontrados) else "yellow"
    console.print(f"[{color}]{liberados} de {len(encontrados)} cerrados.[/]")
    if liberados < len(encontrados):
        raise typer.Exit(1)


def _confirm_detected(services: list[config.Service], yes: bool) -> bool:
    """Muestra lo detectado y pide confirmacion: nadie quiere arrancar a ciegas."""
    console.print("[dim]Sin stack.yaml. Detectado:[/]")
    table = Table(box=None, pad_edge=False, show_header=False)
    for service in services:
        port = str(service.port) if service.port else "[dim]al arrancar[/]"
        table.add_row(f"  [bold]{service.name}[/]", service.command, port)
    console.print(table)
    console.print("[dim]Para congelarlo en un archivo editable: stackhelx init[/]")
    return yes or typer.confirm("Arrancar?", default=True)


def _free_ports(services: list[config.Service], yes: bool, force: bool) -> None:
    """Libera los puertos declarados que tenga otro proceso, antes de arrancar."""
    for service in services:
        if not service.port:
            continue
        status = ports.scan(service.port)
        if status.free:
            continue
        motor = ports.proxy_owner(status)
        if motor:
            console.print(
                f"[dim]{service.name}: el puerto {service.port} ya lo publica {motor}, "
                f"no hay nada que liberar.[/]"
            )
            continue
        if not _release(service.port, yes, force):
            err.print(f"Puerto {service.port} sigue ocupado ({service.name}). Cancelado.")
            raise typer.Exit(1)


@app.command("up")
def up_cmd(
    profile: str = typer.Option(None, "--profile", "-p", help="Perfil de stack.yaml."),
    env_file: str = typer.Option(None, "--env-file", "-e", help="Ruta al archivo .env personalizado."),
    yes: bool = typer.Option(False, "--yes", "-y", help="No preguntar nada, arrancar."),
    force: bool = typer.Option(False, "--force", help="kill() si ignora terminate()."),
    free: bool = typer.Option(
        True, "--free/--no-free", help="Liberar los puertos declarados antes de arrancar."
    ),
    timeout: float = typer.Option(60.0, help="Segundos de espera por servicio."),
) -> None:
    """Levanta el stack: libera puertos, arranca en orden y sigue los logs."""
    _levantar(Path.cwd(), profile, yes, force, free, timeout, env_file=env_file)


def _levantar(
    root: Path,
    profile: str | None,
    yes: bool,
    force: bool,
    free: bool,
    timeout: float,
    env_file: str | None = None,
) -> None:
    """El cuerpo de `up`, por raiz explicita. `switch` levanta otro directorio."""
    extra_env: dict[str, str] = {}
    if env_file:
        custom_env = (root / env_file).resolve()
        if not custom_env.is_file():
            err.print(f"No se encontró el archivo env: {env_file}")
            raise typer.Exit(1)
        extra_env = config.parse_env_file(custom_env)

    try:
        stack = detect.stack_for(root)
        services = stack.resolve(profile)
    except config.ConfigError as exc:
        err.print(str(exc))
        raise typer.Exit(1)

    console.print(f"[bold]{stack.name}[/] [dim]{stack.path}[/]")

    if stack.detected and not _confirm_detected(services, yes):
        raise typer.Exit(1)

    if free:
        _free_ports(services, yes, force)

    engine = runner.Runner(stack, console=console, timeout=timeout, extra_env=extra_env)
    try:
        engine.up(profile)
    except runner.StartupError as exc:
        err.print(f"Fallo el arranque: {exc}")
        raise typer.Exit(1)

    if all(p.service.detached for p in engine.procs):
        console.print(
            "[green]Todo listo.[/] Servicios detached, nada que seguir. "
            "Para bajarlos: [bold]stackhelx down[/]"
        )
        return

    console.print("[green]Todo listo.[/] Ctrl-C para apagar.")
    try:
        engine.follow()
    except KeyboardInterrupt:
        console.print()
    finally:
        engine.down()


MARCA = {"ok": "[green]ok   [/]", "warn": "[yellow]aviso[/]", "fail": "[red]FALLA[/]"}


@app.command("doctor")
def doctor_cmd() -> None:
    """Revisa que puede impedir el arranque, sin arrancar nada."""
    checks = doctor.run(Path.cwd())

    ancho = max(len(c.name) for c in checks)
    for check in checks:
        # Sin `highlight`: rich le pone color a los numeros y a las rutas, y
        # esta salida es para copiar y pegar, no para mirar.
        console.print(
            f"{MARCA[check.level]} {check.name.ljust(ancho)}  {check.detail}", highlight=False
        )
        if check.fix and check.level != "ok":
            console.print(f"{' ' * (ancho + 8)}[dim]-> {check.fix}[/]", highlight=False)

    if doctor.blocking(checks):
        raise typer.Exit(1)


def _correr_stops(apagables: list[config.Service]) -> int:
    """Corre los `stop:` en el orden que se le da. Devuelve cuantos fallaron."""
    fallaron = 0
    for service in apagables:
        console.print(f"[dim]{service.name} | $ {service.stop}[/]")
        done = runner.run_stop(service)
        if done is None:
            err.print(f"{service.name}: el apagado no termino en {runner.STOP_TIMEOUT}s")
            fallaron += 1
            continue
        for line in (done.stdout or "").splitlines():
            console.print(f"[dim]{service.name} |[/] {line.rstrip()}")
        if done.returncode != 0:
            err.print(f"{service.name}: el apagado fallo con codigo {done.returncode}")
            fallaron += 1
    return fallaron


@app.command("down")
def down_cmd(
    profile: str = typer.Option(None, "--profile", "-p", help="Perfil de stack.yaml."),
) -> None:
    """Apaga lo que sobrevive a la terminal: contenedores y demas servicios detached."""
    try:
        stack = detect.stack_for(Path.cwd())
        services = stack.resolve(profile)
    except config.ConfigError as exc:
        err.print(str(exc))
        raise typer.Exit(1)

    console.print(f"[bold]{stack.name}[/] [dim]{stack.path}[/]")

    # En orden inverso al de arranque, igual que `Runner.down`: lo que depende
    # de algo se baja antes que aquello de lo que depende.
    apagables = [s for s in reversed(services) if s.stop]
    if not apagables:
        console.print(
            "Ningun servicio declara [bold]stop[/]. Los que arranca "
            "[bold]stackhelx up[/] son hijos de esa terminal y se apagan con Ctrl-C."
        )
        return

    if _correr_stops(apagables):
        raise typer.Exit(1)
    console.print("[green]Apagado.[/]")


@app.command("switch")
def switch_cmd(
    proyecto: str = typer.Argument(..., help="Nombre o ruta de un proyecto registrado."),
    profile: str = typer.Option(None, "--profile", "-p", help="Perfil de stack.yaml."),
    yes: bool = typer.Option(False, "--yes", "-y", help="No preguntar nada, arrancar."),
    force: bool = typer.Option(False, "--force", help="kill() si ignora terminate()."),
    timeout: float = typer.Option(60.0, help="Segundos de espera por servicio."),
) -> None:
    """Baja los proyectos que le pisan los puertos a este, y lo levanta.

    Rotar entre proyectos con puertos que se pisan son hoy tres comandos en tres
    carpetas. El registro sabe quien declara que puerto, asi que puede hacerlo
    solo.
    """
    root = _resolver_proyecto(proyecto)
    try:
        stack = detect.stack_for(root)
        services = stack.resolve(profile)
    except config.ConfigError as exc:
        err.print(f"{root}: {exc}")
        raise typer.Exit(1)

    for rival in _rivales(root, services):
        _bajar(rival)

    _levantar(root, profile, yes, force, True, timeout)


def _resolver_proyecto(nombre: str) -> Path:
    """Un proyecto registrado, por nombre de carpeta o por ruta."""
    conocidos = registry.paths()
    if not conocidos:
        err.print("No hay proyectos registrados. Registra uno con: stackhelx add .")
        raise typer.Exit(1)

    candidato = Path(nombre).expanduser()
    if candidato.is_dir() and candidato.resolve() in conocidos:
        return candidato.resolve()

    iguales = [p for p in conocidos if p.name.lower() == nombre.lower()]
    if len(iguales) == 1:
        return iguales[0]
    if iguales:
        err.print(f"'{nombre}' es ambiguo: {', '.join(str(p) for p in iguales)}")
        raise typer.Exit(1)

    err.print(
        f"'{nombre}' no es un proyecto registrado. "
        f"Conocidos: {', '.join(sorted(p.name for p in conocidos))}"
    )
    raise typer.Exit(1)


def _rivales(root: Path, services: list[config.Service]) -> list[Path]:
    """Proyectos registrados que declaran alguno de los puertos de `services`.

    Solo los que chocan, y no todo lo registrado: parar una base de datos que
    nadie disputa no ayuda a arrancar, y es lo que mas cuesta volver a levantar.
    Cuando todo choca, esto ya es todo lo demas.
    """
    wanted = {s.port for s in services if s.port}
    if not wanted:
        return []
    mapa = registry.declared_ports()
    return sorted({p for port in wanted for p in mapa.get(port, []) if p != root})


def _bajar(root: Path) -> None:
    """Corre los `stop:` de otro proyecto. Un fallo avisa y no corta el switch.

    Lo que no tiene `stop:` no se toca aca: son hijos de otra terminal, y si
    igual siguen ocupando el puerto los agarra `_free_ports` al levantar, que ya
    pregunta antes de matar a nadie.
    """
    try:
        stack = detect.stack_for(root)
        apagables = [s for s in reversed(stack.resolve()) if s.stop]
    except config.ConfigError as exc:
        err.print(f"[dim]{root.name}: no se pudo leer para bajarlo ({exc})[/]")
        return
    if not apagables:
        return
    console.print(f"[bold]Bajando {stack.name}[/] [dim]{root}[/]")
    if _correr_stops(apagables):
        err.print(f"{stack.name}: quedo algo sin bajar, el puerto puede seguir ocupado.")


def _abrir(url: str) -> None:
    console.print(f"Abriendo [bold]{url}[/]")
    import webbrowser

    webbrowser.open(url)


@app.command("open")
def open_cmd(
    port: int = typer.Argument(None, min=1, max=65535, help="Puerto, si ya lo sabes."),
) -> None:
    """Abre en el navegador el primer servicio del stack que conteste HTTP."""
    if port is None:
        try:
            stack = detect.stack_for(Path.cwd())
        except config.ConfigError as exc:
            err.print(f"{exc}\nPasa el puerto como argumento: stackhelx open 3000")
            raise typer.Exit(1)
        # En orden de arranque: los contenedores primero, el frontend al final.
        # Se recorre al reves porque lo que uno quiere abrir suele ser lo ultimo.
        # Cada candidato es (url declarada o None, puerto o None): un puerto
        # suelto no es un servicio y fabricarle uno seria un objeto que miente.
        candidates = [
            (runner.service_url(s), s.port)
            for s in reversed(stack.resolve())
            if s.port or s.url
        ]
    else:
        candidates = [(None, port)]

    for declarada, puerto in candidates:
        # Sin puerto no hay que sondear: no hay nada que preguntar. Puede abrir
        # una pestaña muerta si el stack no esta arriba, que es exactamente lo
        # que pasa hoy escribiendo la URL a mano, y es preferible a no poder
        # abrirla nunca.
        if puerto is None:
            # Una `url:` con una variable sin valor no resuelve, y sin `port:` no
            # hay a que caer: `_abrir(None)` reventaba con un TypeError crudo en
            # la terminal. Saltearlo deja contestar al candidato siguiente, y si
            # no queda ninguno cae en el error de abajo, que es lo que hay que
            # leer cuando falta la variable.
            if declarada is None:
                continue
            _abrir(declarada)
            return
        if runner.speaks_http(puerto):
            _abrir(declarada or f"http://localhost:{puerto}")
            return

    err.print(
        "Ningun puerto del stack contesta HTTP. "
        "Arrancalo con 'stackhelx up' o pasa el puerto como argumento."
    )
    raise typer.Exit(1)


@app.command("add")
def add_cmd(
    path: str = typer.Argument(".", help="Directorio del proyecto."),
) -> None:
    """Registra un proyecto para que aparezca en la interfaz."""
    try:
        registered = registry.add(path)
    except registry.RegistryError as exc:
        err.print(str(exc))
        raise typer.Exit(1)
    console.print(f"Registrado: [bold]{registered}[/]")


@app.command("list")
@app.command("ls")
def list_cmd() -> None:
    """Lista los proyectos registrados para la interfaz web."""
    items = registry.paths()
    if not items:
        console.print("[dim]No hay proyectos registrados. Registra uno con: stackhelx add <ruta>[/]")
        return

    declared = registry.declared_ports()
    collisions = registry.find_collisions()

    table = Table(box=None, pad_edge=False)
    table.add_column("ID")
    table.add_column("NOMBRE")
    table.add_column("PUERTOS")
    table.add_column("RUTA")
    for path in items:
        pid = registry.project_id(path)
        proj_ports = sorted(p for p, projs in declared.items() if path in projs)
        port_labels = []
        for p in proj_ports:
            if p in collisions:
                port_labels.append(f"[yellow]{p}[/]")
            else:
                port_labels.append(str(p))
        ports_str = ", ".join(port_labels) if port_labels else "-"
        table.add_row(pid, registry.name_of(path), ports_str, str(path))
    console.print(table)
    if collisions:
        console.print("[dim yellow]Puertos en amarillo se disputan entre dos o mas proyectos.[/]")



@app.command("remove")
@app.command("rm")
def remove_cmd(
    target: str = typer.Argument(..., help="Ruta o ID del proyecto a des-registrar."),
) -> None:
    """Des-registra un proyecto de la interfaz."""
    pids = {registry.project_id(p): p for p in registry.paths()}
    if target in pids:
        pid = target
        path = pids[pid]
    else:
        resolved = Path(target).expanduser().resolve()
        pid = registry.project_id(resolved)
        path = resolved

    if registry.remove(pid):
        console.print(f"Quitado: [bold]{path}[/]")
    else:
        err.print(f"El proyecto '{target}' no esta registrado.")
        raise typer.Exit(1)


@app.command("init")
def init_cmd(
    path: str = typer.Argument(".", help="Directorio del proyecto."),
) -> None:
    """Escribe un stack.yaml con lo detectado, para editarlo a mano."""
    root = Path(path).expanduser().resolve()
    try:
        target = detect.freeze(root)
    except config.ConfigError as exc:
        err.print(str(exc))
        raise typer.Exit(1)

    console.print(f"Escrito: [bold]{target}[/]")
    console.print("[dim]Revisalo antes de confiar en el.[/]")


@app.command("export")
def export_cmd(
    out: Path | None = typer.Argument(None, help="Archivo JSON de destino (opcional)."),
) -> None:
    """Exporta las rutas de todos los proyectos registrados en formato JSON."""
    data = registry.export_data()
    text = json.dumps(data, indent=2)
    if out:
        out.write_text(text, encoding="utf-8")
        console.print(f"Exportados {len(data)} proyectos en [bold]{out}[/]")
    else:
        console.print(text)


@app.command("import")
def import_cmd(
    src: Path = typer.Argument(..., help="Archivo JSON con rutas de proyectos."),
) -> None:
    """Importa masivamente proyectos registrados desde un archivo JSON."""
    if not src.is_file():
        err.print(f"El archivo '{src}' no existe.")
        raise typer.Exit(1)
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
        imported = registry.import_data(data)
        console.print(f"Importados [bold]{len(imported)}[/] proyectos desde [bold]{src}[/]")
    except Exception as exc:
        err.print(f"Error al importar desde '{src}': {exc}")
        raise typer.Exit(1)


@app.command("serve")
def serve_cmd(
    port: int = typer.Option(7666, min=1, max=65535, help="Puerto de la interfaz."),
    no_open: bool = typer.Option(False, "--no-open", help="No abrir el navegador."),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Registrar cada peticion que llega."
    ),
) -> None:
    """Levanta la interfaz web local."""
    try:
        import uvicorn

        from . import server
    except ImportError:
        err.print("Falta fastapi o uvicorn. Reinstala stackhelx: pipx reinstall stackhelx")
        raise typer.Exit(1)

    # Antes de imprimir la URL: uvicorn atrapa el error de bind y sale por su
    # cuenta, asi que el `except OSError` de abajo nunca corria y el usuario
    # veia un ERROR de winerror en vez del comando que lo arregla. Averiguar
    # quien tiene un puerto es lo que esta herramienta hace.
    if not ports.is_free(port):
        ocupante = ports.scan(port)
        quien = ocupante.name or "un proceso desconocido"
        err.print(f"El puerto {port} ya esta ocupado por {quien} (pid {ocupante.pid}).")
        try:
            suggested = ports.suggest_alternative(port)
            err.print(f"Cerralo con: stackhelx free {port}   o arranca con: --port {suggested}")
        except Exception:
            err.print(f"Cerralo con: stackhelx free {port}   o arranca con: --port <otro>")
        raise typer.Exit(1)

    token = registry.token()
    url = f"http://127.0.0.1:{port}/?token={token}"

    console.print(f"StackHelx en [bold]http://127.0.0.1:{port}[/]")
    console.print("[dim]Solo loopback. El token va en la URL de abajo.[/]")
    console.print(url)

    if not no_open:
        import webbrowser

        webbrowser.open(url)

    try:
        # Por defecto callado: la interfaz sondea cada 2.5s y el log de acceso
        # tapa cualquier otra cosa. Con --verbose se ve que pide el navegador,
        # que es la unica forma de saber si esta hablando con este servidor o
        # mostrando una pagina vieja de su cache.
        uvicorn.run(
            server.create_app(token),
            host="127.0.0.1",
            port=port,
            log_level="info" if verbose else "warning",
            access_log=verbose,
        )
    except OSError as exc:
        err.print(f"No se pudo iniciar el servidor en 127.0.0.1:{port}: {exc}")
        err.print(f"El puerto {port} esta ocupado. Podes usar 'stackhelx free {port}' o '--port <otro>'.")
        raise typer.Exit(1)


def _version(pedido: bool) -> None:
    if pedido:
        console.print(__version__)
        raise typer.Exit()


# El flag ademas del subcomando: `--version` es lo que prueba cualquiera que
# acaba de instalar la herramienta, y `no_args_is_help` hace que un `stackhelx`
# pelado muestre la ayuda antes de llegar aca. Eager para que conteste sin pedir
# un comando.
@app.callback()
def _root(
    version: bool = typer.Option(
        False, "--version", callback=_version, is_eager=True,
        help="Muestra la version instalada.",
    ),
) -> None:
    pass


@app.command(
    "run",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def run_cmd(
    ctx: typer.Context,
    script: str = typer.Argument(None, help="Nombre del script a ejecutar"),
) -> None:
    """Ejecuta un script o pipeline de tareas definido en stack.yaml."""
    try:
        stack = detect.stack_for(Path.cwd())
    except config.ConfigError as exc:
        err.print(f"{exc}")
        raise typer.Exit(1)

    if not script:
        if not stack.scripts:
            console.print("[dim]No hay scripts declarados en stack.yaml.[/]")
            raise typer.Exit(0)
        table = Table(box=None, pad_edge=False)
        table.add_column("SCRIPT", style="bold cyan")
        table.add_column("COMANDOS")
        for name, cmds in stack.scripts.items():
            table.add_row(name, " && ".join(cmds))
        console.print(table)
        raise typer.Exit(0)

    try:
        code = scripts.run_script(stack, script, extra_args=ctx.args, console=console)
    except config.ConfigError as exc:
        err.print(f"{exc}")
        raise typer.Exit(1)

    if code != 0:
        raise typer.Exit(code)


@app.command("share")
def share_cmd(
    target: str = typer.Argument(
        None,
        help="Servicio o puerto a compartir (ej. 3000, web). Sin argumentos, usa el puerto principal.",
    ),
    provider: str = typer.Option(
        None,
        "--provider",
        "-p",
        help="Proveedor de tuneles: cloudflared, ngrok, lt, tailscale.",
    ),
) -> None:
    """Expone un servicio local a internet mediante un tunel seguro."""
    import time

    port: int | None = None
    if target and target.isdigit():
        # `target` es texto porque tambien acepta el nombre de un servicio, asi
        # que se pierde el `min`/`max` que traen los demas comandos. Sin esto,
        # `stackhelx share 0` levantaba el cliente de tuneles contra 127.0.0.1:0.
        try:
            port = ports.check_port(int(target))
        except ValueError as exc:
            err.print(str(exc))
            raise typer.Exit(1)
    else:
        try:
            stack = detect.stack_for(Path.cwd())
        except config.ConfigError as exc:
            err.print(f"{exc}\nEspecifica el puerto a compartir: stackhelx share 3000")
            raise typer.Exit(1)

        if target and target in stack.services:
            svc = stack.services[target]
            if svc.port:
                port = svc.port
            else:
                err.print(f"El servicio '{target}' no tiene un puerto fijo declarado.")
                raise typer.Exit(1)
        elif target:
            err.print(f"Servicio o puerto '{target}' no encontrado en el stack.")
            raise typer.Exit(1)
        else:
            ports_list = stack.ports()
            if not ports_list:
                err.print(f"{stack.path} no declara ningun puerto.")
                raise typer.Exit(1)
            port = ports_list[-1]

    console.print(f"[bold cyan]Iniciando tunel hacia 127.0.0.1:{port}...[/]")
    try:
        tun = tunnel.start_tunnel(port, provider=provider)
    except tunnel.TunnelError as exc:
        err.print(f"[bold red]Error:[/] {exc}")
        raise typer.Exit(1)

    console.print(f"[bold green]Tunel activo![/] Proveedor: [bold]{tun.provider}[/]")
    console.print(f"Local:   [cyan]http://127.0.0.1:{port}[/]")
    console.print(f"Publico: [bold underline green]{tun.url}[/]")
    console.print("[dim]Presiona Ctrl-C para cerrar el tunel.[/]")

    try:
        while tun.proc.poll() is None:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        tun.stop()
        console.print("\n[dim]Tunel cerrado.[/]")


@app.command("clean")
def clean_cmd(
    solo: list[str] = typer.Option(
        None,
        "--solo",
        "-s",
        help=(
            "Limpiar solo estas categorias: containers, images, networks, cache. "
            "Repetible. Sin esto, las cuatro."
        ),
    ),
    volumes: bool = typer.Option(
        False,
        "--volumes",
        "-v",
        help="Elimina tambien volumenes anonimos/huerfanos de Docker.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="No preguntar."),
) -> None:
    """Limpia contenedores parados, imagenes sin tag y recursos huerfanos de Docker."""
    # `--solo` acota, `--volumes` suma. Son cosas distintas: una elige de lo que
    # se regenera, la otra agrega lo que tiene datos adentro.
    objetivos = list(solo) if solo else list(docker.DEFAULT_TARGETS)
    desconocidos = [t for t in objetivos if t not in docker.DEFAULT_TARGETS]
    if desconocidos:
        err.print(
            f"Categoria desconocida: {', '.join(desconocidos)}. "
            f"Validas: {', '.join(docker.DEFAULT_TARGETS)}"
        )
        raise typer.Exit(1)
    if volumes:
        objetivos.append("volumes")
    # Preguntando, como `free`. Es el unico comando de la herramienta que borra
    # datos en vez de cerrar procesos, y era el unico que no preguntaba nada: el
    # boton de la interfaz ya pedia dos clicks y este se ejecutaba en silencio.
    if not yes:
        tabla = docker.usage()
        if tabla:
            console.print(tabla)
        console.print("Se borra: " + ", ".join(docker.ETIQUETAS[t] for t in objetivos if t != "volumes"))
        if volumes:
            console.print("[bold]Y los volumenes anonimos huerfanos, que tienen datos adentro.[/]")
        if not typer.confirm("Seguir?", default=False):
            console.print("Cancelado.")
            return

    console.print("[bold cyan]Ejecutando limpieza de recursos Docker...[/]")
    ok, msg = docker.prune(objetivos)
    if ok:
        console.print(f"[bold green]Listo:[/] {msg}")
    else:
        err.print(f"[bold red]Error al limpiar Docker:[/] {msg}")
        raise typer.Exit(1)


@app.command("mcp")
def mcp_cmd(
    show_config: bool = typer.Option(
        False,
        "--config",
        "-c",
        help="Muestra el bloque de configuracion JSON para Claude Desktop, Cursor o Antigravity.",
    ),
) -> None:
    """Inicia el servidor Model Context Protocol (MCP) sobre stdio para agentes de IA."""
    if show_config:
        cfg = {
            "mcpServers": {
                "stackhelx": {
                    "command": "stackhelx",
                    "args": ["mcp"],
                }
            }
        }
        console.print(json.dumps(cfg, indent=2))
        return
    mcp.serve_stdio()


@app.command("version")
def version_cmd() -> None:
    """Muestra la version instalada."""
    console.print(__version__)


@app.command("history")
def history_cmd(
    target: str = typer.Argument(None),
    limit: int = typer.Option(5, "--limit", "-n", help="Cantidad de arranques a mostrar"),
) -> None:
    """Muestra el historial de arranques del proyecto."""
    if limit < 1 or limit > history.MAX_LIMIT:
        err.print(f"[red]Error:[/] --limit debe ser entre 1 y {history.MAX_LIMIT}")
        raise typer.Exit(1)
        
    try:
        path = (Path.cwd() / (target or "")).resolve()
        stack = detect.stack_for(path)
        pid = registry.project_id(stack.root)
    except config.ConfigError as exc:
        err.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(1)
        
    runs = history.read(pid, limit=limit)
    if not runs:
        console.print(f"No hay historial para el proyecto [bold]{stack.name}[/]")
        return
        
    table = Table(title=f"Historial de arranques: {stack.name}")
    table.add_column("Fecha")
    table.add_column("Perfil")
    table.add_column("Duración")
    table.add_column("Resultado")
    
    for r in reversed(runs):
        fecha = r.get("timestamp", "").split("T")[0] + " " + r.get("timestamp", "T")[:16].split("T")[-1]
        perfil = r.get("profile") or "-"
        dur = f"{r.get('duration_s', 0)}s"
        res = r.get("result", "unknown")
        
        color = "green" if res == "running" else "red" if res == "error" else "yellow"
        res_format = f"[{color}]{res}[/]"
        if res == "error" and "error" in r:
            res_format += f"\n[dim]{r['error']}[/]"
            
        table.add_row(fecha, perfil, dur, res_format)

    console.print(table)


@app.command("test-stack")
def test_stack_cmd(
    target: str = typer.Argument(None, help="Ruta al proyecto o directorio"),
) -> None:
    """Valida la configuración del stack (puertos, dependencias, variables) sin levantar servicios."""
    path = (Path.cwd() / (target or "")).resolve()
    try:
        stack = detect.stack_for(path)
        services = stack.resolve()
    except config.ConfigError as exc:
        err.print(f"[bold red]Configuración inválida:[/] {exc}")
        raise typer.Exit(1)

    console.print(f"Validando stack [bold]{stack.name}[/] en [dim]{stack.root}[/]...")
    console.print(f"[green]OK:[/] {len(services)} servicio(s) resueltos en orden topológico:")
    for s in services:
        deps = f" (espera a: {', '.join(s.needs)})" if s.needs else ""
        port_info = f" -> puerto {s.port}" if s.port else f" ({s.ready})"
        console.print(f"  - [cyan]{s.name}[/]: [dim]{s.command}[/]{port_info}{deps}")

    # Verificar estado de puertos
    declared_ports = stack.ports()
    if declared_ports:
        occupied = [p for p in declared_ports if not ports.is_free(p)]
        if occupied:
            console.print(f"[yellow]Aviso:[/] Puertos actualmente en uso: {', '.join(map(str, occupied))}")
        else:
            console.print("[green]OK:[/] Todos los puertos declarados están libres")

    # Sin el `✓` que habia aca: no existe en cp1252, o sea la pagina de
    # codigos con la que sale la consola de Windows, y el comando entero
    # terminaba en UnicodeEncodeError despues de haber validado bien. Los
    # acentos si entran en cp1252, por eso se quedan.
    console.print("\n[bold green]Stack validado con éxito.[/]")


@app.command("logs")
def logs_cmd(
    target: str = typer.Argument(None, help="Ruta al proyecto"),
    service: str = typer.Option(None, "--service", "-s", help="Filtrar por nombre de servicio"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Seguir logs en tiempo real"),
    server_port: int = typer.Option(7666, "--port", "-p", help="Puerto del servidor de StackHelx"),
) -> None:
    """Muestra o sigue los logs del proyecto en ejecución en StackHelx."""
    import time
    import urllib.request

    path = (Path.cwd() / (target or "")).resolve()
    try:
        stack = detect.stack_for(path)
        pid = registry.project_id(stack.root)
    except config.ConfigError as exc:
        err.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(1)

    token = registry.token()
    base_url = f"http://127.0.0.1:{server_port}"

    seq = 0
    retries = 0
    while True:
        req = urllib.request.Request(
            f"{base_url}/api/projects/{pid}/logs?since={seq}",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            retries = 0
        except Exception:
            if follow and retries < 3:
                retries += 1
                time.sleep(1.0)
                continue
            err.print(
                f"[yellow]No se pudo conectar con StackHelx en {base_url}. Asegúrate de que `stackhelx serve` está corriendo.[/]"
            )
            raise typer.Exit(1)

        lines = data.get("lines", [])
        for item in lines:
            text = item.get("text", "")
            seq = max(seq, item.get("seq", seq))
            if not service or service in text:
                console.print(text)

        if not follow:
            if not lines and seq == 0:
                console.print(f"[dim]No hay logs disponibles para {stack.name}.[/]")
            break

        time.sleep(0.5)


@app.command("stats")
@app.command("top")
def stats_cmd(
    target: str = typer.Argument(None, help="Ruta al proyecto"),
    server_port: int = typer.Option(7666, "--port", "-p", help="Puerto del servidor de StackHelx"),
) -> None:
    """Muestra el uso de CPU y memoria de los servicios en ejecución."""
    import urllib.request

    path = (Path.cwd() / (target or "")).resolve()
    try:
        stack = detect.stack_for(path)
        pid = registry.project_id(stack.root)
    except config.ConfigError as exc:
        err.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(1)

    token = registry.token()
    base_url = f"http://127.0.0.1:{server_port}"
    req = urllib.request.Request(
        f"{base_url}/api/projects/{pid}/metrics",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        err.print(
            f"[yellow]No se pudo conectar con StackHelx en {base_url}. Asegúrate de que `stackhelx serve` está corriendo.[/]"
        )
        raise typer.Exit(1)

    metrics = data.get("metrics", {})
    if not metrics:
        console.print(f"[dim]No hay servicios activos en {stack.name}.[/]")
        return

    table = Table(title=f"Métricas en tiempo real: {stack.name}")
    table.add_column("Servicio", style="cyan")
    table.add_column("PID", style="dim")
    table.add_column("CPU %", justify="right")
    table.add_column("Memoria (MB)", justify="right")

    for name, s in metrics.items():
        cpu = f"{s.get('cpu_percent', 0.0)}%"
        mem = f"{s.get('memory_mb', 0.0)} MB"
        table.add_row(name, str(s.get("pid", "-")), cpu, mem)

    console.print(table)


@app.command("mcp-status")
def mcp_status_cmd(
    server_port: int = typer.Option(7666, "--port", "-p", help="Puerto del servidor de StackHelx"),
) -> None:
    """Muestra la telemetría y estado de llamadas MCP para agentes IA."""
    import urllib.request

    token = registry.token()
    base_url = f"http://127.0.0.1:{server_port}"
    req = urllib.request.Request(
        f"{base_url}/api/mcp/activity",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        data = mcp.get_telemetry()

    total = data.get("total_calls", 0)
    rate = data.get("active_rate_per_min", 0)
    max_rate = data.get("rate_limit_max", 30)
    console.print(
        f"[bold]Servidor MCP StackHelx[/] · Llamadas: [cyan]{total}[/] · Cuota: [green]{rate}/{max_rate}[/] req/min"
    )

    by_tool = data.get("by_tool", {})
    if by_tool:
        tool_table = Table(title="Invocaciones por herramienta MCP")
        tool_table.add_column("Herramienta", style="cyan")
        tool_table.add_column("Llamadas", justify="right")
        for tool, count in sorted(by_tool.items(), key=lambda x: -x[1]):
            tool_table.add_row(tool, str(count))
        console.print(tool_table)

    events = data.get("recent_events", [])
    if events:
        ev_table = Table(title="Invocaciones recientes (últimas 10)")
        ev_table.add_column("Timestamp", style="dim")
        ev_table.add_column("Herramienta", style="cyan")
        ev_table.add_column("Duración", justify="right")
        ev_table.add_column("Estado")
        for ev in events[:10]:
            st = ev.get("status", "ok")
            st_style = (
                "[green]ok[/]"
                if st == "ok"
                else (f"[yellow]{st}[/]" if st == "rate_limited" else f"[red]{st}[/]")
            )
            dur = f"{ev.get('duration_ms', 0):.1f} ms"
            ts = ev.get("timestamp", "").replace("T", " ")[:19]
            ev_table.add_row(ts, ev.get("tool", "-"), dur, st_style)
        console.print(ev_table)
    elif not by_tool:
        console.print("[dim]Sin actividad reciente de agentes MCP.[/]")


if __name__ == "__main__":
    app()
