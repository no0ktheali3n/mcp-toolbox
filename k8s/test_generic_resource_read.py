"""Unit tests for generic custom-resource read (get_custom_resource,
list_custom_resources), describe_resource, and the storage-kind additions to
get_resource_yaml.

Pure-logic / monkeypatched (no live cluster). Plain-script house style. Run:
    KUBECONFIG=<any kubeconfig> python3 test_generic_resource_read.py
or in-container:
    docker exec mcp-k8s python3 /app/test_generic_resource_read.py
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
    """Records calls; returns canned objects or raises. Mimics CustomObjectsApi."""
    def __init__(self, get_obj=None, list_items=None, raise_status=None):
        self._get_obj = get_obj
        self._list_items = list_items or []
        self._raise = raise_status
        self.calls = []

    def _maybe_raise(self):
        if self._raise:
            from kubernetes.client.rest import ApiException
            raise ApiException(status=self._raise, reason="x")

    def get_namespaced_custom_object(self, group, version, namespace, plural, name):
        self.calls.append(("get_ns", group, version, namespace, plural, name))
        self._maybe_raise()
        return self._get_obj

    def get_cluster_custom_object(self, group, version, plural, name):
        self.calls.append(("get_all", group, version, plural, name))
        self._maybe_raise()
        return self._get_obj

    def list_namespaced_custom_object(self, group, version, namespace, plural, **kw):
        self.calls.append(("list_ns", group, version, namespace, plural, kw))
        self._maybe_raise()
        return {"items": self._list_items}

    def list_cluster_custom_object(self, group, version, plural, **kw):
        self.calls.append(("list_all", group, version, plural, kw))
        self._maybe_raise()
        return {"items": self._list_items}


print("=== D1: tool classification ===")
passes &= ok("get_custom_resource" in mcp_k8s.mcp._tool_manager._tools,
             "get_custom_resource registered")
passes &= ok("get_custom_resource" not in mcp_k8s.WRITE_TOOLS,
             "get_custom_resource is read-only (not in WRITE_TOOLS)")
passes &= ok("list_custom_resources" in mcp_k8s.mcp._tool_manager._tools,
             "list_custom_resources registered")
passes &= ok("list_custom_resources" not in mcp_k8s.WRITE_TOOLS,
             "list_custom_resources is read-only")

print("\n=== D1: get_custom_resource happy path (namespaced) ===")
hr = {"apiVersion": "helm.toolkit.fluxcd.io/v2", "kind": "HelmRelease",
      "metadata": {"name": "app", "namespace": "ns",
                   "managedFields": [{"x": 1}]},
      "spec": {"chart": {}},
      "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
fake = _FakeCustom(get_obj=hr)
mcp_k8s.custom = lambda: fake
r = mcp_k8s.get_custom_resource(group="helm.toolkit.fluxcd.io", version="v2",
                               plural="helmreleases", name="app", namespace="ns")
passes &= ok(r.get("manifest", {}).get("kind") == "HelmRelease",
             "returns the manifest", str(r)[:90])
passes &= ok("managedFields" not in r["manifest"].get("metadata", {}),
             "managedFields stripped")
passes &= ok(fake.calls[0][0] == "get_ns" and fake.calls[0][4] == "helmreleases",
             "namespaced get to correct GVR", str(fake.calls[0]))

print("\n=== D1: get_custom_resource cluster-scoped (no namespace) ===")
cn = {"kind": "CiliumNode", "metadata": {"name": "n1"}}
fake = _FakeCustom(get_obj=cn)
mcp_k8s.custom = lambda: fake
r = mcp_k8s.get_custom_resource(group="cilium.io", version="v2",
                               plural="ciliumnodes", name="n1")
passes &= ok(fake.calls[0][0] == "get_all", "no namespace -> cluster-scoped get",
             str(fake.calls[0]))
passes &= ok(r.get("manifest", {}).get("kind") == "CiliumNode",
             "cluster get returns manifest")

print("\n=== D1: get_custom_resource error mapping ===")
mcp_k8s.custom = lambda: _FakeCustom(raise_status=404)
r = mcp_k8s.get_custom_resource(group="g", version="v", plural="ps",
                               name="ghost", namespace="ns")
passes &= ok(r.get("error") == "not_found", "404 -> not_found", str(r)[:90])
mcp_k8s.custom = lambda: _FakeCustom(raise_status=403)
r = mcp_k8s.get_custom_resource(group="g", version="v", plural="ps",
                               name="x", namespace="ns")
passes &= ok(r.get("error") == "permission_denied", "403 -> permission_denied",
             str(r)[:90])

print("\n=== D1: list_custom_resources ===")
items = [{"metadata": {"name": "a", "namespace": "ns"}},
         {"metadata": {"name": "b", "namespace": "ns"}}]
fake = _FakeCustom(list_items=items)
mcp_k8s.custom = lambda: fake
r = mcp_k8s.list_custom_resources(group="g", version="v", plural="ps", namespace="ns")
passes &= ok(r.get("count") == 2 and {i["name"] for i in r["items"]} == {"a", "b"},
             "namespaced list returns items", str(r)[:90])
passes &= ok(fake.calls[0][0] == "list_ns", "namespaced list path")

fake = _FakeCustom(list_items=items)
mcp_k8s.custom = lambda: fake
r = mcp_k8s.list_custom_resources(group="g", version="v", plural="ps")
passes &= ok(fake.calls[0][0] == "list_all", "no namespace -> cluster-wide list")

mcp_k8s.custom = lambda: _FakeCustom(raise_status=403)
r = mcp_k8s.list_custom_resources(group="g", version="v", plural="ps", namespace="ns")
passes &= ok(r.get("error") == "permission_denied", "list 403 -> permission_denied",
             str(r)[:90])

print("\n=== D3: storage kinds in get_resource_yaml ===")
d = mcp_k8s._YAML_KIND_DISPATCH
passes &= ok("volumeattachment" in d, "volumeattachment in YAML dispatch")
passes &= ok("storageclass" in d, "storageclass in YAML dispatch")
passes &= ok("volumeattachment" in d and d["volumeattachment"][1] == "storage",
             "volumeattachment uses storage api")
passes &= ok("volumeattachment" in d and d["volumeattachment"][2] is False,
             "volumeattachment is cluster-scoped")


class _Obj:
    def __init__(self, dd): self._d = dd
    def to_dict(self): return self._d


class _FakeStorage:
    def __init__(self): self.calls = []
    def read_volume_attachment(self, name):
        self.calls.append(("read_volume_attachment", name))
        return _Obj({"kind": "VolumeAttachment",
                     "metadata": {"name": name, "managedFields": [1]}})


fs = _FakeStorage()
mcp_k8s.storage = lambda: fs
r = mcp_k8s.get_resource_yaml(kind="volumeattachment", name="va1")
passes &= ok(r.get("manifest", {}).get("kind") == "VolumeAttachment",
             "get_resource_yaml reads VolumeAttachment", str(r)[:90])
passes &= ok("managedFields" not in r.get("manifest", {}).get("metadata", {}),
             "VolumeAttachment managedFields stripped")
passes &= ok(fs.calls and fs.calls[0][0] == "read_volume_attachment",
             "called read_volume_attachment")

print("\n=== D2: describe_resource ===")
passes &= ok("describe_resource" in mcp_k8s.mcp._tool_manager._tools,
             "describe_resource registered")
passes &= ok("describe_resource" not in mcp_k8s.WRITE_TOOLS,
             "describe_resource is read-only")


class _Ev:
    def __init__(self, **k): self.__dict__.update(k)


class _EvList:
    def __init__(self, items): self.items = items


class _FakeCore:
    def __init__(self): self.calls = []
    def list_namespaced_event(self, namespace, field_selector=None):
        self.calls.append(("ns", namespace, field_selector))
        return _EvList([_Ev(type="Warning", reason="FailedScheduling",
                            message="0/3 nodes available", count=5,
                            last_timestamp="2026-06-08T00:00:00Z")])
    def list_event_for_all_namespaces(self, field_selector=None):
        self.calls.append(("all", field_selector))
        return _EvList([])


mcp_k8s.get_resource_yaml = lambda kind, name, namespace=None: {
    "kind": kind, "name": name, "namespace": namespace,
    "manifest": {"kind": "Pod", "metadata": {"name": name, "namespace": namespace},
                 "status": {"conditions": [
                     {"type": "Ready", "status": "False", "reason": "Unschedulable"}]}}}
fc = _FakeCore()
mcp_k8s.core = lambda: fc
r = mcp_k8s.describe_resource(kind="pod", name="p1", namespace="ns1")
passes &= ok([c.get("reason") for c in r.get("conditions", [])] == ["Unschedulable"],
             "conditions extracted from manifest.status", str(r.get("conditions"))[:90])
passes &= ok(len(r.get("events", [])) == 1 and r["events"][0]["reason"] == "FailedScheduling",
             "involvedObject events attached", str(r.get("events"))[:90])
passes &= ok(fc.calls and "involvedObject.name=p1" in fc.calls[0][2]
             and "involvedObject.kind=Pod" in fc.calls[0][2],
             "event field selector by name+kind", str(fc.calls[0]))

mcp_k8s.get_resource_yaml = lambda kind, name, namespace=None: {"error": "not_found", "kind": kind}
r = mcp_k8s.describe_resource(kind="pod", name="ghost", namespace="ns1")
passes &= ok(r.get("error") == "not_found", "describe passes through read error", str(r)[:90])

mcp_k8s.get_resource_yaml = lambda kind, name, namespace=None: {
    "manifest": {"kind": "Pod", "status": {}}}


class _ForbiddenCore:
    def list_namespaced_event(self, namespace, field_selector=None):
        from kubernetes.client.rest import ApiException
        raise ApiException(status=403, reason="Forbidden")
    def list_event_for_all_namespaces(self, field_selector=None):
        from kubernetes.client.rest import ApiException
        raise ApiException(status=403, reason="Forbidden")


mcp_k8s.core = lambda: _ForbiddenCore()
r = mcp_k8s.describe_resource(kind="pod", name="p1", namespace="ns1")
passes &= ok(r.get("events") == [] and "error" not in r,
             "events 403 -> graceful empty, no top-level error", str(r)[:90])

print("\n" + "=" * 60)
print(f"OVERALL: {'ALL PASS' if passes else 'FAILURES PRESENT'}")
sys.exit(0 if passes else 1)
