# Interfaz web

Cómo se arranca y qué muestra está en el
[README](../README.md#interfaz-web). Acá está el detalle de cada control y por
qué se comporta como se comporta.

## Caídas y avisos

Cuando un servicio se muere solo, el título de la pestaña lleva un contador y
el encabezado dice cuántos hay caídos. Con `Avisarme` podés además pedir
notificaciones del navegador, que solo avisan de caídas nuevas y nunca de un
`Apagar` ni de un `Reiniciar`. El permiso se pide con ese click y nunca al
cargar la página.

## Congelar a stack.yaml

Un proyecto detectado trae `Congelar a stack.yaml`, que es `stackhelx init`
sin salir de la interfaz: útil cuando ves en la tarjeta que detectó un puerto
que no era. Pide confirmación sobre el mismo botón, escribe la ruta del
registro y nunca una que venga del navegador, y no sobreescribe un archivo
existente.

## Reiniciar un servicio

Cada servicio arrancado desde la interfaz trae un `Reiniciar` propio, que baja y
sube ese solo. Cuando el frontend se cuelga, los contenedores que estaban bien
no tienen por qué pagarlo.

## El botón Abrir

Los servicios que se pueden abrir en el navegador traen un botón `Abrir`, y la
tarjeta del proyecto trae el suyo, que lleva al último de la lista que conteste,
para no buscar cuál de los tres es el frontend. Cuál lo lleva no se adivina por
el nombre: cuando el servicio queda listo, StackHelx le hace una petición al
puerto. Si contesta HTTP, es abrible. Un `404` cuenta,
porque la mayoría de las APIs no sirven nada en la raíz; lo que descarta al
servicio es que no conteste, que es el caso de una base de datos.

## Docker

Si algún proyecto de la página usa Docker, la fila de herramientas dice
`Docker corriendo` o `Docker cerrado`, y al lado hay un botón que cambia de
trabajo según cuál sea: `Abrir Docker` con el motor caído, `Reiniciar Docker`
con el motor arriba, que es lo que uno quiere cuando los contenedores empiezan a
portarse raro. Los dos se ven siempre: un control que solo aparece cuando algo
falla no distingue "está en orden" de "esto dejó de funcionar".

Esa sección de la interfaz se ve siempre que haya un proyecto registrado, y
cuando no hay ningún intruso lo dice en lugar de desaparecer. Por el mismo
motivo que el estado de Docker: una sección vacía informa, una sección ausente
deja dudando si el chequeo corrió.

Reiniciar pide confirmación sobre el mismo botón, como `Congelar`. Se lleva
puestos todos los contenedores que estén corriendo, incluidos los de proyectos
que no estás mirando. Abrir no pregunta nada, porque ahí no hay nada que perder.

Ojo con qué significa `Docker cerrado`: la pregunta es si el daemon contesta,
no si la ventana de Docker Desktop está abierta. Cerrar la ventana deja el motor
corriendo en la bandeja, y ahí tus contenedores arrancan igual.

Por debajo corre `docker desktop start --detach` o `docker desktop restart
--detach`, el plugin oficial del CLI: `docker` ya tiene que estar en el `PATH`
para que un stack con compose sirva de algo, y el ejecutable de Docker Desktop
no lo está en ninguna plataforma. `--detach` porque sin él el comando espera
medio minuto a que el motor termine, y el request se lo comería entero.

El botón dice lo que pasó de verdad, incluido `docker no esta en el PATH` o el
error del propio Docker. Cuando el motor termina de levantar, el botón no
desaparece: pasa a decir `Reiniciar Docker`. Lo mueve la misma vista de estado
que ya sondea `docker info`.

## Explorar carpetas

Para registrar un proyecto no hace falta copiar la ruta: `Explorar…` abre un
navegador de carpetas que empieza en tu home y en las unidades montadas, y marca
las que tienen `stack.yaml`, un compose, un `package.json` o un `manage.py`. El
listado lo arma el servidor, porque una página web no puede conocer rutas
absolutas de tu disco. Devuelve nombres de carpetas y de esos archivos
marcadores, nunca contenido.

## Seguridad

El servidor escucha solo en loopback y exige un token que `serve` genera en
`~/.stackhelx/token` y pasa en la URL de arranque. Ejecuta los comandos de tus
`stack.yaml`, así que se trata como superficie sensible: rate limit en todas las
rutas, CSP estricta, y validación del header `Host` contra rebinding de DNS.
Podés fijar el token vos mismo con `STACKHELX_TOKEN`.
