# mcp-external — Gitea + OCI registry MCP server

**Status:** M12 follow-on complete + Scenario 1 validated
(2026-04-23). `health_check` + four `harbor_*` + five `gitea_*`
read tools are live; agent autonomously diagnoses the
tag-format-mismatch scenario end-to-end.

**Registry-backend pivot (2026-04-23):** the `harbor_*` tools now
target the OCI Distribution API v2 (`/v2/`) directly rather than
Harbor's project-scoped `/api/v2.0/` endpoints. Tool names are
preserved for prompt-vocabulary stability, but the implementations
work against plain `registry:2`, Harbor, ECR, GCR, or any v2-
compliant backend. The local stack runs `registry:2` paired with
`joxit/docker-registry-ui` (see `../registry/docker-compose.yaml`)
so the demo can show repository + tag listings in a browser while
the agent hits the same data via `/v2/`. Full Harbor stayed out of
this pivot — it ran into rsyslog / systemd interop issues on WSL
that would have eaten the time budget without changing the
agent-side capability.

## Purpose

Extend a Kubernetes-specialized agent's diagnostic reach beyond
the cluster itself. When an alert roots in an external system
(image registry, Git repo, Helm chart), the agent pivots through
this MCP server to trace the actual source.

## Runtime

| Field | Value |
|---|---|
| Image | `mcp-external` (built from this Dockerfile) |
| Container name | `mcp-external` |
| Network | `traefik-proxy` (container-to-container with agent0) |
| Port | `8002` (host-exposed; Traefik labels commented until needed) |
| Transport | streamable-http |
| MCP endpoint | `http://mcp-external:8002/mcp` (from agent0) or `http://localhost:8002/mcp` (from host) |
| Health | `GET http://localhost:8002/health` returns 200 JSON |
| Safety mode | `MCP_SAFETY_MODE=full|read-only|non-destructive` (default: full) |

## Configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `GITEA_URL` | `http://gitea:3000` | Gitea base URL (stack-local default) |
| `GITEA_TOKEN` | _host env_ | Gitea API token — docker-compose reads `${GITEA_TOKEN}` from the host shell. **Never commit a literal token.** See "Gitea token bootstrap" below. |
| `HARBOR_URL` | `http://registry:5000` | Harbor/registry base URL |
| `HARBOR_USER` | _empty_ | Harbor basic-auth user (unused for registry:2) |
| `HARBOR_PASSWORD` | _empty_ | Harbor basic-auth password |
| `MCP_SAFETY_MODE` | `full` | Tool filter mode |

## Dev loop (matches mcp-k8s pattern)

```bash
cd ~/projects/persona/k8/docker/mcp-external
docker build -t mcp-external .
docker compose up -d
curl -sS http://localhost:8002/health
docker logs -f mcp-external
```

Source is baked into the image (no volume mount). Rebuild after every
Python edit.

## Tool surface

**Live today (OCI `/v2/` backend):**
- `health_check` — reports configured external endpoints + planned tools
- `harbor_check_tag(repository, tag)` — **PoC-CRITICAL** (Scenario 1).
  `GET /v2/{repository}/manifests/{tag}`. Returns
  `{"exists": bool, "repository", "tag", "digest"}` — 404 surfaces
  as `exists: False` (not an error). `repository` is the slash-joined
  path (e.g. `"demo/whoami"`); the Harbor `project` parameter was
  retired in the pivot since plain v2 registries have no project concept.
- `harbor_list_tags(repository, limit=50)` — **PoC-CRITICAL** (Scenario 1).
  `GET /v2/{repository}/tags/list`. Returns
  `{"repository", "tags": [...tag_names], "count"}`. For per-tag
  digests, loop `harbor_check_tag` (each response carries
  `Docker-Content-Digest`).
- `harbor_get_repository_info(repository)` — aggregates tag count +
  tag list (registry:2 has no dedicated repo-metadata endpoint).
- `harbor_list_projects(limit=100)` — `GET /v2/_catalog`. Derives
  "project" buckets from the first path segment of each repository
  name (so `demo/whoami` contributes to project `demo`). Retains the
  Harbor vocabulary for prompt stability; flat repo list is in
  `repositories`.

All registry tools use optional basic auth (`HARBOR_USER` +
`HARBOR_PASSWORD`); the local stack runs anonymous. 10 s timeout,
never raise on expected HTTP conditions — structured
`{"error": "registry_api_error" | "registry_request_failed", ...}`
payloads so the agent reasons about outcomes without try/except.

**Gitea tools (`gitea_*`, all read-only, 10 s timeout, never raise):**
- `gitea_get_file_content(owner, repo, path, ref="main")` — **PoC-CRITICAL**
  (Scenario 2). Returns `{path, size, encoding, content (base64),
  decoded (utf-8), sha, ref}`.
- `gitea_get_chart_metadata(owner, repo, chart_path, ref="main")` —
  **PoC-CRITICAL** (Scenario 2). Fetches `Chart.yaml` + `values.yaml`
  (+ `values.schema.json` if present) under `{chart_path}/`, parses
  YAML/JSON into dicts. Returns `{chart, values, schema, missing: [...]}`.
- `gitea_get_recent_commits(owner, repo, limit=10, path=None)` — returns
  `{commits: [{sha, author, email, date, message}], count}`.
- `gitea_list_branches(owner, repo)` — returns
  `{branches: [{name, commit_sha}], count}`.
- `gitea_search_code(owner, repo, query, limit=20, ref="main")` — portable
  substring search (Gitea OSS has no repo-scoped code-search endpoint on
  1.22). Walks the git tree recursively and line-matches. Caps: 200 files
  scanned, 256 KiB per file. Returns
  `{matches: [{path, line, text}], count, scanned_files, skipped_large,
  source: "tree+raw"}`.

## Gitea token bootstrap

On first run the Gitea admin and a read-only token must be created by
hand (no auto-seed in the current compose). One-time:

```bash
# 1) create admin user (idempotent if it already exists)
docker exec -u git gitea gitea admin user create \
  --username giteaadmin --password changeme-demo-only \
  --email admin@gitea.localhost --admin --must-change-password=false

# 2) generate a read-only token for mcp-external
docker exec -u git gitea gitea admin user generate-access-token \
  --username giteaadmin --token-name mcp-external \
  --scopes "read:repository,read:user,read:organization"
# → prints: Access token was successfully created: <40-hex>

# 3) export to host shell (do NOT commit) and recreate the container
export GITEA_TOKEN=<40-hex>
cd ~/projects/persona/k8/docker/mcp-external
docker compose up -d --force-recreate
curl -sS http://localhost:8002/health | jq '.endpoints.gitea.authenticated'
# → true
```

For long-lived bench setups put the export into `~/.bashrc` or a
local `.env` file — the compose file reads `${GITEA_TOKEN}` from the
shell. Never hard-code the token into `docker-compose.yaml`.

## Fixtures

- **`test_scenario_1_tag_mismatch.sh`** — end-to-end validated
  (2026-04-23). Pushes `demo/whoami:{1.2.1,1.2.2,1.2.3}` (no `v`
  prefix) to the registry, deploys a Kubernetes `Deployment` that
  references `registry:5000/demo/whoami:v1.2.3`, documents the split-
  screen demo orientation, and prints the synthetic-alert curl to
  bypass the Prometheus `for: 5m` timer. Agent autonomously calls
  `harbor_check_tag` + `harbor_list_tags` and diagnoses "tag format
  mismatch, not a CI build failure."
- `test_scenario_1_missing_image_tag.sh` — original stub, superseded
  by the tag-mismatch script above.
- `test_scenario_2_invalid_helm_values.sh` — still a stub; lands in
  M12's next push alongside the `values.schema.json` path.

## Related infra (persona stack)

- `../gitea/docker-compose.yaml` — Gitea on `gitea.localhost`
- `../registry/docker-compose.yaml` — `registry:2` on `registry.localhost`
  (+ host port 5000) and `joxit/docker-registry-ui` on
  `registry-ui.localhost`
- `../k3d/registries.yaml` + `setup-registry-certs.sh` — insecure-
  HTTP mirror config so k3d nodes can pull from `registry:5000`
  without TLS + without masking 404s with the HTTPS-fallback error
- `../agent-zero/cutover.py` — MCP client config (adds this server
  under the `external` key)

## Dual-repo note

Source is mirrored to `~/projects/mcp-toolbox/external/` per the
persona dual-repo workflow (see top-level `CLAUDE.md`). That copy is
the authoritative standalone repo; this copy is the dev integration
with the running stack.
