# Plan: JVM, Elixir y Bun en la detección

> **Ejecutado y cerrado el 7 de septiembre de 2026.** Entraron los cuatro PRs en
> el orden previsto: `browse.markers`, Bun, Elixir y JVM. 461 tests en verde.
> Este documento queda como registro de cómo se decidió, no como plan pendiente.
> Lo que quedó afuera está al final, en "Lo que queda anotado y no entra".
>
> Dos cosas salieron distinto de lo escrito acá y valen para la próxima tanda.
> El refactor de `browse.markers` no era neutro: comparar nombres exactos habría
> borrado el badge en Windows y macOS, donde el sistema de archivos ya resolvía
> `Cargo.toml` contra un `cargo.toml` en disco. Y el hallazgo del `shutil.which`
> estaba **subestimado**: medido en Windows con 59 directorios en el PATH son
> 13.5ms por llamada, o sea ~162ms de barrido de disco por sondeo con seis
> proyectos y dos pestañas, no el costo menor que el plan sugería.

Fecha: 7 de septiembre de 2026. Rama `main`, 432 tests en verde.

StackHelx detecta hoy compose, Python, Node, Deno, Go, Rust, Ruby, PHP y .NET.
Una carpeta con un proyecto de otro lenguaje no devuelve nada y obliga a escribir
el `stack.yaml` a mano. Este plan cierra los tres huecos que tienen usuarios
reales de desarrollo local multi-servicio: la JVM (Java y Kotlin), Elixir y Bun
como runtime.

Fuera de alcance, decidido y no diferido por olvido: Dart/Flutter web, Swift
(Vapor), Scala (sbt), Zig y Nim. Se agregan si alguien los pide con un proyecto
concreto en la mano.

## Lo que este plan no necesita construir

Dos cosas que un plan de este tipo suele inventar y que acá ya existen. Vale la
pena nombrarlas porque son la mitad del presupuesto que no se va a gastar.

**No hace falta leer la configuración para saber el puerto.** `_served`
(`detect.py:811`) devuelve el servicio con `ready="listen"`, y el runner
descubre el puerto del socket cuando el proceso lo abre (`runner.py:613`). O sea
que nadie tiene que parsear `application.properties`, `application.yml`,
`config/dev.exs` ni `bunfig.toml` para encontrar un `server.port`. El lenguaje
nuevo dice qué comando corre y se termina ahí.

Esto vale también para el caso feo: Spring Boot con el puerto en una variable de
entorno, o Phoenix con el puerto en `config/runtime.exs` leyendo `System.get_env`.
Los dos abren un socket igual, y el socket es lo que se mira.

**No hace falta una abstracción nueva para los detectores.** `_backend_at(root,
detector)` (`detect.py:667`) ya deduplica el wrapper "la raíz, y si no las
subcarpetas de backend" que comparten Python, Go, Rust, Ruby, PHP y .NET. Un
lenguaje nuevo que sigue esa forma son dos funciones:

```
detect(root)
  └─ for detector in (_compose, _python, _go, _rust, _ruby, _php, _dotnet, _deno, _node)
       │                                                    ↑ acá entran _jvm, _elixir, _bun
       └─ _lang(root) ──→ _backend_at(root, _lang_at)
                            └─ _lang_at(path, name) ──→ Service | None
                                 └─ _served(name, comando, path)   ready="listen"
```

Convertir esa tupla en una tabla declarativa no gana nada: los ocho wrappers ya
son una línea cada uno, y lo que difiere de verdad es el `_lang_at`, que es
justamente lo que ninguna tabla puede unificar.

## Decidido

**Entran JVM, Elixir y Bun.** La JVM es el hueco grande y es medio mercado
backend. Elixir y Bun cuestan poco montados sobre el mismo molde y tienen gente
que corre varios servicios en local, que es el caso de uso de PortMaster.

**`detect.py` no se parte en paquete.** Queda en un archivo. El único síntoma es
el tamaño (829 líneas, ~1000 después de esto), y el tamaño solo no es el
problema: los detectores no comparten estado, son bloques contiguos e
independientes, y el archivo se lee como una lista. Partirlo ahora sería un diff
de movimiento sin cambio de comportamiento que hay que revisar igual y que toca
los imports de `cli.py`, `server.py`, `browse.py` y `mcp.py`. Se reconsidera
cuando el problema sea otro y no el número de líneas.

## El problema real: el comando no es el mismo en Windows

Este es el único punto donde el plan no es "copiar `_go_at` tres veces", y hay
que decidirlo antes de escribir código.

Gradle y Maven se invocan por su wrapper, que viene en el repo del proyecto y es
lo que garantiza la versión correcta del build. El wrapper son **dos archivos**:

```
gradlew       script sh    POSIX
gradlew.bat   batch        Windows
mvnw          script sh    POSIX
mvnw.cmd      batch        Windows
```

`./gradlew bootRun` no corre en `cmd.exe`, y `gradlew.bat bootRun` no corre en
bash. El comando detectado se escribe en el `stack.yaml` cuando el usuario
congela el stack (`detect.freeze`), y ese archivo se commitea y viaja al resto
del equipo. O sea que la elección no es "qué corre en esta máquina" sino "qué se
escribe en un archivo que va a abrir alguien en otro sistema operativo".

Tres salidas, y la tercera es la que va:

1. **Detectar por `os.name` y escribir el comando de esta plataforma.** Es lo más
   corto y es lo que hace `_dotnet_at` hoy. Rompe el `stack.yaml` compartido: el
   congelado en Windows falla en la CI de Linux del equipo.
2. **Un campo nuevo en el esquema, tipo `command_windows:`.** Resuelve el caso
   pero agranda la superficie pública de `stack.yaml` por un solo lenguaje, y
   después hay que documentarlo, validarlo y sostenerlo para siempre.
3. **Preferir el binario del PATH cuando existe, y el wrapper sólo si no está.**
   `gradle bootRun` y `mvn spring-boot:run` son iguales en los tres sistemas.
   Con `shutil.which("gradle")` se sabe si están, igual que ya hace
   `server.list_editors` para los editores y `tunnel.detect_providers` para los
   clientes de túneles.

Va la 3. Cuando no hay binario en el PATH y sí hay wrapper, se escribe el wrapper
de la plataforma actual y se documenta el caso en `docs/deteccion.md`.

**El `which` va cacheado, y esto no es opcional.** `detect.stack_for` corre
adentro de `_project_view` (`server.py:1559`), que corre una vez por proyecto y
por request, y la interfaz sondea cada 2.5s por pestaña abierta. El comentario de
`_docker_is_down` (`server.py:424`) ya cuenta lo que pasa cuando algo caro se
mete en ese camino: *"Medido: `/api/health` pasaba de 2ms a 9s con la vista de
estado bajo carga"*. `shutil.which` recorre el PATH entero, y en Windows lo
permuta contra las ~8 extensiones de PATHEXT.

O sea que va un helper de módulo con `@functools.lru_cache`:

```python
@functools.lru_cache(maxsize=None)
def _en_el_path(binario: str) -> bool:
    """Si el binario existe, cacheado.

    `detect` corre en el camino de sondeo de la interfaz (cada 2.5s por
    pestana, por proyecto), y `shutil.which` barre el PATH entero: en Windows
    ademas una vez por extension de PATHEXT. Es el mismo problema que
    `server._docker_is_down` documenta con numeros medidos.

    ponytail: sin vencimiento. Instalar gradle con `serve` abierto pide un
    reinicio para que lo vea, y ese caso no vale un cache con TTL y candado.
    """
    return shutil.which(binario) is not None
```

Se descartó el TTL con candado (el patrón completo de `_docker_is_down`) a
propósito: ahí el valor cambia solo, porque Docker se cae y se levanta. Un
binario en el PATH no.

La primera versión de este plan decía "y se deja un comentario en el
`stack.yaml`". No se puede: `to_yaml` termina en `yaml.safe_dump`
(`detect.py:244`), y PyYAML no emite comentarios. Verificado leyendo la función,
no recordándolo.

Y está bien que no se pueda, porque el comentario era de todas formas la
respuesta floja. Un `stack.yaml` congelado es un punto de partida que el usuario
edita, no un artefacto sellado: eso ya lo dice `docs/stack-yaml.md`. Quien
comparte un stack con un equipo mixto edita una línea, y quien no lo comparte
nunca se entera del problema. El camino del PATH cubre el caso que importa; el
resto es documentación, no esquema.

## Los tres detectores

### JVM: `_jvm(root)` y `_jvm_at(path, name)`

Señal de que hay un proyecto: `pom.xml` (Maven) o `build.gradle` /
`build.gradle.kts` (Gradle, Groovy o Kotlin DSL). Kotlin no es un detector
aparte: el build es el mismo y el `.kts` solo cambia la extensión.

Señal de que **sirve por un puerto**, que es la pregunta que de verdad importa y
la que ya se hacen `_go_at` y `_dotnet_at`. Un `pom.xml` a secas puede ser una
librería, y arrancarla dejaría al runner esperando un socket que nunca abre:

| Framework | Marca en el build | Comando |
|---|---|---|
| Spring Boot | `spring-boot-starter-web`, `spring-boot-starter-webflux` | `spring-boot:run` (Maven), `bootRun` (Gradle) |
| Quarkus | `quarkus-maven-plugin`, `io.quarkus` | `quarkus:dev` (Maven), `quarkusDev` (Gradle) |
| Micronaut | `micronaut-http-server` | `mn:run` (Maven), `run` (Gradle) |
| Ktor | `io.ktor:ktor-server` | `run` (Gradle) |

Sin ninguna de esas marcas, `_jvm_at` devuelve `None`. Es la misma decisión que
tomó `_go_at` con las CLIs y `_rust_at` con las librerías: no detectar es mejor
que detectar algo que se cuelga esperando.

Multi-módulo (`settings.gradle` con varios `include`) queda fuera de este tramo.
Es el análogo de los Cargo workspaces, que entraron en 1.4.4 como un paso
separado y después de que Rust básico ya funcionara. Se anota en
`docs/pendientes.md`.

### Elixir: `_elixir(root)` y `_elixir_at(path, name)`

Señal de proyecto: `mix.exs`. Señal de servidor: `:phoenix` en las dependencias
de `mix.exs`, o la carpeta `lib/<algo>_web/`, que Phoenix genera siempre y que
un proyecto de librería no tiene. Es el mismo par de señales que usa `_ruby_at`
con `config/application.rb` más `Gemfile`.

Comando: `mix phx.server`. Un `mix.exs` sin Phoenix es una librería o una
aplicación OTP sin puerto, y devuelve `None`.

### Bun: `_bun(root)` y `_bun_at(path, name)`

Bun ya está a medias en el código: `LOCKFILES` lo reconoce como **gestor de
paquetes** (`detect.py:36`), o sea que un proyecto Node con `bun.lockb` ya corre
`bun run dev`. Lo que falta es Bun como **runtime**, que es el proyecto sin
`package.json` o sin scripts, con `bunfig.toml` o un `index.ts` suelto que Bun
ejecuta directo.

Va **después** de `_node` en la tupla de detectores, no antes. El orden importa
y ya está resuelto en el código: en `detect()` (`detect.py:141`) el primero gana,
y un proyecto con `package.json` más `bun.lockb` tiene que seguir saliendo por el
camino de `_node`, que es el que sabe leer los scripts. `_bun_at` solo atrapa lo
que `_node` deja pasar.

Ese orden **lleva su propio test**, y es el más importante de los tres de Bun. Es
una dependencia entre dos líneas de una tupla, invisible para cualquier otro
test: alguien reordena, y un proyecto Node con `bun.lockb` empieza a salir por el
detector equivocado sin que nada se ponga rojo. El test escribe `package.json`
con un script `dev` más `bun.lockb`, y afirma que el comando es `bun run dev`, el
que produce `_node` leyendo el script, y no el que produciría `_bun_at`.

`_bun` tampoco pasa por `_backend_at`: Bun sirve tanto un frontend como un
backend, así que necesita el wrapper de `_deno` (`NODE_DIRS` más `BACKEND_DIRS`),
no el de `_go`. Como `_deno_at` y `_bun_at` van a compartir ese wrapper palabra
por palabra, se extrae en el PR de Bun. Es la única deduplicación de este plan, y
sale de que ya hay dos usos, no de anticipar un tercero.

**Este es el único de los tres que se puede probar de punta a punta en la máquina
de desarrollo**, porque Bun 1.3.14 está instalado. Aprovecharlo: un test de
runner con un servidor Bun real, no solo de detección.

## Los tests, y por qué acá los mocks no aplican

`CLAUDE.md` pide sockets y procesos reales, sin mocks. Vale para `runner`,
`server` y `cli`, cuyo trabajo es hablar con el sistema operativo. **No aplica a
`detect`**, y esto no es una excepción que inventa este plan: es lo que la suite
ya hace. `test_go_con_framework` (`tests/test_detect.py:531`) escribe un `go.mod`
de tres líneas en `tmp_path` y afirma que el comando sale `go run .`. Nunca
corre Go.

La razón es que la detección es inspección de archivos y nada más. La entrada es
un árbol de directorios, la salida es un `Service` con un string adentro. Un
`pom.xml` de verdad y uno escrito por el test son el mismo `pom.xml`. No hay
proceso que se pueda escapar ni socket que se pueda quedar abierto, así que no
hay nada que un toolchain real pudiera delatar.

Esto importa porque de las tres cadenas de herramientas, en esta máquina hay
cero: no están `java`, `javac`, `mvn`, `gradle`, `kotlin`, `elixir` ni `mix`.
Verificado corriéndolos. Si la detección necesitara el toolchain, este plan
empezaría por instalar tres SDK en la CI de Linux, macOS y Windows, y ese sería
el trabajo. No lo necesita.

Por lenguaje, en `tests/test_detect.py`:

- El framework detectado en la raíz, con el comando exacto y `ready == "listen"`.
- El mismo proyecto en `backend/`, que es lo que prueba que `_backend_at` está bien enganchado.
- **El caso negativo**, que es el que atrapa el bug caro: una librería (`pom.xml` sin starter web, `mix.exs` sin Phoenix) no se detecta. Sin este test, un detector que devuelve algo siempre pasa los otros dos.
- Para la JVM: el mismo proyecto con Maven y con Gradle, y el `.kts` de Kotlin.
- Para la JVM: con `gradle` en el PATH y sin él, monkeypatcheando `shutil.which`, que es lo que decide entre binario y wrapper.

Y uno solo en `tests/test_runner.py`: un servidor Bun real que abre un puerto y
que el runner ve llegar a `listen`. Es el único que la máquina puede correr, y
cubre el eslabón que los tests de detección por diseño no tocan.

### La verificación que el proyecto pide

De `CLAUDE.md`: reproducir antes de arreglar, revertir después de arreglar. Para
código nuevo se traduce a esto, y no es opcional en ninguno de los tres:

1. El test del caso negativo se escribe **primero** y tiene que **pasar** antes
   de escribir el detector: si falla antes, está mal escrito.
2. El test del caso positivo se escribe segundo y tiene que **fallar** antes de
   escribir el detector.
3. Después de implementar, se revierte el detector y se confirma que el positivo
   se pone rojo. Un detector que pasa con y sin su código no cubre nada.

## Orden de trabajo

Tres PRs, uno por lenguaje, en este orden. Cada uno entra con la suite entera en
verde (`pytest -q -n auto`) antes del siguiente.

| # | Qué | Por qué en ese lugar |
|---|---|---|
| 0 | **`browse.markers` a un solo `scandir`** | Refactor puro, sin comportamiento nuevo. Ver abajo. Va primero por la regla de Beck: hacer fácil el cambio, después hacer el cambio fácil. |
| 1 | **Bun** | El más chico, y el único con toolchain instalado. Valida el molde de punta a punta antes de aplicarlo a ciegas dos veces. |
| 2 | **Elixir** | Dos señales, un comando, cero ramas de plataforma. Es el molde puro. |
| 3 | **JVM** | El más grande y el único con la decisión de PATH contra wrapper. Va último, con el molde ya probado. |

### PR 0: cobrar el techo que `browse.py` ya tenía anotado

`markers()` (`browse.py:74`) hace un `stat` por marcador y por carpeta. Su propio
comentario `ponytail:` nombra el techo (~1800 stats en un listado grande) y la
ruta de salida (un `scandir` por carpeta e intersecar los nombres). Este plan
lleva `MARKERS` de 11 a 15, o sea ~2450: es el cambio que cobra el techo, así que
la salida se toma acá y no después.

Queda como un PR aparte de los tres lenguajes a propósito. Es un refactor sin
cambio de comportamiento, y mezclarlo con detección nueva haría que el revisor
tuviera que separar las dos cosas a ojo. Después de esto, el costo del explorador
deja de crecer con cada lenguaje.

Su test: una carpeta con varios marcadores devuelve la misma lista que antes, en
el mismo orden (el orden de `MARKERS`, que es lo que decide qué badge se pinta
primero). Y se verifica revirtiendo, como todo lo demás acá.

Cada PR toca:

- `stackhelx/detect.py`: las dos funciones, sus constantes, y el nombre en la tupla de `detect()`.
- `stackhelx/browse.py`: el marcador nuevo en `MARKERS` (`pom.xml`, `build.gradle`, `mix.exs`, `bunfig.toml`), que es lo que pinta el badge en el explorador de carpetas.
- `tests/test_detect.py`: los casos de arriba, negativo incluido.
- `docs/deteccion.md`, `README.md` y `CHANGELOG.md`. De `CLAUDE.md`: lo que se escribe en la doc se comprueba corriendo el comando, no recordándolo.

## Lo que queda anotado y no entra

A `docs/pendientes.md`, para que no se pierda ni se cuele:

- Multi-módulo de Gradle y Maven (`settings.gradle` con varios `include`), el análogo de los Cargo workspaces.
- Dart/Flutter web, Swift/Vapor, Scala/sbt, Zig, Nim.

## GSTACK REVIEW REPORT

Revisión de ingeniería sobre este plan, 7 de septiembre de 2026. El plan lo
escribió y lo revisó el mismo agente; los hallazgos salieron de leer el código
que el plan iba a tocar, no de releer el plan.

| Corrida | Estado | Hallazgos |
|---|---|---|
| Step 0 — desafío de alcance | COMPLETO | 2 reducciones. `ready: "listen"` ya existe (`detect.py:811`): ningún lenguaje necesita parsear config para el puerto. `_backend_at` (`detect.py:667`) ya es la abstracción que un plan tendería a inventar. |
| 1 — Arquitectura | COMPLETO | 1 hallazgo (D3, P1). |
| 2 — Calidad de código | COMPLETO | 1 hallazgo: `_deno_at` y `_bun_at` comparten wrapper palabra por palabra. Se extrae en el PR de Bun, con dos usos reales sobre la mesa. |
| 3 — Tests | COMPLETO | 1 hallazgo (P2): el orden `_bun` después de `_node` no tenía test. Agregado. |
| 4 — Performance | COMPLETO | 2 hallazgos (D3 y D4), los dos con precedente medido en el repo. |

### Hallazgos, con la línea que los motiva

**[P1] (confianza: 9/10) `detect.py` corre en el camino de sondeo.** Un
`shutil.which` en `_jvm_at` habría entrado a la ruta que la interfaz golpea cada
2.5s por pestaña y por proyecto. La línea que lo motiva, `server.py:424`:

```
`_project_view` corre una vez por proyecto y por request, y la interfaz
sondea cada 2.5s por pestana abierta: sin cache, tres proyectos con
contenedores se llevaban un segundo entero de cada `/api/state` [...]
Medido: `/api/health` pasaba de 2ms a 9s con la vista de estado bajo carga.
```

Y `server.py:1559`, que es quien mete `detect` ahí: `stack = detect.stack_for(path)`.

Resuelto: `@lru_cache` sobre el `which` (D3 → A). Se descartó el TTL con candado
porque un binario del PATH no cambia solo, a diferencia del daemon de Docker.

**[P2] (confianza: 8/10) `browse.MARKERS` cruza un techo anotado.** La línea, en
`browse.py:74`:

```
ponytail: un stat por marcador y por carpeta, hasta ~1800 en un listado
grande. Es local y en SSD no se nota. Si alguna vez pesa, la salida es un
solo scandir por carpeta e intersecar los nombres.
```

De 11 marcadores a 15. Resuelto: se toma la salida ahí escrita, como PR 0
(D4 → A).

**[P2] (confianza: 8/10) Dependencia de orden sin test.** `detect.py:141` es
`for detector in (_compose, _python, _go, _rust, _ruby, _php, _dotnet, _deno,
_node)`, y el comentario de abajo dice `# el primero gana`. Poner `_bun` antes de
`_node` rompe todo proyecto Node con `bun.lockb`, y ningún test existente lo
vería. Resuelto: test explícito en el PR de Bun.

**[P3] (confianza: 10/10) `to_yaml` no puede emitir comentarios.** La primera
versión del plan proponía marcar la línea del wrapper de Gradle con un
comentario en el `stack.yaml`. `detect.py:244` es `yaml.safe_dump(...)`, y PyYAML
no emite comentarios. Corregido en el plan, con el motivo escrito.

### Lo que se verificó ejecutando, no recordando

| Afirmación | Cómo se comprobó |
|---|---|
| Los tests de detección no necesitan toolchain | Leído `tests/test_detect.py:531`: escribe un `go.mod` en `tmp_path` y afirma el string del comando. |
| En esta máquina no hay JVM ni Elixir | Corrido: `java`, `javac`, `mvn`, `gradle`, `kotlin`, `elixir`, `mix` → todos NO INSTALADO. |
| Bun sí está, y sirve para un test de runner real | Corrido: `bun --version` → 1.3.14. |
| `_served` deja el puerto en `listen` | Leído `detect.py:811`. |
| `to_yaml` no admite comentarios | Leído `detect.py:244`. |

### VERDICT

APROBADO CON CAMBIOS, ya incorporados. Alcance reducido dos veces en Step 0 sin
perder cobertura: la parte cara resultó no existir. Los cuatro hallazgos están
resueltos en el cuerpo del plan. El trabajo son cuatro PRs (`browse`, Bun,
Elixir, JVM), sin dependencias nuevas, sin cambios de esquema en `stack.yaml` y
sin infraestructura.

Sin corrida cruzada de modelos: no se pidió.

NO UNRESOLVED DECISIONS
