"""StackHelx CLI."""

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
    help="Local development environment orchestrator.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err = Console(stderr=True)

PortArg = typer.Argument(..., min=1, max=65535)


def _row(status: ports.PortStatus, cmd_width: int) -> tuple[str, ...]:
    if status.free:
        return (str(status.port), "[green]free[/]", "-", "-", "-")
    if status.owner_unknown:
        return (str(status.port), "[red]in use[/]", "?", "[dim]no permissions[/]", "-")
    cmd = status.cmdline or "-"
    if len(cmd) > cmd_width:
        cmd = cmd[: cmd_width - 1] + "…"
    return (str(status.port), "[red]in use[/]", str(status.pid), status.name or "?", cmd)


@app.command("ports")
def ports_cmd(
    port: list[int] = typer.Argument(None, min=1, max=65535),
) -> None:
    """Inspect TCP ports. Without arguments, checks ports from stack.yaml or auto-detection."""
    if not port:
        try:
            stack = detect.stack_for(Path.cwd())
        except config.ConfigError as exc:
            err.print(f"{exc}\nPass ports as arguments: stackhelx ports 3000 8080")
            raise typer.Exit(1)
        port = stack.ports()
        if not port:
            err.print(
                f"{stack.path} does not declare any ports. Services with "
                "'ready: listen' only get a port once started."
            )
            raise typer.Exit(1)

    table = Table(box=None, pad_edge=False)
    for column in ("PORT", "STATUS", "PID", "PROCESS", "COMMAND"):
        table.add_column(column)
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
    """Free an occupied port. If expected_pid and expected_create_time are passed,
    verifies that the process did not change between confirmation and termination.
    """
    status = ports.scan(port)
    if status.free:
        console.print(f"Port {port} was already free.")
        return True

    if status.pid is None:
        err.print(
            f"Port {port} is in use, but the process is not visible with current "
            "permissions. Try from an elevated terminal."
        )
        return False

    if expected_pid is not None and status.pid != expected_pid:
        err.print(
            f"Port {port} changed process (now PID {status.pid} [{status.name}]). "
            "Skipped for safety."
        )
        return False

    target_pid = expected_pid if expected_pid is not None else status.pid
    target_create_time = (
        expected_create_time if expected_create_time is not None else status.create_time
    )

    if expected_pid is None:
        console.print(f"Port {port} occupied by PID {status.pid} ([bold]{status.name}[/])")
        if status.cmdline:
            console.print(f"  [dim]{status.cmdline}[/]")

        if not yes and not typer.confirm(f"Close PID {status.pid}?"):
            return False

    try:
        ports.kill(target_pid, target_create_time, force=force, port=port)
    except ports.KillRefused as exc:
        err.print(f"Refused: {exc}")
        return False
    except psutil.NoSuchProcess:
        pass
    except psutil.AccessDenied:
        err.print(
            f"No permission to close PID {target_pid}. "
            "Try from an elevated terminal."
        )
        return False

    console.print(f"PID {target_pid} closed. Port {port} [green]free[/].")
    return True


@app.command("free")
def free_cmd(
    port: int | None = typer.Argument(None, help="Port to free (e.g. 8080)."),
    all_ports: bool = typer.Option(
        False, "--all", "-a", help="Free all stray ports across registered projects."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
    force: bool = typer.Option(False, "--force", help="Use kill() if terminate() is ignored."),
) -> None:
    """Free an occupied port or all stray ports across registered projects."""
    if not all_ports and port is None:
        err.print("Specify a port (e.g. stackhelx free 8080) or pass --all")
        raise typer.Exit(1)

    if all_ports:
        return _free_all(yes, force)

    if ports.is_free(port):
        console.print(f"Port {port} [green]free[/].")
        return
    if not _release(port, yes, force):
        console.print(f"Next free port: [bold]{ports.next_free(port)}[/]")


def _free_all(yes: bool, force: bool) -> None:
    """Close processes holding ports declared by registered projects."""
    encontrados = registry.find_orphans()
    if not encontrados:
        console.print("No ports from your registered projects are currently occupied.")
        return

    table = Table(box=None, pad_edge=False, show_header=False)
    for item in encontrados:
        reclaman = ", ".join(item["projects"])
        table.add_row(
            f"  [bold]:{item['port']}[/]",
            f"{item['name']} (pid {item['pid']})",
            f"[dim]declared by {reclaman}[/]",
        )
    console.print(f"Occupying ports from your projects ({len(encontrados)}):")
    console.print(table)
    console.print("[dim]If you started any of these from another terminal, it will also be closed.[/]")
    if not yes and not typer.confirm("Close them?", default=False):
        console.print("Cancelled.")
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
    console.print(f"[{color}]{liberados} of {len(encontrados)} closed.[/]")
    if liberados < len(encontrados):
        raise typer.Exit(1)


def _confirm_detected(services: list[config.Service], yes: bool) -> bool:
    """Show detected services and ask for confirmation before starting."""
    console.print("[dim]No stack.yaml found. Detected:[/]")
    table = Table(box=None, pad_edge=False, show_header=False)
    for service in services:
        port = str(service.port) if service.port else "[dim]on startup[/]"
        table.add_row(f"  [bold]{service.name}[/]", service.command, port)
    console.print(table)
    console.print("[dim]To freeze into an editable file: stackhelx init[/]")
    return yes or typer.confirm("Start?", default=True)


def _free_ports(services: list[config.Service], yes: bool, force: bool) -> None:
    """Free declared ports held by other processes before starting."""
    for service in services:
        if not service.port:
            continue
        status = ports.scan(service.port)
        if status.free:
            continue
        motor = ports.proxy_owner(status)
        if motor:
            console.print(
                f"[dim]{service.name}: port {service.port} is already published by {motor}, "
                f"nothing to free.[/]"
            )
            continue
        if not _release(service.port, yes, force):
            err.print(f"Port {service.port} is still occupied ({service.name}). Cancelled.")
            raise typer.Exit(1)


@app.command("up")
def up_cmd(
    profile: str = typer.Option(None, "--profile", "-p", help="Profile from stack.yaml."),
    env_file: str = typer.Option(None, "--env-file", "-e", help="Path to custom .env file."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompts and start."),
    force: bool = typer.Option(False, "--force", help="Use kill() if terminate() is ignored."),
    free: bool = typer.Option(
        True, "--free/--no-free", help="Free declared ports before starting."
    ),
    timeout: float = typer.Option(60.0, help="Wait timeout in seconds per service."),
) -> None:
    """Start the stack: free ports, launch in topological order, and follow logs."""
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
    """Core logic for `up` by explicit root directory."""
    extra_env: dict[str, str] = {}
    if env_file:
        custom_env = (root / env_file).resolve()
        if not custom_env.is_file():
            err.print(f"Env file not found: {env_file}")
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
        err.print(f"Startup failed: {exc}")
        raise typer.Exit(1)

    if all(p.service.detached for p in engine.procs):
        console.print(
            "[green]All ready.[/] Detached services only, nothing to follow. "
            "To stop them: [bold]stackhelx down[/]"
        )
        return

    console.print("[green]All ready.[/] Ctrl-C to stop.")
    try:
        engine.follow()
    except KeyboardInterrupt:
        console.print()
    finally:
        engine.down()


MARCA = {"ok": "[green]ok   [/]", "warn": "[yellow]warn [/]", "fail": "[red]FAIL [/]"}


@app.command("doctor")
def doctor_cmd() -> None:
    """Diagnose environment issues that could prevent startup without starting anything."""
    checks = doctor.run(Path.cwd())

    ancho = max(len(c.name) for c in checks)
    for check in checks:
        console.print(
            f"{MARCA[check.level]} {check.name.ljust(ancho)}  {check.detail}", highlight=False
        )
        if check.fix and check.level != "ok":
            console.print(f"{' ' * (ancho + 8)}[dim]-> {check.fix}[/]", highlight=False)

    if doctor.blocking(checks):
        raise typer.Exit(1)


def _correr_stops(apagables: list[config.Service]) -> int:
    """Run `stop:` commands in the given order. Returns how many failed."""
    fallaron = 0
    for service in apagables:
        console.print(f"[dim]{service.name} | $ {service.stop}[/]")
        done = runner.run_stop(service)
        if done is None:
            err.print(f"{service.name}: shutdown timed out after {runner.STOP_TIMEOUT}s")
            fallaron += 1
            continue
        for line in (done.stdout or "").splitlines():
            console.print(f"[dim]{service.name} |[/] {line.rstrip()}")
        if done.returncode != 0:
            err.print(f"{service.name}: shutdown failed with exit code {done.returncode}")
            fallaron += 1
    return fallaron


@app.command("down")
def down_cmd(
    profile: str = typer.Option(None, "--profile", "-p", help="Profile from stack.yaml."),
) -> None:
    """Stop services that outlive the terminal: containers and other detached services."""
    try:
        stack = detect.stack_for(Path.cwd())
        services = stack.resolve(profile)
    except config.ConfigError as exc:
        err.print(str(exc))
        raise typer.Exit(1)

    console.print(f"[bold]{stack.name}[/] [dim]{stack.path}[/]")

    apagables = [s for s in reversed(services) if s.stop]
    if not apagables:
        console.print(
            "No service declares [bold]stop[/]. Services started by "
            "[bold]stackhelx up[/] are children of that terminal and stop with Ctrl-C."
        )
        return

    if _correr_stops(apagables):
        raise typer.Exit(1)
    console.print("[green]Stopped.[/]")


@app.command("switch")
def switch_cmd(
    proyecto: str = typer.Argument(..., help="Name or path of a registered project."),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile from stack.yaml."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompts and start."),
    force: bool = typer.Option(False, "--force", help="Use kill() if terminate() is ignored."),
    timeout: float = typer.Option(60.0, help="Wait timeout in seconds per service."),
) -> None:
    """Stop registered projects that conflict on ports with the target project, then start it."""
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
    """Resolve a registered project by folder name or path."""
    conocidos = registry.paths()
    if not conocidos:
        err.print("No registered projects. Register one with: stackhelx add .")
        raise typer.Exit(1)

    candidato = Path(nombre).expanduser()
    if candidato.is_dir() and candidato.resolve() in conocidos:
        return candidato.resolve()

    iguales = [p for p in conocidos if p.name.lower() == nombre.lower()]
    if len(iguales) == 1:
        return iguales[0]
    if iguales:
        err.print(f"'{nombre}' is ambiguous: {', '.join(str(p) for p in iguales)}")
        raise typer.Exit(1)

    err.print(
        f"'{nombre}' is not a registered project. "
        f"Known: {', '.join(sorted(p.name for p in conocidos))}"
    )
    raise typer.Exit(1)


def _rivales(root: Path, services: list[config.Service]) -> list[Path]:
    """Registered projects that declare any of the ports in `services`."""
    wanted = {s.port for s in services if s.port}
    if not wanted:
        return []
    mapa = registry.declared_ports()
    return sorted({p for port in wanted for p in mapa.get(port, []) if p != root})


def _bajar(root: Path) -> None:
    """Run `stop:` commands for another project."""
    try:
        stack = detect.stack_for(root)
        apagables = [s for s in reversed(stack.resolve()) if s.stop]
    except config.ConfigError as exc:
        err.print(f"[dim]{root.name}: could not read stack to stop it ({exc})[/]")
        return
    if not apagables:
        return
    console.print(f"[bold]Stopping {stack.name}[/] [dim]{root}[/]")
    if _correr_stops(apagables):
        err.print(f"{stack.name}: some services failed to stop; ports may still be in use.")


def _abrir(url: str) -> None:
    console.print(f"Opening [bold]{url}[/]")
    import webbrowser

    webbrowser.open(url)


@app.command("open")
def open_cmd(
    port: int = typer.Argument(None, min=1, max=65535, help="Port number, if known."),
) -> None:
    """Open the first HTTP-responding service of the stack in the browser."""
    if port is None:
        try:
            stack = detect.stack_for(Path.cwd())
        except config.ConfigError as exc:
            err.print(f"{exc}\nPass the port as an argument: stackhelx open 3000")
            raise typer.Exit(1)
        candidates = [
            (runner.service_url(s), s.port)
            for s in reversed(stack.resolve())
            if s.port or s.url
        ]
    else:
        candidates = [(None, port)]

    for declarada, puerto in candidates:
        if puerto is None:
            if declarada is None:
                continue
            _abrir(declarada)
            return
        if runner.speaks_http(puerto):
            _abrir(declarada or f"http://localhost:{puerto}")
            return

    err.print(
        "No port in the stack is responding over HTTP. "
        "Start it with 'stackhelx up' or pass the port as an argument."
    )
    raise typer.Exit(1)


@app.command("add")
def add_cmd(
    path: str = typer.Argument(".", help="Project directory."),
) -> None:
    """Register a project so it appears in the web dashboard."""
    try:
        registered = registry.add(path)
    except registry.RegistryError as exc:
        err.print(str(exc))
        raise typer.Exit(1)
    console.print(f"Registered: [bold]{registered}[/]")


@app.command("list")
@app.command("ls")
def list_cmd() -> None:
    """List projects registered in the web dashboard."""
    items = registry.paths()
    if not items:
        console.print("[dim]No registered projects. Register one with: stackhelx add <path>[/]")
        return

    declared = registry.declared_ports()
    collisions = registry.find_collisions()

    table = Table(box=None, pad_edge=False)
    table.add_column("ID")
    table.add_column("NAME")
    table.add_column("PORTS")
    table.add_column("PATH")
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
        console.print("[dim yellow]Ports in yellow are shared across two or more projects.[/]")


@app.command("remove")
@app.command("rm")
def remove_cmd(
    target: str = typer.Argument(..., help="Path or ID of the project to unregister."),
) -> None:
    """Unregister a project from the web dashboard."""
    pids = {registry.project_id(p): p for p in registry.paths()}
    if target in pids:
        pid = target
        path = pids[pid]
    else:
        resolved = Path(target).expanduser().resolve()
        pid = registry.project_id(resolved)
        path = resolved

    if registry.remove(pid):
        console.print(f"Removed: [bold]{path}[/]")
    else:
        err.print(f"Project '{target}' is not registered.")
        raise typer.Exit(1)


@app.command("init")
def init_cmd(
    path: str = typer.Argument(".", help="Project directory."),
) -> None:
    """Write a stack.yaml file from auto-detected services so you can edit it by hand."""
    root = Path(path).expanduser().resolve()
    try:
        target = detect.freeze(root)
    except config.ConfigError as exc:
        err.print(str(exc))
        raise typer.Exit(1)

    console.print(f"Wrote: [bold]{target}[/]")
    console.print("[dim]Review it before relying on it.[/]")


@app.command("export")
def export_cmd(
    out: Path | None = typer.Argument(None, help="Destination JSON file (optional)."),
) -> None:
    """Export all registered project paths in JSON format."""
    data = registry.export_data()
    text = json.dumps(data, indent=2)
    if out:
        out.write_text(text, encoding="utf-8")
        console.print(f"Exported {len(data)} projects to [bold]{out}[/]")
    else:
        console.print(text)


@app.command("import")
def import_cmd(
    src: Path = typer.Argument(..., help="JSON file containing project paths."),
) -> None:
    """Bulk-import registered projects from a JSON file."""
    if not src.is_file():
        err.print(f"File '{src}' does not exist.")
        raise typer.Exit(1)
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
        imported = registry.import_data(data)
        console.print(f"Imported [bold]{len(imported)}[/] projects from [bold]{src}[/]")
    except Exception as exc:
        err.print(f"Error importing from '{src}': {exc}")
        raise typer.Exit(1)


@app.command("serve")
def serve_cmd(
    port: int = typer.Option(7666, min=1, max=65535, help="Web dashboard port."),
    no_open: bool = typer.Option(False, "--no-open", help="Do not open the browser automatically."),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Log every incoming HTTP request."
    ),
) -> None:
    """Start the local web dashboard."""
    try:
        import uvicorn

        from . import server
    except ImportError:
        err.print("Missing fastapi or uvicorn. Reinstall stackhelx: pipx reinstall stackhelx")
        raise typer.Exit(1)

    if not ports.is_free(port):
        ocupante = ports.scan(port)
        quien = ocupante.name or "an unknown process"
        err.print(f"Port {port} is already occupied by {quien} (pid {ocupante.pid}).")
        try:
            suggested = ports.suggest_alternative(port)
            err.print(f"Free it with: stackhelx free {port}   or start with: --port {suggested}")
        except Exception:
            err.print(f"Free it with: stackhelx free {port}   or start with: --port <other>")
        raise typer.Exit(1)

    token = registry.token()
    url = f"http://127.0.0.1:{port}/?token={token}"

    console.print(f"StackHelx at [bold]http://127.0.0.1:{port}[/]")
    console.print("[dim]Loopback only. Access token is included in the URL below.[/]")
    console.print(url)

    if not no_open:
        import webbrowser

        webbrowser.open(url)

    try:
        uvicorn.run(
            server.create_app(token),
            host="127.0.0.1",
            port=port,
            log_level="info" if verbose else "warning",
            access_log=verbose,
        )
    except OSError as exc:
        err.print(f"Could not start server on 127.0.0.1:{port}: {exc}")
        err.print(f"Port {port} is occupied. Use 'stackhelx free {port}' or '--port <other>'.")
        raise typer.Exit(1)


def _version(pedido: bool) -> None:
    if pedido:
        console.print(__version__)
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version,
        is_eager=True,
        help="Show the installed version.",
    ),
) -> None:
    pass


@app.command(
    "run",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def run_cmd(
    ctx: typer.Context,
    script: str = typer.Argument(None, help="Name of the script to execute."),
) -> None:
    """Execute a script or task pipeline defined in stack.yaml."""
    try:
        stack = detect.stack_for(Path.cwd())
    except config.ConfigError as exc:
        err.print(f"{exc}")
        raise typer.Exit(1)

    if not script:
        if not stack.scripts:
            console.print("[dim]No scripts declared in stack.yaml.[/]")
            raise typer.Exit(0)
        table = Table(box=None, pad_edge=False)
        table.add_column("SCRIPT", style="bold cyan")
        table.add_column("COMMANDS")
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
        help="Service or port to share (e.g. 3000, web). Without arguments, uses the primary port.",
    ),
    provider: str = typer.Option(
        None,
        "--provider",
        "-p",
        help="Tunnel provider: cloudflared, ngrok, lt, tailscale.",
    ),
) -> None:
    """Expose a local service to the internet via a secure HTTPS tunnel."""
    import time

    port: int | None = None
    if target and target.isdigit():
        try:
            port = ports.check_port(int(target))
        except ValueError as exc:
            err.print(str(exc))
            raise typer.Exit(1)
    else:
        try:
            stack = detect.stack_for(Path.cwd())
        except config.ConfigError as exc:
            err.print(f"{exc}\nSpecify the port to share: stackhelx share 3000")
            raise typer.Exit(1)

        if target and target in stack.services:
            svc = stack.services[target]
            if svc.port:
                port = svc.port
            else:
                err.print(f"Service '{target}' does not have a fixed port declared.")
                raise typer.Exit(1)
        elif target:
            err.print(f"Service or port '{target}' not found in the stack.")
            raise typer.Exit(1)
        else:
            ports_list = stack.ports()
            if not ports_list:
                err.print(f"{stack.path} does not declare any ports.")
                raise typer.Exit(1)
            port = ports_list[-1]

    console.print(f"[bold cyan]Starting tunnel to 127.0.0.1:{port}...[/]")
    try:
        tun = tunnel.start_tunnel(port, provider=provider)
    except tunnel.TunnelError as exc:
        err.print(f"[bold red]Error:[/] {exc}")
        raise typer.Exit(1)

    console.print(f"[bold green]Tunnel active![/] Provider: [bold]{tun.provider}[/]")
    console.print(f"Local:  [cyan]http://127.0.0.1:{port}[/]")
    console.print(f"Public: [bold underline green]{tun.url}[/]")
    console.print("[dim]Press Ctrl-C to close the tunnel.[/]")

    try:
        while tun.proc.poll() is None:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        tun.stop()
        console.print("\n[dim]Tunnel closed.[/]")


@app.command("clean")
def clean_cmd(
    solo: list[str] = typer.Option(
        None,
        "--solo",
        "-s",
        help=(
            "Clean only these categories: containers, images, networks, cache. "
            "Repeatable. Without this, cleans all four."
        ),
    ),
    volumes: bool = typer.Option(
        False,
        "--volumes",
        "-v",
        help="Also remove anonymous/dangling Docker volumes.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Prune stopped containers, untagged images, and unused Docker resources."""
    objetivos = list(solo) if solo else list(docker.DEFAULT_TARGETS)
    desconocidos = [t for t in objetivos if t not in docker.DEFAULT_TARGETS]
    if desconocidos:
        err.print(
            f"Unknown category: {', '.join(desconocidos)}. "
            f"Valid: {', '.join(docker.DEFAULT_TARGETS)}"
        )
        raise typer.Exit(1)
    if volumes:
        objetivos.append("volumes")

    if not yes:
        tabla = docker.usage()
        if tabla:
            console.print(tabla)
        console.print("Will remove: " + ", ".join(docker.ETIQUETAS[t] for t in objetivos if t != "volumes"))
        if volumes:
            console.print("[bold]And anonymous dangling volumes, which contain data inside.[/]")
        if not typer.confirm("Continue?", default=False):
            console.print("Cancelled.")
            return

    console.print("[bold cyan]Pruning Docker resources...[/]")
    ok, msg = docker.prune(objetivos)
    if ok:
        console.print(f"[bold green]Done:[/] {msg}")
    else:
        err.print(f"[bold red]Error cleaning Docker:[/] {msg}")
        raise typer.Exit(1)


@app.command("mcp")
def mcp_cmd(
    show_config: bool = typer.Option(
        False,
        "--config",
        "-c",
        help="Print the JSON configuration block for Claude Desktop, Cursor, or Antigravity.",
    ),
    show_prompt: bool = typer.Option(
        False,
        "--prompt",
        "-p",
        help="Print recommended system instructions for AI agents.",
    ),
) -> None:
    """Start the Model Context Protocol (MCP) server over stdio for AI agents."""
    if show_config:
        cfg = {
            "mcpServers": {
                "stackhelx": {
                    "command": "shx",
                    "args": ["mcp"],
                }
            }
        }
        console.print(json.dumps(cfg, indent=2))
        return
    if show_prompt:
        console.print(
            "You have access to StackHelx MCP tools (`stackhelx_*`). "
            "Use them to inspect port status (`stackhelx_ports`), diagnose environment issues (`stackhelx_doctor`), "
            "free conflicting ports (`stackhelx_free`), start the project stack (`stackhelx_up`), "
            "stop it (`stackhelx_down`), run declared scripts (`stackhelx_run`), and share services via HTTPS tunnels (`stackhelx_share`)."
        )
        return
    mcp.serve_stdio()


@app.command("version")
def version_cmd() -> None:
    """Show the installed version."""
    console.print(__version__)


@app.command("history")
def history_cmd(
    target: str = typer.Argument(None, help="Path to the project."),
    limit: int = typer.Option(5, "--limit", "-n", help="Number of startup records to show."),
) -> None:
    """Show the startup history for the project."""
    if limit < 1 or limit > history.MAX_LIMIT:
        err.print(f"[red]Error:[/] --limit must be between 1 and {history.MAX_LIMIT}")
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
        console.print(f"No startup history for project [bold]{stack.name}[/]")
        return

    table = Table(title=f"Startup history: {stack.name}")
    table.add_column("Date")
    table.add_column("Profile")
    table.add_column("Duration")
    table.add_column("Result")

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
    target: str = typer.Argument(None, help="Path to the project or directory."),
) -> None:
    """Validate stack configuration (ports, dependencies, variables) without starting services."""
    path = (Path.cwd() / (target or "")).resolve()
    try:
        stack = detect.stack_for(path)
        services = stack.resolve()
    except config.ConfigError as exc:
        err.print(f"[bold red]Invalid configuration:[/] {exc}")
        raise typer.Exit(1)

    console.print(f"Validating stack [bold]{stack.name}[/] at [dim]{stack.root}[/]...")
    console.print(f"[green]OK:[/] {len(services)} service(s) resolved in topological order:")
    for s in services:
        deps = f" (waits for: {', '.join(s.needs)})" if s.needs else ""
        port_info = f" -> port {s.port}" if s.port else f" ({s.ready})"
        console.print(f"  - [cyan]{s.name}[/]: [dim]{s.command}[/]{port_info}{deps}")

    declared_ports = stack.ports()
    if declared_ports:
        occupied = [p for p in declared_ports if not ports.is_free(p)]
        if occupied:
            console.print(f"[yellow]Warn:[/] Ports currently in use: {', '.join(map(str, occupied))}")
        else:
            console.print("[green]OK:[/] All declared ports are free")

    console.print("\n[bold green]Stack validated successfully.[/]")


@app.command("logs")
def logs_cmd(
    target: str = typer.Argument(None, help="Path to the project."),
    service: str = typer.Option(None, "--service", "-s", help="Filter by service name."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow logs in real time."),
    server_port: int = typer.Option(7666, "--port", "-p", help="StackHelx server port."),
) -> None:
    """Show or follow logs for a project running in StackHelx."""
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
                f"[yellow]Could not connect to StackHelx at {base_url}. Make sure `stackhelx serve` is running.[/]"
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
                console.print(f"[dim]No logs available for {stack.name}.[/]")
            break

        time.sleep(0.5)


@app.command("stats")
@app.command("top")
def stats_cmd(
    target: str = typer.Argument(None, help="Path to the project."),
    server_port: int = typer.Option(7666, "--port", "-p", help="StackHelx server port."),
) -> None:
    """Display live CPU and memory usage for running services."""
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
            f"[yellow]Could not connect to StackHelx at {base_url}. Make sure `stackhelx serve` is running.[/]"
        )
        raise typer.Exit(1)

    metrics = data.get("metrics", {})
    if not metrics:
        console.print(f"[dim]No active services in {stack.name}.[/]")
        return

    table = Table(title=f"Live metrics: {stack.name}")
    table.add_column("Service", style="cyan")
    table.add_column("PID", style="dim")
    table.add_column("CPU %", justify="right")
    table.add_column("Memory (MB)", justify="right")

    for name, s in metrics.items():
        cpu = f"{s.get('cpu_percent', 0.0)}%"
        mem = f"{s.get('memory_mb', 0.0)} MB"
        table.add_row(name, str(s.get("pid", "-")), cpu, mem)

    console.print(table)


@app.command("mcp-status")
def mcp_status_cmd(
    server_port: int = typer.Option(7666, "--port", "-p", help="StackHelx server port."),
) -> None:
    """Display MCP tool call telemetry and rate-limit status for AI agents."""
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
        f"[bold]StackHelx MCP Server[/] · Calls: [cyan]{total}[/] · Quota: [green]{rate}/{max_rate}[/] req/min"
    )

    by_tool = data.get("by_tool", {})
    if by_tool:
        tool_table = Table(title="MCP calls by tool")
        tool_table.add_column("Tool", style="cyan")
        tool_table.add_column("Calls", justify="right")
        for tool, count in sorted(by_tool.items(), key=lambda x: -x[1]):
            tool_table.add_row(tool, str(count))
        console.print(tool_table)

    events = data.get("recent_events", [])
    if events:
        ev_table = Table(title="Recent invocations (last 10)")
        ev_table.add_column("Timestamp", style="dim")
        ev_table.add_column("Tool", style="cyan")
        ev_table.add_column("Duration", justify="right")
        ev_table.add_column("Status")
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
        console.print("[dim]No recent MCP agent activity.[/]")


if __name__ == "__main__":
    app()
