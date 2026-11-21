/* Logica pura de la interfaz: sin DOM, sin red, sin estado. Vive aparte de
 * app.js para poder probarla con `node` (tests/test_web_logic.py). */

/* Que paso al cargar el estado. `error.status` lo pone `api()` cuando hubo
 * respuesta HTTP; sin status y con TypeError es que `fetch` no llego al servidor. */
function classifyLoadError(error) {
  const status = error && error.status;
  if (status === 401) return "auth";
  if (status >= 500) return "server";
  if (status) return "request";
  if (error instanceof TypeError) return "offline";
  return "unexpected";
}

function loadErrorText(kind, detail, lang) {
  const es = lang === "es";
  switch (kind) {
    case "offline":
      return {
        head: es ? "sin conexión" : "offline",
        body: es
          ? "Sin conexión con StackHelx. ¿Sigue corriendo `stackhelx serve`?"
          : "No connection to StackHelx. Is `stackhelx serve` still running?",
      };
    case "server":
      return {
        head: es ? "error del servidor" : "server error",
        body: es
          ? `El servidor respondió con un error: ${detail}`
          : `The server answered with an error: ${detail}`,
      };
    case "auth":
      return {
        head: es ? "token inválido" : "invalid token",
        body: es
          ? "El token de acceso no es válido. Pégalo de nuevo."
          : "The access token is not valid. Paste it again.",
      };
    default:
      return {
        head: es ? "error" : "error",
        body: es ? `No se pudo cargar: ${detail}` : `Could not load: ${detail}`,
      };
  }
}

/* Puertos declarados por mas de un proyecto distinto. `items` son filas del
 * mapa: { port, project }. Dos servicios del mismo proyecto no son conflicto. */
function portConflicts(items) {
  const owners = new Map();
  for (const { port, project } of items) {
    if (!port || !project) continue;
    if (!owners.has(port)) owners.set(port, new Set());
    owners.get(port).add(project);
  }
  return new Set([...owners].filter(([, set]) => set.size > 1).map(([port]) => port));
}

/* Texto de la confirmacion antes de matar un proceso: nombra proceso y puerto. */
function killConfirmText({ name, pid, port }, lang) {
  const who = name || (lang === "es" ? "proceso desconocido" : "unknown process");
  const withPid = pid ? `${who} (pid ${pid})` : who;
  return lang === "es"
    ? `¿Detener ${withPid} en el puerto ${port}?`
    : `Stop ${withPid} on port ${port}?`;
}

if (typeof module !== "undefined") {
  module.exports = { classifyLoadError, loadErrorText, portConflicts, killConfirmText };
}
