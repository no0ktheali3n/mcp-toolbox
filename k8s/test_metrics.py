"""Unit tests for live resource metrics (top_pods, top_nodes) and the
quantity parsers (_parse_cpu, _parse_mem).

Pure-logic / monkeypatched (no live cluster). Plain-script house style.
metrics.k8s.io is an aggregated API served through the same CustomObjectsApi
(custom()) the generic-read tools use, so the fake mirrors that surface. Run:
    KUBECONFIG=<any kubeconfig> python3 test_metrics.py
or in-container:
    docker exec mcp-k8s python3 /app/test_metrics.py
"""
import sys
sys.path.insert(0, "/app")  # in-container import path (no-op on host)
import mcp_k8s


def ok(cond, label, detail=""):
    marker = "PASS" if cond else "FAIL"
    print(f"  [{marker}] {label}" + (f"  -- {detail}" if detail else ""))
    return cond


passes = True


class _FakeCustom:
    """Records calls; returns canned metric lists or raises. Mimics the
    metrics.k8s.io list paths on CustomObjectsApi."""
    def __init__(self, list_items=None, raise_status=None):
        self._list_items = list_items or []
        self._raise = raise_status
        self.calls = []

    def _maybe_raise(self):
        if self._raise:
            from kubernetes.client.rest import ApiException
            raise ApiException(status=self._raise, reason="x")

    def list_namespaced_custom_object(self, group, version, namespace, plural, **kw):
        self.calls.append(("list_ns", group, version, namespace, plural, kw))
        self._maybe_raise()
        return {"items": self._list_items}

    def list_cluster_custom_object(self, group, version, plural, **kw):
        self.calls.append(("list_all", group, version, plural, kw))
        self._maybe_raise()
        return {"items": self._list_items}


# ── fixtures: real metrics.k8s.io/v1beta1 shapes ────────────────────────────
NODE_ITEMS = [
    {"metadata": {"name": "node-a"}, "timestamp": "2026-06-09T00:00:00Z",
     "window": "10s", "usage": {"cpu": "143000000n", "memory": "1387520Ki"}},
    {"metadata": {"name": "node-b"}, "timestamp": "2026-06-09T00:00:00Z",
     "window": "10s", "usage": {"cpu": "2", "memory": "512Mi"}},
]
POD_ITEMS = [
    {"metadata": {"name": "pod-1", "namespace": "ns1"},
     "timestamp": "2026-06-09T00:00:00Z", "window": "10s",
     "containers": [
         {"name": "c1", "usage": {"cpu": "100m", "memory": "50Mi"}},
         {"name": "c2", "usage": {"cpu": "150m", "memory": "100Mi"}}]},
    {"metadata": {"name": "pod-2", "namespace": "ns1"},
     "timestamp": "2026-06-09T00:00:00Z", "window": "10s",
     "containers": [
         {"name": "only", "usage": {"cpu": "10m", "memory": "1Gi"}}]},
]


print("=== quantity parsers ===")
# CPU -> integer millicores (rounded)
passes &= ok(mcp_k8s._parse_cpu("143000000n") == 143, "cpu nanocores -> millicores")
passes &= ok(mcp_k8s._parse_cpu("143750000n") == 144, "cpu nanocores rounds")
passes &= ok(mcp_k8s._parse_cpu("250m") == 250, "cpu millicores passthrough")
passes &= ok(mcp_k8s._parse_cpu("2") == 2000, "cpu whole cores -> millicores")
passes &= ok(mcp_k8s._parse_cpu("") == 0 and mcp_k8s._parse_cpu(None) == 0,
             "cpu empty/None -> 0")
# memory -> integer bytes
passes &= ok(mcp_k8s._parse_mem("1387520Ki") == 1387520 * 1024, "mem Ki -> bytes")
passes &= ok(mcp_k8s._parse_mem("512Mi") == 512 * 1024 * 1024, "mem Mi -> bytes")
passes &= ok(mcp_k8s._parse_mem("1Gi") == 1024 ** 3, "mem Gi -> bytes")
passes &= ok(mcp_k8s._parse_mem("1000000") == 1000000, "mem plain -> bytes")
passes &= ok(mcp_k8s._parse_mem("1M") == 1000000, "mem decimal M -> bytes")
passes &= ok(mcp_k8s._parse_mem("") == 0 and mcp_k8s._parse_mem(None) == 0,
             "mem empty/None -> 0")

print("\n=== tool classification ===")
passes &= ok("top_nodes" in mcp_k8s.mcp._tool_manager._tools, "top_nodes registered")
passes &= ok("top_nodes" not in mcp_k8s.WRITE_TOOLS, "top_nodes is read-only")
passes &= ok("top_pods" in mcp_k8s.mcp._tool_manager._tools, "top_pods registered")
passes &= ok("top_pods" not in mcp_k8s.WRITE_TOOLS, "top_pods is read-only")

print("\n=== top_nodes happy path + sort ===")
fake = _FakeCustom(list_items=NODE_ITEMS)
mcp_k8s.custom = lambda: fake
r = mcp_k8s.top_nodes(sort_by="cpu")
passes &= ok(fake.calls[0][0] == "list_all" and fake.calls[0][1] == "metrics.k8s.io"
             and fake.calls[0][3] == "nodes", "queries metrics.k8s.io nodes (cluster)",
             str(fake.calls[0]))
passes &= ok(r.get("count") == 2, "two node rows")
passes &= ok([n["name"] for n in r["nodes"]] == ["node-b", "node-a"],
             "sorted desc by cpu", str([n["name"] for n in r["nodes"]]))
na = next(n for n in r["nodes"] if n["name"] == "node-a")
passes &= ok(na["cpu_millicores"] == 143 and na["cpu_human"] == "143m",
             "node cpu parsed + humanized", str(na))
passes &= ok(na["memory_bytes"] == 1387520 * 1024 and na["memory_human"] == "1355Mi",
             "node mem parsed + humanized", str(na))
r2 = mcp_k8s.top_nodes(sort_by="memory")
passes &= ok([n["name"] for n in r2["nodes"]] == ["node-a", "node-b"],
             "sort_by=memory reorders", str([n["name"] for n in r2["nodes"]]))

print("\n=== top_pods sum + breakdown + scope ===")
fake = _FakeCustom(list_items=POD_ITEMS)
mcp_k8s.custom = lambda: fake
r = mcp_k8s.top_pods(namespace="ns1", sort_by="cpu")
passes &= ok(fake.calls[0][0] == "list_ns" and fake.calls[0][3] == "ns1"
             and fake.calls[0][4] == "pods", "namespaced pods query", str(fake.calls[0]))
p1 = next(p for p in r["pods"] if p["name"] == "pod-1")
passes &= ok(p1["cpu_millicores"] == 250, "pod sums container cpu (100m+150m)", str(p1))
passes &= ok(p1["memory_bytes"] == 150 * 1024 * 1024, "pod sums container mem (50Mi+100Mi)")
passes &= ok("containers" not in p1, "no per-container breakdown by default")
passes &= ok(r.get("scope") == "ns1", "scope reflects namespace")
passes &= ok([p["name"] for p in r["pods"]] == ["pod-1", "pod-2"], "pods sorted desc by cpu")

r3 = mcp_k8s.top_pods(namespace="ns1", containers=True)
p1c = next(p for p in r3["pods"] if p["name"] == "pod-1")
passes &= ok(len(p1c.get("containers", [])) == 2
             and p1c["containers"][0].get("cpu_millicores") is not None,
             "containers=True returns per-container breakdown", str(p1c.get("containers"))[:90])

print("\n=== top_pods cluster-wide + limit truncation ===")
fake = _FakeCustom(list_items=POD_ITEMS)
mcp_k8s.custom = lambda: fake
r = mcp_k8s.top_pods(limit=1)
passes &= ok(fake.calls[0][0] == "list_all" and fake.calls[0][3] == "pods",
             "namespace=None -> cluster-wide pods", str(fake.calls[0]))
passes &= ok(r.get("returned") == 1 and r.get("total") == 2,
             "limit truncates + reports returned/total (no silent cap)", str(r)[:120])
passes &= ok(r.get("scope") == "cluster-wide", "cluster-wide scope label")

print("\n=== error mapping ===")
mcp_k8s.custom = lambda: _FakeCustom(raise_status=403)
r = mcp_k8s.top_nodes()
passes &= ok(r.get("error") == "permission_denied", "403 -> permission_denied", str(r)[:90])
mcp_k8s.custom = lambda: _FakeCustom(raise_status=503)
r = mcp_k8s.top_nodes()
passes &= ok(r.get("error") == "metrics_unavailable",
            "503 (no metrics-server) -> metrics_unavailable", str(r)[:90])
mcp_k8s.custom = lambda: _FakeCustom(raise_status=404)
r = mcp_k8s.top_pods(namespace="ns1")
passes &= ok(r.get("error") == "metrics_unavailable",
            "404 -> metrics_unavailable", str(r)[:90])

print("\n" + "=" * 60)
print(f"OVERALL: {'ALL PASS' if passes else 'FAILURES PRESENT'}")
sys.exit(0 if passes else 1)
