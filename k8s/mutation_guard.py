"""Operator-configurable mutation denylist for mcp-k8s.

Operators can forbid the agent from mutating specific Kubernetes resource
kinds (e.g. Secrets, ClusterRoleBindings) regardless of the MCP safety
mode. This file is the single source of truth for that policy.

Design
------
- DEFAULT_DENYLIST is the baseline - kinds whose mutation would be
  dangerous-by-default. Operators can extend or override it via the
  MCP_K8S_DENYLIST environment variable (comma-separated kinds).
- load_denylist_from_env() reads the env once at import time. The
  process-wide cached result (ACTIVE_DENYLIST) is what mutation tools
  consult. Changing the env in a running container does NOT change the
  policy - denylist is a deploy-time decision.
- is_kind_denied(kind, denylist) performs a case-insensitive match so
  callers do not have to normalize ("secret", "Secret", "SECRET" all
  match). The canonical form is PascalCase (matches the kind field in
  manifests and k8s API types).
- denial_response(kind, tool) returns a structured error dict that
  mutation tools can return verbatim. Callers never need to raise
  exceptions for denylist rejections.

Env var format
--------------
  MCP_K8S_DENYLIST=Secret,Pod,ClusterRoleBinding

Empty entries are ignored. Whitespace is stripped. Setting the env var
REPLACES the default (it does not merge) - if operators want to extend
the default, they must repeat its entries.

Tools that route through the guard
----------------------------------
- patch_resource_limits (by workload kind)
- restart_deployment    (Deployment)
- rollback_deployment   (Deployment)
- clear_finalizer       (by resource kind) - when/if added
"""
from __future__ import annotations

import os
from typing import Iterable


# Canonical PascalCase kinds. This default is intentionally small and
# high-blast-radius - these are the kinds where agent mistakes are most
# likely to cause a security or auth outage.
DEFAULT_DENYLIST: frozenset = frozenset({
    "Secret",
    "ClusterRole",
    "ClusterRoleBinding",
    "ServiceAccount",
})


# Sentinel used by load_denylist_from_env() when the env var is unset.
_UNSET = object()


def _normalize(kind: str) -> str:
    """Return the lowercase-stripped form of a kind name for matching."""
    return (kind or "").strip().lower()


def is_kind_denied(kind: str, denylist: Iterable[str]) -> bool:
    """Return True if kind is present in denylist (case-insensitive)."""
    if not kind:
        return False
    normalized = _normalize(kind)
    for entry in denylist:
        if _normalize(entry) == normalized:
            return True
    return False


def load_denylist_from_env(env: dict | None = None) -> set:
    """Load the effective denylist from the environment.

    Behaviour:
      - If MCP_K8S_DENYLIST is unset, return a mutable copy of
        DEFAULT_DENYLIST.
      - If set (even empty), replace the default entirely.
      - Whitespace and empty tokens are ignored.

    Args:
        env: Optional dict for testing. Defaults to os.environ.
    """
    source = env if env is not None else os.environ
    raw = source.get("MCP_K8S_DENYLIST", _UNSET)
    if raw is _UNSET:
        return set(DEFAULT_DENYLIST)
    tokens = [t.strip() for t in str(raw).split(",")]
    return {t for t in tokens if t}


def denial_response(kind: str, tool: str, *, extra: dict | None = None) -> dict:
    """Structured error dict returned when a mutation is denied.

    Tools return this dict directly - they do not raise. This keeps the
    wire contract uniform: a caller that sees {"error": "mutation_denied", ...}
    can branch on it without exception handling.
    """
    payload = {
        "error": "mutation_denied",
        "kind": kind,
        "tool": tool,
        "reason": "operator denylist",
        "denylist_env_var": "MCP_K8S_DENYLIST",
    }
    if extra:
        payload.update(extra)
    return payload


# Cached denylist for the life of the process. Tools import and use this.
ACTIVE_DENYLIST: set = load_denylist_from_env()


def guard(kind: str, tool: str) -> dict | None:
    """One-liner for mutation tools.

    Usage:
        denied = guard("Secret", "delete_resource")
        if denied:
            return denied

    Returns the denial dict when kind is denied, or None otherwise.
    """
    if is_kind_denied(kind, ACTIVE_DENYLIST):
        return denial_response(kind, tool)
    return None
