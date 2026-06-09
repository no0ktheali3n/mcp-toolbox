"""Unit tests for propose_patch (remediation-prep) and its diff helpers.

propose_patch NEVER persists: it server-side dry-runs (dryRun=All) where the
agent has patch RBAC, else computes the change client-side, and returns the
before->after diff + the exact patch for operator approval. read_only; NOT in
WRITE_TOOLS. Pure-logic / monkeypatched. Run:
    KUBECONFIG=<any kubeconfig> python3 test_propose_patch.py
or in-container:
    docker exec mcp-k8s python3 /app/test_propose_patch.py
"""
import sys
sys.path.insert(0, "/app")
import mcp_k8s


def ok(cond, label, detail=""):
    marker = "PASS" if cond else "FAIL"
    print(f"  [{marker}] {label}" + (f"  -- {detail}" if detail else ""))
    return cond


passes = True

print("=== _deep_diff ===")
d = mcp_k8s._deep_diff({"a": {"b": 1}}, {"a": {"b": 2}})
passes &= ok(d == [{"path": "a.b", "from": 1, "to": 2}], "nested scalar change", str(d))
d = mcp_k8s._deep_diff({"a": 1, "b": 2}, {"a": 1, "b": 3})
passes &= ok(d == [{"path": "b", "from": 2, "to": 3}], "unchanged paths excluded", str(d))
d = mcp_k8s._deep_diff({"c": [{"x": 1}]}, {"c": [{"x": 2}]})
passes &= ok(d == [{"path": "c[0].x", "from": 1, "to": 2}], "list index change", str(d))
d = mcp_k8s._deep_diff({}, {"a": 1})
passes &= ok(d == [{"path": "a", "from": None, "to": 1}], "added key (from None)", str(d))
passes &= ok(mcp_k8s._deep_diff({"a": 1}, {"a": 1}) == [], "identical -> empty diff")

print("\n=== _strip_volatile ===")
sv = mcp_k8s._strip_volatile({"metadata": {"name": "x", "resourceVersion": "9", "managedFields": [1]},
                              "spec": {"r": 1}, "status": {"phase": "Running"}})
passes &= ok("resourceVersion" not in sv["metadata"] and "managedFields" not in sv["metadata"],
             "strips resourceVersion + managedFields", str(sv))
passes &= ok("status" not in sv, "strips status")
passes &= ok(sv.get("spec") == {"r": 1}, "keeps spec")

print("\n=== classification ===")
passes &= ok("propose_patch" in mcp_k8s.mcp._tool_manager._tools, "propose_patch registered")
passes &= ok("propose_patch" not in mcp_k8s.WRITE_TOOLS,
             "propose_patch NOT in WRITE_TOOLS (non-mutating, survives read-only)")

CURRENT = {"kind": "Deployment",
           "metadata": {"name": "app", "namespace": "ns", "resourceVersion": "1", "managedFields": [1]},
           "spec": {"template": {"spec": {"containers": [
               {"name": "c", "resources": {"limits": {"memory": "256Mi"}}}]}}}}
PATCH = {"spec": {"template": {"spec": {"containers": [
    {"name": "c", "resources": {"limits": {"memory": "512Mi"}}}]}}}}


def _after_memory(mem):
    import json
    a = json.loads(json.dumps(CURRENT))
    a["metadata"]["resourceVersion"] = "2"
    a["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"] = mem
    return a


print("\n=== propose_patch happy path (server-side dry-run) ===")
calls = []


def _fake_dry_run(kind, name, namespace, patch):
    calls.append((kind, name, namespace))
    return _after_memory("512Mi")


mcp_k8s.get_resource_yaml = lambda kind, name, namespace=None: {"manifest": CURRENT}
mcp_k8s._dry_run_patch = _fake_dry_run
mcp_k8s._guard_kind = lambda kind, op: None
r = mcp_k8s.propose_patch(kind="Deployment", name="app", namespace="ns", patch=PATCH)
passes &= ok(r.get("applied") is False and r.get("requires_operator") is True,
             "applied=False, requires_operator=True", str(r)[:80])
passes &= ok(r.get("validated") is True, "validated=True (server dry-run path)")
passes &= ok(len(calls) == 1, "_dry_run_patch invoked once")
mem_diffs = [x for x in r.get("diff", []) if x["path"].endswith("memory")]
passes &= ok(mem_diffs and mem_diffs[0]["from"] == "256Mi" and mem_diffs[0]["to"] == "512Mi",
             "diff shows 256Mi -> 512Mi", str(mem_diffs))
passes &= ok(not any("resourceVersion" in x["path"] for x in r.get("diff", [])),
             "volatile fields excluded from diff")
passes &= ok(r.get("target") == {"kind": "Deployment", "name": "app", "namespace": "ns"},
             "target echoed")

print("\n=== mutation_guard denylist (no read, returns denial) ===")
read_calls = []
mcp_k8s.get_resource_yaml = lambda kind, name, namespace=None: read_calls.append(1) or {"manifest": {}}
mcp_k8s._guard_kind = lambda kind, op: {"error": "mutation_denied", "kind": kind}
r = mcp_k8s.propose_patch(kind="Secret", name="s", namespace="ns", patch={"data": {"x": "y"}})
passes &= ok(r.get("error") == "mutation_denied", "denied kind -> mutation_denied", str(r)[:80])
passes &= ok(len(read_calls) == 0, "no resource read after guard denial (no API call)")

print("\n=== 403 on dry-run -> client-side fallback (validated=False) ===")
from kubernetes.client.rest import ApiException


def _dry_run_403(kind, name, namespace, patch):
    raise ApiException(status=403, reason="Forbidden")


mcp_k8s.get_resource_yaml = lambda kind, name, namespace=None: {"manifest": CURRENT}
mcp_k8s._dry_run_patch = _dry_run_403
mcp_k8s._guard_kind = lambda kind, op: None
r = mcp_k8s.propose_patch(kind="Deployment", name="app", namespace="ns", patch=PATCH)
passes &= ok(r.get("validated") is False, "403 -> validated=False (client-side computed)")
passes &= ok(r.get("applied") is False, "still applied=False")
mem_diffs = [x for x in r.get("diff", []) if x["path"].endswith("memory")]
passes &= ok(mem_diffs and mem_diffs[0]["to"] == "512Mi",
             "client-side merge still yields the diff", str(mem_diffs))

print("\n=== read error passthrough ===")
mcp_k8s.get_resource_yaml = lambda kind, name, namespace=None: {"error": "not_found", "kind": kind}
mcp_k8s._guard_kind = lambda kind, op: None
r = mcp_k8s.propose_patch(kind="Deployment", name="ghost", namespace="ns", patch=PATCH)
passes &= ok(r.get("error") == "not_found", "read error passes through", str(r)[:80])

print("\n" + "=" * 60)
print(f"OVERALL: {'ALL PASS' if passes else 'FAILURES PRESENT'}")
sys.exit(0 if passes else 1)
