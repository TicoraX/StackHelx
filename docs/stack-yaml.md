# Referencia de stack.yaml

El ejemplo mínimo está en el [README](../README.md#stackyaml). Acá está cada
campo, con el ejemplo completo y comentado en
[`stack.example.yaml`](../stack.example.yaml).

## Campos

`command` es el único campo obligatorio. `needs` define el orden de arranque
y los ciclos fallan al cargar el archivo, no a mitad del arranque. Un perfil
arrastra sus dependencias transitivas: pedir `api` sin su base de datos nunca
es lo que alguien quiso decir.

Los servicios que no dependen entre sí arrancan juntos, y cada tanda espera a
estar lista antes de la siguiente. El stack tarda el healthcheck más lento de
cada nivel en lugar de la suma de todos, a cambio de que los logs de los
primeros segundos se entreveren. Van prefijados por servicio y con un color
propio cada uno, así que se siguen leyendo.

`default` es opcional y lista qué arranca cuando no pedís perfil. Sin él,
arranca todo, que es lo que casi siempre querés. Existe para el caso de abajo.

## Perfiles de un compose detectado

Un `compose.yaml` con `profiles:` los trae puestos:

```yaml
services:
  db: { image: postgres }
  seed:
    image: seed
    profiles: [tools]
```

Ahí `seed` queda afuera del arranque por defecto y entra con
`stackhelx up --profile tools`, igual que con `docker compose --profile tools`.
Ojo con la semántica, porque está invertida: en compose `profiles:` **excluye**
un servicio hasta que lo pidas, mientras que en `stack.yaml` un perfil es la
lista de lo que se arranca. `detect` traduce de una a la otra, y por eso
`stackhelx init` sobre ese proyecto escribe también un `default`: sin él, el
archivo congelado prendería lo que el compose deja apagado a propósito.

## ready

`ready` decide cuándo un servicio cuenta como listo, y acepta cinco formas:

| Valor | Espera a que |
|---|---|
| `port` | el puerto acepte conexiones (default si hay `port`) |
| `listen` | el proceso abra un puerto cualquiera, y lo reporta |
| `log:texto` | ese texto aparezca en la salida del servicio |
| `http://...` | esa URL responda con menos de 400 |
| `none` | nada (default si no hay `port`) |

`listen` es para servicios que eligen su propio puerto. Es incompatible con
`port`: si lo conocés, el healthcheck es `port`.

`port` no distingue quién contesta. Si alguien ya escuchaba ahí antes de
arrancar, el servicio se declara listo en el acto aunque el `listo` sea de otro
proceso, y por eso el arranque lo dice: `listo (3000) · el puerto ya estaba
ocupado antes de arrancar`. Con `up` normal no pasa, porque libera los puertos
primero; aparece con `--no-free`, y en el caso legítimo de un
`docker compose up -d` sobre un contenedor que ya estaba arriba.

## stop

`stop` es un comando de apagado propio, opcional. Sin él, apagar mata el árbol
de procesos del servicio, que es lo correcto para un `npm run dev` y no alcanza
para un contenedor: `docker compose up -d` termina enseguida y lo que queda
vivo no es hijo nuestro. Los servicios detectados de un compose traen
`stop: docker compose stop <nombre>`. Si el comando falla o tarda más de 90s, se
loguea y el apagado sigue con el resto.

## env_file

`env_file` permite cargar variables de entorno desde uno o varios archivos (ej.
`env_file: .env` o `env_file: [.env, .env.local]`).

Las rutas van **relativas a la raíz del proyecto y no pueden salir de ella**:
`../../.env` o una ruta absoluta son `ConfigError` al cargar el archivo, igual
que `cwd`. Un `stack.yaml` ajeno no puede pedir el `.env` de otro proyecto tuyo.

La precedencia de variables es:
1. `os.environ` del sistema anfitrión.
2. `~/.stackhelx/env.global` (bóveda global de variables compartidas, si existe).
3. Archivos listados en `env_file` (en orden de aparición).
4. `env:` declarado explícitamente en el servicio.

Con una excepción: después de aplicar todo lo anterior, `build_env` fija
`PYTHONUNBUFFERED=1` y `FORCE_COLOR=1`. Para esas dos claves `env:` no gana,
porque de ellas depende que los logs del servicio lleguen a la terminal y a
la interfaz en vivo en lugar de quedarse en un buffer.

## url

Adónde lleva el botón `Abrir`, en la interfaz y en `stackhelx open`. Sin él es
`http://localhost:<port>`, que es lo correcto para la mayoría de los servicios y
no alcanza para los que no viven en la raíz del puerto:

```yaml
services:
  studio:
    command: uv run python ui/server.py 8765
    port: 8765
    env_file: [.env]
    url: http://127.0.0.1:8765/?token=${ORQUESTER_TOKEN}
```

Reglas:

- Solo `http://` y `https://`. Otro esquema es error al cargar el archivo. No es
  una barrera de seguridad —un `stack.yaml` ya ejecuta comandos arbitrarios, y
  eso está en el modelo de confianza del README— sino que convierte el error de
  tipeo más probable, escribir `127.0.0.1:8765` sin esquema, en un mensaje claro
  en vez de una ruta relativa que el navegador interpreta como puede.
- Admite `${VAR}` y `${VAR:-default}`, resueltos con **el mismo entorno con el
  que corre el servicio**: la precedencia es la de `env_file`, de arriba. Si la
  URL necesita un token, es el token que recibió el proceso.
- No se combina con `ready: listen`. El puerto de un servicio `listen` lo elige
  el proceso y se descubre al arrancar, así que cualquier puerto escrito en la
  URL es una apuesta. Y como `url` le gana al puerto descubierto, declararla
  **empeora** el botón: si el proceso arranca en 3001, sigue llevando al 3000.
- Una variable sin valor y sin default deja el servicio **sin URL**. Con `port:`,
  el botón vuelve al `http://localhost:<port>` de siempre. **Sin `port:` no hay
  a qué caer**: ese servicio se saltea y `stackhelx open` sigue con el siguiente
  candidato; si no queda ninguno, sale con código 1. Abrir una URL con un
  `${TOKEN}` literal adentro sería peor: la página carga, falla por dentro, y
  parece que funcionó.
- En la interfaz, el botón sigue apareciendo solo cuando el puerto contestó
  HTTP. Un servicio sin `port:` nunca se puede saber si está arriba, así que ahí
  no se dibuja; ese caso lo abre `stackhelx open` desde la terminal, que no
  sondea nada.

## pre_start y post_start

Hooks síncronos de ciclo de vida:
*   `pre_start`: Comando que se ejecuta antes de lanzar el proceso principal (ej.
    migraciones de base de datos o compilación). Si retorna un código distinto de 0,
    el arranque del servicio se aborta inmediatamente.
*   `post_start`: Comando que se ejecuta una vez que el servicio confirma que está
    listo (`ready`). Si falla, se reporta el error y se detiene el stack.

## restart y max_retries

`restart` decide qué pasa cuando un servicio termina solo, y vale `no` (el
default), `on-failure` o `always`. Con `on-failure` reintenta si el código de
salida no es cero; con `always`, también si salió limpio. `max_retries` es el
tope de reintentos y por default son 3.

```yaml
services:
  worker:
    command: python worker.py
    ready: none
    restart: on-failure
    max_retries: 2
```

Corriendo eso con un comando que sale con código 3:

```
worker | proceso terminado con codigo 3. Reiniciando automaticamente (intento 1/2)...
worker | proceso terminado con codigo 3. Reiniciando automaticamente (intento 2/2)...
```

Agotados los reintentos, ese servicio queda abajo y no se vuelve a levantar. El
resto del stack sigue como si nada: el seguimiento de logs continúa y el stack
se da por terminado solo cuando no queda ningún servicio vivo. Con un solo
servicio declarado son la misma cosa, con varios no.

Dos límites que conviene saber antes de apoyarse en esto. El primero es que el
vigilante vive en el seguimiento de logs, o sea que `restart` actúa mientras
`stackhelx up` sigue corriendo o mientras la interfaz tiene la sesión viva, y
no después. El segundo es que la cuenta de reintentos no se reinicia cuando el
servicio se estabiliza: un proceso que cae una vez por hora agota su
`max_retries` a lo largo del día y deja de levantarse.

Los servicios `detached` quedan afuera: su comando termina por diseño y
tratarlo como una caída lo relanzaría en loop.

Cualquier otro valor en `restart` es error al cargar el archivo, no a mitad del
arranque:

```
services.x.restart debe ser 'no', 'on-failure' o 'always'
services.x.max_retries debe ser un entero positivo o 0
```

## scripts

La sección `scripts` permite declarar tareas de desarrollo o pipelines secuenciales:

```yaml
scripts:
  test: pytest tests/ -v
  lint: ruff check .
  check: [lint, test]           # ejecuta lint y luego test
  migrate: alembic upgrade head
```

Se ejecutan con `stackhelx run <nombre>` (ej. `stackhelx run test`). Cada comando corre en la raíz del proyecto y recibe el contexto de variables de entorno inyectadas.

## includes

Permite componer stacks importando servicios de otros repositorios o subcarpetas:

```yaml
includes:
  - ../servicio-auth
  - ./servicios/pagos
```

Cada ruta relativa se resuelve respecto al `stack.yaml` padre. Los servicios importados
se ejecutan en su propio directorio de trabajo (`cwd`) y pueden declararse como dependencias
en `needs:` de cualquier otro servicio del stack. Se detectan y previenen ciclos de inclusión.



