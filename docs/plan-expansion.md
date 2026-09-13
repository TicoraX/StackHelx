# Plan de Expansión Estratégica: StackHelx (Ecosistema Completo de Desarrollo)

Fecha: 14 de agosto de 2026.  
Rama: `feature/expansion-plan`

Este documento define la evolución de StackHelx desde un orquestador de *stacks* locales hacia una **plataforma integral de productividad, diagnóstico, automatización y control del entorno de desarrollo local**.

---

## 1. Arquitectura y Nuevas Dimensiones

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            STACKHELX ECOSYSTEM                              │
├───────────────────────────────┬───────────────────────────────┬─────────────┤
│ 1. ORQUESTACIÓN DE STACK      │ 2. RUNNER DE TAREAS           │ 3. TÚNELES  │
│ - Topological sort & health   │ - Scripts y pipelines locales │ - ngrok     │
│ - env_file & pre_start hooks  │ - Inyección de variables      │ - cloudflare│
│ - Monorrepos (pnpm/turbo/uv)  │ - stackhelx run <tarea>       │ - share     │
├───────────────────────────────┼───────────────────────────────┼─────────────┤
│ 4. MULTI-PROYECTO COMPUESTO   │ 5. HIGIENE Y MANTENIMIENTO    │ 6. MCP & AI │
│ - Inclusión inter-repositorios│ - Docker prune inteligente    │ - Tools MCP │
│ - Grupos de servicios         │ - Detección de zombies        │ - AI Agent  │
│ - Matriz de colisiones        │ - stackhelx clean             │   Diagnosis │
└───────────────────────────────┴───────────────────────────────┴─────────────┘
```

---

## 2. Especificación de Fases y Módulos

### Fase 1: Variables de Entorno y Hooks del Stack
*   **Módulo**: [`stackhelx/config.py`](../stackhelx/config.py), [`stackhelx/runner.py`](../stackhelx/runner.py)
*   **`env_file`**: Carga de `.env`, `.env.local` o lista ordenada sin dependencias externas.
*   **Hooks de Ciclo de Vida**:
    *   `pre_start`: Ejecución síncrona preparatoria (ej. migraciones, build).
    *   `post_start`: Ejecución tras confirmación de salud (ej. `seed`, notificación).
*   **Bóveda Global (`~/.stackhelx/env.global`)**: Herencia automática de variables comunes entre proyectos.

### Fase 2: Runner de Tareas y Scripts de Proyecto (`stackhelx run`)
*   **Módulo**: [`stackhelx/scripts.py`](../stackhelx/scripts.py), [`stackhelx/cli.py`](../stackhelx/cli.py)
*   **Declaración en `stack.yaml`**:
    ```yaml
    scripts:
      test: pytest tests/ -v
      lint: ruff check .
      migrate: alembic upgrade head
      check: [lint, test]  # Pipeline secuencial
    ```
*   **Comando CLI**: `stackhelx run <script>` ejecuta en la raíz del proyecto, con el contexto de variables del stack.

### Fase 3: Exposición Segura y Compartir (`stackhelx share`)
*   **Módulo**: [`stackhelx/tunnel.py`](../stackhelx/tunnel.py), [`stackhelx/cli.py`](../stackhelx/cli.py)
*   **Integración de Túneles**: Detección y manejo de binarios locales (`cloudflared`, `ngrok`, `tailscale`).
*   **Comando CLI & Web**: `stackhelx share web` genera URL pública temporal, QR en consola y botón directo en la UI web.

### Fase 4: Orquestación Multi-Proyecto y Dependencias Inter-Repositorios
*   **Módulo**: [`stackhelx/registry.py`](../stackhelx/registry.py), [`stackhelx/runner.py`](../stackhelx/runner.py)
*   **Proyectos Compuestos (`includes`)**:
    ```yaml
    # frontend/stack.yaml
    name: frontend
    includes:
      - ../backend-api  # Levanta backend automáticamente si no está corriendo
    ```
*   **Grupos de Proyectos**: `stackhelx group up <nombre_grupo>` y matriz preventiva de colisión de puertos.

### Fase 5: Higiene del Sistema y Docker (`stackhelx clean`)
*   **Módulo**: [`stackhelx/docker.py`](../stackhelx/docker.py), [`stackhelx/cli.py`](../stackhelx/cli.py)
*   **Limpieza Asistida**:
    *   `stackhelx clean` (con `--volumes` opcional): Remueve contenedores parados, imágenes huérfanas, build-cache y redes no usadas.
    *   `stackhelx doctor`: Diagnóstico integral del entorno local y detección de problemas.

### Fase 6: Servidor MCP e Integración con Agentes de IA
*   **Módulo**: [`stackhelx/mcp.py`](../stackhelx/mcp.py)
*   **Herramientas MCP Expuestas**:
    *   `stackhelx_status`: Estado de salud, puertos y procesos.
    *   `stackhelx_restart`: Reinicio atómico de un servicio específico.
    *   `stackhelx_logs`: Consulta de logs con filtrado para depuración autónoma.
    *   `stackhelx_free_port`: Liberación de puertos conflictivos.
    *   `stackhelx_clean`: Limpieza segura de recursos Docker.
    *   `stackhelx_doctor`: Diagnóstico integral del sistema.
    *   `stackhelx_history`: Historial de arranques de proyectos.
    *   `stackhelx_init`: Inicialización asistida de `stack.yaml`.

### Fase 7: Detección Avanzada (Monorrepos, `uv`, Frameworks) y UI Web Polish
*   **Módulo**: [`stackhelx/detect.py`](../stackhelx/detect.py), [`stackhelx/web/`](../stackhelx/web)
*   Detección de `pnpm-workspace.yaml`, `turbo.json`, `uv.lock`, Astro (`4321`), Vite (`5173`).
*   UI Web: Pausa y búsqueda en streaming de logs, editor de `stack.yaml` y disparador de tareas `run`.

---

## 3. Principios de Implementación

1.  **Zero Bloat (Lazy Senior Dev / Ponytail)**: No añadir dependencias pesadas innecesarias. El soporte de túneles y MCP utiliza subprocesos y protocolos JSON-RPC estándar sobre stdio.
2.  **Seguridad y Aislamiento**: Toda API expuesta mantiene validación de cabecera `Host` y autenticación de token estricta.
3.  **Compatibilidad Multiplataforma**: Verificación en Windows (pwsh/cmd), Linux (bash) y macOS (zsh).
