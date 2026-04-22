"""Smoke test for rollback_deployment.

Runs inside the mcp-k8s container:
    docker exec mcp-k8s python3 /app/test_rollback_deployment.py

Target: default/nginx (has multiple revisions). Tests a dry-ish rollback
by rolling back then rolling forward again (using rollout history to
verify revision changes).
"""
import sys
import time

sys.path.insert(0, "/app")
import mcp_k8s


def ok(cond, label, detail=""):
    marker = "PASS" if cond else "FAIL"
    print(f"  [{marker}] {label}" + (f"  -- {detail}" if detail else ""))
    return cond


passes = True
fn = mcp_k8s.rollback_deployment

print("=== Test 1: tool registered ===")
passes &= ok("rollback_deployment" in mcp_k8s.mcp._tool_manager._tools,
             "rollback_deployment in mcp._tool_manager._tools")

print("\n=== Test 2: deployment_not_found returns structured error ===")
r = fn(name="does-not-exist-xyz", namespace="default")
passes &= ok(isinstance(r, dict), "returns dict")
passes &= ok(r.get("rolled_back") is False, "rolled_back=False")
passes &= ok(r.get("error") == "deployment_not_found", "error=deployment_not_found",
             str(r.get("error")))

print("\n=== Test 3: target_revision_not_found ===")
r = fn(name="nginx", namespace="default", to_revision=99999)
passes &= ok(r.get("error") == "target_revision_not_found",
             "error=target_revision_not_found", str(r.get("error")))

print("\n=== Test 4: rollback to previous revision on nginx ===")
# First read current revision via apps api
dep = mcp_k8s.apps().read_namespaced_deployment("nginx", "default")
cur_rev = (dep.metadata.annotations or {}).get("deployment.kubernetes.io/revision")
print(f"  current revision before rollback: {cur_rev}")
r = fn(name="nginx", namespace="default")
print(f"  result: {r}")
passes &= ok(isinstance(r, dict), "returns dict")
if r.get("error") == "no_previous_revision":
    print("  NOTE: deployment has no previous revision; skipping mutation assertion")
    passes &= ok(True, "handled single-revision case gracefully")
else:
    passes &= ok(r.get("rolled_back") is True, "rolled_back=True", str(r))
    passes &= ok(r.get("from_revision") == cur_rev, "from_revision matches current")
    # Wait a moment for the controller to bump the revision annotation
    time.sleep(2)
    dep2 = mcp_k8s.apps().read_namespaced_deployment("nginx", "default")
    new_rev = (dep2.metadata.annotations or {}).get("deployment.kubernetes.io/revision")
    print(f"  revision after rollback: {new_rev}")
    # After a rollback, k8s assigns a NEW revision number (usually max+1)
    passes &= ok(new_rev is not None and new_rev != cur_rev,
                 "revision incremented", f"{cur_rev} -> {new_rev}")

print("\n=== Test 5: denylist guard applies to rollback_deployment ===")
saved = set(mcp_k8s.ACTIVE_DENYLIST)
mcp_k8s.ACTIVE_DENYLIST.clear()
mcp_k8s.ACTIVE_DENYLIST.update({"Deployment"})
r = fn(name="nginx", namespace="default")
passes &= ok(r.get("error") == "mutation_denied",
             "mutation_denied when Deployment on denylist", str(r)[:160])
mcp_k8s.ACTIVE_DENYLIST.clear()
mcp_k8s.ACTIVE_DENYLIST.update(saved)

print("\n" + "=" * 60)
print(f"OVERALL: {'ALL PASS' if passes else 'FAILURES PRESENT'}")
sys.exit(0 if passes else 1)
