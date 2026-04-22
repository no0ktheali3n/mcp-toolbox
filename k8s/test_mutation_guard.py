"""Smoke test for mutation_guard module + integration into mcp_k8s mutation tools.

Runs inside the mcp-k8s container via:
    docker exec mcp-k8s python3 /app/test_mutation_guard.py
"""
import os
import sys

sys.path.insert(0, "/app")


def ok(cond, label, detail=""):
    marker = "PASS" if cond else "FAIL"
    print(f"  [{marker}] {label}" + (f"  -- {detail}" if detail else ""))
    return cond


passes = True
import mutation_guard as g

print("=== Test 1: DEFAULT_DENYLIST has expected baseline ===")
expected = {"Secret", "ClusterRole", "ClusterRoleBinding", "ServiceAccount"}
passes &= ok(set(g.DEFAULT_DENYLIST) == expected, "DEFAULT_DENYLIST matches",
             str(g.DEFAULT_DENYLIST))

print("\n=== Test 2: is_kind_denied is case-insensitive ===")
passes &= ok(g.is_kind_denied("Secret", g.DEFAULT_DENYLIST), "Secret denied")
passes &= ok(g.is_kind_denied("secret", g.DEFAULT_DENYLIST), "secret denied")
passes &= ok(g.is_kind_denied("SECRET", g.DEFAULT_DENYLIST), "SECRET denied")
passes &= ok(not g.is_kind_denied("Deployment", g.DEFAULT_DENYLIST),
             "Deployment NOT denied")
passes &= ok(not g.is_kind_denied("", g.DEFAULT_DENYLIST), "empty string NOT denied")

print("\n=== Test 3: load_denylist_from_env default (no var) ===")
d = g.load_denylist_from_env({})
passes &= ok(d == set(g.DEFAULT_DENYLIST), "returns default when unset")

print("\n=== Test 4: load_denylist_from_env overrides with comma list ===")
d = g.load_denylist_from_env({"MCP_K8S_DENYLIST": "Secret,Pod,  Node  "})
passes &= ok(d == {"Secret", "Pod", "Node"}, "parses + strips whitespace",
             str(sorted(d)))

print("\n=== Test 5: load_denylist_from_env empty replaces default ===")
d = g.load_denylist_from_env({"MCP_K8S_DENYLIST": ""})
passes &= ok(d == set(), "empty string -> empty set")

print("\n=== Test 6: denial_response shape ===")
r = g.denial_response("Secret", "patch_resource_limits")
passes &= ok(r.get("error") == "mutation_denied", "error=mutation_denied")
passes &= ok(r.get("kind") == "Secret", "kind echoed")
passes &= ok(r.get("tool") == "patch_resource_limits", "tool echoed")
passes &= ok(r.get("reason") == "operator denylist", "reason set")
passes &= ok(r.get("denylist_env_var") == "MCP_K8S_DENYLIST", "env var name set")

print("\n=== Test 7: guard() returns dict for denied, None for allowed ===")
passes &= ok(isinstance(g.guard("Secret", "t"), dict), "guard(Secret) -> dict")
passes &= ok(g.guard("Deployment", "t") is None, "guard(Deployment) -> None")

print("\n=== Test 8: mcp_k8s imports mutation_guard cleanly ===")
try:
    import mcp_k8s
    passes &= ok(hasattr(mcp_k8s, "ACTIVE_DENYLIST"),
                 "mcp_k8s.ACTIVE_DENYLIST exposed")
    passes &= ok("rollback_deployment" in mcp_k8s.mcp._tool_manager._tools,
                 "rollback_deployment registered with mcp")
    passes &= ok("restart_deployment" in mcp_k8s.mcp._tool_manager._tools,
                 "restart_deployment still registered")
except Exception as e:
    passes &= ok(False, "mcp_k8s import failed", str(e))

print("\n=== Test 9: restart_deployment denied when Deployment is on denylist ===")
# Temporarily re-load mcp_k8s active list with Deployment denied
# by monkey-patching the module's ACTIVE_DENYLIST
import mcp_k8s
saved = set(mcp_k8s.ACTIVE_DENYLIST)
mcp_k8s.ACTIVE_DENYLIST.clear()
mcp_k8s.ACTIVE_DENYLIST.update({"Deployment"})
# also need to reset the guard's cached ref since restart_deployment uses _guard_kind
# which calls is_kind_denied(kind, ACTIVE_DENYLIST). The import of ACTIVE_DENYLIST
# in mcp_k8s is a reference to the same set, so mutating it should be visible.
# mutation_guard.ACTIVE_DENYLIST is the same set.
r = mcp_k8s.restart_deployment(name="nginx", namespace="default")
passes &= ok(isinstance(r, dict) and r.get("error") == "mutation_denied",
             "restart_deployment returns mutation_denied dict", str(r)[:160])
# restore
mcp_k8s.ACTIVE_DENYLIST.clear()
mcp_k8s.ACTIVE_DENYLIST.update(saved)

print("\n=== Test 10: patch_resource_limits denied for a denied kind ===")
# Add "Deployment" to denylist temporarily and call patch_resource_limits
saved = set(mcp_k8s.ACTIVE_DENYLIST)
mcp_k8s.ACTIVE_DENYLIST.clear()
mcp_k8s.ACTIVE_DENYLIST.update({"Deployment"})
r = mcp_k8s.patch_resource_limits(
    kind="Deployment", name="nginx", container="c",
    limits={"memory": "128Mi"}, namespace="default", dry_run=True,
)
passes &= ok(isinstance(r, dict) and r.get("error") == "mutation_denied",
             "patch_resource_limits returns mutation_denied", str(r)[:160])
mcp_k8s.ACTIVE_DENYLIST.clear()
mcp_k8s.ACTIVE_DENYLIST.update(saved)

print("\n" + "=" * 60)
print(f"OVERALL: {'ALL PASS' if passes else 'FAILURES PRESENT'}")
sys.exit(0 if passes else 1)
