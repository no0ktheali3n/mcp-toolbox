"""Unit tests for analyze_cluster_capacity (Phase 0 of node-capacity management).

The core is _assemble_capacity: per-node allocatable vs SUM(requests) vs actual
usage. Scheduling headroom is governed by REQUESTS (the scheduler's view), not
usage; reclaimable = requests above actual usage (right-size to free schedulable
capacity). Pure-logic / monkeypatched. Run:
    KUBECONFIG=<any kubeconfig> python3 test_capacity_analysis.py
"""
import sys
sys.path.insert(0, "/app")
import mcp_k8s

Gi = 1024 ** 3


def ok(cond, label, detail=""):
    marker = "PASS" if cond else "FAIL"
    print(f"  [{marker}] {label}" + (f"  -- {detail}" if detail else ""))
    return cond


passes = True

# nodeA: 16 cores / 64Gi; nodeB: 16 cores / 64Gi
NODES = [
    {"name": "nodeA", "cpu_alloc_milli": 16000, "mem_alloc_bytes": 64 * Gi},
    {"name": "nodeB", "cpu_alloc_milli": 16000, "mem_alloc_bytes": 64 * Gi},
]
# nodeA: 14000m / 12Gi requested; nodeB: 2000m / 2Gi
POD_REQS = [
    {"node": "nodeA", "cpu_milli": 8000, "mem_bytes": 8 * Gi},
    {"node": "nodeA", "cpu_milli": 6000, "mem_bytes": 4 * Gi},
    {"node": "nodeB", "cpu_milli": 2000, "mem_bytes": 2 * Gi},
]
USAGE = {"nodeA": {"cpu_milli": 4000, "mem_bytes": 8 * Gi},
         "nodeB": {"cpu_milli": 1000, "mem_bytes": 1 * Gi}}

print("=== _assemble_capacity: per-node CPU math ===")
r = mcp_k8s._assemble_capacity(NODES, POD_REQS, USAGE)
byname = {n["name"]: n for n in r["nodes"]}
a = byname["nodeA"]["cpu"]
passes &= ok(a["allocatable_milli"] == 16000 and a["requests_milli"] == 14000,
             "nodeA cpu allocatable + summed requests", str(a))
passes &= ok(a["requests_pct"] == 87.5, "nodeA cpu requests_pct = 87.5 (scheduler's view)", str(a["requests_pct"]))
passes &= ok(a["usage_pct"] == 25.0, "nodeA cpu usage_pct = 25 (actual)")
passes &= ok(a["schedulable_milli"] == 2000, "nodeA schedulable cpu = alloc-requests = 2000m")
passes &= ok(a["reclaimable_milli"] == 10000, "nodeA reclaimable cpu = requests-usage = 10000m")

b = byname["nodeB"]["cpu"]
passes &= ok(b["requests_pct"] == 12.5 and b["schedulable_milli"] == 14000,
             "nodeB cpu 12.5% committed, 14000m schedulable", str(b))

print("\n=== nodes sorted most-committed first (cpu requests) ===")
passes &= ok([n["name"] for n in r["nodes"]] == ["nodeA", "nodeB"],
             "nodeA (87.5%) before nodeB (12.5%)", str([n["name"] for n in r["nodes"]]))

print("\n=== cluster rollup ===")
c = r["cluster"]
passes &= ok(c["cpu"]["allocatable_milli"] == 32000 and c["cpu"]["requests_milli"] == 16000,
             "cluster cpu alloc 32000 / requests 16000", str(c["cpu"]))
passes &= ok(c["cpu"]["schedulable_milli"] == 16000, "cluster schedulable cpu = 16000m")
passes &= ok(c["cpu"]["reclaimable_milli"] == 11000,
             "cluster reclaimable cpu = (14000-4000)+(2000-1000) = 11000m", str(c["cpu"]["reclaimable_milli"]))
passes &= ok(c["nodes_near_cpu_capacity"] == ["nodeA"],
             "nodeA flagged near cpu capacity (>85% requests)", str(c["nodes_near_cpu_capacity"]))
passes &= ok(c["most_committed_node"] == "nodeA" and c["least_committed_node"] == "nodeB",
             "imbalance: most=nodeA least=nodeB (the rebalance signal)")

print("\n=== mem path sanity ===")
am = byname["nodeA"]["mem"]
passes &= ok(am["requests_bytes"] == 12 * Gi and am["allocatable_bytes"] == 64 * Gi,
             "nodeA mem requests 12Gi / alloc 64Gi", str(am.get("requests_bytes")))
passes &= ok(am["reclaimable_bytes"] == 12 * Gi - 8 * Gi, "nodeA reclaimable mem = 12Gi-8Gi usage = 4Gi")

print("\n=== _pod_requests sums container requests ===")
pod = {"spec": {"nodeName": "nodeA", "containers": [
    {"resources": {"requests": {"cpu": "500m", "memory": "256Mi"}}},
    {"resources": {"requests": {"cpu": "1", "memory": "1Gi"}}}]}}
pr = mcp_k8s._pod_requests(pod)
passes &= ok(pr["node"] == "nodeA" and pr["cpu_milli"] == 1500,
             "_pod_requests sums cpu (500m+1=1500m)", str(pr))
passes &= ok(pr["mem_bytes"] == 256 * 1024 * 1024 + 1 * Gi, "_pod_requests sums mem (256Mi+1Gi)")

print("\n=== tool registered + read-only ===")
passes &= ok("analyze_cluster_capacity" in mcp_k8s.mcp._tool_manager._tools, "registered")
passes &= ok("analyze_cluster_capacity" not in mcp_k8s.WRITE_TOOLS, "read-only (not in WRITE_TOOLS)")

print("\n" + "=" * 60)
print(f"OVERALL: {'ALL PASS' if passes else 'FAILURES PRESENT'}")
sys.exit(0 if passes else 1)
