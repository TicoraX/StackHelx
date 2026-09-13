"""El explorador de carpetas.

`markers()` decide que badge se pinta en el selector de proyectos. Es lo unico
de este modulo que corre una vez por carpeta y por listado, asi que es lo unico
que tiene tests de costo ademas de tests de resultado.
"""

import os
from pathlib import Path

import pytest

from stackhelx import browse


def escribir(raiz: Path, *nombres: str) -> Path:
    raiz.mkdir(parents=True, exist_ok=True)
    for nombre in nombres:
        (raiz / nombre).write_text("", encoding="utf-8")
    return raiz


# markers ------------------------------------------------------------------


def test_markers_respeta_el_orden_de_la_constante(tmp_path):
    """El orden es el de MARKERS, no el del disco.

    Es lo que decide cual badge se pinta primero, y un `set` sin ordenar
    despues lo dejaria a merced de como el sistema devuelva las entradas.
    """
    carpeta = escribir(tmp_path / "app", "package.json", "compose.yaml", "stack.yaml")

    assert browse.markers(carpeta) == ["stack.yaml", "compose.yaml", "package.json"]


def test_markers_ignora_las_carpetas_que_se_llaman_como_un_marcador(tmp_path):
    """Un directorio llamado `package.json` no es un proyecto Node.

    Suena absurdo hasta que aparece: `dist/package.json` mal desempaquetado, o
    un `Cargo.toml/` creado por un script que se equivoco de flag. El chequeo
    de archivo estaba en el `is_file()` original y no se puede perder.
    """
    carpeta = tmp_path / "app"
    (carpeta / "package.json").mkdir(parents=True)
    (carpeta / "Cargo.toml").write_text("[package]", encoding="utf-8")

    assert browse.markers(carpeta) == ["Cargo.toml"]


def test_markers_no_distingue_mayusculas(tmp_path):
    """Un `cargo.toml` en minuscula tambien pinta el badge.

    En Windows y en el APFS por defecto de macOS el sistema de archivos ya lo
    resolvia solo: `(path / "Cargo.toml").is_file()` daba True con el archivo
    en minuscula. Verificado corriendolo antes de tocar nada.

    O sea que comparar nombres exactos seria una regresion silenciosa en dos de
    las tres plataformas de la CI, y del tipo peor: el badge desaparece y nada
    se pone rojo. Se compara sin distinguir en todas, que ademas empareja el
    comportamiento en vez de dejarlo depender del sistema de archivos.
    """
    carpeta = escribir(tmp_path / "app", "cargo.toml")

    assert browse.markers(carpeta) == ["Cargo.toml"]


def test_markers_en_una_carpeta_sin_permisos_o_inexistente(tmp_path):
    """Sin permisos se ve vacia, que no es un error del usuario."""
    assert browse.markers(tmp_path / "no-existe") == []


def test_markers_no_paga_una_consulta_por_marcador(tmp_path, monkeypatch):
    """El costo no crece con MARKERS, que es el punto entero del cambio.

    Antes era una consulta al disco por marcador y por carpeta, la tuviera o
    no: el comentario `ponytail:` de este modulo ya nombraba el techo (~1800 en
    un listado grande) y la salida (un solo scandir, cruzar los nombres). Sumar
    JVM, Elixir y Bun lleva MARKERS de 11 a 15, o sea que este es el cambio que
    cobra el techo, y sin un test la mejora se pierde en el proximo que
    escriba `for name in MARKERS` por costumbre.

    Se afirma el efecto y no la forma: cuantas veces la funcion le pregunta al
    sistema por una ruta, contando las dos maneras de preguntarlo (`Path` o la
    entrada del scandir). Cual de las dos use es asunto suyo.

    MARKERS se infla a 200 nombres a proposito. Con los 9 de verdad, un limite
    de 9 y uno de 200 se ven casi igual y el test pasaria por accidente; con
    200 la diferencia entre "crece con la lista" y "no crece" es imposible de
    confundir.
    """
    carpeta = escribir(tmp_path / "app", "package.json", *[f"ruido{i}.txt" for i in range(50)])
    monkeypatch.setattr(
        browse, "MARKERS", ("package.json", *[f"inventado{i}.toml" for i in range(199)])
    )

    consultas = []
    for objetivo in (Path, os.DirEntry):
        real = objetivo.is_file

        def contando(self, *args, __real=real, **kwargs):
            consultas.append(str(self))
            return __real(self, *args, **kwargs)

        monkeypatch.setattr(objetivo, "is_file", contando, raising=False)

    assert browse.markers(carpeta) == ["package.json"]
    assert len(consultas) <= 2, (
        f"se le pregunto al disco {len(consultas)} veces por una carpeta con un solo "
        f"marcador y 200 nombres declarados: el costo sigue atado a MARKERS"
    )


# listing ------------------------------------------------------------------


def test_listing_exige_ruta_absoluta(tmp_path):
    with pytest.raises(ValueError, match="absoluta"):
        browse.listing("relativa/mala")


def test_listing_rechaza_un_archivo(tmp_path):
    archivo = tmp_path / "archivo.txt"
    archivo.write_text("hola", encoding="utf-8")
    with pytest.raises(ValueError, match="no es un directorio"):
        browse.listing(str(archivo))
