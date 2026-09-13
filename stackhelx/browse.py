"""Exploracion de carpetas para el selector de proyectos de la interfaz.

El navegador no puede dar rutas absolutas (webkitdirectory las oculta a
proposito), asi que el listado sale de aca. Solo nombres de carpetas y de los
archivos marcadores: nunca contenido, nunca archivos sueltos.

Recorrer el disco no tiene nada que ver con servir HTTP, y este modulo no
importa nada de fastapi: `server` traduce el ValueError a un 400.
"""

from __future__ import annotations

import os
from pathlib import Path

import psutil

from . import detect

MARKERS = (
    "stack.yaml", "stack.yml", *detect.COMPOSE_NAMES,
    "package.json", "manage.py", "Cargo.toml",
    "bunfig.toml", "mix.exs",
    "pom.xml", "build.gradle", "build.gradle.kts",
)

# Carpetas que nunca son un proyecto y solo hacen ruido al navegar.
SKIP = {"node_modules", "__pycache__", "venv", "env", "dist", "build", "target"}
LIMIT = 300


def listing(raw: str) -> dict:
    """Contenido navegable de `raw`, o las raices si viene vacio.

    Levanta ValueError con un motivo legible si la ruta no sirve.
    """
    if not raw:
        return roots()

    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError(f"la ruta debe ser absoluta: {raw}")
    try:
        path = path.resolve(strict=True)
    except OSError:
        raise ValueError(f"no existe: {raw}") from None
    if not path.is_dir():
        raise ValueError(f"no es un directorio: {path}")

    names = _subdirs(path)
    return {
        "path": str(path),
        # "" manda a la vista de raices; None es no tener a donde subir.
        "parent": "" if path.parent == path else str(path.parent),
        "markers": markers(path),
        "truncated": len(names) > LIMIT,
        "entries": [
            {"name": name, "path": str(path / name), "markers": markers(path / name)}
            for name in names[:LIMIT]
        ],
    }


def _subdirs(path: Path) -> list[str]:
    found = []
    try:
        with os.scandir(path) as items:
            for item in items:
                if item.name.startswith(".") or item.name in SKIP:
                    continue
                try:
                    if item.is_dir():
                        found.append(item.name)
                except OSError:
                    continue  # enlace roto o unidad desconectada
    except OSError:
        return []  # sin permisos se ve vacia, que no es un error del usuario
    return sorted(found, key=str.lower)


def _es_archivo(entry: os.DirEntry) -> bool:
    try:
        return entry.is_file()
    except OSError:
        return False  # enlace roto o unidad desconectada


def markers(path: Path) -> list[str]:
    """Archivos que delatan un proyecto.

    Un `scandir` por carpeta y cruce de nombres, y no un `is_file()` por
    marcador, que era el techo que este mismo comentario dejaba anotado: el
    costo estaba atado al largo de `MARKERS`, y sumar lenguajes lo empuja.
    Ahora no crece con la lista, solo con lo que la carpeta tiene de verdad.

    Se pregunta por la entrada solo cuando el nombre ya coincide con un
    marcador. Sin ese filtro, una carpeta con mil archivos pagaria mil
    preguntas y el cambio seria una regresion en vez de una mejora.

    **Sin distinguir mayusculas, y no es un detalle.** `(path / "Cargo.toml")
    .is_file()` daba True con un `cargo.toml` en disco, porque el sistema de
    archivos lo resolvia: NTFS y el APFS por defecto de macOS no distinguen.
    Comparar nombres exactos habria borrado el badge en dos de las tres
    plataformas de la CI, en silencio y sin ningun test en rojo. Se compara
    igual en las tres, que ademas es una respuesta y no una casualidad del
    sistema de archivos donde toco correr.

    El conjunto se arma en cada llamada a proposito: `MARKERS` se puede
    parchear (los tests lo hacen), y un conjunto de modulo quedaria viejo. Es
    un punado de cadenas en memoria contra una llamada al sistema, no se nota.
    """
    objetivo = {name.lower(): name for name in MARKERS}
    try:
        with os.scandir(path) as items:
            presentes = {
                item.name.lower()
                for item in items
                if item.name.lower() in objetivo and _es_archivo(item)
            }
    except OSError:
        return []  # sin permisos o no existe: se ve sin badges, no es un error
    return [name for clave, name in objetivo.items() if clave in presentes]


def roots() -> dict:
    """Punto de partida: la home del usuario y las unidades montadas."""
    seen = {str(Path.home())}
    entries = [{"name": "Inicio", "path": str(Path.home()), "markers": []}]
    try:
        partitions = psutil.disk_partitions(all=False)
    except OSError:
        partitions = []
    for partition in partitions:
        mount = partition.mountpoint
        if mount not in seen and os.path.isdir(mount):
            seen.add(mount)
            entries.append({"name": mount, "path": mount, "markers": []})
    return {"path": "", "parent": None, "markers": [], "truncated": False, "entries": entries}


def frequent_roots(project_paths: list[Path]) -> list[str]:
    """Calcula las carpetas padre mas comunes de los proyectos registrados.

    Permite saltos directos en el selector de carpetas y autocompletado sin
    recorrer todo el arbol de discos.
    """
    counts: dict[str, int] = {}
    for p in project_paths:
        try:
            parent = p.parent
            if parent != p and parent.is_dir():
                parent_str = str(parent)
                counts[parent_str] = counts.get(parent_str, 0) + 1
        except OSError:
            continue

    # Ordenar por frecuencia descendente
    sorted_parents = [k for k, _ in sorted(counts.items(), key=lambda item: item[1], reverse=True)]
    return sorted_parents[:6]

