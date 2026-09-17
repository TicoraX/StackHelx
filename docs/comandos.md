# Otros comandos

> **Nota:** Todos los comandos del sistema pueden ejecutarse indistintamente con `stackhelx <cmd>` o mediante el alias corto oficial **`shx <cmd>`** (por ejemplo: `shx down`, `shx doctor`, `shx switch`, `shx run`, `shx share`).

`up`, `serve`, `ports` y `free` están en el [README](../README.md#comandos).
Acá están los cuatro que quedan, con qué revisa cada uno y por qué.

## Bajar lo que sobrevive a la terminal

```bash
shx down
# o stackhelx down
shx down --profile backend
```

`Ctrl-C` sobre un `stackhelx up` apaga a sus hijos, pero un
`docker compose up -d` termina enseguida y deja los contenedores corriendo.
`down` ejecuta el `stop` de cada servicio que lo declara, en orden inverso al
de arranque. Si ningún servicio declara `stop`, lo dice y no hace nada: esos
son hijos de la terminal y ya se fueron con `Ctrl-C`.

## Cambiar de proyecto

```bash
stackhelx switch fitness
stackhelx switch A:\Proyectos\Fitness    # o la ruta, si hay dos con el mismo nombre
```

Baja los proyectos registrados que declaran alguno de los puertos que este
necesita, y después lo levanta. Solo los que chocan: parar una base de datos que
nadie disputa no ayuda a arrancar y es lo que más cuesta volver a levantar.

Baja lo que declara `stop`, o sea contenedores. Un `npm run dev` de otra
terminal no es hijo de nadie que StackHelx controle, así que si sigue ocupando
el puerto lo agarra el paso de liberación de `up`, que pregunta antes de cerrar
nada.

## Diagnóstico

```bash
stackhelx doctor
```

Revisa, sin arrancar nada, lo que suele impedir un arranque: qué stack se lee o
se detecta, si cada comando existe en el `PATH`, si el daemon de Docker
contesta, y qué puertos declarados están ocupados y por quién. Cada chequeo en
rojo trae la línea para arreglarlo.

```
ok    token                  C:\Users\vos\.stackhelx\token
ok    stack                  detectado (3 servicios)
ok    comando docker         C:\Program Files\Docker\...\docker.EXE
FALLA daemon de docker       no esta en ejecucion
                             -> abri Docker Desktop
aviso puerto 3000            ocupado por node.exe (pid 24180), lo pide web
                             -> stackhelx free 3000
```

Si hay un `.env.example`, compara sus claves contra el `.env` y avisa cuáles
faltan o quedaron sin valor. Nombres de claves nada más: los valores no salen
ni por la terminal ni por la API.

Sale con 1 solo si algo impide arrancar. Un puerto ocupado es aviso, porque
`stackhelx up` ofrece liberarlo, y una clave que falta también, porque puede
ser opcional o venir del entorno. Fuera de un proyecto revisa nada más el
entorno, que es lo que uno quiere recién instalado.

## Abrir el stack en el navegador

```bash
stackhelx open         # el ultimo servicio del stack que conteste HTTP
stackhelx open 3000    # o el puerto que le pases
```

Sirve cuando el stack ya está corriendo en otra terminal. Recorre los puertos
en orden inverso al de arranque, porque lo que uno quiere mirar suele ser el
frontend, y abre el primero que contesta. Una base de datos no contesta HTTP,
así que nunca es el elegido.

## Ejecutar scripts y pipelines de tareas

```bash
stackhelx run              # lista las tareas declaradas en stack.yaml
stackhelx run test         # ejecuta una tarea específica
stackhelx run test -k foo  # pasa argumentos adicionales al comando
stackhelx run check        # ejecuta un pipeline secuencial de scripts
```

Permite definir scripts del proyecto en `stack.yaml` (ej. tests, linters, migraciones, seeders)
y ejecutarlos con el contexto completo de variables de entorno inyectadas (`.env`, `env.global`, etc.).
Si un paso del pipeline falla, la ejecución se detiene inmediatamente con el código de error correspondiente.

## Compartir servicios vía túneles públicos

```bash
stackhelx share               # expone el servicio web principal
stackhelx share 3000          # expone un puerto específico
stackhelx share api           # expone el servicio por nombre
stackhelx share --provider ngrok  # fuerza el proveedor (cloudflared, ngrok, lt, tailscale)
```

Genera un túnel HTTPS seguro y efímero hacia el puerto local, ideal para probar webhooks,
compartir vistas previas con clientes o probar en dispositivos móviles. Presioná `Ctrl-C` para
cerrar el túnel de inmediato.

**No se puede compartir el puerto de `stackhelx serve`.** Detrás de ese puerto está la API
que arranca los servicios de tu `stack.yaml`, o sea ejecución de comandos: publicarla dejaría
al token como única puerta entre internet y tu consola. Vale para el CLI y para el botón de la
interfaz por igual:

```console
$ stackhelx share 7667
Iniciando tunel hacia 127.0.0.1:7667...
Error: el puerto 7667 es de un `stackhelx serve`. Publicarlo expone la API que
ejecuta los comandos de tu stack.yaml, no tu proyecto.
```

## Limpieza de recursos Docker (Higiene)

```bash
stackhelx clean                        # contenedores parados, imagenes sin tag, redes sin usar y cache de build
stackhelx clean --solo cache --solo images   # solo esas dos categorias
stackhelx clean --volumes              # ademas, volumenes anonimos/huerfanos
```

Limpia **por categorías**, con un comando propio para cada una, no con un
`docker system prune` que las tira todas juntas. No es lo mismo borrar
contenedores parados que caché de build: la caché se regenera sola y las
imágenes sin tag pueden ser la capa base que vas a volver a bajar. Poder
separarlo es el punto.

Los **volúmenes van aparte en todos lados** y nunca entran por defecto: adentro
hay datos y no se regeneran. Hay que pedirlos con `--volumes`; en la interfaz
son una casilla propia, y el servidor MCP directamente los rechaza.

Antes de borrar nada muestra `docker system df` para que veas qué hay en juego,
y pregunta. `--yes` saltea la pregunta, para scripts. Si una categoría falla, las
demás siguen y el resumen dice cuál falló: quedarse a mitad sin decir dónde es
peor que terminar y contarlo.

## Validar el stack sin arrancarlo

```bash
stackhelx test-stack
stackhelx test-stack ../otro-proyecto
```

Carga el `stack.yaml`, resuelve el orden topológico y mira si los puertos
declarados están libres. No arranca nada, así que es lo más barato para saber si
un archivo que acabás de editar carga bien.

```
Validando stack demo en C:\...\demo...
OK: 1 servicio(s) resueltos en orden topológico:
  - api: python -c "..." -> puerto 8123
OK: Todos los puertos declarados están libres

Stack validado con éxito.
```

Un archivo inválido sale por código 1 con el motivo, el mismo mensaje que daría
`up` al arrancar:

```
Configuración inválida: services.x.restart debe ser 'no', 'on-failure' o 'always'
```

Los puertos ocupados salen como aviso y no como error: que algo esté escuchando
ahí no es necesariamente un problema, y `up` los libera preguntando antes.

## Historial de arranques

```bash
stackhelx history
stackhelx history --limit 20
```

```
            Historial de arranques: demo
+--------------------------------------------------+
| Fecha            | Perfil | Duración | Resultado |
|------------------+--------+----------+-----------|
| 2026-08-29 05:38 | -      | 7.4s     | running   |
+--------------------------------------------------+
```

**El historial lo escribe la interfaz web, no el CLI.** Cada arranque desde
`stackhelx serve` deja una línea con su duración y su resultado; `stackhelx
up` desde la terminal no registra nada. Si solo usaste el CLI, esto contesta
`No hay historial para el proyecto <nombre>` y no está roto.

Los archivos viven en `~/.stackhelx/history/<id>.jsonl`, uno por proyecto, y se
recortan solos a los últimos 250 arranques. `--limit` acepta de 1 a 50.

## Logs y métricas del stack que corre en la interfaz

```bash
stackhelx logs                    # lo que haya hasta ahora
stackhelx logs --follow           # y seguir
stackhelx logs --service api      # filtrar por nombre
stackhelx stats                   # CPU y memoria (alias: stackhelx top)
```

Los dos le preguntan al `stackhelx serve` que ya tengas abierto, así que
necesitan que esté corriendo. Con `--port` se apunta a otro:

```
       Métricas en tiempo real: demo
+-----------------------------------------+
| Servicio | PID  |  CPU % | Memoria (MB) |
|----------+------+--------+--------------|
| api      | 7532 | 108.8% |      25.6 MB |
+-----------------------------------------+
```

Los números son del árbol completo de cada servicio, no del proceso directo: con
`shell=True` el hijo inmediato es el shell y el servidor de verdad es un nieto,
así que sumar solo el padre daría una memoria de juguete. Por eso el CPU pasa de
100%, que es un núcleo entero: son porcentajes por núcleo sumados.

Sin el servidor levantado, los dos salen por código 1 diciéndolo:

```
No se pudo conectar con StackHelx en http://127.0.0.1:7666.
Asegúrate de que `stackhelx serve` está corriendo.
```

## Servidor MCP para Agentes de IA

```bash
stackhelx mcp
```

Inicia un servidor estándar Model Context Protocol (MCP) sobre `stdio`. Permite que
asistentes inteligentes (Claude Desktop, Cursor, Gemini, Antigravity) inspeccionen el estado
del stack, ejecuten scripts declarados en `stack.yaml`, consulten puertos y diagnostiquen errores
en tiempo real durante sesiones de desarrollo guiado.

Las nueve herramientas que expone:

| Herramienta | Qué hace |
|---|---|
| `stackhelx_status` | Estado de servicios, proyectos y puertos |
| `stackhelx_doctor` | Diagnóstico del entorno, con la solución sugerida de cada check |
| `stackhelx_ports` | Escanea puertos, los que le pases o los del stack |
| `stackhelx_free_port` | Cierra el proceso que ocupa un puerto |
| `stackhelx_share` | Abre un túnel público hacia un puerto local |
| `stackhelx_run` | Ejecuta un script o pipeline de `stack.yaml` |
| `stackhelx_clean` | Limpia recursos de Docker |
| `stackhelx_history` | Historial de arranques del proyecto |
| `stackhelx_init` | Congela lo detectado en un `stack.yaml` |

Todas aceptan un `path` opcional; sin él trabajan sobre el directorio actual.

### Lo que el agente no puede hacer

Del otro lado hay un agente y no una persona mirando la pantalla, así que tres
cosas están cortadas a propósito y no son las mismas que en el CLI:

- **`stackhelx_share` solo publica puertos que el proyecto declara.** Pedir uno
  ajeno responde `El puerto 5432 no pertenece a los puertos declarados`, y el
  puerto de la propia interfaz está vetado aparte. El CLI no tiene esta
  restricción: ahí el puerto lo escribís vos.
- **`stackhelx_clean` no borra volúmenes.** El flag existe en el CLI y no en el
  esquema de la herramienta.
- **Hay un tope de 30 llamadas por minuto.** Pasado eso contesta `Límite de
  acciones MCP excedido`. Es contra el bucle de un agente que se traba, no
  contra un uso normal.

Los túneles que abre la sesión se cierran cuando la sesión termina. Sin eso, el
cliente de túneles seguía vivo publicando el puerto después de que el agente se
fuera.




