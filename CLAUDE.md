# StackHelx

Herramienta de consola en Python que orquesta entornos de desarrollo locales:
libera puertos, arranca Docker, backend y frontend en orden, y expone una
interfaz web local para gestionar varios proyectos.

## Estructura

| Modulo | Responsabilidad |
|---|---|
| `ports.py` | Escaneo de puertos, identificacion del proceso dueno, cierre seguro |
| `config.py` | Carga y validacion de `stack.yaml` |
| `detect.py` | Servicios inferidos del proyecto cuando no hay `stack.yaml` |
| `runner.py` | Arranque en orden topologico, healthchecks, apagado del arbol |
| `registry.py` | Proyectos conocidos por la interfaz, y el token de la API |
| `docker.py` | Arranque y reinicio de Docker Desktop. Diagnosticar si esta arriba es de `doctor.py` |
| `server.py` | API local en FastAPI |
| `web/` | Interfaz: HTML, CSS y JS sin build |
| `cli.py` | Comandos Typer (`stackhelx` / `shx`) |

El core no depende de la terminal. El CLI y el servidor son dos consumidores de
las mismas funciones; cualquier logica nueva va en el modulo correspondiente, no
en `cli.py` ni en `server.py`.

## Desviaciones deliberadas de las reglas globales

Cada una con su motivo. No revertirlas sin leerlo primero.

### El token no vive en un `.env`

La regla pide secretos en `.env` con validacion al arrancar. StackHelx se
instala con `pipx` o `uv tool`: no hay repositorio donde poner un `.env`, y
pedirle al usuario que genere y copie un token a mano garantiza que termine
usando `token=1234`.

En su lugar: `STACKHELX_TOKEN` (o `PORTMASTER_TOKEN` legacy) tiene prioridad si existe, y si no,
`registry.token()` genera uno de 32 bytes en `~/.stackhelx/token` con permisos
0600. Se valida largo minimo de 16 en ambos casos y el servidor no arranca sin
token. El secreto queda fuera del repo, que es lo que la regla protege.

### Las cuotas de rate limit no son las de referencia

La referencia es 100/15min general. La interfaz sondea `/api/state` cada 2.5s,
que da unas 360 peticiones por ventana: con 100 se rompe el uso normal. Las
cuotas quedaron en `QUOTA_READ = 1800`, `QUOTA_WRITE = 60`, `QUOTA_KILL = 30`.
Las rutas que ejecutan comandos o matan procesos son las que van cortas, que es
donde la regla importa.

### Sin `Strict-Transport-Security`

El servidor es http sobre loopback. Ese header le impondria https a todo
`127.0.0.1` en el navegador del usuario y romperia cualquier otro proyecto local
que corra en http. Los otros headers (CSP, nosniff, DENY, no-referrer) si estan.

En su lugar, contra rebinding de DNS: validacion del header `Host` contra
`127.0.0.1` y `localhost`, antes de cualquier otra cosa.

### Los comandos de `stack.yaml` no se filtran por contenido

Hubo una lista de patrones destructivos (`rm -rf /`, `format C:`, `DROP
DATABASE`) que revisaba cada `command`, `pre_start`, `post_start`, `stop` y
script antes de ejecutarlo. Se saco, y no conviene volver a ponerla.

No protege de nada. El unico que escribe esos comandos es el dueño del
`stack.yaml`, y `stack.yaml` ya es codigo ejecutable por diseño: quien lo
escribe tiene ejecucion arbitraria por definicion. Un atacante no necesita
esquivar la lista, pero igual la esquiva sin esfuerzo:

```
BLOQ  rm -rf /          PASA  X=/; rm -rf $X
BLOQ  rm -rf ~          PASA  python -c "shutil.rmtree(os.path.expanduser('~'))"
```

Y si contra el atacante no hace nada, contra el usuario si:

```
BLOQ  rm -rf ~/.cache/mi-proyecto      BLOQ  del /s /q <dir>
BLOQ  rm -rf ~/proyecto/node_modules   BLOQ  psql -c "drop database test_db"
```

Los cuatro son limpiezas normales de un hook de desarrollo. La lista convertia
un `stack.yaml` valido en un error, y el mensaje acusaba al usuario de escribir
algo destructivo.

Lo que si queda es `guardrails.validate_identifier`, que es otra cosa: valida el
`pid` que viene por la URL antes de que `history` lo use como nombre de archivo.
Ahi el valor si es de un tercero y la validacion si tiene sentido.

### `shell=True` en los subprocesos

`npm run dev` y `docker compose up -d` no son ejecutables. `stack.yaml` ya es
codigo ejecutable por diseno, igual que `package.json` o un `Makefile`, y el
README lo documenta. Por eso el apagado mata el arbol de procesos completo: con
`shell=True` el hijo directo es el shell y matarlo solo a el deja huerfano al
servidor de verdad.

Vale para **todo** subproceso, no solo para los servicios del runner. `tunnel.py`
nacio con un `proc.terminate()` propio y por eso cerrar un tunel mataba el `cmd`
y dejaba el `cloudflared` publicando el puerto. Hay una sola implementacion,
`runner._terminate_tree`: cualquier cosa que lance con `shell=True` la usa.

Lo mismo con lo que se concatena a una linea de shell. `shlex.join` es correcto
en POSIX y no alcanza en Windows, y `subprocess.list2cmdline` tampoco: escapa las
comillas como `\"`, que es la convencion de `CreateProcess`, y `cmd.exe` no la
entiende. Hacen falta las dos capas. Ver `scripts._entrecomillar`.

### La interfaz no usa webfonts

La CSP es `default-src 'self'`. Una herramienta local que le pide fuentes a un
CDN en cada carga filtra actividad y deja de funcionar sin internet. Los tres
roles tipograficos salen de stacks del sistema.

## Tests

`pytest`. Todo con sockets y procesos reales, sin mocks: es la unica forma de
probar un modulo cuyo trabajo es hablar con el sistema operativo. La CI corre en
Linux, macOS y Windows porque los tres divergen justo ahi.

### Correrla sin esperar seis minutos

Casi todo el costo esta en tres archivos, que son justo los que arrancan
procesos y esperan healthchecks:

| Que corro | Tests | Serial |
|---|---|---|
| Todo | 388 | 6m 30s |
| `test_runner` + `test_server` + `test_cli` | 188 | 6m 12s |
| Todo lo demas | 200 | 17s |

O sea que la mitad de la suite cuesta el 4% del tiempo. Durante el ciclo,
nombrar el archivo (`pytest tests/test_runner.py`) alcanza casi siempre.

Antes de commitear va entera, y va **en paralelo**: `pytest -q -n auto`, que
baja de 6m30s a poco mas de un minuto. El tiempo es espera, no CPU, asi que
escala casi lineal con los workers.

Lo que hace seguro el paralelo es `banda()` en `tests/conftest.py`. `free_ports`
bindea un puerto al azar para ver si esta libre y lo suelta antes de
devolverlo, y esa ventana entre soltar y usar la disputa cualquiera. De a uno
la disputan los tests entre si, que es lo que el propio conftest ya
documentaba despues de tres rojos de CI; con `-n`, cada worker es un proceso
aparte y la disputan todos a la vez. Darle a cada worker su franja de la banda
hace que la carrera **no exista**, en vez de hacerla menos probable. Sin xdist,
o con un `PYTEST_XDIST_WORKER` que no se pueda leer, se usa la banda entera.

No aflojar eso para "simplificar". Un intermitente que aparece una vez cada
veinte corridas cuesta mas caro que uno que aparece siempre.

### Un test verde no prueba nada por si solo

Prueba que el test corrio. Que ademas pruebe el codigo hay que ganarselo, y en
este proyecto se gana de dos formas.

**Reproducir antes de arreglar.** Ejecutando, no leyendo. Si no se puede mostrar
el sintoma, todavia no se entendio el bug y el arreglo es una apuesta.

**Revertir despues de arreglar.** Se deshace el cambio y se confirma que el test
se pone rojo. Un test que pasa con y sin el arreglo no cubre nada, y eso no se
ve mirandolo.

Los tres bugs mas caros de la version 1.1.0 estaban tapados por tests en verde:

- `portmaster_free_port` llamaba `ports.kill(port)`, el numero de puerto en el
  lugar del pid: pedir liberar el 3000 mataba al proceso 3000. El test parcheaba
  `kill` con `lambda port: True`. El mock **nombraba el parametro como el bug** y
  devolvia algo que la funcion real no devuelve, asi que afirmaba un mensaje que
  el codigo no podia producir.
- `Tunnel.stop` mataba el shell y dejaba el cliente de tuneles publicando el
  puerto. El test usaba `MagicMock` y afirmaba `terminate`. **El proceso que
  sobrevivia es uno que el mock no tiene**, asi que era invisible por
  construccion.
- Un cache global sin invalidar hacia que la fila de Docker tardara 30s en
  aparecer. Se delato como un test que pasa solo y falla acompañado. Ese sintoma
  nunca es ruido del runner: es estado compartido entre tests.

El patron es siempre el mismo. El mock se escribe mirando el codigo que se va a
probar, hereda sus errores, y despues los confirma. Contra eso solo sirve el
sistema operativo de verdad: un proceso que se puede consultar con `psutil`, un
socket que se puede escanear, un archivo que aparece o no aparece.

**Afirmar el efecto, no la forma.** Un test de "no paso nada malo" que pregunta
por un nombre exacto es fragil justo contra las variantes de lo malo: con un
entrecomillado a medias, la redireccion inyectada creaba el archivo con una
comilla pegada al nombre y preguntar por `inyectado.txt` daba verde con la
inyeccion hecha. Preguntar si aparecio **cualquier** archivo nuevo, no.

## Probar la interfaz: contra el repo, no contra lo instalado

`stackhelx serve` desde el PATH **no corre este repo**. Se instala con `pipx` o
`uv tool`, asi que el binario apunta a la copia de
`~/.local/bin` (o `AppData/Roaming/uv/tools/stackhelx` en Windows). Editas
`stackhelx/web/app.css`, recargas el navegador, y ves el build viejo.

El sintoma es peor que un error: el arreglo **parece no hacer nada**. Costo dos
correcciones dadas por invalidas y descartadas antes de mirar quien servia el
archivo. La segunda hipotesis ya iba camino a la tercera.

Para probar cambios de interfaz:

```bash
# Windows: .venv/Scripts/python -m stackhelx serve --port 7667 --no-open
# POSIX:   .venv/bin/python -m stackhelx serve --port 7667 --no-open
# O con el entorno activado:
python -m stackhelx serve --port 7667 --no-open
curl -s http://127.0.0.1:7667/static/app.css | grep lo-que-acabas-de-escribir
```

El `grep` es el paso que importa: confirma que el navegador va a recibir tu
edicion antes de que interpretes lo que ves en pantalla. Un `--port` distinto
ademas deja vivo el `serve` que el usuario ya tenia abierto.

La regla general: **antes de dudar del arreglo, confirmar que el arreglo llego.**
Vale para cualquier capa servida desde disco.

## La documentacion tambien se verifica ejecutando

`README.md` y `docs/comandos.md` describieron `clean` como `docker system prune`
durante varias versiones despues de que pasara a limpiar por categorias, con los
volumenes aparte. El CHANGELOG estaba bien; los otros dos no. Nadie lo nota
porque no hay test que lea prosa.

Cuando un cambio toca el comportamiento de un comando, la lista de archivos a
mirar es `README.md`, `docs/`, `stack.example.yaml` y `CHANGELOG.md`, y lo que se
escribe ahi se comprueba corriendo el comando, no recordandolo. Tres ejemplos que
salieron de correrlos: `env_file` fuera de la raiz es `ConfigError` (verificado
con `../../.env` y con una ruta absoluta), `url:` y `ready: listen` no se
combinan porque `url` le gana al puerto descubierto, y sin `port:` no existe el
`http://localhost:<port>` "de siempre" que la doc prometia.
