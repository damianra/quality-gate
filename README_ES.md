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

- `GITHUB_TOKEN` — solo por entorno, nunca en logs/código (requerido)
- `DATABASE_URL` — DSN del checkpointer Postgres (opcional)
- `REPO_PATH`, `PR_NUMBER`, `DRY_RUN` — inputs del orquestador
- `QG_*` — timeouts y umbrales del scanner (ver `config.py` de cada paquete)

## CI

- `mcp-ci.yml` / `orchestrator-ci.yml` — tests unitarios filtrados por `paths:`.
- `pr-review.yml` — en `pull_request`, levanta un servicio Postgres, corre
  el grafo y falla si sobrevive algún proceso `tsc`/`node --check`/`rg`.

## Separar el monorepo en el futuro

`git subtree split --prefix=packages/mcp-server` (o `git filter-repo`)
extrae cualquiera de los paquetes con su historial intacto; los paquetes
solo se acoplan mediante la dependencia `quality-gate-mcp` declarada en el
pyproject del orquestador.
