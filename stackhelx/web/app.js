/* Interfaz de StackHelx. Sin framework, sin build: la pagina la sirve el
 * propio CLI y la CSP es default-src 'self'. Nada de estilos inline, todo por
 * clases y atributos. */

const POLL_MS = 2500;

const ui = {
  projects: document.getElementById("projects"),
  empty: document.getElementById("empty"),
  connection: document.getElementById("connection"),
  flash: document.getElementById("flash"),
  enroll: document.getElementById("enroll"),
  path: document.getElementById("path"),
  browse: document.getElementById("browse"),
  find: document.getElementById("find"),
  search: document.getElementById("search"),
  count: document.getElementById("count"),
  pager: document.getElementById("pager"),
  pagerAt: document.getElementById("pager-at"),
  picker: document.getElementById("picker"),
  pickerPath: document.getElementById("picker-path"),
  pickerFrequent: document.getElementById("picker-frequent"),
  pickerFrequentChips: document.getElementById("picker-frequent-chips"),
  pickerList: document.getElementById("picker-list"),
  pickerNote: document.getElementById("picker-note"),
  tplProject: document.getElementById("tpl-project"),
  tplService: document.getElementById("tpl-service"),
  tunnels: document.getElementById("tunnels"),
  tunnelsList: document.getElementById("tunnels-list"),
  tunnelsHeading: document.getElementById("tunnels-heading"),
  orphans: document.getElementById("orphans"),
  orphansList: document.getElementById("orphans-list"),
  orphansHeading: document.getElementById("orphans-heading"),
  orphansKillAll: document.getElementById("orphans-kill-all"),
  health: document.getElementById("health"),
  notify: document.getElementById("notify"),
  pathSuggestions: document.getElementById("path-suggestions"),
  dockerState: document.getElementById("docker-state"),
  btnDocker: document.getElementById("btn-docker"),
  btnDockerClean: document.getElementById("btn-docker-clean"),
  cleanModal: document.getElementById("clean-modal"),
  cleanUsage: document.getElementById("clean-usage"),
  cleanTargets: document.getElementById("clean-targets"),
  cleanRun: document.getElementById("clean-run"),
  cleanWarn: document.getElementById("clean-warn"),
  btnPortsModal: document.getElementById("btn-ports-modal"),
  portsModal: document.getElementById("ports-modal"),
  portsModalList: document.getElementById("ports-modal-list"),
  btnMcpModal: document.getElementById("btn-mcp-modal"),
  mcpModal: document.getElementById("mcp-modal"),
  mcpTotalCalls: document.getElementById("mcp-total-calls"),
  mcpQuotaUsed: document.getElementById("mcp-quota-used"),
  mcpBreakdown: document.getElementById("mcp-breakdown"),
  mcpTbody: document.getElementById("mcp-tbody"),
};

const TITLE = document.title;

const cards = new Map(); // id -> {root, logSeq, logsOpen}
let flashTimer = null;
let latestOrphansList = [];
// Lo pone `render`, lo usa `refreshOrphans`: los dos sondeos son distintos y el
// de intrusos no recibe el total de proyectos registrados.
let hayProyectos = false;

let query = "";
let statusFilter = "";
let page = 1;
let cachedEditors = null;

async function loadAvailableEditors() {
  if (cachedEditors !== null) return cachedEditors;
  try {
    const data = await api("/api/editors");
    cachedEditors = data && data.editors ? data.editors : [];
  } catch {
    cachedEditors = [];
  }
  return cachedEditors;
}


/* token ------------------------------------------------------------------- */

function getCookieToken() {
  const match = document.cookie.match(/(?:^|; )stackhelx_token=([^;]*)/);
  return match ? decodeURIComponent(match[1]) : "";
}

function readToken() {
  const url = new URL(window.location.href);
  const fromUrl = url.searchParams.get("token");
  if (fromUrl) {
    localStorage.setItem("stackhelx.token", fromUrl);
    sessionStorage.setItem("stackhelx.token", fromUrl);
    url.searchParams.delete("token");
    // Sacarlo de la barra: no tiene por que quedar en el historial.
    window.history.replaceState({}, "", url.pathname + url.search + url.hash);
    return fromUrl;
  }
  return (
    localStorage.getItem("stackhelx.token") ||
    sessionStorage.getItem("stackhelx.token") ||
    localStorage.getItem("portmaster.token") ||
    sessionStorage.getItem("portmaster.token") ||
    getCookieToken() ||
    ""
  );
}

let token = readToken();

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      Authorization: `Bearer ${token}`,
      ...(options.body ? { "Content-Type": "application/json" } : {}),
    },
  });

  if (response.status === 401) {
    localStorage.removeItem("stackhelx.token");
    sessionStorage.removeItem("stackhelx.token");
    token = "";
    promptAuthModal();
  }

  if (!response.ok) {
    let detail = `error ${response.status}`;
    try {
      const body = await response.json();
      if (body && body.detail) detail = body.detail;
    } catch {
      /* respuesta sin cuerpo JSON */
    }
    throw new Error(detail);
  }
  return response.json();
}

function promptAuthModal() {
  const modal = document.getElementById("auth-modal");
  const input = document.getElementById("auth-token-input");
  const saveBtn = document.getElementById("auth-token-save");
  if (!modal || modal.open) return;
  modal.showModal();
  saveBtn.onclick = () => {
    const val = input.value.trim();
    if (!val) return;
    localStorage.setItem("stackhelx.token", val);
    sessionStorage.setItem("stackhelx.token", val);
    token = val;
    modal.close();
    refresh();
  };
}

/* avisos ------------------------------------------------------------------ */

function flash(message, tone) {
  ui.flash.textContent = message;
  ui.flash.dataset.tone = tone || "";
  ui.flash.hidden = false;
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => {
    ui.flash.hidden = true;
  }, 4500);
}

async function act(button, work) {
  if (button.dataset.busy === "true") return;
  button.setAttribute("aria-disabled", "true");
  button.dataset.busy = "true";
  try {
    await work();
    await refresh();
  } catch (error) {
    flash(error.message, "bad");
  } finally {
    button.removeAttribute("aria-disabled");
    delete button.dataset.busy;
  }
}

/* etiquetas --------------------------------------------------------------- */

const PROJECT_LABELS = {
  stopped: ["detenido", ""],
  starting: ["arrancando", "starting"],
  running: ["corriendo", "ready"],
  stopping: ["apagando", "starting"],
  error: ["con error", "bad"],
  invalid: ["config invalida", "bad"],
};

const SERVICE_LABELS = {
  stopped: ["detenido", ""],
  starting: ["arrancando", "starting"],
  ready: ["listo", "ready"],
};

/* render ------------------------------------------------------------------ */

const SVG = "http://www.w3.org/2000/svg";

/* Dos iconos, uno por cada cosa que el servidor sabe con certeza: contenedor o
 * proceso local. Trazos a 16px, sin dependencias ni webfonts. */
const ICONS = {
  container: "M2.5 5.5h11v3.5h-11zM2.5 10h11v3.5h-11zM4.5 3.5h7",
  local: "M2.5 3.5h11v9h-11zM5 7l1.75 1.75L5 10.5M8.75 10.5h3",
};

function kindIcon(kind) {
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("class", "service__icon");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(SVG, "path");
  path.setAttribute("d", ICONS[kind] || ICONS.local);
  svg.append(path);
  return svg;
}

/* Adonde lleva "Abrir". El `url:` del stack.yaml gana cuando existe: la raiz
   del puerto no siempre es la entrada, y hay apps que piden un token en la
   query o viven en un path. El servidor solo lo manda para servicios ya
   abribles, asi que aca no hay que chequear estado otra vez. */
function abrirUrl(service) {
  return service.url || `http://localhost:${service.port}`;
}

function escapeXml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

function createAltPortButton(port) {
  const copyAlt = document.createElement("button");
  copyAlt.type = "button";
  copyAlt.className = "btn btn--quiet btn--alt-port";
  copyAlt.textContent = `:${port}`;
  copyAlt.title = `Copiar puerto alternativo libre :${port}`;
  copyAlt.addEventListener("click", () => {
    if (navigator.clipboard) {
      navigator.clipboard.writeText(String(port));
      flash(`Puerto :${port} copiado al portapapeles`, "good");
    }
  });
  return copyAlt;
}

function renderService(service, projectId) {
  const node = ui.tplService.content.firstElementChild.cloneNode(true);
  node.querySelector(".service__name").prepend(kindIcon(service.kind));
  node.querySelector(".service__name").title =
    service.kind === "container" ? "contenedor" : "proceso local";

  const portCell = node.querySelector(".service__port");
  portCell.textContent = service.port ? String(service.port) : "—";

  // Otro proyecto registrado declara este mismo puerto. No es un error todavia,
  // por eso es una marca al lado del numero y no un estado: conviven mientras
  // no corran a la vez.
  const shared = service.shared_with || [];
  if (shared.length) {
    const mark = document.createElement("span");
    mark.className = "service__shared";
    mark.textContent = "△";
    const aviso = `El puerto ${service.port} tambien lo declara ${shared.join(", ")}`;
    mark.title = aviso;
    mark.setAttribute("aria-label", aviso);
    mark.setAttribute("role", "img");
    portCell.append(" ", mark);
  }

  // El puerto ya estaba ocupado cuando arrancamos, asi que el verde puede ser
  // de otro proceso. Marca y no estado: con un compose ya arriba es lo normal.
  if (service.port_taken) {
    const mark = document.createElement("span");
    mark.className = "service__taken";
    mark.textContent = "?";
    const aviso =
      `El puerto ${service.port} ya estaba ocupado antes de arrancar: ` +
      "el listo puede ser de otro proceso";
    mark.title = aviso;
    mark.setAttribute("aria-label", aviso);
    mark.setAttribute("role", "img");
    portCell.append(" ", mark);
  }

  // El boton de abrir sale solo cuando el puerto contesto HTTP. Un postgres
  // listo tiene puerto y abrirlo en el navegador no lleva a ningun lado.
  if (service.openable && service.port) {
    const destino = abrirUrl(service);
    const link = document.createElement("a");
    link.href = destino;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.className = "btn btn--open";
    link.title = `Abrir ${destino}`;
    link.textContent = "Abrir ↗";
    node.querySelector(".service__act").append(link);

    // Abierto o cerrado, el mismo boton: dos controles para un estado que solo
    // puede estar de una de las dos formas se pisan y confunden.
    const abierto = tunnelPorts.has(service.port);
    const shareBtn = document.createElement("button");
    shareBtn.type = "button";
    shareBtn.className = "btn btn--quiet";
    shareBtn.textContent = abierto ? "Cerrar túnel" : "Túnel";
    shareBtn.title = abierto
      ? `El puerto ${service.port} está expuesto a internet. Cerrar el túnel.`
      : "Compartir este puerto con un túnel público seguro";
    shareBtn.addEventListener("click", () => {
      act(shareBtn, async () => {
        if (abierto) {
          await api(`/api/share/${service.port}`, { method: "DELETE" });
          flash(`Túnel del puerto ${service.port} cerrado`, "good");
          return;
        }
        const res = await api(`/api/share?port=${service.port}`, { method: "POST" });
        if (res.ok && res.url) {
          // El aviso de "copiado" iba antes de copiar, y sin esperar: si el
          // navegador negaba el permiso, decia que estaba en el portapapeles y
          // no estaba. La URL va en el mensaje igual, que es lo unico que no
          // puede fallar.
          let copiado = false;
          if (navigator.clipboard) {
            try {
              await navigator.clipboard.writeText(res.url);
              copiado = true;
            } catch {
              copiado = false;
            }
          }
          // El `open` va despues de un `await`, asi que el navegador ya no lo
          // cuenta como gesto del usuario y el bloqueador de popups se lo come
          // sin avisar. Mismo criterio que el portapapeles de arriba: no decir
          // que paso algo que no paso. La URL viaja en el mensaje igual, que es
          // lo unico que no puede fallar.
          const pestaña = window.open(res.url, "_blank");
          const detalle = [
            copiado ? "copiado al portapapeles" : null,
            pestaña ? null : "el navegador bloqueó la pestaña nueva",
          ].filter(Boolean);
          flash(
            `Túnel activo: ${res.url}${detalle.length ? ` (${detalle.join("; ")})` : ""}`,
            "good",
          );
        } else {
          flash(res.detail || "Error al iniciar túnel", "bad");
        }
      });
    });
    node.querySelector(".service__act").append(shareBtn);
  }

  node.querySelector(".service__label").textContent = service.name;

  const stateCell = node.querySelector(".service__state");
  const text = stateCell.querySelector("span:last-child");

  if (service.occupant && service.occupant.proxy) {
    // Detras del proxy hay un contenedor, no un proceso que cerrar: el pid es
    // el del motor. Quien lo publica sale en la lista de proyectos que comparten
    // el puerto, que ya esta al lado.
    const extra = service.suggested_port ? ` · Libre: :${service.suggested_port}` : "";
    text.textContent = `contenedor publicado por ${service.occupant.proxy}${extra}`;
    stateCell.dataset.tone = "warn";
    if (service.suggested_port) {
      node.querySelector(".service__act").append(createAltPortButton(service.suggested_port));
    }
  } else if (service.occupant) {
    const who = service.occupant;
    const extra = service.suggested_port ? ` · Libre: :${service.suggested_port}` : "";
    text.textContent = `ocupado por ${who.name}${who.pid ? ` (${who.pid})` : ""}${extra}`;
    stateCell.dataset.tone = "bad";

    const kill = document.createElement("button");
    kill.type = "button";
    kill.className = "btn btn--kill";
    kill.textContent = "Liberar";
    kill.addEventListener("click", () =>
      act(kill, () => api(`/api/ports/${service.port}/kill`, { method: "POST" })),
    );
    node.querySelector(".service__act").append(kill);

    if (service.suggested_port) {
      node.querySelector(".service__act").append(createAltPortButton(service.suggested_port));
    }
  } else {
    const [label, tone] = SERVICE_LABELS[service.state] || SERVICE_LABELS.stopped;
    text.textContent = label;
    stateCell.dataset.tone = tone;
  }

  // Solo hay algo que reiniciar si el stack lo arranco esta interfaz. El estado
  // no alcanza para saberlo: un contenedor levantado por afuera tambien se ve
  // "listo", y el boton contestaba 404. `managed` es lo que lo dice.
  if (service.managed && (service.state === "ready" || service.state === "starting")) {
    const again = document.createElement("button");
    again.type = "button";
    again.className = "btn btn--quiet";
    again.textContent = "Reiniciar";
    again.title = `Reiniciar ${service.name} sin tocar el resto del stack`;
    again.addEventListener("click", () =>
      act(again, () =>
        api(`/api/projects/${projectId}/services/${encodeURIComponent(service.name)}/restart`, {
          method: "POST",
        }),
      ),
    );
    node.querySelector(".service__act").append(again);
  }

  node.dataset.project = projectId;
  return node;
}

function buildCard(project) {
  const root = ui.tplProject.content.firstElementChild.cloneNode(true);
  const entry = { root, logSeq: 0, logsOpen: false, expanded: false, userToggled: false, lastState: project.state };
  const logs = root.querySelector(".logs");

  const toggleBtn = root.querySelector(".project__toggle");
  const detailsEl = root.querySelector(".project__details");
  if (detailsEl) detailsEl.id = `details-${project.id}`;
  if (toggleBtn) {
    if (detailsEl) toggleBtn.setAttribute("aria-controls", `details-${project.id}`);
    toggleBtn.addEventListener("click", () => {
      entry.userToggled = true;
      entry.expanded = !entry.expanded;
      root.setAttribute("data-expanded", String(entry.expanded));
      toggleBtn.setAttribute("aria-expanded", String(entry.expanded));
    });
  }

  root.querySelector('[data-act="up"]').addEventListener("click", (event) => {
    const profile = root.querySelector(".profile__select").value || null;
    act(event.currentTarget, () =>
      api(`/api/projects/${project.id}/up`, {
        method: "POST",
        body: JSON.stringify({ profile }),
      }),
    );
  });

  const profileSelect = root.querySelector(".profile__select");
  if (profileSelect) {
    profileSelect.addEventListener("change", () => {
      const live = entry.lastState === "starting" || entry.lastState === "running";
      const newProfile = profileSelect.value || null;
      if (live) {
        flash(`Conmutando ${project.name} al perfil "${newProfile || 'por defecto'}"...`, "neutral");
      }
      api(`/api/projects/${project.id}/switch-profile`, {
        method: "POST",
        body: JSON.stringify({ profile: newProfile }),
      })
        .then(() => {
          if (live) pull();
        })
        .catch((err) => {
          if (live) flash(`Fallo al conmutar perfil: ${err.message}`, "bad");
        });
    });
  }

  root.querySelector('[data-act="down"]').addEventListener("click", (event) => {
    act(event.currentTarget, () =>
      api(`/api/projects/${project.id}/down`, { method: "POST" }),
    );
  });

  root.querySelector('[data-act="drop"]').addEventListener("click", (event) => {
    act(event.currentTarget, () =>
      api(`/api/projects/${project.id}`, { method: "DELETE" }),
    );
  });

  // Congelar escribe en el disco del usuario, asi que pide confirmacion. Dos
  // pasos sobre el mismo boton en vez de un dialogo: la interfaz no tiene
  // primitiva de confirmacion y `window.confirm` rompe el registro visual.
  const freezeButton = root.querySelector('[data-act="freeze"]');
  freezeButton.addEventListener("click", (event) => {
    const button = event.currentTarget;
    if (button.dataset.armed !== "true") {
      button.dataset.armed = "true";
      button.textContent = `Escribir en ${project.path}\\stack.yaml?`;
      setTimeout(() => disarmFreeze(button), 6000);
      return;
    }
    disarmFreeze(button);
    act(button, async () => {
      const hecho = await api(`/api/projects/${project.id}/freeze`, { method: "POST" });
      flash(`Escrito ${hecho.path}. Revisalo antes de confiar en el.`, "good");
    });
  });

  const logsBox = root.querySelector(".logs__box");
  if (logsBox) logsBox.id = `logs-${project.id}`;
  const logsFilter = root.querySelector(".logs__filter");
  if (logsFilter) {
    logsFilter.addEventListener("input", () => {
      renderLogsText(entry);
    });
  }

  const copyLogsBtn = root.querySelector('[data-act="copy-logs"]');
  if (copyLogsBtn) {
    copyLogsBtn.setAttribute("aria-label", `Copiar logs de ${project.name}`);
    copyLogsBtn.addEventListener("click", async () => {
      if (!entry.rawLogs) {
        flash("No hay logs disponibles para copiar", "neutral");
        return;
      }
      try {
        await navigator.clipboard.writeText(entry.rawLogs);
        flash("Logs copiados al portapapeles", "good");
      } catch {
        flash("No se pudo acceder al portapapeles", "bad");
      }
    });
  }

  const clearLogsBtn = root.querySelector('[data-act="clear-logs"]');
  if (clearLogsBtn) {
    clearLogsBtn.setAttribute("aria-label", `Limpiar logs de ${project.name}`);
    clearLogsBtn.addEventListener("click", () => {
      entry.rawLogs = "";
      renderLogsText(entry);
    });
  }

  const logsButton = root.querySelector('[data-act="logs"]');
  if (logsButton && logsBox) logsButton.setAttribute("aria-controls", `logs-${project.id}`);
  logsButton.addEventListener("click", () => {
    entry.logsOpen = !entry.logsOpen;
    if (logsBox) logsBox.hidden = !entry.logsOpen;
    logsButton.setAttribute("aria-expanded", String(entry.logsOpen));
    if (entry.logsOpen) {
      entry.historyOpen = false;
      entry.graphOpen = false;
      entry.envOpen = false;
      const hb = root.querySelector(".history__box");
      if (hb) hb.hidden = true;
      const gb = root.querySelector(".graph__box");
      if (gb) gb.hidden = true;
      const eb = root.querySelector(".env__box");
      if (eb) eb.hidden = true;
      const hBtn = root.querySelector('[data-act="history"]');
      if (hBtn) hBtn.setAttribute("aria-expanded", "false");
      const gBtn = root.querySelector('[data-act="graph"]');
      if (gBtn) gBtn.setAttribute("aria-expanded", "false");
      const eBtn = root.querySelector('[data-act="env-audit"]');
      if (eBtn) eBtn.setAttribute("aria-expanded", "false");
      startLogsStream(project.id, entry);
    } else {
      stopLogsStream(entry);
    }
  });

  const historyBox = root.querySelector(".history__box");
  if (historyBox) historyBox.id = `history-${project.id}`;
  const historyButton = root.querySelector('[data-act="history"]');
  if (historyButton && historyBox) historyButton.setAttribute("aria-controls", `history-${project.id}`);
  historyButton.addEventListener("click", () => {
    entry.historyOpen = !entry.historyOpen;
    if (historyBox) historyBox.hidden = !entry.historyOpen;
    historyButton.setAttribute("aria-expanded", String(entry.historyOpen));
    if (entry.historyOpen) {
      entry.logsOpen = false;
      entry.graphOpen = false;
      entry.envOpen = false;
      stopLogsStream(entry);
      if (logsBox) logsBox.hidden = true;
      logsButton.setAttribute("aria-expanded", "false");
      const gb = root.querySelector(".graph__box");
      if (gb) gb.hidden = true;
      const gBtn = root.querySelector('[data-act="graph"]');
      if (gBtn) gBtn.setAttribute("aria-expanded", "false");
      const eb = root.querySelector(".env__box");
      if (eb) eb.hidden = true;
      const eBtn = root.querySelector('[data-act="env-audit"]');
      if (eBtn) eBtn.setAttribute("aria-expanded", "false");
      entry.lastHistoryState = project.state;
      pullHistory(project.id, entry);
    }
  });

  const graphBox = root.querySelector(".graph__box");
  if (graphBox) graphBox.id = `graph-${project.id}`;
  const graphButton = root.querySelector('[data-act="graph"]');
  if (graphButton && graphBox) graphButton.setAttribute("aria-controls", `graph-${project.id}`);
  if (graphButton) {
    graphButton.addEventListener("click", () => {
      entry.graphOpen = !entry.graphOpen;
      if (graphBox) graphBox.hidden = !entry.graphOpen;
      graphButton.setAttribute("aria-expanded", String(entry.graphOpen));
      if (entry.graphOpen) {
        entry.logsOpen = false;
        entry.historyOpen = false;
        entry.envOpen = false;
        stopLogsStream(entry);
        if (logsBox) logsBox.hidden = true;
        logsButton.setAttribute("aria-expanded", "false");
        if (historyBox) historyBox.hidden = true;
        historyButton.setAttribute("aria-expanded", "false");
        const eb = root.querySelector(".env__box");
        if (eb) eb.hidden = true;
        const eBtn = root.querySelector('[data-act="env-audit"]');
        if (eBtn) eBtn.setAttribute("aria-expanded", "false");
        renderGraph(project, entry);
      }
    });
  }

  const envBox = root.querySelector(".env__box");
  if (envBox) envBox.id = `env-${project.id}`;
  const envButton = root.querySelector('[data-act="env-audit"]');
  if (envButton && envBox) envButton.setAttribute("aria-controls", `env-${project.id}`);
  if (envButton) {
    envButton.addEventListener("click", () => {
      entry.envOpen = !entry.envOpen;
      if (envBox) envBox.hidden = !entry.envOpen;
      envButton.setAttribute("aria-expanded", String(entry.envOpen));
      if (entry.envOpen) {
        entry.logsOpen = false;
        entry.historyOpen = false;
        entry.graphOpen = false;
        stopLogsStream(entry);
        if (logsBox) logsBox.hidden = true;
        logsButton.setAttribute("aria-expanded", "false");
        if (historyBox) historyBox.hidden = true;
        historyButton.setAttribute("aria-expanded", "false");
        if (graphBox) graphBox.hidden = true;
        if (graphButton) graphButton.setAttribute("aria-expanded", "false");
        pullEnvAudit(project.id, entry);
      }
    });
  }

  // Copiar ruta del proyecto con transicion de texto sobria
  const copyBtn = root.querySelector(".project__path-copy");
  if (copyBtn) {
    copyBtn.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(project.path);
        const prevText = copyBtn.textContent;
        copyBtn.textContent = "Copiado";
        copyBtn.disabled = true;
        setTimeout(() => {
          copyBtn.textContent = prevText;
          copyBtn.disabled = false;
        }, 1500);
      } catch {
        flash("No se pudo copiar la ruta", "bad");
      }
    });
  }

  // Abrir carpeta en explorador nativo
  const openFolderBtn = root.querySelector('[data-act="open-folder"]');
  if (openFolderBtn) {
    openFolderBtn.addEventListener("click", (event) => {
      act(event.currentTarget, async () => {
        try {
          await api("/api/open-folder", {
            method: "POST",
            body: JSON.stringify({ path: project.path }),
          });
          flash(`Explorador abierto en ${project.name}`, "neutral");
        } catch (err) {
          flash(`Error al abrir explorador: ${err.message}`, "bad");
        }
      });
    });
  }

  // Cuadro deslizable para elegir y abrir editor (VS Code / Cursor / etc.)
  const editorSelect = root.querySelector('[data-act="select-editor"]');
  if (editorSelect) {
    editorSelect.addEventListener("change", async (event) => {
      const chosenEditor = editorSelect.value;
      if (!chosenEditor) return;
      // Regresar el select a su valor inicial para permitir re-selección
      editorSelect.value = "";
      try {
        const res = await api("/api/open-editor", {
          method: "POST",
          body: JSON.stringify({ path: project.path, editor: chosenEditor }),
        });
        flash(`Abriendo ${project.name} en ${res.editor || chosenEditor}...`, "neutral");
      } catch (err) {
        flash(err.message, "warn");
      }
    });
  }

  cards.set(project.id, entry);
  return entry;
}

function disarmFreeze(button) {
  delete button.dataset.armed;
  button.textContent = "Congelar a stack.yaml";
}

function disarmDocker(button) {
  delete button.dataset.armed;
  button.textContent = "Reiniciar Docker";
}

ui.btnDocker.addEventListener("click", (event) => {
  const button = event.currentTarget;
  const action = button.dataset.action;

  // Abrir no pide confirmacion: no hay nada que perder. Reiniciar si, y en dos
  // pasos como Congelar, porque se lleva puestos todos los contenedores que
  // esten corriendo, incluidos los de proyectos que no estas mirando.
  if (action === "restart" && button.dataset.armed !== "true") {
    button.dataset.armed = "true";
    button.textContent = "Reiniciar y bajar los contenedores?";
    setTimeout(() => disarmDocker(button), 6000);
    // Cuales, si el motor contesta a tiempo. "los contenedores" no dice si son
    // los dos de este proyecto o los nueve de la maquina, y reiniciar el motor
    // se los lleva a todos. La respuesta llega despues del primer texto porque
    // el boton no puede quedarse esperando a docker para armarse.
    api("/api/docker/containers")
      .then((res) => {
        const nombres = res.running || [];
        if (!nombres.length || button.dataset.armed !== "true") return;
        button.textContent = `Reiniciar y bajar ${nombres.length}: ${nombres.join(", ")}?`;
      })
      .catch(() => {
        /* se queda con la frase generica, que ya es una advertencia */
      });
    return;
  }
  if (action === "restart") disarmDocker(button);

  act(button, async () => {
    const res = await api(`/api/docker/${action}`, { method: "POST" });
    // El motor tarda medio minuto. El boton cambia de texto solo, cuando la
    // vista de estado deja de reportar docker_down.
    flash(res.detail, res.ok ? "good" : "bad");
  });
});

/* Que se puede limpiar, en el orden en que conviene mirarlo: primero lo que se
 * regenera solo, ultimo lo que tiene datos adentro. Los tres primeros vienen
 * tildados porque son la limpieza de siempre; los volumenes nunca. */
const CLEAN_TARGETS = [
  { id: "cache", label: "Caché de build", nota: "se regenera al volver a construir", on: true },
  { id: "containers", label: "Contenedores parados", nota: "no los que están corriendo", on: true },
  { id: "networks", label: "Redes sin usar", nota: "las que no tienen contenedores", on: true },
  { id: "images", label: "Imágenes sin tag", nota: "hay que volver a bajarlas", on: true },
  {
    id: "volumes",
    label: "Volúmenes anónimos",
    nota: "tienen datos adentro y no se regeneran",
    on: false,
    riesgo: true,
  },
];

function renderCleanTargets() {
  ui.cleanTargets.replaceChildren(
    ...CLEAN_TARGETS.map((target) => {
      const li = document.createElement("li");
      const row = document.createElement("label");
      row.className = "clean__row";
      if (target.riesgo) row.dataset.riesgo = "true";

      const box = document.createElement("input");
      box.type = "checkbox";
      box.value = target.id;
      box.checked = target.on;
      box.addEventListener("change", refreshCleanButton);

      const texto = document.createElement("span");
      texto.textContent = `${target.label} · ${target.nota}`;

      row.append(box, texto);
      li.append(row);
      return li;
    }),
  );
  refreshCleanButton();
}

function cleanPicks() {
  return [...ui.cleanTargets.querySelectorAll("input:checked")].map((b) => b.value);
}

function refreshCleanButton() {
  const elegidos = cleanPicks();
  // Desarmar junto con la etiqueta. Sin esto, armar el boton y cerrar el
  // dialogo antes de que venzan los 6s dejaba el `armed` puesto: al reabrirlo,
  // el primer click borraba sin el paso de confirmacion que el boton promete.
  delete ui.cleanRun.dataset.armed;
  ui.cleanRun.disabled = elegidos.length === 0;
  ui.cleanRun.textContent = elegidos.length ? `Limpiar ${elegidos.length}` : "Elegí algo";
  ui.cleanWarn.textContent = elegidos.includes("volumes")
    ? "Los volúmenes no se pueden recuperar."
    : "";
}

ui.btnDockerClean.addEventListener("click", () => {
  renderCleanTargets();
  ui.cleanUsage.textContent = "Consultando a Docker…";
  ui.cleanModal.showModal();
  api("/api/docker/usage")
    .then((res) => {
      // Sin la tabla igual se puede elegir: es contexto, no un requisito.
      ui.cleanUsage.textContent = res.table || "Docker no informó cuánto ocupa.";
    })
    .catch(() => {
      ui.cleanUsage.textContent = "No se pudo consultar cuánto ocupa Docker.";
    });
});

ui.cleanModal.querySelector('[data-clean="close"]').addEventListener("click", () => {
  ui.cleanModal.close();
});

ui.cleanRun.addEventListener("click", (event) => {
  const button = event.currentTarget;
  const targets = cleanPicks();
  if (targets.length === 0) return;

  // Dos pasos sobre el mismo boton, como Congelar y como Liberar todos: el
  // segundo nombra lo que se va a borrar antes de borrarlo.
  if (button.dataset.armed !== "true") {
    button.dataset.armed = "true";
    button.textContent = `Borrar ${targets.join(", ")}?`;
    setTimeout(() => {
      delete button.dataset.armed;
      refreshCleanButton();
    }, 6000);
    return;
  }
  delete button.dataset.armed;

  act(button, async () => {
    const res = await api("/api/docker/clean", {
      method: "POST",
      body: JSON.stringify({ targets }),
    });
    ui.cleanModal.close();
    flash(res.detail, res.ok ? "good" : "bad");
  });
});

let killAllTimer = null;
let killAllSnapshot = null;
// Los puertos tildados. Vacio quiere decir "todos", que era el unico
// comportamiento posible hasta ahora: el boton no puede quedarse sin efecto por
// no haber tildado nada.
let orphanPicks = new Set();

function refreshKillAllLabel() {
  if (ui.orphansKillAll.dataset.armed === "true") return;
  const elegidos = orphanPicks.size;
  ui.orphansKillAll.textContent = elegidos ? `Cerrar ${elegidos}` : "Liberar todos";
}

function disarmKillAll() {
  if (killAllTimer !== null) {
    clearTimeout(killAllTimer);
    killAllTimer = null;
  }
  killAllSnapshot = null;
  delete ui.orphansKillAll.dataset.armed;
  refreshKillAllLabel();
}

// Dos pasos sobre el mismo boton, igual que Congelar: cerrar varios procesos de
// un click es lo mas destructivo de la interfaz, asi que el segundo paso nombra
// cuales antes de hacerlo.
ui.orphansKillAll.addEventListener("click", (event) => {
  const button = event.currentTarget;

  if (button.dataset.armed !== "true") {
    const elegidos = latestOrphansList.filter((o) => orphanPicks.has(o.port));
    killAllSnapshot = elegidos.length ? elegidos : [...latestOrphansList];
    if (killAllSnapshot.length === 0) return;

    button.dataset.armed = "true";
    const detalle = killAllSnapshot.map((o) => `:${o.port} (${o.name})`).join(", ");
    button.textContent = `Cerrar ${killAllSnapshot.length}: ${detalle}?`;

    if (killAllTimer !== null) clearTimeout(killAllTimer);
    killAllTimer = setTimeout(() => disarmKillAll(), 6000);
    return;
  }

  const victimas = killAllSnapshot || [];
  disarmKillAll();
  if (victimas.length === 0) return;

  act(button, async () => {
    // Los puertos que se mostraron en el armado, y solo esos. El servidor vuelve a
    // calcular quien los ocupa: nunca le mandamos un PID desde aca.
    const res = await api("/api/ports/kill-all", {
      method: "POST",
      body: JSON.stringify({ ports: victimas.map((o) => o.port) }),
    });
    delete ui.orphansList.dataset.ids;
    orphanPicks.clear();
    if (res.failed.length) {
      const errores = res.failed.map((f) => `:${f.port} (${f.reason})`).join(", ");
      flash(`Cerrados ${res.killed.length} de ${victimas.length}. Fallaron: ${errores}`, "warn");
    } else {
      flash(`Cerrados ${res.killed.length} procesos`, "good");
    }
    await refreshOrphans();
  });
});

function updateCard(entry, project) {
  const { root } = entry;
  entry.lastState = project.state;
  root.querySelector(".project__name").textContent = project.name;
  root.querySelector(".project__path").textContent = project.path;

  if (!entry.userToggled) {
    entry.expanded = project.state === "running" || project.state === "starting" || Boolean(project.error);
    root.setAttribute("data-expanded", String(entry.expanded));
    const toggleBtn = root.querySelector(".project__toggle");
    if (toggleBtn) toggleBtn.setAttribute("aria-expanded", String(entry.expanded));
  }

  const [label, tone] = PROJECT_LABELS[project.state] || PROJECT_LABELS.stopped;
  root.querySelector(".state").dataset.tone = tone;
  root.querySelector(".state__text").textContent = label;

  // El ultimo abrible, no el primero: el orden de arranque va de los
  // contenedores al frontend, y lo que uno quiere mirar es el final.
  const abrible = [...project.services].reverse().find((s) => s.openable && s.port);
  const open = root.querySelector(".project__open");
  open.hidden = !abrible;
  if (abrible) {
    open.href = abrirUrl(abrible);
    open.title = `Abrir ${abrible.name} en ${abrirUrl(abrible)}`;
  }

  // Solo lo detectado se puede congelar: lo que ya tiene archivo, no.
  const freeze = root.querySelector('[data-act="freeze"]');
  freeze.hidden = !project.detected;
  if (freeze.hidden) disarmFreeze(freeze);

  const error = root.querySelector(".project__error");
  error.textContent = project.error || "";
  error.hidden = !project.error;

  const dockerWarn = root.querySelector(".project__docker-warning");
  if (dockerWarn) {
    dockerWarn.textContent = project.docker_down
      ? "Docker Desktop está cerrado — abrilo para arrancar los contenedores"
      : "";
    dockerWarn.hidden = !project.docker_down;
  }

  // Solo reconstruir la lista de servicios si algo cambio. La huella
  // serializa todo lo que afecta al render: estado, puerto, occupant,
  // botones, marcas. En estado estable (90% del polling) esto evita
  // destruir y reconstruir el DOM cada 2.5s, preservando la seleccion de
  // texto, el foco del teclado y reduciendo GC.
  const services = root.querySelector(".services");
  const fingerprint = JSON.stringify(
    project.services.map((s) => [s.name, s.state, s.port, s.openable, s.managed,
      s.port_taken, s.shared_with, s.occupant, tunnelPorts.has(s.port)]),
  );
  if (services.dataset.fingerprint !== fingerprint) {
    services.dataset.fingerprint = fingerprint;
    services.replaceChildren(
      ...project.services.map((service) => renderService(service, project.id)),
    );
  }

  const metrics = project.metrics || {};
  const items = services.querySelectorAll(".service");
  project.services.forEach((s, idx) => {
    const item = items[idx];
    if (!item) return;
    const badge = item.querySelector(".service__metrics");
    if (badge && metrics[s.name]) {
      const m = metrics[s.name];
      if (m.memory_mb > 0 || m.cpu_percent > 0) {
        badge.textContent = `${m.cpu_percent}% · ${m.memory_mb} MB`;
        badge.setAttribute("aria-label", `CPU: ${m.cpu_percent}%, Memoria: ${m.memory_mb} MB`);
        badge.hidden = false;
      } else {
        badge.hidden = true;
      }
    } else if (badge) {
      badge.hidden = true;
    }
  });

  const select = root.querySelector(".profile__select");
  const wanted = [project.default.join(","), ...project.profiles].join("|");
  if (select.dataset.options !== wanted) {
    select.dataset.options = wanted;
    const all = document.createElement("option");
    all.value = "";
    // "todo" mentia cuando el stack declara `default:`: Arrancar levantaba solo
    // esos, y el resto de la lista quedaba abajo en gris sin explicacion.
    all.textContent = project.default.length ? `por defecto (${project.default.join(", ")})` : "todo";
    select.replaceChildren(
      all,
      ...project.profiles.map((name) => {
        const option = document.createElement("option");
        option.value = name;
        option.textContent = name;
        return option;
      }),
    );
  }
  root.querySelector(".profile").hidden = project.profiles.length === 0;
  if (document.activeElement !== select) {
    select.value = project.profile || "";
  }

  // Poblar select de editores disponibles de forma no intrusiva
  const editorSelect = root.querySelector('[data-act="select-editor"]');
  if (editorSelect && editorSelect.dataset.loaded !== "true") {
    loadAvailableEditors().then((editors) => {
      editorSelect.dataset.loaded = "true";
      if (!editors || editors.length === 0) {
        editorSelect.closest(".editor-select-wrap").hidden = true;
        return;
      }
      editorSelect.closest(".editor-select-wrap").hidden = false;
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.disabled = true;
      placeholder.selected = true;
      placeholder.textContent = "Editor…";
      editorSelect.replaceChildren(
        placeholder,
        ...editors.map((ed) => {
          const opt = document.createElement("option");
          opt.value = ed.id;
          opt.textContent = ed.name;
          return opt;
        }),
      );
    });
  }


  const live = project.state === "starting" || project.state === "running";
  const stopping = project.state === "stopping";
  // Hay algo que apagar aunque no lo hayamos arrancado nosotros: un contenedor
  // levantado desde la terminal publica su puerto y se ve "listo".
  const algoVivo = live || project.services.some((s) => s.state !== "stopped");
  root.querySelector('[data-act="up"]').disabled =
    live || stopping || project.state === "invalid";
  root.querySelector('[data-act="down"]').disabled = !algoVivo || stopping;

  if (entry.logsOpen) {
    if (live && !entry.eventSource) {
      startLogsStream(project.id, entry);
    } else if (!live && entry.eventSource) {
      stopLogsStream(entry);
    }
  } else {
    stopLogsStream(entry);
  }
  
  if (entry.historyOpen && entry.lastHistoryState !== project.state) {
    entry.lastHistoryState = project.state;
    pullHistory(project.id, entry);
  }

  const graphButton = root.querySelector('[data-act="graph"]');
  const hasGraph = project.graph && project.graph.nodes && project.graph.nodes.length > 1;
  if (graphButton) {
    graphButton.hidden = !hasGraph;
  }
  if (entry.graphOpen && hasGraph) {
    renderGraph(project, entry);
  }
  if (entry.envOpen) {
    pullEnvAudit(project.id, entry);
  }
}

function renderGraph(project, entry) {
  const svg = entry.root.querySelector(".graph__svg");
  if (!svg || !project.graph || !project.graph.nodes || !project.graph.nodes.length) return;

  const { levels, nodes, edges } = project.graph;
  const nodeMap = new Map();
  const serviceStateMap = new Map((project.services || []).map((s) => [s.name, s.state]));

  const nodeWidth = 130;
  const nodeHeight = 36;
  const colGap = 60;
  const rowGap = 16;
  const padding = 20;

  const maxRows = Math.max(...levels.map((l) => l.length), 1);
  const totalWidth = Math.max(340, padding * 2 + levels.length * nodeWidth + Math.max(0, levels.length - 1) * colGap);
  const totalHeight = padding * 2 + maxRows * nodeHeight + Math.max(0, maxRows - 1) * rowGap;

  levels.forEach((level, colIdx) => {
    const x = padding + colIdx * (nodeWidth + colGap);
    const colHeight = level.length * nodeHeight + (level.length - 1) * rowGap;
    const startY = padding + (totalHeight - 2 * padding - colHeight) / 2;

    level.forEach((name, rowIdx) => {
      const y = startY + rowIdx * (nodeHeight + rowGap);
      const nodeData = nodes.find((n) => n.name === name) || { name, port: null };
      nodeMap.set(name, {
        x,
        y,
        name,
        port: nodeData.port,
        state: serviceStateMap.get(name) || "stopped",
      });
    });
  });

  const arrowId = `arrow-${project.id}`;
  let html = `
    <defs>
      <marker id="${arrowId}" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
        <path d="M0,0 L0,6 L6,3 z" fill="var(--color-rule-strong)" />
      </marker>
    </defs>
  `;

  (edges || []).forEach((edge) => {
    const from = nodeMap.get(edge.from);
    const to = nodeMap.get(edge.to);
    if (!from || !to) return;

    const startX = from.x + nodeWidth;
    const startY = from.y + nodeHeight / 2;
    const endX = to.x;
    const endY = to.y + nodeHeight / 2;
    const c1X = startX + (endX - startX) / 2;
    const c2X = c1X;

    html += `
      <path d="M ${startX} ${startY} C ${c1X} ${startY}, ${c2X} ${endY}, ${endX} ${endY}"
            stroke="var(--color-rule-strong)" stroke-width="1.5" fill="none"
            marker-end="url(#${arrowId})" />
    `;
  });

  nodeMap.forEach((node) => {
    let dotColor = "var(--color-ink-4)";
    if (node.state === "ready") dotColor = "var(--color-good)";
    else if (node.state === "starting") dotColor = "var(--color-warn)";
    else if (node.state === "error") dotColor = "var(--color-bad)";

    const label = node.name.length > 12 ? node.name.slice(0, 11) + "…" : node.name;
    const portText = node.port ? `:${node.port}` : "";

    html += `
      <g class="graph__node" transform="translate(${node.x}, ${node.y})">
        <rect width="${nodeWidth}" height="${nodeHeight}" rx="6"
              fill="var(--color-paper)" stroke="var(--color-rule-strong)" stroke-width="1" />
        <circle cx="12" cy="${nodeHeight / 2}" r="4" fill="${dotColor}" />
        <text x="24" y="${nodeHeight / 2 + (portText ? -2 : 4)}"
              fill="var(--color-ink)" font-family="var(--font-mono)" font-size="11" font-weight="600">
          ${escapeXml(label)}
        </text>
        ${
          portText
            ? `<text x="24" y="${nodeHeight / 2 + 10}" fill="var(--color-ink-3)" font-family="var(--font-mono)" font-size="9">${escapeXml(portText)}</text>`
            : ""
        }
      </g>
    `;
  });

  svg.setAttribute("viewBox", `0 0 ${totalWidth} ${totalHeight}`);
  svg.setAttribute("width", String(totalWidth));
  svg.setAttribute("height", String(totalHeight));
  svg.innerHTML = html;
}

function startLogsStream(id, entry) {
  if (entry.eventSource) return;
  const liveBadge = entry.root.querySelector(".logs__live");
  const isRunning = entry.lastState === "running" || entry.lastState === "starting";
  if (!window.EventSource || !isRunning) {
    if (liveBadge) liveBadge.hidden = true;
    pullLogs(id, entry);
    return;
  }
  const url = `/api/projects/${id}/logs/stream?since=${entry.logSeq}&token=${encodeURIComponent(token)}`;
  try {
    const es = new EventSource(url);
    entry.eventSource = es;

    es.onopen = () => {
      if (liveBadge) liveBadge.hidden = false;
    };

    es.onmessage = (event) => {
      try {
        const item = JSON.parse(event.data);
        if (item.seq > entry.logSeq) {
          entry.logSeq = item.seq;
          entry.rawLogs = (entry.rawLogs || "") + item.text + "\n";
          renderLogsText(entry);
        }
      } catch {
        /* noop */
      }
    };

    es.onerror = () => {
      stopLogsStream(entry);
      pullLogs(id, entry);
    };
  } catch {
    stopLogsStream(entry);
    pullLogs(id, entry);
  }
}

function stopLogsStream(entry) {
  if (entry.eventSource) {
    entry.eventSource.close();
    entry.eventSource = null;
  }
  const liveBadge = entry.root.querySelector(".logs__live");
  if (liveBadge) liveBadge.hidden = true;
}

async function pullLogs(id, entry) {
  try {
    const data = await api(`/api/projects/${id}/logs?since=${entry.logSeq}`);
    if (data.lines.length) {
      entry.logSeq = data.lines[data.lines.length - 1].seq;
      entry.rawLogs = (entry.rawLogs || "") + data.lines.map((l) => l.text).join("\n") + "\n";
      renderLogsText(entry);
    } else if (!entry.rawLogs) {
      // Sin logs todavia: hay que pintar igual, que es donde va el cartel. Solo
      // mientras este vacio, y no en cada sondeo: reescribir el contenido cada
      // 2.5s le borraria la seleccion a quien este copiando una linea.
      renderLogsText(entry);
    }
  } catch {
    /* el proximo ciclo reintenta */
  }
}

function renderLogsText(entry) {
  const logsEl = entry.root.querySelector(".logs");
  const filterInput = entry.root.querySelector(".logs__filter");
  if (!logsEl) return;
  const atBottom = logsEl.scrollHeight - logsEl.scrollTop - logsEl.clientHeight < 40;
  const raw = entry.rawLogs || "";
  const filter = (filterInput ? filterInput.value : "").trim().toLowerCase();
  if (!raw) {
    // Una caja en blanco no distingue "no arrancaste nada" de "esto se rompio".
    // El servidor devuelve {lines: [], seq: 0} para un proyecto sin sesion, que
    // es correcto, y `pullLogs` cortaba sin escribir nada en la pantalla.
    logsEl.textContent =
      "Todavía no hay logs. Solo se registran los de un stack arrancado desde acá.";
  } else if (!filter) {
    logsEl.textContent = raw;
  } else {
    const lines = raw.split("\n");
    const encontrados = lines.filter((l) => l.toLowerCase().includes(filter));
    logsEl.textContent = encontrados.length
      ? encontrados.join("\n")
      : `Ningún renglón contiene "${filter}".`;
  }
  if (atBottom) logsEl.scrollTop = logsEl.scrollHeight;
}

async function pullHistory(id, entry, retries = 3) {
  try {
    const data = await api(`/api/projects/${id}/history`);
    renderHistoryTable(entry, data.history || []);
  } catch {
    if (retries > 0) {
      setTimeout(() => pullHistory(id, entry, retries - 1), 1000);
    }
  }
}

function renderHistoryTable(entry, runs) {
  const tbody = entry.root.querySelector(".history__tbody");
  if (!tbody) return;
  
  if (runs.length === 0) {
    tbody.innerHTML = '<tr><td colspan="4">No hay historial de arranques todavía.</td></tr>';
    return;
  }
  
  tbody.replaceChildren(
    ...[...runs].reverse().map((r) => {
      const tr = document.createElement("tr");
      
      const tdFecha = document.createElement("td");
      let fechaTexto = "";
      if (r.timestamp) {
        try {
          const raw = r.timestamp.endsWith("Z") || r.timestamp.includes("+") ? r.timestamp : `${r.timestamp}Z`;
          const d = new Date(raw);
          if (!isNaN(d.getTime())) {
            const pad = (n) => String(n).padStart(2, "0");
            const yyyy = d.getFullYear();
            const mm = pad(d.getMonth() + 1);
            const dd = pad(d.getDate());
            const hh = pad(d.getHours());
            const min = pad(d.getMinutes());
            fechaTexto = `${yyyy}-${mm}-${dd} ${hh}:${min}`;
          } else {
            fechaTexto = r.timestamp.replace("T", " ").substring(0, 16);
          }
        } catch (_) {
          fechaTexto = r.timestamp.replace("T", " ").substring(0, 16);
        }
      }
      tdFecha.textContent = fechaTexto;
      tr.appendChild(tdFecha);
      
      const tdPerfil = document.createElement("td");
      tdPerfil.textContent = r.profile || "-";
      tr.appendChild(tdPerfil);
      
      const tdDur = document.createElement("td");
      tdDur.textContent = r.duration_s != null ? `${r.duration_s}s` : "-";
      tr.appendChild(tdDur);
      
      const tdRes = document.createElement("td");
      let resText = r.result || "unknown";
      if (resText === "error" && r.error) {
        resText += `\n${r.error}`;
      }
      tdRes.textContent = resText;
      if (r.result === "running") {
        tdRes.style.color = "var(--color-good)";
      } else if (r.result === "error") {
        tdRes.style.color = "var(--color-bad)";
      }
      tr.appendChild(tdRes);
      
      return tr;
    })
  );
}

async function pullEnvAudit(id, entry) {
  const envBox = entry.root.querySelector(".env__box");
  if (!envBox) return;

  const badge = envBox.querySelector(".env__status-badge");
  const summary = envBox.querySelector(".env__summary");
  const missingSec = envBox.querySelector(".env__section--missing");
  const missingList = envBox.querySelector(".env__list--missing");
  const placeSec = envBox.querySelector(".env__section--placeholders");
  const placeList = envBox.querySelector(".env__list--placeholders");
  const emptySec = envBox.querySelector(".env__section--empty");
  const emptyList = envBox.querySelector(".env__list--empty");

  try {
    const data = await api(`/api/projects/${id}/env-audit`);
    if (data.ok) {
      badge.textContent = "OK";
      badge.className = "env__status-badge env__status-badge--ok";
      if (data.has_example) {
        summary.textContent = `.env sincronizado con ${data.example_file} (sin secretos por defecto).`;
      } else if (data.has_env) {
        summary.textContent = ".env local detectado sin placeholders inseguros.";
      } else {
        summary.textContent = "Sin archivos de entorno (.env ni .env.example).";
      }
    } else {
      badge.textContent = "ADVERTENCIA";
      badge.className = "env__status-badge env__status-badge--warn";
      const motivos = [];
      if (!data.has_env && data.has_example) {
        motivos.push(`falta .env local (existe ${data.example_file})`);
      }
      if (data.missing_keys && data.missing_keys.length > 0) {
        motivos.push(`${data.missing_keys.length} clave(s) faltante(s)`);
      }
      if (data.placeholder_keys && data.placeholder_keys.length > 0) {
        motivos.push(`${data.placeholder_keys.length} clave(s) con valores placeholder`);
      }
      summary.textContent = motivos.join(" · ") || "Revisa la configuración de entorno.";
    }

    function renderKeys(sec, list, items, pillClass) {
      if (!sec || !list) return;
      if (items && items.length > 0) {
        sec.hidden = false;
        list.replaceChildren(
          ...items.map((k) => {
            const li = document.createElement("li");
            const span = document.createElement("span");
            span.className = `env__key-pill ${pillClass}`.trim();
            span.textContent = k;
            li.appendChild(span);
            return li;
          })
        );
      } else {
        sec.hidden = true;
        list.replaceChildren();
      }
    }

    renderKeys(missingSec, missingList, data.missing_keys, "env__key-pill--missing");
    renderKeys(placeSec, placeList, data.placeholder_keys, "env__key-pill--placeholder");
    renderKeys(emptySec, emptyList, data.empty_keys, "");
  } catch (err) {
    if (summary) summary.textContent = `Error al verificar entorno: ${err.message}`;
  }
}

function render(projects, data) {
  // Sin resultados con un filtro puesto no es lo mismo que no tener proyectos:
  // el cartel de "registrá el primero" ahi seria mentira.
  ui.empty.hidden = projects.length > 0 || Boolean(query) || Boolean(statusFilter);

  // El buscador y los chips se muestran siempre que haya al menos un proyecto registrado.
  ui.find.hidden = data.registered === 0;
  ui.count.textContent = query || statusFilter
    ? `${data.total} ${data.total === 1 ? "coincidencia" : "coincidencias"}`
    : "";

  ui.pager.hidden = data.pages <= 1;
  ui.pagerAt.textContent = `${data.page} de ${data.pages}`;
  ui.pager.querySelector('[data-page="prev"]').disabled = data.page <= 1;
  ui.pager.querySelector('[data-page="next"]').disabled = data.page >= data.pages;
  page = data.page;

  const seen = new Set();
  for (const project of projects) {
    seen.add(project.id);
    let entry = cards.get(project.id);
    if (!entry) entry = buildCard(project);
    updateCard(entry, project);
  }

  for (const [id, entry] of cards) {
    if (!seen.has(id)) {
      entry.root.remove();
      cards.delete(id);
    }
  }

  ui.projects.replaceChildren(
    ...projects.map((project) => cards.get(project.id).root),
  );
  ui.projects.setAttribute("aria-busy", "false");
  hayProyectos = data.registered > 0;
  updateDocker(data.docker || { needed: false, down: false });
  updateFavicon(projects, data);
}

/* Estado y accion siempre que alguno de los proyectos de la pagina use Docker,
 * aunque este todo bien: un control que solo aparece cuando algo falla no
 * distingue "esta todo en orden" de "esto no funciona". Con el motor arriba el
 * boton no se esconde, cambia de trabajo: reiniciar Docker es lo que uno quiere
 * cuando los contenedores empiezan a portarse raro. */
function updateDocker(docker) {
  // Del estado global y no de los proyectos de la pagina: colgado de la pagina,
  // apretar "Siguiente" apagaba la fila entera cuando ahi no habia ninguno con
  // contenedores, y una fila que desaparece no distingue "esta en orden" de
  // "esto dejo de funcionar".
  const usan = docker.needed;
  const caido = docker.down;

  ui.dockerState.hidden = !usan;
  ui.dockerState.textContent = caido ? "Docker cerrado" : "Docker corriendo";
  ui.dockerState.dataset.tone = caido ? "bad" : "ready";

  ui.btnDocker.hidden = !usan;
  ui.btnDocker.dataset.action = caido ? "start" : "restart";
  ui.btnDockerClean.hidden = !usan || caido;
  // El sondeo pasa cada 2.5s y el armado dura 6: sin esto le pisaria la
  // pregunta al usuario mientras la esta leyendo.
  if (ui.btnDocker.dataset.armed !== "true") {
    ui.btnDocker.textContent = caido ? "Abrir Docker" : "Reiniciar Docker";
  }
}

function updateFavicon(projects, data) {
  const link = document.getElementById("favicon");
  if (!link) return;
  const fallenCount = data && data.fallen ? data.fallen.length : 0;
  const hasError = projects.some((p) => p.state === "error" || p.state === "invalid");
  const hasRunning = projects.some((p) => p.state === "running" || p.state === "starting");

  let color = "%2364748b";
  if (fallenCount > 0 || hasError) {
    color = "%23ef4444";
  } else if (hasRunning) {
    color = "%2322c55e";
  }

  link.href = `data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ccircle cx='50' cy='50' r='40' fill='${color}'/%3E%3C/svg%3E`;
}

/* ciclo ------------------------------------------------------------------- */

/* Los puertos abiertos a internet, sacados de /api/state para no sumar un
 * sondeo mas. Se pinta con lo que ya llego, sin pedir nada. */
let tunnelPorts = new Set();

function renderTunnels(list) {
  tunnelPorts = new Set(list.map((t) => t.port));
  ui.tunnels.hidden = list.length === 0;
  if (list.length === 0) {
    ui.tunnelsList.replaceChildren();
    // Y la firma: sin borrarla, cerrar el ultimo tunel y volver a abrir el
    // mismo puerto daba la misma cadena, el return temprano se saltaba el
    // repintado y la lista quedaba vacia con un tunel abierto.
    delete ui.tunnelsList.dataset.firma;
    return;
  }

  ui.tunnelsHeading.textContent = `Túneles abiertos (${list.length})`;

  // El proveedor entra en la firma: es un dato que se muestra, y si cambia sin
  // cambiar puerto ni URL la fila seguiria diciendo el anterior.
  const firma = list.map((t) => `${t.port}:${t.provider}:${t.url}`).join(",");
  if (ui.tunnelsList.dataset.firma === firma) return;
  ui.tunnelsList.dataset.firma = firma;

  ui.tunnelsList.replaceChildren(
    ...list.map((tun) => {
      const li = document.createElement("li");
      li.className = "orphan";

      const portTag = document.createElement("span");
      portTag.className = "orphan__port";
      portTag.textContent = `:${tun.port}`;

      const info = document.createElement("div");
      info.className = "orphan__info";

      const enlace = document.createElement("a");
      enlace.className = "orphan__name";
      enlace.href = tun.url;
      enlace.target = "_blank";
      enlace.rel = "noopener noreferrer";
      enlace.textContent = tun.url;

      const meta = document.createElement("div");
      meta.className = "orphan__meta";
      meta.textContent = `via ${tun.provider}`;

      info.append(enlace, meta);

      const cerrar = document.createElement("button");
      cerrar.className = "orphan__kill";
      cerrar.type = "button";
      cerrar.textContent = "Cerrar";
      cerrar.addEventListener("click", () => {
        act(cerrar, async () => {
          await api(`/api/share/${tun.port}`, { method: "DELETE" });
          // Sin `refresh()` propio: `act` ya lo llama al terminar el trabajo.
          // Borrar la firma antes alcanza para forzar el repintado, y el que
          // habia aca duplicaba /api/state y refreshHealth en cada cierre.
          delete ui.tunnelsList.dataset.firma;
        });
      });

      li.append(portTag, info, cerrar);
      return li;
    }),
  );
}

async function refreshOrphans() {
  try {
    const data = await api("/api/ports/orphans");
    const list = data.orphans || [];

    // Visible aunque no haya ninguno, mientras haya algun proyecto registrado:
    // una seccion que desaparece no distingue "no hay intrusos" de "esto dejo
    // de funcionar". Con cero proyectos si se esconde, porque ahi la pagina
    // entera es el cartel de registrar el primero.
    ui.orphans.hidden = !hayProyectos;

    // Rojo solo cuando hay algo. La seccion se ve igual estando vacia, para
    // informar que el chequeo corrio, pero vestida de alarma decia lo contrario
    // de lo que su propio texto dice.
    ui.orphans.dataset.tone = list.length ? "bad" : "";

    // Con uno solo no aporta nada: la fila ya trae su propio boton Cerrar.
    const varios = list.length >= 2;
    ui.orphansKillAll.hidden = !varios;
    latestOrphansList = list;

    // Lo tildado que ya no esta en la lista deja de contar: si no, el boton
    // diria "Cerrar 3" con dos filas en pantalla.
    const vigentes = new Set(list.map((o) => o.port));
    for (const port of [...orphanPicks]) {
      if (!vigentes.has(port)) orphanPicks.delete(port);
    }
    refreshKillAllLabel();

    const nextIds = list.map((o) => o.port).join(",") || "__empty__";
    if (ui.orphansList.dataset.ids === nextIds) return;
    ui.orphansList.dataset.ids = nextIds;

    if (list.length === 0) {
      ui.orphansHeading.textContent = "Procesos intrusos";
      const limpio = document.createElement("li");
      limpio.className = "orphan orphan--empty";
      // "estan libres" era mentira con un contenedor publicando el puerto: la
      // tarjeta lo mostraba ocupado y este cartel decia lo contrario.
      limpio.textContent =
        "Ninguno. Lo que ocupa tus puertos lo arrancaste vos o es un contenedor de Docker.";
      ui.orphansList.replaceChildren(limpio);
      return;
    }

    ui.orphansHeading.textContent = `Procesos intrusos (${list.length})`;

    ui.orphansList.replaceChildren(
      ...list.map((orphan) => {
        const li = document.createElement("li");
        li.className = "orphan";

        // La casilla solo con dos o mas: con una sola fila, elegirla y despues
        // apretar un boton es un paso de mas para lo que ya hace su Cerrar.
        let pick = null;
        if (varios) {
          pick = document.createElement("input");
          pick.type = "checkbox";
          pick.className = "orphan__pick";
          pick.checked = orphanPicks.has(orphan.port);
          pick.setAttribute(
            "aria-label",
            `Elegir el puerto ${orphan.port}, ocupado por ${orphan.name}`,
          );
          pick.addEventListener("change", () => {
            if (pick.checked) orphanPicks.add(orphan.port);
            else orphanPicks.delete(orphan.port);
            refreshKillAllLabel();
          });
        }

        const portTag = document.createElement("span");
        portTag.className = "orphan__port";
        portTag.textContent = `:${orphan.port}`;

        const info = document.createElement("div");
        info.className = "orphan__info";

        // Tres renglones y no dos campos pegados con un punto. `node.exe ·
        // Decepticon` se lee como "este proceso es de Decepticon", y es al
        // reves: el proceso es un desconocido y Decepticon es quien reclama el
        // puerto. Son dos hechos distintos y ahora ocupan lugares distintos.
        const name = document.createElement("div");
        name.className = "orphan__name";
        name.textContent = `${orphan.name} · pid ${orphan.pid}`;

        const claim = document.createElement("div");
        claim.className = "orphan__claim";
        const reclaman = orphan.projects || [];
        claim.textContent =
          reclaman.length > 1
            ? `ocupa un puerto que declaran ${reclaman.join(" y ")}`
            : `ocupa un puerto que declara ${reclaman[0] || "un proyecto registrado"}`;

        const meta = document.createElement("div");
        meta.className = "orphan__meta";
        // La linea de comando es lo que deja decidir si cerrarlo: sale entera
        // en el title, porque en la fila entra recortada.
        meta.textContent = orphan.cmd || "sin linea de comando visible";
        if (orphan.cmd) meta.title = orphan.cmd;

        info.append(name, claim, meta);

        const kill = document.createElement("button");
        kill.className = "orphan__kill";
        kill.textContent = "Cerrar";
        kill.type = "button";
        kill.addEventListener("click", () => {
          act(kill, async () => {
            await api(`/api/ports/${orphan.port}/kill`, { method: "POST" });
            delete ui.orphansList.dataset.ids;
            await refreshOrphans();
          });
        });

        if (pick) li.append(pick);
        li.append(portTag, info, kill);
        return li;
      }),
    );
  } catch {
    // Fallo silencioso: la seccion de intrusos no es critica.
  }
}


/* salud ------------------------------------------------------------------- */

/* Un servicio que se muere cambia un punto de color y nada mas. Si la pestaña
 * esta de fondo, que es donde vive esta herramienta, no te enteras hasta que el
 * navegador te tira un ERR_CONNECTION_REFUSED diez minutos despues.
 *
 * `/api/health` mira todas las sesiones y no la pagina actual: con mas de
 * cuatro proyectos, alimentar esto de `/api/state` seria una mentira
 * silenciosa. */

// Servicios caidos en el sondeo anterior, para avisar solo de los nuevos: sin
// esto, uno caido notifica 24 veces por minuto.
let fallen = new Set();
// El primer sondeo no notifica. Al cargar la pagina con algo ya caido, la
// noticia es vieja y el usuario no la pidio.
let healthKnown = false;

async function refreshHealth() {
  let data;
  try {
    data = await api("/api/health");
  } catch {
    return; // sin conexion ya lo dice el masthead
  }

  // `service: null` es el stack entero, no un servicio suelto.
  const clave = (f) => `${f.project}/${f.service ?? ""}`;
  const ahora = new Set(data.fallen.map(clave));
  const nuevos = data.fallen.filter((f) => !fallen.has(clave(f)));
  fallen = ahora;

  document.title = ahora.size ? `(${ahora.size}) ${TITLE}` : TITLE;
  ui.health.hidden = !ahora.size;
  ui.health.textContent = ahora.size === 1 ? "1 caído" : `${ahora.size} caídos`;

  const puedePedirse = "Notification" in window && Notification.permission === "default";
  ui.notify.hidden = !ahora.size || !puedePedirse;
  if (!ui.notify.hidden) {
    ui.notify.textContent = "Avisarme al caer";
    ui.notify.title = "Activar notificaciones de escritorio para servicios caídos";
  }

  if (healthKnown && nuevos.length && window.Notification?.permission === "granted") {
    for (const caido of nuevos) {
      const que = caido.service ? `${caido.stack}: ${caido.service}` : caido.stack;
      new Notification(`${que} se cayó`, {
        body: "StackHelx no lo apagó, se murió solo.",
        tag: clave(caido), // el navegador tambien deduplica
      });
    }
  }
  healthKnown = true;
}

ui.notify.addEventListener("click", async () => {
  // El permiso se pide con un click y nunca al cargar: un pedido de
  // notificaciones que aparece solo es lo que hace que la gente lo deniegue
  // para siempre.
  if (!("Notification" in window)) {
    flash("Tu navegador no soporta notificaciones de escritorio");
    return;
  }
  const perm = await Notification.requestPermission();
  if (perm === "granted") {
    flash("Notificaciones de escritorio activadas");
    ui.notify.hidden = true;
  } else if (perm === "denied") {
    flash("Permiso de notificaciones denegado en el navegador");
    ui.notify.hidden = true;
  }
});

const ORPHAN_EVERY = 4; // cada N ciclos de POLL_MS
let orphanTick = 0;
let refreshAbortController = null;

async function refresh() {
  if (refreshAbortController) {
    refreshAbortController.abort();
  }
  refreshAbortController = new AbortController();
  const signal = refreshAbortController.signal;
  try {
    const params = new URLSearchParams({ page: String(page) });
    if (query) params.set("q", query);
    if (statusFilter) params.set("status", statusFilter);
    const data = await api(`/api/state?${params}`, { signal });
    renderTunnels(data.tunnels || []);
    render(data.projects, data);
    const n = data.registered;
    ui.connection.textContent = `${n} ${n === 1 ? "proyecto" : "proyectos"}`;
    ui.connection.dataset.down = "false";
  } catch (error) {
    if (error.name === "AbortError") return;
    ui.connection.textContent = `sin conexión · ${error.message}`;
    ui.connection.dataset.down = "true";
  }
  await refreshHealth();
  orphanTick++;
  if (orphanTick % ORPHAN_EVERY === 1) await refreshOrphans();
}

/* explorador de carpetas -------------------------------------------------- */

/* La ruta absoluta la pone el servidor: el navegador no la conoce y no la puede
 * conocer. Cada click pide el listado de una carpeta y nada mas. */

let here = { path: "", parent: null, markers: [] };
let historyStack = [];

async function browseTo(path, isHistoryAction = false) {
  const from = here.path;

  let data;
  try {
    data = await api(`/api/browse?path=${encodeURIComponent(path)}`);
  } catch (error) {
    // Una ruta mala escrita a mano no puede dejar el dialogo en blanco: se cae
    // a las raices y recien despues se muestra el aviso, que si no lo tapa. El
    // salto a las raices es del historial: si no, Volver traeria de vuelta la
    // ruta que acaba de fallar.
    if (path) await browseTo("", true);
    ui.pickerNote.textContent = error.message;
    ui.pickerNote.hidden = false;
    return;
  }

  // El historial se anota recien cuando la navegacion salio bien, y contra la
  // ruta que devolvio el servidor: es la normalizada, la que Volver puede
  // pedir de nuevo.
  if (!isHistoryAction && from !== data.path) {
    historyStack.push(from);
  }

  here = data;
  ui.pickerPath.textContent = data.path || "Elegí dónde empezar";
  ui.pickerNote.hidden = !data.truncated;
  if (data.truncated) {
    ui.pickerNote.textContent = `Se muestran las primeras ${data.entries.length} carpetas.`;
  }

  const backBtn = ui.picker.querySelector('[data-picker="back"]');
  if (backBtn) backBtn.disabled = historyStack.length === 0;

  ui.picker.querySelector('[data-picker="up"]').disabled = data.parent === null;
  ui.picker.querySelector('[data-picker="pick"]').disabled = !data.path;

  ui.pickerList.replaceChildren(...data.entries.map(entryRow));
  if (!data.entries.length) {
    const empty = document.createElement("li");
    empty.className = "picker__empty";
    empty.textContent = "Sin subcarpetas visibles.";
    ui.pickerList.append(empty);
  }
  ui.pickerList.scrollTop = 0;
}

function entryRow(entry) {
  const item = document.createElement("li");
  const row = document.createElement("button");
  row.type = "button";
  row.className = "picker__row";
  row.dataset.path = entry.path;

  const name = document.createElement("span");
  name.className = "picker__name";
  name.textContent = entry.name;
  row.append(name);

  if (entry.markers.length) {
    const tag = document.createElement("span");
    tag.className = "picker__markers";
    tag.textContent = entry.markers.join(" · ");
    row.append(tag);
  }

  row.addEventListener("click", () => browseTo(entry.path));
  item.append(row);
  return item;
}

/* buscador y paginado ----------------------------------------------------- */

let searchTimer = null;

ui.search.addEventListener("input", () => {
  // Sin esperar, cada tecla dispara un escaneo de puertos en el servidor.
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    query = ui.search.value.trim();
    page = 1;
    refresh();
  }, 200);
});

ui.pager.addEventListener("click", (event) => {
  const move = event.target.dataset.page;
  if (!move) return;
  page = move === "next" ? page + 1 : Math.max(1, page - 1);
  refresh();
});

const filterChips = document.getElementById("filter-chips");
if (filterChips) {
  filterChips.addEventListener("click", (event) => {
    const btn = event.target.closest("button[data-status]");
    if (!btn) return;
    statusFilter = btn.dataset.status;
    for (const chip of filterChips.querySelectorAll(".chip")) {
      chip.classList.toggle("chip--active", chip === btn);
    }
    page = 1;
    refresh();
  });
}

document.addEventListener("keydown", (event) => {
  const tag = document.activeElement ? document.activeElement.tagName : "";
  const isInput = ["INPUT", "TEXTAREA", "SELECT"].includes(tag);
  if (event.key === "/" && !isInput) {
    event.preventDefault();
    ui.search.focus();
    ui.search.select();
  } else if (event.key === "Escape" && document.activeElement === ui.search) {
    ui.search.blur();
  } else if (!isInput && (event.key === "ArrowLeft" || event.key === "ArrowRight")) {
    if (ui.pager.hidden) return;
    if (event.key === "ArrowLeft" && page > 1) {
      page--;
      refresh();
    } else if (event.key === "ArrowRight") {
      page++;
      refresh();
    }
  }
});

ui.browse.addEventListener("click", () => {
  historyStack = [];
  const backBtn = ui.picker.querySelector('[data-picker="back"]');
  if (backBtn) backBtn.disabled = true;
  ui.picker.showModal();
  loadFrequentRoots();
  browseTo(ui.path.value.trim() || here.path, true);
});

async function loadFrequentRoots() {
  if (!ui.pickerFrequent || !ui.pickerFrequentChips) return;
  try {
    const data = await api("/api/browse/frecuentes");
    if (data && data.roots && data.roots.length > 0) {
      ui.pickerFrequentChips.replaceChildren(
        ...data.roots.map((rootPath) => {
          const chip = document.createElement("button");
          chip.type = "button";
          chip.className = "picker__chip";
          chip.textContent = rootPath;
          chip.title = `Ir a ${rootPath}`;
          chip.addEventListener("click", () => browseTo(rootPath));
          return chip;
        })
      );
      ui.pickerFrequent.hidden = false;
    } else {
      ui.pickerFrequent.hidden = true;
    }
  } catch {
    ui.pickerFrequent.hidden = true;
  }
}

ui.picker.querySelector('[data-picker="close"]').addEventListener("click", () => {
  ui.picker.close();
});

const osFolderBtn = ui.picker.querySelector('[data-picker="os-folder"]');
if (osFolderBtn) {
  osFolderBtn.addEventListener("click", (event) => {
    const targetPath = here.path || ui.path.value.trim();
    if (!targetPath) {
      flash("No hay una carpeta seleccionada para abrir", "neutral");
      return;
    }
    act(event.currentTarget, async () => {
      try {
        await api("/api/open-folder", {
          method: "POST",
          body: JSON.stringify({ path: targetPath }),
        });
        flash(`Explorador abierto en ${targetPath}`, "neutral");
      } catch (err) {
        flash(`Error al abrir explorador: ${err.message}`, "bad");
      }
    });
  });
}

const backBtn = ui.picker.querySelector('[data-picker="back"]');
if (backBtn) {
  backBtn.addEventListener("click", () => {
    if (historyStack.length > 0) {
      const prevPath = historyStack.pop();
      browseTo(prevPath, true);
    }
  });
}

ui.picker.querySelector('[data-picker="up"]').addEventListener("click", () => {
  if (here.parent !== null) browseTo(here.parent);
});

ui.picker.querySelector('[data-picker="pick"]').addEventListener("click", (event) => {
  const chosen = here.path;
  if (!chosen) return;
  act(event.currentTarget, async () => {
    await api("/api/projects", { method: "POST", body: JSON.stringify({ path: chosen }) });
    ui.picker.close();
    ui.path.value = "";
  });
});

/* drag and drop en zona de registro ---------------------------------------- */

if (ui.enroll) {
  ui.enroll.addEventListener("dragover", (e) => {
    e.preventDefault();
    ui.enroll.classList.add("enroll--dragover");
  });

  ui.enroll.addEventListener("dragleave", (e) => {
    if (!ui.enroll.contains(e.relatedTarget)) {
      ui.enroll.classList.remove("enroll--dragover");
    }
  });

  ui.enroll.addEventListener("drop", async (e) => {
    e.preventDefault();
    ui.enroll.classList.remove("enroll--dragover");

    const items = e.dataTransfer.items;
    let droppedName = "";
    if (items && items.length > 0) {
      const item = items[0];
      const entry = item.webkitGetAsEntry ? item.webkitGetAsEntry() : null;
      if (entry) {
        droppedName = entry.name;
      }
    }
    if (!droppedName && e.dataTransfer.files.length > 0) {
      droppedName = e.dataTransfer.files[0].name;
    }

    if (droppedName) {
      // Comparar contra rutas frecuentes
      try {
        const freq = await api("/api/browse/frecuentes");
        if (freq && freq.roots) {
          for (const r of freq.roots) {
            const sep = r.includes("\\") ? "\\" : "/";
            const candidate = `${r.replace(/[\\/]+$/, "")}${sep}${droppedName}`;
            try {
              const test = await api(`/api/browse?path=${encodeURIComponent(candidate)}`);
              if (test && test.path) {
                ui.path.value = test.path;
                flash(`Ruta asignada: ${test.path}`, "good");
                return;
              }
            } catch {
              // No era esta raiz, probar siguiente
            }
          }
        }
      } catch {
        // Continuar a fallback
      }

      // Si no se resolvio con raices frecuentes, abrir picker y sugerir nombre
      ui.picker.showModal();
      loadFrequentRoots();
      browseTo("", true);
      flash(`Arrastraste "${droppedName}". Selecciona su carpeta padre.`, "neutral");
    }
  });
}


/* autocompletado no intrusivo en el registro ----------------------------- */

let pathDebounceTimer = null;
if (ui.path && ui.pathSuggestions) {
  ui.path.addEventListener("input", () => {
    clearTimeout(pathDebounceTimer);
    const val = ui.path.value.trim();
    if (val.length < 3) {
      ui.pathSuggestions.replaceChildren();
      return;
    }

    pathDebounceTimer = setTimeout(async () => {
      const sep = val.includes("\\") ? "\\" : "/";
      const lastSepIdx = val.lastIndexOf(sep);
      if (lastSepIdx === -1) return;

      const parentDir = val.slice(0, lastSepIdx) || sep;
      const prefix = val.slice(lastSepIdx + 1).toLowerCase();

      try {
        const data = await api(`/api/browse?path=${encodeURIComponent(parentDir)}`);
        if (!data || !data.entries) return;

        const matches = data.entries.filter((entry) =>
          entry.name.toLowerCase().startsWith(prefix)
        );

        ui.pathSuggestions.replaceChildren(
          ...matches.map((m) => {
            const opt = document.createElement("option");
            opt.value = m.path;
            return opt;
          })
        );
      } catch {
        // Silencioso: mientras se tipea una ruta parcial es normal que no exista aun
      }
    }, 150);
  });
}

/* importacion de proyectos en JSON --------------------------------------- */

const btnImport = document.getElementById("btn-import");
const fileImport = document.getElementById("file-import");

if (btnImport && fileImport) {
  btnImport.addEventListener("click", () => {
    fileImport.click();
  });
  fileImport.addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    try {
      const text = await file.text();
      const pathsList = JSON.parse(text);
      if (!Array.isArray(pathsList)) throw new Error("El archivo debe ser una lista JSON de rutas");
      const res = await api("/api/projects/import", {
        method: "POST",
        body: JSON.stringify(pathsList),
      });
      flash(`Importados ${res.count} proyectos.`, "good");
      refresh();
    } catch (err) {
      flash(`Fallo al importar: ${err.message}`, "bad");
    } finally {
      fileImport.value = "";
    }
  });
}

ui.enroll.addEventListener("submit", (event) => {
  event.preventDefault();
  const button = ui.enroll.querySelector("button");
  const path = ui.path.value.trim();
  if (!path) return;
  act(button, async () => {
    await api("/api/projects", {
      method: "POST",
      body: JSON.stringify({ path }),
    });
    ui.path.value = "";
    if (ui.pathSuggestions) ui.pathSuggestions.replaceChildren();
  });
});

/* mapa de puertos modal --------------------------------------------------- */

if (ui.btnPortsModal && ui.portsModal) {
  ui.btnPortsModal.addEventListener("click", () => {
    ui.portsModal.showModal();
    refreshPortsModal();
  });
  const closeBtn = ui.portsModal.querySelector('[data-ports-modal="close"]');
  if (closeBtn) {
    closeBtn.addEventListener("click", () => {
      ui.portsModal.close();
    });
  }
}

async function refreshPortsModal() {
  if (!ui.portsModalList) return;
  try {
    const [stateData, orphansData] = await Promise.all([
      api("/api/state?size=50"),
      api("/api/ports/orphans"),
    ]);

    const items = [];
    for (const project of stateData.projects || []) {
      for (const service of project.services || []) {
        if (service.port) {
          items.push({
            port: service.port,
            label: `${project.name} · ${service.name}`,
            kind: service.state === "ready" ? "corriendo" : "detenido",
            openable: service.openable,
            url: service.url,
          });
        }
      }
    }

    for (const orphan of orphansData.orphans || []) {
      items.push({
        port: orphan.port,
        // El mismo fallback que la lista de intrusos: sin esto, un intruso cuyo
        // puerto no reclama nadie quedaba en "ocupa el puerto de " y se cortaba.
        label: `${orphan.name} ocupa el puerto de ${
          (orphan.projects || []).join(" y ") || "un proyecto registrado"
        }`,
        kind: "intruso",
        isOrphan: true,
      });
    }

    items.sort((a, b) => a.port - b.port);

    if (items.length === 0) {
      const empty = document.createElement("li");
      empty.className = "orphan orphan--empty";
      empty.textContent = "Sin puertos asignados ni intrusos";
      ui.portsModalList.replaceChildren(empty);
      return;
    }

    ui.portsModalList.replaceChildren(
      ...items.map((item) => {
        const li = document.createElement("li");
        li.className = "orphan";

        const portTag = document.createElement("span");
        portTag.className = "orphan__port";
        portTag.textContent = `:${item.port}`;

        const info = document.createElement("div");
        info.className = "orphan__info";

        const name = document.createElement("div");
        name.className = "orphan__name";
        name.textContent = item.label;

        const meta = document.createElement("div");
        meta.className = "orphan__meta";
        meta.textContent = `Estado: ${item.kind}`;

        info.append(name, meta);

        if (item.openable) {
          const actLink = document.createElement("a");
          actLink.className = "btn btn--open";
          actLink.target = "_blank";
          actLink.rel = "noopener noreferrer";
          actLink.href = abrirUrl(item);
          actLink.textContent = "Abrir ↗";
          li.append(portTag, info, actLink);
        } else if (item.isOrphan) {
          const killBtn = document.createElement("button");
          killBtn.className = "orphan__kill";
          killBtn.textContent = "Cerrar";
          killBtn.type = "button";
          killBtn.addEventListener("click", () => {
            act(killBtn, async () => {
              await api(`/api/ports/${item.port}/kill`, { method: "POST" });
              await refreshPortsModal();
            });
          });
          li.append(portTag, info, killBtn);
        } else {
          li.append(portTag, info);
        }

        return li;
      }),
    );
  } catch {
    // Silencioso
  }
}

/* mcp modal ----------------------------------------------------------------- */

if (ui.btnMcpModal && ui.mcpModal) {
  ui.btnMcpModal.addEventListener("click", () => {
    ui.mcpModal.showModal();
    refreshMcpModal();
  });
  const closeBtn = ui.mcpModal.querySelector('[data-mcp-modal="close"]');
  if (closeBtn) {
    closeBtn.addEventListener("click", () => {
      ui.mcpModal.close();
    });
  }
}

async function refreshMcpModal() {
  if (!ui.mcpModal) return;
  try {
    const data = await api("/api/mcp/activity");
    if (ui.mcpTotalCalls) ui.mcpTotalCalls.textContent = String(data.total_calls || 0);
    if (ui.mcpQuotaUsed) {
      ui.mcpQuotaUsed.textContent = `${data.active_rate_per_min || 0}/${data.rate_limit_max || 30}`;
    }

    if (ui.mcpBreakdown) {
      ui.mcpBreakdown.replaceChildren();
      const byTool = data.by_tool || {};
      const entries = Object.entries(byTool).sort((a, b) => b[1] - a[1]);
      for (const [tool, count] of entries) {
        const pill = document.createElement("span");
        pill.className = "mcp__pill";
        pill.textContent = `${tool}: ${count}`;
        ui.mcpBreakdown.append(pill);
      }
    }

    if (ui.mcpTbody) {
      ui.mcpTbody.replaceChildren();
      const events = data.recent_events || [];
      if (events.length === 0) {
        const tr = document.createElement("tr");
        const td = document.createElement("td");
        td.colSpan = 4;
        td.style.textAlign = "center";
        td.style.color = "var(--color-ink-3)";
        td.textContent = "Sin llamadas registradas aún";
        tr.append(td);
        ui.mcpTbody.append(tr);
        return;
      }
      for (const ev of events) {
        const tr = document.createElement("tr");

        const tdTime = document.createElement("td");
        const ts = (ev.timestamp || "").replace("T", " ").substring(11, 19);
        tdTime.textContent = ts;

        const tdTool = document.createElement("td");
        tdTool.className = "mcp__tool-name";
        tdTool.textContent = ev.tool || "-";

        const tdDur = document.createElement("td");
        tdDur.textContent = `${ev.duration_ms || 0} ms`;

        const tdStatus = document.createElement("td");
        const badge = document.createElement("span");
        badge.className = `mcp__status-badge mcp__status-badge--${ev.status || "ok"}`;
        badge.textContent = ev.status || "ok";
        tdStatus.append(badge);

        tr.append(tdTime, tdTool, tdDur, tdStatus);
        ui.mcpTbody.append(tr);
      }
    }
  } catch (err) {
    console.error("Error al cargar telemetría MCP:", err);
  }
}

/* arranque ---------------------------------------------------------------- */

// Que build sirve el servidor, en el pie. Una pagina vieja servida de la cache
// del navegador deja este hueco vacio, que es la unica senal a simple vista de
// que lo que estas mirando no es lo que corre.
async function showBuild() {
  const slot = document.getElementById("build");
  if (!slot) return;
  try {
    const data = await api("/api/version");
    slot.textContent = `v${data.version} · ${data.assets}`;
  } catch {
    /* sin token todavia: lo intenta el proximo arranque */
  }
}
showBuild();

// Sin esto la pagina carga en blanco y solo se puebla cuando tocas algo, porque
// todas las demas llamadas a `refresh` viven adentro de un handler. Se perdio en
// a252013 al reescribir el final del archivo.
refresh();

// Sondear solo con la pestana a la vista. `setInterval(refresh, POLL_MS)` a
// secas corria igual con la pestana oculta, minimizada o detras de otra
// ventana, y ahi esta el grueso del trabajo al pedo: una pestana abierta ocho
// horas hacia 11.520 sondeos, casi todos sin nadie mirando. Cada uno le pide al
// servidor que resuelva cada proyecto registrado, o sea disco.
//
// `visibilityState` es API nativa del navegador y no hace falta nada mas: no
// hay boton que apretar ni preferencia que guardar, y el que deja la pestana
// abierta de fondo no tiene que acordarse de nada.
//
// Al volver se refresca en el acto, antes de esperar el intervalo: si no, la
// pagina mostraba el estado de hace horas durante los primeros 2.5 segundos, y
// eso en una herramienta que dice que esta corriendo ahora es peor que nada.
let pollTimer = setInterval(() => {
  if (document.visibilityState === "visible") refresh();
}, POLL_MS);

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    clearInterval(pollTimer);
    refresh();
    pollTimer = setInterval(() => {
      if (document.visibilityState === "visible") refresh();
    }, POLL_MS);
  }
});
