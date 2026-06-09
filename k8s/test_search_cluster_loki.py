"""Unit tests for search_resources, get_cluster_summary, query_loki, search_logs.
Pure-logic / monkeypatched (no live cluster), plain-script house style. Run:
    KUBECONFIG=<any kubeconfig> python3 test_search_cluster_loki.py
"""
import json
import sys
sys.path.insert(0, "/app")
import mcp_k8s


def ok(cond, label, detail=""):
    marker = "PASS" if cond else "FAIL"
    print(f"  [{marker}] {label}" + (f"  -- {detail}" if detail else ""))
    return cond


passes = True


class _O:
    def __init__(self, **k): self.__dict__.update(k)


class _Lst:
    def __init__(self, items, cont=None):
        self.items = items
        self.metadata = _O(_continue=cont)


# ───────────────────────── search_resources ─────────────────────────
print("=== search_resources ===")
passes &= ok("search_resources" in mcp_k8s.mcp._tool_manager._tools, "registered")
passes &= ok("search_resources" not in mcp_k8s.WRITE_TOOLS, "read-only")
passes &= ok("pod" in mcp_k8s._SEARCH_KIND_DISPATCH, "_SEARCH_KIND_DISPATCH has pod")


class _SearchCore:
    def __init__(self): self.calls = []
    def list_namespaced_pod(self, namespace, **kw):
        self.calls.append(("ns", namespace, kw))
        return _Lst([_O(metadata=_O(name="p1", namespace=namespace, creation_timestamp=None))])
    def list_pod_for_all_namespaces(self, **kw):
        self.calls.append(("all", kw))
        return _Lst([_O(metadata=_O(name="p1", namespace="a", creation_timestamp=None)),
                     _O(metadata=_O(name="p2", namespace="b", creation_timestamp=None))])


sc = _SearchCore()
mcp_k8s.core = lambda: sc
r = mcp_k8s.search_resources(kind="pod", namespace="ns")
passes &= ok(r.get("count") == 1 and r["items"][0]["name"] == "p1" and sc.calls[0][0] == "ns",
             "namespaced search", str(r)[:90])
sc = _SearchCore(); mcp_k8s.core = lambda: sc
r = mcp_k8s.search_resources(kind="pod")
passes &= ok(r.get("count") == 2 and sc.calls[0][0] == "all", "cluster-wide search")
r = mcp_k8s.search_resources(kind="frobnicator")
passes &= ok(r.get("error") == "unsupported_kind", "unsupported kind", str(r)[:90])


class _ForbiddenSearch:
    def list_pod_for_all_namespaces(self, **kw):
        from kubernetes.client.rest import ApiException
        raise ApiException(status=403, reason="Forbidden")


mcp_k8s.core = lambda: _ForbiddenSearch()
r = mcp_k8s.search_resources(kind="pod")
passes &= ok(r.get("error") == "permission_denied", "403 -> permission_denied", str(r)[:90])


# ───────────────────────── get_cluster_summary ─────────────────────────
print("\n=== get_cluster_summary ===")
passes &= ok("get_cluster_summary" in mcp_k8s.mcp._tool_manager._tools, "registered")
passes &= ok("get_cluster_summary" not in mcp_k8s.WRITE_TOOLS, "read-only")

node_ready = _O(status=_O(conditions=[_O(type="Ready", status="True")]))
node_nr = _O(status=_O(conditions=[_O(type="Ready", status="False")]))
pod_run = _O(status=_O(phase="Running", container_statuses=None))
pod_cl = _O(status=_O(phase="Running",
                      container_statuses=[_O(state=_O(waiting=_O(reason="CrashLoopBackOff")))]))
dep_av = _O(status=_O(conditions=[_O(type="Available", status="True", reason=None)]))
dep_stuck = _O(status=_O(conditions=[_O(type="Progressing", status="False",
                                        reason="ProgressDeadlineExceeded")]))


class _SummCore:
    def list_node(self): return _Lst([node_ready, node_nr])
    def list_pod_for_all_namespaces(self): return _Lst([pod_run, pod_cl])
    def list_namespace(self): return _Lst([_O(), _O(), _O()])


class _SummApps:
    def list_deployment_for_all_namespaces(self): return _Lst([dep_av, dep_stuck])


mcp_k8s.core = lambda: _SummCore()
mcp_k8s.apps = lambda: _SummApps()
r = mcp_k8s.get_cluster_summary()
passes &= ok(r["nodes"] == {"ready": 1, "not_ready": 1, "total": 2}, "node counts", str(r["nodes"]))
passes &= ok(r["pods"]["running"] == 2 and r["pods"]["crashloop"] == 1, "pod counts", str(r["pods"]))
passes &= ok(r["deployments"]["available"] == 1 and r["deployments"]["stuck"] == 1,
             "deployment counts", str(r["deployments"]))
passes &= ok(r["namespace_count"] == 3, "namespace count")


# ───────────────────────── query_loki + search_logs ─────────────────────────
print("\n=== query_loki / search_logs ===")
passes &= ok("query_loki" in mcp_k8s.mcp._tool_manager._tools, "query_loki registered")
passes &= ok("search_logs" in mcp_k8s.mcp._tool_manager._tools, "search_logs registered")
passes &= ok("query_loki" not in mcp_k8s.WRITE_TOOLS, "query_loki read-only")


class _Resp:
    def __init__(self, payload): self.data = json.dumps(payload).encode("utf-8")


class _ApiClient:
    def __init__(self, payload, raise_status=None):
        self._p, self._raise, self.calls = payload, raise_status, []
    def call_api(self, path, method, **kw):
        self.calls.append((path, method, kw))
        if self._raise:
            from kubernetes.client.rest import ApiException
            raise ApiException(status=self._raise, reason="boom")
        return (_Resp(self._p), 200, {})


class _LokiCore:
    def __init__(self, payload, raise_status=None):
        self.api_client = _ApiClient(payload, raise_status)


payload = {"data": {"resultType": "streams",
                    "result": [{"stream": {"app": "x"}, "values": [["t", "line"]]}]}}
lc = _LokiCore(payload)
mcp_k8s.core = lambda: lc
r = mcp_k8s.query_loki(query='{app="x"}')
passes &= ok(r.get("stream_count") == 1 and r["streams"][0]["labels"] == {"app": "x"},
             "query_loki parses streams", str(r)[:90])
passes &= ok("services/loki:3100/proxy/loki/api/v1/query_range" in lc.api_client.calls[0][0],
             "proxy resource path", lc.api_client.calls[0][0])
mcp_k8s.core = lambda: _LokiCore(payload, raise_status=500)
r = mcp_k8s.query_loki(query='{app="x"}')
passes &= ok(r.get("error") == "loki_api_error", "loki ApiException -> loki_api_error", str(r)[:90])

captured = {}
mcp_k8s.query_loki = lambda query, start=None, end=None, limit=100: (
    captured.update(query=query, start=start, end=end, limit=limit) or {"ok": 1})
mcp_k8s.search_logs(namespace="ns", pattern="ERROR", time_range_seconds=60, limit=50)
passes &= ok('namespace="ns"' in captured["query"] and '|~ "ERROR"' in captured["query"],
             "search_logs builds LogQL selector", captured.get("query"))
passes &= ok(captured["limit"] == 50 and bool(captured["start"]) and bool(captured["end"]),
             "search_logs passes window + limit")

print("\n" + "=" * 60)
print(f"OVERALL: {'ALL PASS' if passes else 'FAILURES PRESENT'}")
sys.exit(0 if passes else 1)
