"""P5-BLOCKER #14 smoke test for patch_resource_limits.

Imports the module directly inside the mcp-k8s container and calls the
function to exercise guardrails + k8s API integration. Skips the MCP
transport layer — that's intentional; we're testing the tool's logic,
not FastMCP's wire protocol.
"""
import json
import sys

sys.path.insert(0, "/app")
import mcp_k8s  # registers tools + loads kubeconfig on import

fn = mcp_k8s.patch_resource_limits


def ok(cond: bool, label: str, detail: str = ""):
    marker = "PASS" if cond else "FAIL"
    print(f"  [{marker}] {label}" + (f"  -- {detail}" if detail else ""))
    return cond


passes = True


print("=== Test 1: tool registered under MCP name ===")
try:
    # Internal inspection — FastMCP stores under _tool_manager._tools
    registered = "patch_resource_limits" in mcp_k8s.mcp._tool_manager._tools
except Exception:
    registered = False
passes &= ok(registered, "patch_resource_limits in mcp._tool_manager._tools")


print()
print("=== Test 2: dry_run on fixture-b-oom (expect accept) ===")
r = fn(
    kind="Deployment", name="fixture-b-oom", container="stress",
    limits={"memory": "128Mi"}, namespace="default", dry_run=True,
)
print(f"  before={r.get('before')}")
print(f"  after={r.get('after')}")
passes &= ok(r.get("applied") is False, "applied=False", f"got {r.get('applied')}")
passes &= ok(r.get("dry_run") is True, "dry_run=True")
passes &= ok(r.get("reason_rejected") is None, "not rejected", str(r.get("reason_rejected")))
passes &= ok(r.get("before") is not None, "before state captured")


print()
print("=== Test 3: absolute ceiling rejection (10Gi > 2Gi) ===")
r = fn(
    kind="Deployment", name="fixture-b-oom", container="stress",
    limits={"memory": "10Gi"}, namespace="default", dry_run=True,
)
passes &= ok(r.get("reason_rejected") == "memory_exceeds_ceiling",
             "memory_exceeds_ceiling", str(r.get("reason_rejected")))


print()
print("=== Test 4: multiplier rejection (512Mi > 2x 64Mi) ===")
r = fn(
    kind="Deployment", name="fixture-b-oom", container="stress",
    limits={"memory": "512Mi"}, namespace="default", dry_run=True,
)
passes &= ok(r.get("reason_rejected") == "memory_exceeds_multiplier",
             "memory_exceeds_multiplier", str(r.get("reason_rejected")))


print()
print("=== Test 5: container_not_found ===")
r = fn(
    kind="Deployment", name="fixture-b-oom", container="nonexistent",
    limits={"memory": "128Mi"}, namespace="default", dry_run=True,
)
passes &= ok(r.get("reason_rejected") == "container_not_found",
             "container_not_found", str(r.get("reason_rejected")))


print()
print("=== Test 6: unsupported_kind (Pod) ===")
r = fn(
    kind="Pod", name="whatever", container="c",
    limits={"memory": "128Mi"}, namespace="default", dry_run=True,
)
passes &= ok(r.get("reason_rejected") == "unsupported_kind",
             "unsupported_kind", str(r.get("reason_rejected")))


print()
print("=== Test 7: requests > limit rejection ===")
r = fn(
    kind="Deployment", name="fixture-b-oom", container="stress",
    limits={"memory": "128Mi"}, requests={"memory": "256Mi"},
    namespace="default", dry_run=True,
)
passes &= ok(r.get("reason_rejected") == "memory_request_exceeds_limit",
             "memory_request_exceeds_limit", str(r.get("reason_rejected")))


print()
print("=== Test 8: REAL APPLY — 64Mi -> 128Mi (exactly 2x current) ===")
r = fn(
    kind="Deployment", name="fixture-b-oom", container="stress",
    limits={"memory": "128Mi"}, namespace="default", dry_run=False,
)
print(f"  before={r.get('before')}  after={r.get('after')}  applied={r.get('applied')}")
passes &= ok(r.get("applied") is True, "applied=True", str(r.get("reason_rejected")))


print()
print("=" * 60)
print(f"OVERALL: {'ALL PASS' if passes else 'FAILURES PRESENT'}")
sys.exit(0 if passes else 1)
