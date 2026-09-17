# StackHelx

[![pypi](https://img.shields.io/pypi/v/stackhelx?logo=pypi&logoColor=white)](https://pypi.org/project/stackhelx/)
[![tests](https://github.com/TicoraX/StackHelx/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/TicoraX/StackHelx/actions/workflows/ci.yml)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

Orquestador de entornos de desarrollo locales. Un archivo en la raíz del
proyecto, un comando, y el stack entero arriba: puertos libres, Docker,
backend y frontend, sin cuatro terminales abiertas.

## Instalación

```bash
uv tool install stackhelx
# o
pipx install stackhelx
```

La instalación registra dos ejecutables idénticos en el sistema: el comando principal **`stackhelx`** y su versión abreviada **`shx`**.

Requiere Python 3.10 o superior. Funciona en Windows, macOS y Linux.

## Comandos

Todos los comandos pueden ejecutarse como `stackhelx <comando>` o de forma abreviada con su alias oficial **`shx <comando>`** (ej. `shx up`, `shx down`, `shx doctor`, `shx ports`):

| Comando (`stackhelx` / `shx`) | Qué hace |
|---|---|
| `stackhelx up` | Levanta el stack entero: libera puertos, arranca en orden y sigue los logs |
| `stackhelx down` | Baja lo que sobrevive a la terminal, o sea contenedores |
| `stackhelx serve` | Abre la interfaz web en `http://127.0.0.1:7666` |
| `stackhelx doctor` | Revisa qué puede impedir el arranque, sin arrancar nada |
| `stackhelx ports` | Estado de los puertos declarados |
| `stackhelx free 3000` | Cierra el proceso que ocupa un puerto, preguntando antes |
| `stackhelx free --all` | Lo mismo para todos los puertos de todos los proyectos registrados |
| `stackhelx switch fitness` | Baja los proyectos que le pisan los puertos a este, y lo levanta |
| `stackhelx open` | Abre en el navegador el primer servicio que conteste HTTP |
| `stackhelx init` | Congela lo detectado en un `stack.yaml` editable |
| `stackhelx add .` | Registra el proyecto para que aparezca en la interfaz |
| `stackhelx list` | Lista los proyectos registrados (alias: `ls`) |
| `stackhelx remove .` | Des-registra un proyecto (alias: `rm`) |
| `stackhelx run [tarea]` | Ejecuta scripts o pipelines de tareas del proyecto |
| `stackhelx share [target]` | Expone un servicio local a internet mediante un túnel seguro |
| `stackhelx clean` | Limpia Docker por categorías: contenedores parados, imágenes sin tag, redes sin usar y caché de build. Los volúmenes van aparte, con `--volumes`. Pregunta antes |
| `stackhelx mcp` | Inicia el servidor Model Context Protocol (MCP) sobre stdio para IA |
| `stackhelx test-stack` | Valida el `stack.yaml` sin arrancar nada: orden, dependencias y puertos |
| `stackhelx history` | Últimos arranques del proyecto, con duración y resultado |
| `stackhelx logs` | Logs del proyecto que corre en `serve`, con `--follow` para seguirlos |
| `stackhelx stats` | CPU y memoria de los servicios que corren en `serve` (alias: `top`) |
| `stackhelx version` | Versión instalada (también `--version`) |

`logs` y `stats` consultan al `stackhelx serve` que ya tengas abierto, así que
necesitan que esté corriendo. `history` y `test-stack` leen del disco y no.

Cada uno con `--help`.

## Arrancar un stack

```bash
shx up
# o
stackhelx up

shx up --profile backend    # solo un subconjunto
shx up --no-free            # no tocar los puertos ocupados
shx up --env-file .env.qa   # carga ese .env antes de arrancar
```

`--env-file` no reemplaza al `env_file:` de `stack.yaml`, se suma: carga el
archivo en el entorno del proceso antes de resolver el stack, así que lo ven
todos los servicios. Es para la corrida puntual contra otro entorno, sin editar
el archivo. A diferencia de `env_file:`, acepta rutas fuera de la raíz del
proyecto, porque acá la ruta la escribiste vos en la terminal y no viene de un
archivo de un repo ajeno.

Antes de arrancar libera los puertos declarados que tenga otro proceso, y
pregunta antes de cerrar cada uno. Los que ya publica Docker los saltea: ahí
no hay nada que liberar, el contenedor ya está arriba.

```
demo  stack.yaml
db  | $ docker compose up -d postgres
db  | listo (5432)
api | $ npm run dev
api | escuchando en 8080
api | listo (8080)
web | $ npm run dev
web | listo (3000)
Todo listo. Ctrl-C para apagar.
api | GET /health 200
web | ready in 412 ms
```

Antes de arrancar nada revisa los puertos declarados. Si alguno está tomado
por un proceso huérfano, muestra cuál es y pregunta si cerrarlo. `Ctrl-C`
apaga los servicios en orden inverso, árbol de procesos incluido.

## Sin stack.yaml

`stack.yaml` es opcional. Si no hay uno, StackHelx mira la raíz del proyecto:

| Encuentra | Arranca |
|---|---|
| `compose.yaml`, `compose.yml`, `docker-compose.yml`, `docker-compose.yaml` | un servicio por contenedor: `docker compose up -d <nombre>` |
| `manage.py` | `python manage.py runserver` |
| `fastapi` o `uvicorn` declarados, con un módulo que defina `app` | `uvicorn <módulo>:app --reload` |
| `package.json` con un script que sirva (`dev`, `start:dev`, `serve`, `start`) | `npm run dev`, con `pnpm`/`yarn`/`bun` según el lockfile o el campo `packageManager` |

```
mi-app  A:\Proyectos\mi-app
Sin stack.yaml. Detectado:
  docker  docker compose up -d        5433
  web     pnpm run dev                al arrancar
Para congelarlo en un archivo editable: stackhelx init
Arrancar? [Y/n]
```

Arranca en ese orden y encadena las dependencias: el frontend espera al
backend, el backend a los contenedores.

`stackhelx init` escribe lo detectado como `stack.yaml` para editarlo a mano.
No sobreescribe uno existente.

Dónde busca cada lenguaje y por qué reconoce eso y no otra cosa, en
[`docs/deteccion.md`](docs/deteccion.md).

## stack.yaml

En la raíz del proyecto. StackHelx lo busca hacia arriba, así que podés correr
los comandos desde cualquier subdirectorio.

```yaml
name: mi-proyecto

services:
  db:
    command: docker compose up -d postgres
    port: 5432
    detached: true       # el comando termina, el servicio sigue vivo

  api:
    command: npm run dev
    cwd: backend
    port: 8080
    needs: [db]
    env:
      DATABASE_URL: postgres://localhost:5432/app

  web:
    command: npm run dev
    cwd: frontend
    port: 3000
    needs: [api]

profiles:
  backend: [api]         # arrastra db, que es su dependencia
```

`command` es el único obligatorio. La referencia de todos los campos, los
healthchecks de `ready` y los perfiles heredados de un compose están en
[`docs/stack-yaml.md`](docs/stack-yaml.md).

## Interfaz web

Cuando tenés varios proyectos, el CLI se queda corto: trabaja sobre el
directorio actual. La interfaz los muestra todos a la vez.

```bash
stackhelx serve        # abre http://127.0.0.1:7666
```

Viene con la instalación, no hace falta nada más. Registrar proyectos se puede
desde la propia interfaz con `Explorar…`, o desde la terminal con
`stackhelx add .`.

Estado de cada servicio, arrancar y apagar stacks, liberar puertos tomados por
procesos ajenos, y logs en vivo por proyecto.

El detalle de cada control, y el modelo de seguridad del servidor local, en
[`docs/interfaz.md`](docs/interfaz.md).

## Puertos

Revisar el estado de los puertos sin arrancar nada:

```bash
stackhelx ports              # los declarados en stack.yaml
stackhelx ports 3000 8080    # o los que le pases
```

```
PUERTO  ESTADO   PID    PROCESO   COMANDO
3000    ocupado  24188  node.exe  node C:\proj\frontend\node_modules\.bin\vite
8080    libre    -      -         -
5432    ocupado  9012   com.docker.backend.exe
```

Liberar un puerto tomado por un proceso zombie:

```bash
stackhelx free 3000
```

Muestra qué proceso lo ocupa y pide confirmación antes de cerrarlo. Si decís
que no, sugiere el siguiente puerto disponible.

Opciones: `--yes` salta la confirmación (para scripts), `--force` aplica
`kill()` cuando el proceso ignora la señal de terminación.

Después de un crash o un cambio de rama suele quedar más de uno colgado:

```bash
stackhelx free --all
```

Recorre los puertos declarados por todos los proyectos registrados, lista lo
que encuentre ocupado y pide una sola confirmación. Sale con código 1 si no
pudo cerrar alguno.

El CLI no sabe qué arrancaste vos: si tenés un stack levantado en otra
terminal, sus servicios aparecen en esa lista y también se cierran. Por eso la
muestra entera antes de tocar nada, y por eso la confirmación viene con "no"
por defecto. La interfaz web sí lo sabe, y ahí el botón "Liberar todos"
descarta lo que arrancó ella.

## Qué no hace el kill switch

Estas reglas están en el código, no en la documentación:

- Nunca cierra PID 0, PID 4, el propio StackHelx ni un proceso padre suyo.
  Matar tu propia terminal no es una función.
- Revalida la hora de creación del proceso entre el escaneo y el cierre. Los
  PID se reciclan; sin ese chequeo terminás matando algo al azar.
- Manda `terminate()` y espera 5 segundos. `kill()` solo con `--force`
  explícito, porque un `npm run dev` matado a lo bruto deja hijos huérfanos.
- Sin permisos, lo dice y corta. No reintenta escalando privilegios.
- Nunca cierra el proxy de Docker o de WSL. Un puerto publicado por un
  contenedor no lo escucha el contenedor: lo escucha un proceso compartido por
  todos, y cerrarlo apaga el motor entero. En vez de eso te dice qué contenedor
  parar.

## Otros comandos

`down`, `switch`, `doctor` y `open`, con qué revisa cada uno y por qué, en
[`docs/comandos.md`](docs/comandos.md).

## Modelo de confianza

`stack.yaml` ejecuta comandos arbitrarios, igual que `package.json` o un
`Makefile`. StackHelx no lo sandboxea: sería teatro. Tratá un `stack.yaml`
de un repo ajeno con el mismo cuidado que sus scripts de build.

Sin `stack.yaml`, los comandos salen de la detección, y `scripts.dev` de un
`package.json` ajeno es igual de arbitrario. Por eso `up` muestra qué va a
ejecutar y pregunta antes, y `-y` es tuyo para saltarlo cuando ya lo leíste.

## Desarrollo

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"    # .venv\Scripts\pip en Windows
pytest
```

Los tests levantan sockets y procesos reales, sin mocks. Es lo único que
prueba de verdad un módulo cuyo trabajo es hablar con el sistema operativo.

## Licencia

MIT
