# quality-gate

Revisión autónoma de Pull Requests para repositorios Expo/React Native.
Reemplaza la revisión manual: en cada PR el sistema perfila el diff, detecta
*vibecoding* (ruido de whitespace, bloques comentados, TODOs, restos de
debug, secretos, dependencias nuevas, cambios fuera de scope), corre
validación estructural (`tsc --noEmit` / `node --check`) y publica un
comentario contractual con el veredicto en GitHub.

## Arquitectura

Dos paquetes versionados de forma independiente en un workspace de `uv`:

```text
packages/mcp-server     servidor FastMCP stateless — herramientas
                        determinísticas (git/GitHub API, ripgrep, procesos)
packages/orchestrator   cerebro LangGraph stateful — router, especialistas,
                        sintetizador, cleanup obligatorio
.github/workflows       CI selectivo (path filters) + trigger pr-review
compose.yaml            postgres (checkpointer) + mcp + orchestrator
```

```text
START -> router -> fan-out {review, security, qa} -> synthesizer -> cleanup -> END
```

- **Postgres** se usa *únicamente* para `langgraph-checkpoint-postgres`, de
  modo que una ejecución puede reanudarse cuando el PR recibe commits nuevos
  (`thread_id = pr-review/<repo>/<pr>`).
- **La seguridad de procesos es contractual**: `shell=True` está prohibido,
  cada proceso de validación se lanza en su propio grupo de procesos, su
  `pgid` se registra en `active_pgids`, y el nodo `cleanup` obligatorio (más
  una red de seguridad `finally` en `run_pr_review`) envía `SIGTERM`/`SIGKILL`
  a cada grupo. El GitHub Action además verifica que no queden procesos
  huérfanos.

## Inicio rápido

```bash
# 1. dependencias (instala ambos paquetes, modo editable)
uv sync --all-packages

# 2. tests unitarios (sin red ni git)
uv run pytest packages

# 3. stack local (postgres + mcp en :8000/sse + orchestrator)
export GITHUB_TOKEN=ghp_...
export REPO_UNDER_TEST=/path/a/repo-con-pr
export PR_NUMBER=123
podman compose up -d            # o: docker compose up -d
podman compose run --rm orchestrator
```

Dry-run local sin levantar el stack (no publica comentario, checkpointer
en memoria):

```bash
REPO_PATH=/path/al/checkout PR_NUMBER=123 GITHUB_TOKEN=ghp_... \
  uv run --package pr-review-orchestrator pr-review --dry-run
```

## Registrar el MCP en un IDE (depuración manual)

Apuntá el IDE compatible con MCP al servidor stdio — ej. `.vscode/mcp.json`:

```json
{
  "servers": {
    "quality-gate": {
      "command": "uv",
      "args": ["run", "--package", "quality-gate-mcp", "quality-gate-mcp"],
      "env": { "GITHUB_TOKEN": "ghp_..." }
    }
  }
}
```

O conectate al endpoint SSE que levanta compose:
`http://localhost:8000/sse`.

## Contrato del veredicto

El comentario publicado siempre tiene esta forma:

```markdown
## Validación funcional

**Veredicto:** ✅ Aprobado | ❌ Cambios requeridos

- ✅/❌ **check** — detalle

**Result:** <resumen>
**Note:** <nota>

<!-- pr-review head:<sha7> -->
```

Idempotencia: las re-ejecuciones sobre el mismo head **actualizan** el
comentario marcado existente; un PR está `pending` hasta que exista un
marker que coincida con su `head:<sha7>`.

## Variables de entorno

Copiá `.env.example` a `.env` (ignorado por git) y completá los valores —
lista todas las variables de abajo con sus defaults. `compose.yaml` lo
levanta automáticamente.

- `GITHUB_TOKEN` — solo por entorno, nunca en logs/código (requerido)
- `DATABASE_URL` — DSN del checkpointer Postgres (opcional)
- `REPO_PATH`, `PR_NUMBER`, `DRY_RUN` — inputs del orquestador
- `QG_*` — timeouts y umbrales del scanner (ver `config.py` de cada paquete)
- `QG_SCOPE_MAX_FILES`, `QG_SCOPE_MAX_CHARS` — límites del blast radius:
  los agentes revisan el diff + archivos cambiados + dependientes directos
  únicamente, nunca el repo completo
- `QG_LLM_*` — proveedor de LLM opcional (ver abajo)

### Métricas

Cada ejecución reporta un bloque `metrics` (JSON del CLI + línea
`**Métricas:**` en el veredicto del PR): duración por nodo, llamadas LLM
con tokens de prompt/completion y tiempos de procesos lanzados — para ver
dónde se va el tiempo y los tokens.

### Proveedor de LLM (opcional)

La revisión es determinística por defecto. Definir `QG_LLM_PROVIDER`
habilita enriquecimiento con LLM en el orquestador; las claves viven solo
en variables de entorno (`QG_LLM_API_KEY` o la variable canónica del
proveedor).

| `QG_LLM_PROVIDER` | Variable de clave (fallback) | Base URL por defecto |
|---|---|---|
| `ollama` | — (sin clave) | `http://localhost:11434/v1` |
| `openai` | `OPENAI_API_KEY` | `https://api.openai.com/v1` |
| `anthropic` | `ANTHROPIC_API_KEY` | `https://api.anthropic.com` |
| `nvidia` | `NVIDIA_API_KEY` | `https://integrate.api.nvidia.com/v1` |
| `openrouter` | `OPENROUTER_API_KEY` | `https://openrouter.ai/api/v1` |
| `custom` | `QG_LLM_API_KEY` | requiere `QG_LLM_BASE_URL` |

Cada proveedor trae un modelo por defecto; se sobreescribe con
`QG_LLM_MODEL`.

## CI

- `mcp-ci.yml` / `orchestrator-ci.yml` — tests unitarios filtrados por `paths:`.
- `pr-review.yml` — en `pull_request`, levanta un servicio Postgres, corre
  el grafo y falla si sobrevive algún proceso `tsc`/`node --check`/`rg`.

## Separar el monorepo en el futuro

`git subtree split --prefix=packages/mcp-server` (o `git filter-repo`)
extrae cualquiera de los paquetes con su historial intacto; los paquetes
solo se acoplan mediante la dependencia `quality-gate-mcp` declarada en el
pyproject del orquestador.
