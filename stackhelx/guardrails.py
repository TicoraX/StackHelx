"""Validacion de los identificadores que se convierten en nombre de archivo.

Aca no hay lista de comandos peligrosos, y es a proposito. Ver la nota en
`CLAUDE.md`, "Los comandos de stack.yaml no se filtran por contenido".
"""

from __future__ import annotations

import re

# Longitud máxima permitida para identificadores seguros
MAX_IDENT_LEN = 64

# Identificadores alfanuméricos seguros para IDs de proyecto y servicios
_IDENT_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

# Nombres de dispositivos reservados en Windows (DOS Device Names)
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


class GuardrailError(ValueError):
    """Identificador rechazado por no ser seguro como nombre de archivo."""


def validate_identifier(ident: str, field_name: str = "identificador") -> str:
    """Valida que un ID de proyecto o servicio sea alfanumérico seguro (sin path traversal ni DOS devices)."""
    clean = str(ident).strip()
    if not clean:
        raise GuardrailError(f"{field_name} no puede estar vacío")
    if len(clean) > MAX_IDENT_LEN:
        raise GuardrailError(f"{field_name} excede la longitud máxima permitida ({MAX_IDENT_LEN} caracteres)")
    if not _IDENT_PATTERN.match(clean):
        raise GuardrailError(
            f"{field_name} inválido ({clean!r}): solo se permiten letras, números, guiones y guiones bajos"
        )
    if clean.upper() in _WINDOWS_RESERVED:
        raise GuardrailError(f"{field_name} inválido ({clean!r}): nombre de dispositivo reservado en Windows")
    return clean
