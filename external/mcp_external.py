"""mcp-external FastMCP server for Gitea + Harbor (M12 + follow-on).

M12 shipped the scaffolding + Harbor tools. This revision adds the
five read-only Gitea tools the demo agent needs to investigate a Helm
chart whose image tag doesn't exist in Harbor.

Conventions (mirror mcp-k8s):
- FastMCP, transport: streamable-http, port 8002
- All tools are async def decorated with @mcp.tool(...)
- Return dict/JSON, never raw YAML
- Never raise on expected API conditions; return {"error": ...} dicts
- Tool names prefixed by domain (gitea_*, harbor_*) per the
  project-wide MCP tool namespacing rule (see persona/CLAUDE.md).
"""

import base64
import json as _json
import logging
import os
from typing import Any, Optional
from urllib.parse import quote

import httpx
import yaml
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp-external")

MCP_SAFETY_MODE = os.environ.get("MCP_SAFETY_MODE", "full")

WRITE_TOOLS: set[str] = set()
DESTRUCTIVE_TOOLS: set[str] = set()

GITEA_URL = os.environ.get("GITEA_URL", "http://gitea:3000").rstrip("/")
GITEA_TOKEN = os.environ.get("GITEA_TOKEN", "")
GITEA_TIMEOUT = 10.0
HARBOR_URL = os.environ.get("HARBOR_URL", "http://registry:5000")
HARBOR_USER = os.environ.get("HARBOR_USER", "")
HARBOR_PASSWORD = os.environ.get("HARBOR_PASSWORD", "")

mcp = FastMCP("mcp-external", host="0.0.0.0", port=8002)


def _status_payload() -> dict:
    return {
        "status": "ok",
        "server": "mcp-external",
        "version": "0.2.0-gitea-tools",
        "safety_mode": MCP_SAFETY_MODE,
        "endpoints": {
            "gitea": {
                "url": GITEA_URL,
                "configured": bool(GITEA_URL),
                "authenticated": bool(GITEA_TOKEN),
            },
            "harbor": {
                "url": HARBOR_URL,
                "configured": bool(HARBOR_URL),
                "authenticated": bool(HARBOR_USER and HARBOR_PASSWORD),
            },
        },
        "tools_planned": {
            "gitea": [
                "gitea_get_file_content",
                "gitea_get_chart_metadata",
                "gitea_get_recent_commits",
                "gitea_list_branches",
                "gitea_search_code",
            ],
            "harbor": [
                "harbor_check_tag",
                "harbor_list_tags",
                "harbor_get_repository_info",
                "harbor_list_projects",
            ],
        },
    }


def apply_safety_mode():
    """Remove tools based on MCP_SAFETY_MODE environment variable."""
    mode = os.environ.get("MCP_SAFETY_MODE", "full")

    if mode == "read-only":
        for tool_name in WRITE_TOOLS:
            if tool_name in mcp._tool_manager._tools:
                del mcp._tool_manager._tools[tool_name]
    elif mode == "non-destructive":
        for tool_name in DESTRUCTIVE_TOOLS:
            if tool_name in mcp._tool_manager._tools:
                del mcp._tool_manager._tools[tool_name]


@mcp.custom_route("/health", methods=["GET"])
async def http_health(request: Request) -> JSONResponse:
    return JSONResponse(_status_payload())


@mcp.tool(annotations={"idempotent": True, "destructive": False, "read_only": True})
async def health_check() -> dict:
    """Return server health and configured external endpoints.

    Placeholder tool used to verify mcp-external is reachable from
    Agent Zero during M12 scaffolding. Reports which external systems
    are configured (URL set). It does NOT probe them yet.
    """
    return _status_payload()


# ── OCI Registry ──────────────────────────────────────────────────────────────
#
# Read-only tools against a plain OCI Distribution API v2 registry (registry:2
# for the PoC; Harbor, ECR, GCR, etc. would work identically). Tools are named
# ``harbor_*`` for prompt-vocabulary stability — the semantic contract is the
# same whether the backend is Harbor or a generic v2 registry.
#
# /v2/ endpoints hit:
#   GET /v2/                        liveness
#   GET /v2/_catalog?n={N}          list repositories
#   GET /v2/{repo}/tags/list        list tags for a repository
#   GET /v2/{repo}/manifests/{tag}  check whether a tag exists + grab digest
#
# Auth: if HARBOR_USER + HARBOR_PASSWORD are set, Basic auth is used.
# Otherwise anonymous. Registry:2 in the PoC config runs without auth.
#
# Contract reference: plans/ADD_ON_A_EXTERNAL_SYSTEMS.md


REGISTRY_TIMEOUT = 10.0


def _registry_auth() -> tuple[str, str] | None:
    if HARBOR_USER and HARBOR_PASSWORD:
        return (HARBOR_USER, HARBOR_PASSWORD)
    return None


def _registry_base() -> str:
    return HARBOR_URL.rstrip("/")


async def _registry_get(path: str, params: dict[str, Any] | None = None) -> dict:
    """Perform a GET against the registry /v2/ API.

    Returns one of:
      {"ok": True, "status": 200, "json": <parsed body>, "headers": {...}}
      {"ok": True, "status": 404, "json": None, "headers": {...}}
      {"error": "registry_api_error", "status": N, "reason": "..."}
      {"error": "registry_request_failed", "detail": "..."}
    """
    url = f"{_registry_base()}{path}"
    try:
        async with httpx.AsyncClient(timeout=REGISTRY_TIMEOUT) as client:
            resp = await client.get(url, params=params, auth=_registry_auth())
    except httpx.HTTPError as exc:
        logger.warning("registry request failed: %s %s", url, exc)
        return {"error": "registry_request_failed", "detail": str(exc)}

    headers = dict(resp.headers)
    if resp.status_code == 404:
        return {"ok": True, "status": 404, "json": None, "headers": headers}
    if resp.status_code >= 400:
        reason = resp.text[:300] if resp.text else resp.reason_phrase
        return {
            "error": "registry_api_error",
            "status": resp.status_code,
            "reason": reason,
        }
    try:
        return {
            "ok": True,
            "status": resp.status_code,
            "json": resp.json(),
            "headers": headers,
        }
    except ValueError:
        return {
            "ok": True,
            "status": resp.status_code,
            "json": None,
            "headers": headers,
        }


@mcp.tool(annotations={"idempotent": True, "destructive": False, "read_only": True})
async def harbor_check_tag(repository: str, tag: str) -> dict:
    """Check whether an image tag exists in the registry.

    PoC-CRITICAL (Scenario 1: tag-format mismatch). A 404 is NOT an error —
    returns ``{"exists": False, ...}``. Use this to confirm whether a
    Helm values.yaml image reference (e.g. ``v1.2.3``) resolves against
    an actual registry tag (e.g. ``1.2.3``).

    Args:
        repository: slash-joined image path (e.g. ``"demo/whoami"``)
        tag: tag name (e.g. ``"v1.2.3"``)
    """
    headers = {"Accept": "application/vnd.docker.distribution.manifest.v2+json"}
    url = f"{_registry_base()}/v2/{repository}/manifests/{tag}"
    try:
        async with httpx.AsyncClient(timeout=REGISTRY_TIMEOUT) as client:
            resp = await client.get(url, headers=headers, auth=_registry_auth())
    except httpx.HTTPError as exc:
        return {
            "error": "registry_request_failed",
            "detail": str(exc),
            "repository": repository,
            "tag": tag,
        }
    if resp.status_code == 404:
        return {
            "exists": False,
            "repository": repository,
            "tag": tag,
            "digest": None,
        }
    if resp.status_code >= 400:
        return {
            "error": "registry_api_error",
            "status": resp.status_code,
            "reason": resp.text[:300] if resp.text else resp.reason_phrase,
            "repository": repository,
            "tag": tag,
        }
    return {
        "exists": True,
        "repository": repository,
        "tag": tag,
        "digest": resp.headers.get("docker-content-digest"),
    }


@mcp.tool(annotations={"idempotent": True, "destructive": False, "read_only": True})
async def harbor_list_tags(repository: str, limit: int = 50) -> dict:
    """List tags for a registry repository (PoC-CRITICAL, Scenario 1).

    Uses the OCI v2 API ``/v2/{repository}/tags/list``. Returns a plain
    list of tag names. To get per-tag digests, call ``harbor_check_tag``
    for each tag (the response carries ``Docker-Content-Digest``).

    Args:
        repository: slash-joined image path (e.g. ``"demo/whoami"``)
        limit: cap on the number of tags returned (default 50)
    """
    resp = await _registry_get(f"/v2/{repository}/tags/list")
    if "error" in resp:
        return {**resp, "repository": repository}
    if resp["status"] == 404 or not resp.get("json"):
        return {"tags": [], "count": 0, "repository": repository}
    body = resp["json"]
    tag_names = body.get("tags") or []
    return {
        "repository": repository,
        "tags": tag_names[:limit],
        "count": len(tag_names),
    }


@mcp.tool(annotations={"idempotent": True, "destructive": False, "read_only": True})
async def harbor_get_repository_info(repository: str) -> dict:
    """Fetch aggregated info for a registry repository.

    Registry:2 has no dedicated repository-metadata endpoint, so this
    aggregates the tag list. If the caller wants push-timestamps or
    per-artifact digests, they walk ``harbor_check_tag`` per tag.
    """
    tags_result = await harbor_list_tags(repository, limit=500)
    if "error" in tags_result:
        return tags_result
    return {
        "repository": repository,
        "tag_count": tags_result["count"],
        "tags": tags_result["tags"],
    }


@mcp.tool(annotations={"idempotent": True, "destructive": False, "read_only": True})
async def harbor_list_projects(limit: int = 100) -> dict:
    """List all repositories in the registry via ``/v2/_catalog``.

    Registry:2 has no ``project`` concept — "projects" in the return
    payload are derived from the top-level namespace segment of each
    repository path (e.g. repository ``demo/whoami`` -> project ``demo``).
    The ``projects`` key is retained for Harbor-vocabulary compatibility;
    the flat list of repositories lives under ``repositories``.
    """
    resp = await _registry_get("/v2/_catalog", params={"n": limit})
    if "error" in resp:
        return resp
    body = resp.get("json") or {}
    repositories: list[str] = body.get("repositories") or []
    projects: list[dict] = []
    seen: set[str] = set()
    for r in repositories:
        project = r.split("/", 1)[0] if "/" in r else r
        if project in seen:
            continue
        seen.add(project)
        repo_count = sum(
            1 for x in repositories if x.startswith(project + "/") or x == project
        )
        projects.append({"name": project, "repo_count": repo_count})
    return {
        "projects": projects,
        "repositories": repositories,
        "count": len(repositories),
    }


# ── Gitea ─────────────────────────────────────────────────────────────────────
#
# Read-only tools against the Gitea REST API v1. Token auth via the
# ``Authorization: token <GITEA_TOKEN>`` header. As with the Harbor helpers,
# all tools return structured dicts; transport + API errors surface as
# ``{"error": ..., ...}`` rather than raising so an MCP client can react
# without wrapping every call in try/except.
#
# Contract reference: plans/ADD_ON_A_EXTERNAL_SYSTEMS.md


def _gitea_headers() -> dict[str, str]:
    h = {"Accept": "application/json"}
    if GITEA_TOKEN:
        h["Authorization"] = f"token {GITEA_TOKEN}"
    return h


async def _gitea_get(path: str, params: dict[str, Any] | None = None) -> dict:
    """Perform an authenticated GET against the Gitea API.

    Returns one of:
      {"ok": True, "status": N, "data": <parsed body>}
      {"error": "gitea_api_error", "status": N, "body": "..."}
      {"error": "gitea_request_failed", "detail": "..."}
    """
    if not GITEA_URL:
        return {"error": "gitea_not_configured"}
    url = f"{GITEA_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=GITEA_TIMEOUT) as client:
            resp = await client.get(url, headers=_gitea_headers(), params=params)
    except httpx.TimeoutException:
        return {"error": "gitea_request_failed", "detail": "timeout", "url": url}
    except httpx.HTTPError as exc:
        logger.warning("gitea request failed: %s %s", url, exc)
        return {"error": "gitea_request_failed", "detail": str(exc), "url": url}

    if resp.status_code >= 400:
        body: Any
        try:
            body = resp.json()
        except ValueError:
            body = resp.text[:300]
        return {
            "error": "gitea_api_error",
            "status": resp.status_code,
            "body": body,
            "url": url,
        }

    try:
        return {"ok": True, "status": resp.status_code, "data": resp.json()}
    except ValueError:
        return {"ok": True, "status": resp.status_code, "data": resp.text}


@mcp.tool(annotations={"idempotent": True, "destructive": False, "read_only": True})
async def gitea_get_file_content(
    owner: str, repo: str, path: str, ref: str = "main"
) -> dict:
    """Fetch a single file from a Gitea repo and return its decoded text.

    PoC-CRITICAL. Used to read Chart.yaml / values.yaml during the tag-
    format-mismatch demo (scenario: Helm chart references ``v1.2.3`` but
    Harbor only has ``1.2.3``).

    GET /api/v1/repos/{owner}/{repo}/contents/{path}?ref={ref}

    Returns:
        On success: ``{"path", "size", "encoding", "content" (base64),
        "decoded" (utf-8 text if decodable), "sha", "ref", "owner", "repo"}``
        On directory path: ``{"error": "path_is_directory", "entries": [...]}``
        On API failure: ``{"error": ..., "status": N, "body": ...}``
    """
    api = (
        f"/api/v1/repos/{quote(owner)}/{quote(repo)}/contents/"
        f"{quote(path, safe='/')}"
    )
    resp = await _gitea_get(api, params={"ref": ref})
    if "error" in resp:
        return {**resp, "owner": owner, "repo": repo, "path": path, "ref": ref}

    data = resp["data"]
    if isinstance(data, list):
        return {
            "error": "path_is_directory",
            "owner": owner,
            "repo": repo,
            "path": path,
            "entries": [e.get("path") for e in data[:50]],
        }
    if not isinstance(data, dict):
        return {
            "error": "unexpected_response_shape",
            "data_type": type(data).__name__,
        }

    content_b64 = data.get("content") or ""
    encoding = data.get("encoding") or ""
    decoded: Optional[str] = None
    decode_error: Optional[str] = None
    if encoding == "base64" and content_b64:
        try:
            decoded = base64.b64decode(content_b64).decode("utf-8")
        except UnicodeDecodeError:
            decode_error = "binary_or_non_utf8"
        except Exception as exc:  # pragma: no cover - defensive
            decode_error = f"decode_failed: {exc.__class__.__name__}"

    out: dict[str, Any] = {
        "path": data.get("path"),
        "size": data.get("size"),
        "encoding": encoding,
        "content": content_b64,
        "sha": data.get("sha"),
        "ref": ref,
        "owner": owner,
        "repo": repo,
    }
    if decoded is not None:
        out["decoded"] = decoded
    if decode_error:
        out["decode_error"] = decode_error
    return out


@mcp.tool(annotations={"idempotent": True, "destructive": False, "read_only": True})
async def gitea_get_chart_metadata(
    owner: str, repo: str, chart_path: str, ref: str = "main"
) -> dict:
    """Fetch Chart.yaml + values.yaml + values.schema.json for a Helm chart.

    PoC-CRITICAL. Looks under ``{chart_path}/`` in the repo. YAML files
    are parsed into dicts; ``values.schema.json`` (optional) is parsed as
    JSON. Missing files are listed in ``missing`` — missing schema is
    soft, missing Chart.yaml surfaces ``error: chart_yaml_missing``.

    Returns:
        ``{"chart": {...}, "values": {...}, "schema": {...} | None,
        "missing": ["schema"?], "owner", "repo", "chart_path", "ref"}``
    """
    chart_path = chart_path.strip("/")
    files = {
        "chart": f"{chart_path}/Chart.yaml",
        "values": f"{chart_path}/values.yaml",
        "schema": f"{chart_path}/values.schema.json",
    }

    out: dict[str, Any] = {
        "owner": owner,
        "repo": repo,
        "chart_path": chart_path,
        "ref": ref,
        "missing": [],
    }

    for key, fpath in files.items():
        res = await gitea_get_file_content(
            owner=owner, repo=repo, path=fpath, ref=ref
        )
        if "error" in res:
            if res.get("status") == 404:
                out["missing"].append(key)
                out[key] = None
                continue
            out[key] = {
                "error": res["error"],
                "status": res.get("status"),
                "path": fpath,
            }
            continue

        text = res.get("decoded")
        if text is None:
            out[key] = {"error": "no_decoded_content", "path": fpath}
            continue

        try:
            if key == "schema":
                out[key] = _json.loads(text)
            else:
                out[key] = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            out[key] = {
                "error": "yaml_parse_failed",
                "detail": str(exc)[:200],
                "path": fpath,
            }
        except Exception as exc:  # pragma: no cover
            out[key] = {
                "error": f"parse_failed: {exc.__class__.__name__}",
                "detail": str(exc)[:200],
                "path": fpath,
            }

    if out.get("chart") is None:
        out["error"] = "chart_yaml_missing"
    return out


@mcp.tool(annotations={"idempotent": True, "destructive": False, "read_only": True})
async def gitea_get_recent_commits(
    owner: str, repo: str, limit: int = 10, path: Optional[str] = None
) -> dict:
    """List recent commits on a Gitea repo (optionally scoped to ``path``).

    GET /api/v1/repos/{owner}/{repo}/commits?limit={limit}[&path={path}]

    Returns:
        ``{"commits": [{"sha", "author", "email", "date", "message"}],
        "count": N, "owner", "repo", "path", "limit"}``
    """
    limit = max(1, min(int(limit), 50))
    params: dict[str, Any] = {"limit": limit}
    if path:
        params["path"] = path

    api = f"/api/v1/repos/{quote(owner)}/{quote(repo)}/commits"
    resp = await _gitea_get(api, params=params)
    if "error" in resp:
        return {**resp, "owner": owner, "repo": repo}

    data = resp["data"]
    if not isinstance(data, list):
        return {
            "error": "unexpected_response_shape",
            "data_type": type(data).__name__,
        }

    commits: list[dict] = []
    for c in data:
        commit_meta = c.get("commit") or {}
        author_meta = commit_meta.get("author") or {}
        commits.append(
            {
                "sha": c.get("sha"),
                "author": author_meta.get("name")
                or (c.get("author") or {}).get("login"),
                "email": author_meta.get("email"),
                "date": author_meta.get("date"),
                "message": (commit_meta.get("message") or "").strip(),
            }
        )

    return {
        "commits": commits,
        "count": len(commits),
        "owner": owner,
        "repo": repo,
        "path": path,
        "limit": limit,
    }


@mcp.tool(annotations={"idempotent": True, "destructive": False, "read_only": True})
async def gitea_list_branches(owner: str, repo: str) -> dict:
    """List branches on a Gitea repo.

    GET /api/v1/repos/{owner}/{repo}/branches

    Returns:
        ``{"branches": [{"name", "commit_sha"}], "count": N, "owner", "repo"}``
    """
    api = f"/api/v1/repos/{quote(owner)}/{quote(repo)}/branches"
    resp = await _gitea_get(api)
    if "error" in resp:
        return {**resp, "owner": owner, "repo": repo}

    data = resp["data"]
    if not isinstance(data, list):
        return {
            "error": "unexpected_response_shape",
            "data_type": type(data).__name__,
        }

    branches: list[dict] = []
    for b in data:
        commit = b.get("commit") or {}
        branches.append(
            {
                "name": b.get("name"),
                "commit_sha": commit.get("id") or commit.get("sha"),
            }
        )
    return {
        "branches": branches,
        "count": len(branches),
        "owner": owner,
        "repo": repo,
    }


@mcp.tool(annotations={"idempotent": True, "destructive": False, "read_only": True})
async def gitea_search_code(
    owner: str, repo: str, query: str, limit: int = 20, ref: str = "main"
) -> dict:
    """Search file contents inside a Gitea repo (line-scoped matches).

    Gitea's OSS build does NOT expose a repo-scoped code-search REST
    endpoint (indexed search is an enterprise/front-end feature). This
    tool implements a portable fallback: it walks the repo's git tree
    recursively, fetches each blob under ``/raw/``, and performs a
    case-sensitive substring match line-by-line. Intended for small
    Helm chart repos; bounds: skip files > 256 KiB, cap scanned files
    at 200.

    Returns:
        ``{"matches": [{"path", "line", "text"}], "count": N, "query",
        "owner", "repo", "ref", "limit", "source": "tree+raw",
        "scanned_files": N, "skipped_large": N}``
    """
    limit = max(1, min(int(limit), 100))
    base = {
        "query": query,
        "owner": owner,
        "repo": repo,
        "ref": ref,
        "limit": limit,
        "source": "tree+raw",
    }

    # 1) Resolve ref -> commit SHA so we can hit git/trees recursively.
    branch_api = f"/api/v1/repos/{quote(owner)}/{quote(repo)}/branches/{quote(ref)}"
    branch_resp = await _gitea_get(branch_api)
    if "error" in branch_resp:
        return {"matches": [], "count": 0, **base, "note": "ref_lookup_failed",
                "error": branch_resp["error"], "status": branch_resp.get("status")}
    commit_sha = ((branch_resp["data"] or {}).get("commit") or {}).get("id")
    if not commit_sha:
        return {"matches": [], "count": 0, **base, "note": "ref_has_no_commit"}

    # 2) Walk the tree recursively.
    tree_api = f"/api/v1/repos/{quote(owner)}/{quote(repo)}/git/trees/{commit_sha}"
    tree_resp = await _gitea_get(tree_api, params={"recursive": "true", "per_page": 1000})
    if "error" in tree_resp:
        return {"matches": [], "count": 0, **base, "note": "tree_fetch_failed",
                "error": tree_resp["error"], "status": tree_resp.get("status")}
    tree = (tree_resp["data"] or {}).get("tree") or []

    MAX_FILES = 200
    MAX_BYTES = 256 * 1024
    matches: list[dict] = []
    scanned = 0
    skipped_large = 0

    for entry in tree:
        if entry.get("type") != "blob":
            continue
        if scanned >= MAX_FILES or len(matches) >= limit:
            break
        size = entry.get("size") or 0
        if size > MAX_BYTES:
            skipped_large += 1
            continue
        path = entry.get("path") or ""
        raw_url = (
            f"{GITEA_URL}/api/v1/repos/{quote(owner)}/{quote(repo)}/raw/"
            f"{quote(path, safe='/')}"
        )
        try:
            async with httpx.AsyncClient(timeout=GITEA_TIMEOUT) as client:
                r = await client.get(raw_url, headers=_gitea_headers(),
                                     params={"ref": ref})
        except httpx.HTTPError:
            continue
        if r.status_code != 200:
            continue
        try:
            text = r.text
        except Exception:
            continue
        scanned += 1
        if query not in text:
            continue
        for ln_no, line in enumerate(text.splitlines(), start=1):
            if query in line:
                matches.append({"path": path, "line": ln_no,
                                "text": line[:500]})
                if len(matches) >= limit:
                    break

    return {
        "matches": matches,
        "count": len(matches),
        **base,
        "scanned_files": scanned,
        "skipped_large": skipped_large,
    }


if __name__ == "__main__":
    apply_safety_mode()
    logger.info("mcp-external starting on 0.0.0.0:8002 (streamable-http)")
    logger.info("GITEA_URL=%s  HARBOR_URL=%s", GITEA_URL, HARBOR_URL)
    mcp.run(transport="streamable-http")
