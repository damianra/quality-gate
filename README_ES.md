# quality-gate

Revisión autónoma de Pull Requests para repositorios de GitHub de
cualquier lenguaje o ecosistema. Reemplaza la revisión manual: en cada PR el sistema perfila el diff, detecta
*vibecoding* (ruido de whitespace, bloques comentados, TODOs, restos de
debug, secretos, dependencias nuevas, cambios fuera de scope, intentos
de prompt-injection), corre validación estructural con la mejor
toolchain disponible para el ecosistema y publica un comentario
contractual con el veredicto en GitHub.

¿Primera vez por aquí? Empezá por la
[descripción general no técnica](PROJECT_OVERVIEW_ES.md)
(también disponible [en inglés](PROJECT_OVERVIEW.md)).

## Arquitectura

Dos paquetes versionados de forma independiente en un workspace de `uv`:

```text
packages/mcp-server     servidor MCP stateless — herramientas
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

## Prueba local

El chequeo end-to-end más rápido — corre el grafo completo contra un
**checkout local** del head del PR, no publica nada en GitHub
(`--dry-run`) y usa el checkpointer en memoria (sin Postgres):

```bash
export GITHUB_TOKEN=ghp_...           # contents:read + pull_requests/issues:write + repo:status
export REPO_PATH=/path/al/checkout    # repo con la rama del PR chequeada
export PR_NUMBER=123
uv run --package pr-review-orchestrator pr-review --dry-run
```

La salida es un JSON de veredicto por stdout — `approve`, `summary`,
`checks`, `cleanup_report` y el bloque `metrics` (duraciones, tokens por
modelo, telemetría). Exit code: `0` aprobado, `1` cambios requeridos.
Sin `--dry-run` publica/actualiza el comentario real — apuntá
`REPO_PATH` a un repo descartable si probás ese camino.

Para ejercitar el camino del **webhook**, levantá el ingress + un túnel
(sección siguiente).

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

O conectate al endpoint SSE que levanta el profile `debug` de compose
(`docker compose --profile debug up -d mcp-server`):
`http://localhost:8000/sse`.

## Contrato del veredicto

El comentario publicado siempre tiene esta forma:

```markdown
## Functional validation

**Verdict:** ✅ Approved | ❌ Changes required

- ✅/⚠️/❌ **check** — detalle

**Result:** <resumen>
**Note:** <nota>

**Validated in sandbox:** <paso → pass/fail/timeout>  # FR-O28, pasos que corrieron en sandbox

**Metrics:** <una línea>
<details><summary>LLM telemetry</summary>   # tabla por modelo, si hubo llamadas

<!-- pr-review head:<sha7> -->
```

Idempotencia: las re-ejecuciones sobre el mismo head **actualizan** el
comentario marcado existente; un PR está `pending` hasta que exista un
marker que coincida con su `head:<sha7>`.

## Validación en sandbox (FR-O28)

Todo lo que ejecuta código controlado por el PR corre dentro de un
contenedor `qg-*` en lugar del host — un mismo contrato de aislamiento
sirve a los validadores de QA, la auditoría de dependencias y la etapa
funcional:

- **Etapa funcional.** Un `quality-gate.yaml` en la raíz del repo
  declara pasos de build/test/smoke (`{steps: [{name, argv, timeout?,
  required?, net?}]}`); sin contrato se autodetecta por ecosistema
  (`pytest`/`npm test`/`go test`/`cargo test`). Un paso `required`
  fallido es blocker; el resto son warnings — una rotura preexistente
  nunca bloquea un merge. Los resultados aparecen en la línea
  `Validated in sandbox` del veredicto.
- **Contención.** `run --rm --init`, `--network none` por defecto —
  el egress va solo a la auditoría de dependencias y a los pasos
  cuando el operador lo habilita globalmente (`QG_SANDBOX_ALLOW_NET`
  honra las declaraciones `net: true`, así un PR no puede otorgarse
  red a sí mismo), rootfs y worktree de solo lectura (overlay
  `:O` de podman / ro+tmpfs en docker), usuario no-root,
  `no-new-privileges`, `--cap-drop ALL`, límites de memoria/CPU/pids y
  env por whitelist — los secretos nunca entran al sandbox.
- **Runtime.** `QG_SANDBOX=auto` usa podman/docker cuando la imagen
  `qg-sandbox` está buildeada (`podman build -f Dockerfile.sandbox -t
  qg-sandbox:local .`); si no, cae a spawns en host bajo la política
  `QG_EXEC_VALIDATORS`. Dentro del sandbox los validadores que ejecutan
  código (`go vet`, `cargo check`) siempre están permitidos — la
  contención reemplaza el requisito de confianza.
- **Limpieza.** Los sandboxes que agotan su timeout reciben un
  `container kill` como segunda red y el nodo cleanup barre contenedores
  `qg-*` residuales de corridas caídas.
- **Toolchain de seguridad (FR-O29).** `semgrep` (ruleset curado),
  `gitleaks` (secretos, solo líneas agregadas — nunca emite el texto
  matcheado) y `trivy` (misconfig de IaC en Dockerfiles/compose/
  workflows/`deploy/`/`*.tf`) corren en el mismo sandbox cuando su
  superficie de disparo aparece en el diff. Lista habilitada, timeout
  y tope de findings vía `QG_SEC_TOOLS`/`QG_SEC_TOOL_TIMEOUT`/
  `QG_SEC_MAX_FINDINGS`; herramientas ausentes degradan a notas info.
  Archivos de supresión de scanners o directivas tipo `nosem`/
  `tfsec:ignore` agregadas por el PR advierten. FR-O30 fija cada
  input controlado por el repo — gitleaks/trivy corren con cwd fuera
  del árbol (el `trivy.yaml` del repo nunca carga), el `.gitleaksignore`
  del repo queda enmascarado por un mount de `/dev/null` en el sandbox
  (el fallback de host no puede enmascarar; el counter-scan igual lo
  reporta),
  un counter-scan enumera las superficies de supresión activas como
  info, y las tres herramientas corren de forma concurrente.

- **Benchmark golden-set (FR-O31)** — `bench/` reejecuta un corpus de
  PRs sintéticos (casos de detección + adversarios + falsos positivos)
  contra las etapas deterministas reales y reporta detección por tag y
  findings inesperados; ver `bench/README.md`.

- **Salida SARIF (FR-O32)** — los findings también se escriben a
  `quality-gate.sarif` (SARIF 2.1.0, un `run` por agente;
  `QG_SARIF_PATH`, `off` lo desactiva). `pr-review.yml` lo sube a
  code scanning, así los findings aparecen como anotaciones de línea
  en el PR y entradas en la pestaña Security — solo advisory, nunca
  una segunda compuerta.

## Variables de entorno

Copiá `.env.example` a `.env` (ignorado por git) y completá los valores —
lista todas las variables de abajo con sus defaults. `compose.yaml` lo
levanta automáticamente.

- `GITHUB_TOKEN` — solo por entorno, nunca en logs/código (requerido
  salvo que se use el camino de GitHub App)
- `GITHUB_APP_ID` / `GITHUB_APP_PRIVATE_KEY` /
  `GITHUB_APP_INSTALLATION_ID` — recomendado en producción (FR-P18):
  el servicio mintea tokens de instalación de 1h en runtime en lugar de
  un PAT de larga vida; los comentarios/statuses quedan a nombre del bot
  de la app con permisos mínimos, y dar de alta un repo es "instalar la
  app". El PAT sigue soportado para corridas locales.
- `DATABASE_URL` — DSN del checkpointer Postgres (opcional)
- `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` — credenciales
  del stack compose (requeridas para `compose up`, FR-P24); el DSN
  interno de los servicios se compone a partir de ellas
- `REPO_PATH`, `PR_NUMBER`, `DRY_RUN` — inputs del orquestador
- `QG_*` — timeouts y umbrales del scanner (ver `config.py` de cada paquete)
- `QG_SCOPE_MAX_FILES`, `QG_SCOPE_MAX_CHARS` — límites del blast radius:
  los agentes revisan el diff + archivos cambiados + dependientes directos
  únicamente, nunca el repo completo
- `QG_LLM_*` — proveedor de LLM opcional (ver abajo)

### Métricas

Cada ejecución reporta un bloque `metrics` (JSON del CLI + línea
`**Metrics:**` en el veredicto del PR): duración por nodo, llamadas LLM
con tokens de prompt/completion desglosados por modelo y tiempos de
procesos lanzados — para ver dónde se va el tiempo y los tokens. Un
bloque `<details>` plegable agrega telemetría por modelo: tokens (p+c),
duración, tok/s de completion, delta de RSS del proceso y el footprint
que reporta el proveedor (Ollama `/api/ps` → RAM/VRAM). Lo que un
proveedor no devuelve se muestra como `—`; la telemetría nunca bloquea
una ejecución. Las filas persistidas de `run_metrics` llevan columnas
`status` (`ok|error|timeout`) + `error` (FR-P2): un run fallido es una
fila consultable, no un hueco silencioso —
`SELECT * FROM run_metrics WHERE status <> 'ok'` lista los fallos.

### Proveedor de LLM (opcional)

La revisión es determinística por defecto. Hay tres perfiles de
despliegue soportados (FR-P19), seleccionados por rol vía
`QG_LLM_PROVIDER*`:

- **`deterministic`** — todos los providers sin definir: cero llamadas
  LLM. Línea base para hosts sin GPU y para código que no debe salir
  del perímetro.
- **`api`** — providers de API por rol; el perfil recomendado en hosts
  sin GPU cuando el código puede salir del perímetro (~10–11k tokens de
  entrada por llamada de especialista, ~USD 0,01–0,20/review según el
  tier del modelo).
- **`local`** — ollama; solo donde el host tiene GPU o donde una
  latencia de varios minutos por llamada es aceptable. En hosts sin GPU
  **no es viable para revisión interactiva**: un reasoning 8B tarda
  ~3–8 min/llamada → timeouts contra `QG_LLM_TIMEOUT` y findings
  degradados.

Definir `QG_LLM_PROVIDER`
habilita enriquecimiento con LLM en el orquestador; las claves viven solo
en variables de entorno (`QG_LLM_API_KEY` o la variable canónica del
proveedor). Los especialistas `review`, `security` y `qa` llaman cada uno
a su config de rol sobre el `pr_context` acotado; los findings LLM
aparecen con prefijo `LLM:` en el veredicto y cualquier fallo degrada a
una nota info — nunca rechaza un PR por sí solo. El sintetizador también
llama al rol `harness` (la config base `QG_LLM_*` — p. ej. Ollama local)
para un resumen ejecutivo breve del veredicto, registrado en
`llm_call_metrics` como cualquier otro rol.

| `QG_LLM_PROVIDER` | Variable de clave (fallback) | Base URL por defecto |
|---|---|---|
| `ollama` | — (sin clave) | `http://localhost:11434/v1` |
| `openai` | `OPENAI_API_KEY` | `https://api.openai.com/v1` |
| `anthropic` | `ANTHROPIC_API_KEY` | `https://api.anthropic.com` |
| `nvidia` | `NVIDIA_API_KEY` | `https://integrate.api.nvidia.com/v1` |
| `openrouter` | `OPENROUTER_API_KEY` | `https://openrouter.ai/api/v1` |
| `custom` | `QG_LLM_API_KEY` | requiere `QG_LLM_BASE_URL` |
| `devin-cli` | — (sesión `devin auth`) | subproceso local (solo host), sin URL |

Cada proveedor trae un modelo por defecto; se sobreescribe con
`QG_LLM_MODEL`.

Toda variable `QG_LLM_*` acepta además un sufijo de rol — `_HARNESS`,
`_REVIEW`, `_SECURITY` o `_QA` — para configurar ese rol del grafo de
forma independiente (p.ej. `QG_LLM_MODEL_QA`). El bloque base es la
configuración del harness y el fallback compartido, de modo que un
Ollama local puede manejar el harness mientras los especialistas usan
modelos `:free` de OpenRouter (ver `.env.example`).

El endurecimiento contra prompt-injection (FR-O25) está siempre activo:
`analyze_diff` marca como findings `prompt_injection` las líneas
agregadas que parecen directivas a un LLM o marcadores de rol de
chat-template (severidad warning; info en contextos de tests/docs/
definición de patrones), todo bloque de evidencia enviado a los modelos
se envuelve en centinelas explícitos de datos no confiables, y los
findings del LLM solo pueden referenciar archivos realmente modificados
en el PR.

## Modo servicio (webhook)

El stack también puede correr como servicio en vez de por invocación:
nginx expone UN solo puerto público, el ingress verifica y encola los
webhooks `pull_request` de GitHub, cada evento aceptado clona el head
del PR en un worktree efímero, y el grafo publica el veredicto en el PR
(las métricas caen en `run_metrics` / `llm_call_metrics`). Con
`DATABASE_URL` definida, los deliveries aceptados persisten en la tabla
`pending_runs` *antes* del `202` — `delivery_id` es la clave de dedupe,
el arranque re-despacha las filas que un proceso caído dejó en
`queued`/`running`, y un fallo de persistencia responde `500` para que
GitHub reintente en vez de perder la revisión (FR-P4).

```bash
GITHUB_TOKEN=ghp_... \
GITHUB_WEBHOOK_SECRET=<secret del webhook del repo> \
QG_ALLOWED_REPOS=your-org/your-repo \
QG_INGRESS_PORT=8089 \
docker compose up -d ingress nginx

# TLS en producción (FR-P10): caddy termina HTTPS en :443 y proxea al
# nginx de loopback — definí QG_PUBLIC_HOST con el hostname público y
# levantá el stack con el profile tls:
QG_PUBLIC_HOST=qg.example.com \
docker compose --profile tls up -d
# La URL del webhook queda https://qg.example.com/github/webhook.

# Cada delivery aceptado publica un commit status en el head sha del PR
# (context `quality-gate`, FR-P8): pending -> success/failure/error.
# Requerí ese check en branch protection ("require status checks
# before merging") para que el veredicto realmente bloquee merges — el
# token necesita repo:status (clásico) o Commit statuses: write
# (fine-grained).

# Perfil LLM (FR-P19): el stack arranca en DETERMINISTIC — sin llamadas
# LLM. Para `api`, exportá QG_LLM_PROVIDER(+clave) según .env.example.
# Para `local` (solo host con GPU), apuntá el ingress a un ollama del
# HOST (http://localhost:11434 vía host.docker.internal) — `ollama
# serve` + `ollama pull qwen3:8b` en el host — o caé al contenedor:
docker compose --profile llm up -d ollama
docker compose exec ollama ollama pull qwen3:8b
QG_LLM_PROVIDER=ollama QG_LLM_BASE_URL=http://ollama:11434/v1 \
docker compose up -d ingress
# Prompts >4k chocan con el default num_ctx=4096 de ollama — ver la
# nota de tuning de ctx en docs/local-webhook-setup.md.
```

Para una **prueba local del webhook** (sin compose, sin IP pública)
levantá el ingress en el host y publicalo con un túnel:

```bash
set -a && source .env && set +a       # GITHUB_TOKEN, GITHUB_WEBHOOK_SECRET,
                                      # QG_ALLOWED_REPOS, QG_INGRESS_PORT=8089
uv run qg-ingress                     # http://127.0.0.1:8089
curl http://127.0.0.1:8089/healthz    # -> {"status":"ok"}

# túnel SSH sin instalar nada -> https://<id>.lhr.life (TLS válido)
ssh -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 \
    -R 80:localhost:8089 nokey@localhost.run
# alternativas: cloudflared tunnel --url http://localhost:8089
#               ngrok http 8089
```

Después registrá `https://<id>.lhr.life/github/webhook` en el repo
(Settings → Webhooks): content type `application/json`, secret =
`GITHUB_WEBHOOK_SECRET`, eventos: Pull requests. Verificá con
`curl https://<id>.lhr.life/healthz` y mirá *Recent deliveries* en
GitHub: `202` (aceptado) / `401` (firma inválida) / `403` (repo no
permitido). Ver [docs/local-webhook-setup.md](docs/local-webhook-setup.md)
para el levantamiento completo, todas las opciones de exposición,
caveats y teardown.

Controles de exposición pública (FR-O19..O22/P9/P10, INV-6): el endpoint
TLS de caddy es la única superficie pública en producción (nginx,
postgres y el backend ingress bindean loopback o la red interna); nginx
aplica 10 r/s por IP y body cap de 1 MB en el path del webhook; todo
payload se verifica por HMAC antes de parsear; `QG_ALLOWED_REPOS` es
deny-by-default; el token llega a git solo vía headers de env; cada
worktree se borra tras su run más un sweep de arranque. El servicio SSE
de `mcp-server` queda detrás del profile `debug` — su tool
`spawn_process` es ejecución de comandos sin autenticación y nunca debe
ser pública. Todos los contenedores del modo servicio reinician
automáticamente (`unless-stopped`). Los validadores corren con un
entorno depurado — los secretos nunca llegan al env de un subproceso
(FR-P11) — y los validadores que ejecutarían código controlado por el
PR (`cargo check`, `go vet`) están deshabilitados por defecto en modo
servicio (`QG_EXEC_VALIDATORS=0`, FR-P12).

Operación (FR-P15/P16): los logs del servicio emiten un objeto JSON por
registro a `QG_LOG_LEVEL`, correlacionados por `run_id`
(`pr-review/{repo}/{pr}` — la misma clave que usa `run_metrics`), así
que "qué pasó con el PR #N" es un grep o una query SQL.
`GET /readyz` reporta alcanzabilidad de Postgres y profundidad de cola
para monitoreo (distinto del liveness `/healthz`), y
`QG_HEARTBEAT_URL` es un ping dead-man (p.ej. healthchecks.io) que se
golpea tras cada run completado — el silencio más allá del intervalo
del monitor significa que el servicio murió. Los runs fallidos se
consultan con `SELECT * FROM run_metrics WHERE status <> 'ok'` y
`SELECT * FROM pending_runs WHERE status = 'failed'`. Los backups de
Postgres (cron de pg_dump + runbook de restore) están en
[docs/backup-restore.md](docs/backup-restore.md) (FR-P17), y los
procedimientos de levantamiento/monitoreo/recuperación en una sola VM
están consolidados en el [runbook de operación](docs/ops-runbook.md)
(FR-P22). Al arrancar,
el ingress loguea un reporte doctor de qué binarios validador/scanner
resolvió la imagen (FR-P13); todas las imágenes base y de compose están
pineadas por digest y las actualiza Dependabot, y `supply-chain.yml`
buildea + escanea con trivy ambas imágenes y el repo ante cambios de
empaquetado y semanalmente (FR-P14).

## CI

- `mcp-ci.yml` / `orchestrator-ci.yml` — tests unitarios filtrados por `paths:`.
- `pr-review.yml` — en `pull_request`, levanta un servicio Postgres, corre
  el grafo y falla si sobrevive algún proceso validador/scanner.
- `supply-chain.yml` — buildea ambos Dockerfiles y escanea con trivy el
  repo y las imágenes (CRITICAL falla) ante cambios de empaquetado y
  semanalmente.

## Separar el monorepo en el futuro

`git subtree split --prefix=packages/mcp-server` (o `git filter-repo`)
extrae cualquiera de los paquetes con su historial intacto; los paquetes
solo se acoplan mediante la dependencia `quality-gate-mcp` declarada en el
pyproject del orquestador.
