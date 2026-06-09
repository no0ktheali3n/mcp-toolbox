"""Smoke test for the Flux reconciliation tools (get_flux_status,
reconcile_flux_resource) and their pure helpers.

Run on the host (local k3d has no Flux, so we test logic with synthetic
inputs and a fake CustomObjectsApi):
    KUBECONFIG=$HOME/.kube/config python3 test_flux_reconcile.py
or in-container:
    docker exec mcp-k8s python3 /app/test_flux_reconcile.py
"""
import sys
sys.path.insert(0, "/app")          # in-container import path (no-op on host)
import mcp_k8s


def ok(cond, label, detail=""):
    marker = "PASS" if cond else "FAIL"
    print(f"  [{marker}] {label}" + (f"  -- {detail}" if detail else ""))
    return cond


passes = True

print("=== Task 1: _FLUX_KINDS table ===")
passes &= ok(set(mcp_k8s._FLUX_KINDS) == {
    "HelmRelease", "Kustomization", "GitRepository",
    "OCIRepository", "HelmRepository", "HelmChart"}, "six Flux kinds registered")
passes &= ok(mcp_k8s._FLUX_KINDS["HelmRelease"][0] == "helm.toolkit.fluxcd.io",
             "HelmRelease group")
passes &= ok(mcp_k8s._FLUX_KINDS["GitRepository"][1] == "gitrepositories",
             "GitRepository plural")
passes &= ok(mcp_k8s._FLUX_KINDS["HelmRelease"][2] is True, "HelmRelease force-capable")
passes &= ok(mcp_k8s._FLUX_KINDS["GitRepository"][2] is False, "GitRepository not force-capable")

print("\n=== Task 1: _validate_flux_kind ===")
passes &= ok(mcp_k8s._validate_flux_kind("HelmRelease") is None, "valid kind -> None")
err = mcp_k8s._validate_flux_kind("Pod")
passes &= ok(isinstance(err, dict) and err.get("error") == "unsupported_kind",
             "non-Flux kind -> unsupported_kind", str(err))
passes &= ok(mcp_k8s._validate_flux_kind("helmrelease") is None,
             "kind match is case-insensitive")

print("\n=== Task 1: _flux_reconcile_patch_body ===")
b = mcp_k8s._flux_reconcile_patch_body("HelmRelease", force=False, now_iso="2026-06-05T00:00:00Z")
passes &= ok(b == {"metadata": {"annotations": {
    "reconcile.fluxcd.io/requestedAt": "2026-06-05T00:00:00Z"}}}, "plain reconcile body", str(b))
bf = mcp_k8s._flux_reconcile_patch_body("HelmRelease", force=True, now_iso="2026-06-05T00:00:00Z")
passes &= ok(bf["metadata"]["annotations"].get("reconcile.fluxcd.io/forceAt")
             == "2026-06-05T00:00:00Z", "force adds forceAt for HelmRelease")
bg = mcp_k8s._flux_reconcile_patch_body("GitRepository", force=True, now_iso="2026-06-05T00:00:00Z")
passes &= ok("reconcile.fluxcd.io/forceAt" not in bg["metadata"]["annotations"],
             "force ignored for non-force-capable kind")
passes &= ok(set(bf["metadata"]) == {"annotations"} and "spec" not in bf,
             "patch body is annotation-only (no spec)")

print("\n=== Task 1: _parse_flux_status ===")
obj = {
    "kind": "HelmRelease",
    "metadata": {"name": "agents-litellm", "namespace": "dev-litellm"},
    "spec": {"suspend": False},
    "status": {
        "lastAppliedRevision": "1.2.3",
        "lastHandledReconcileAt": "2026-06-04T05:00:00Z",
        "conditions": [
            {"type": "Released", "status": "True", "reason": "InstallSucceeded"},
            {"type": "Ready", "status": "False", "reason": "RetriesExceeded",
             "message": "Helm upgrade failed: timed out"},
        ],
    },
}
s = mcp_k8s._parse_flux_status(obj)
passes &= ok(s["ready"] is False, "ready=False from Ready condition")
passes &= ok(s["reason"] == "RetriesExceeded", "reason extracted", s["reason"])
passes &= ok(s["message"].startswith("Helm upgrade failed"), "message extracted")
passes &= ok(s["name"] == "agents-litellm" and s["namespace"] == "dev-litellm", "name/namespace")
passes &= ok(s["suspended"] is False, "suspended flag")
passes &= ok(s["lastAppliedRevision"] == "1.2.3", "lastAppliedRevision")

# fallback: no lastAppliedRevision -> use lastAttemptedRevision
obj_attempted = {
    "kind": "HelmRelease",
    "metadata": {"name": "x", "namespace": "y"},
    "spec": {"suspend": True},
    "status": {
        "lastAttemptedRevision": "9.9.9",
        "conditions": [{"type": "Ready", "status": "False", "reason": "Failed"}],
    },
}
s2 = mcp_k8s._parse_flux_status(obj_attempted)
passes &= ok(s2["lastAppliedRevision"] == "9.9.9",
             "falls back to lastAttemptedRevision when no applied revision", str(s2["lastAppliedRevision"]))
passes &= ok(s2["suspended"] is True, "suspended=True parsed from spec.suspend")

# no Ready condition at all -> ready False, reason/message None (no crash)
s3 = mcp_k8s._parse_flux_status({"kind": "HelmRelease", "metadata": {}, "status": {"conditions": []}})
passes &= ok(s3["ready"] is False and s3["reason"] is None,
             "missing Ready condition -> ready=False, reason=None")

print("\n=== Task 1: _flux_served_version (real fn, mocked ApisApi discovery) ===")
# Regression guard: ApisApi has NO get_api_group() in kubernetes 35.x — the
# resolver must enumerate get_api_versions().groups. Runs BEFORE Task 2 stubs it.
class _FakeGV:
    def __init__(self, v): self.version = v
class _FakeGroup:
    def __init__(self, name, ver): self.name = name; self.preferred_version = _FakeGV(ver)
class _FakeGroupList:
    def __init__(self, groups): self.groups = groups
class _FakeApisApi:
    def get_api_versions(self):
        return _FakeGroupList([_FakeGroup("helm.toolkit.fluxcd.io", "v2"),
                               _FakeGroup("source.toolkit.fluxcd.io", "v1")])
_real_apisapi = mcp_k8s.client.ApisApi
mcp_k8s.client.ApisApi = _FakeApisApi
passes &= ok(mcp_k8s._flux_served_version("helm.toolkit.fluxcd.io") == "v2",
             "resolves preferred served version via get_api_versions")
try:
    mcp_k8s._flux_served_version("nope.example.com")
    passes &= ok(False, "unknown group should raise")
except Exception as e:
    passes &= ok(type(e).__name__ == "ApiException", "unknown group -> ApiException", type(e).__name__)
mcp_k8s.client.ApisApi = _real_apisapi

print("\n=== Task 2: get_flux_status (fake CustomObjectsApi) ===")


class _FakeCustom:
    """Records calls; returns canned objects. Mimics CustomObjectsApi."""
    def __init__(self, get_obj=None, list_items=None):
        self._get_obj = get_obj
        self._list_items = list_items or []
        self.calls = []

    def get_namespaced_custom_object(self, group, version, namespace, plural, name):
        self.calls.append(("get", group, version, namespace, plural, name))
        if self._get_obj is None:
            from kubernetes.client.rest import ApiException
            raise ApiException(status=404, reason="Not Found")
        return self._get_obj

    def list_namespaced_custom_object(self, group, version, namespace, plural):
        self.calls.append(("list_ns", group, version, namespace, plural))
        return {"items": self._list_items}

    def list_cluster_custom_object(self, group, version, plural):
        self.calls.append(("list_all", group, version, plural))
        return {"items": self._list_items}


_stuck = {
    "kind": "HelmRelease",
    "metadata": {"name": "agents-litellm", "namespace": "dev-litellm"},
    "spec": {},
    "status": {"conditions": [
        {"type": "Ready", "status": "False", "reason": "RetriesExceeded",
         "message": "timed out"}]},
}
_healthy = {
    "kind": "HelmRelease",
    "metadata": {"name": "ok-release", "namespace": "dev-litellm"},
    "spec": {},
    "status": {"conditions": [
        {"type": "Ready", "status": "True", "reason": "ReconciliationSucceeded"}]},
}

# Avoid the real served-version discovery call in unit context.
mcp_k8s._flux_served_version = lambda group: "v2"

# single-resource lookup
mcp_k8s.custom = lambda: _FakeCustom(get_obj=_stuck)
r = mcp_k8s.get_flux_status(kind="HelmRelease", name="agents-litellm", namespace="dev-litellm")
passes &= ok(r.get("ready") is False and r.get("reason") == "RetriesExceeded",
             "single-resource status parsed", str(r)[:120])

# scan (name omitted) returns only NOT-ready items
mcp_k8s.custom = lambda: _FakeCustom(list_items=[_stuck, _healthy])
r = mcp_k8s.get_flux_status(kind="HelmRelease", namespace="dev-litellm")
names = [x["name"] for x in r["stalled"]]
passes &= ok(names == ["agents-litellm"], "scan returns only Ready!=True", str(names))

# namespace omitted -> cluster-wide list path
fake = _FakeCustom(list_items=[])
mcp_k8s.custom = lambda: fake
mcp_k8s.get_flux_status(kind="HelmRelease")
passes &= ok(any(c[0] == "list_all" for c in fake.calls), "no-namespace -> cluster-wide list")

# focused lookup without a namespace -> explicit error, not a misleading 404
r = mcp_k8s.get_flux_status(kind="HelmRelease", name="x")
passes &= ok(r.get("error") == "namespace_required",
             "focused without namespace -> namespace_required", str(r)[:120])

# focused missing resource (404) -> not_found
mcp_k8s.custom = lambda: _FakeCustom(get_obj=None)
r = mcp_k8s.get_flux_status(kind="HelmRelease", name="ghost", namespace="dev-litellm")
passes &= ok(r.get("error") == "not_found", "focused 404 -> not_found", str(r)[:120])

# scan with a per-kind list failure -> errors separated, count NOT inflated
class _FailList:
    def list_namespaced_custom_object(self, group, version, namespace, plural):
        from kubernetes.client.rest import ApiException
        raise ApiException(status=403, reason="Forbidden")
    def list_cluster_custom_object(self, *a, **k):
        from kubernetes.client.rest import ApiException
        raise ApiException(status=403, reason="Forbidden")
mcp_k8s.custom = lambda: _FailList()
r = mcp_k8s.get_flux_status(kind="HelmRelease", namespace="dev-litellm")
passes &= ok(r["count"] == 0 and r["stalled"] == [], "list 403 -> no stalled, count 0", str(r)[:120])
passes &= ok(bool(r.get("errors")) and r["errors"][0].get("error") == "permission_denied",
             "list 403 -> errors list populated", str(r.get("errors"))[:120])

print("\n=== Task 3: reconcile_flux_resource ===")

# classification: WRITE but not DESTRUCTIVE; read tool never in WRITE_TOOLS
passes &= ok("reconcile_flux_resource" in mcp_k8s.WRITE_TOOLS,
             "reconcile_flux_resource in WRITE_TOOLS")
passes &= ok("reconcile_flux_resource" not in mcp_k8s.DESTRUCTIVE_TOOLS,
             "reconcile_flux_resource NOT in DESTRUCTIVE_TOOLS")
passes &= ok("get_flux_status" not in mcp_k8s.WRITE_TOOLS,
             "get_flux_status is read-only (not in WRITE_TOOLS)")
passes &= ok("reconcile_flux_resource" in mcp_k8s.mcp._tool_manager._tools,
             "reconcile_flux_resource registered")

# non-Flux kind rejected before any API call
fake = _FakeCustom()
mcp_k8s.custom = lambda: fake
r = mcp_k8s.reconcile_flux_resource(kind="Pod", name="x", namespace="default")
passes &= ok(r.get("error") == "unsupported_kind", "non-Flux kind rejected", str(r)[:120])
passes &= ok(fake.calls == [], "no API call on rejected kind")

# happy path records an annotation-only patch
class _PatchRecorder:
    def __init__(self): self.patches = []
    def patch_namespaced_custom_object(self, group, version, namespace, plural, name, body):
        self.patches.append((group, version, namespace, plural, name, body))
        return {"metadata": {"annotations": body["metadata"]["annotations"]}}

rec = _PatchRecorder()
mcp_k8s.custom = lambda: rec
r = mcp_k8s.reconcile_flux_resource(kind="HelmRelease", name="agents-litellm",
                                    namespace="dev-litellm", force=True)
passes &= ok(r.get("reconciled") is True, "reconciled=True", str(r)[:120])
g, v, ns, plural, nm, body = rec.patches[0]
passes &= ok((g, plural, nm, ns) == ("helm.toolkit.fluxcd.io", "helmreleases",
             "agents-litellm", "dev-litellm"), "patch targets correct GVR", f"{g}/{plural}")
ann = body["metadata"]["annotations"]
passes &= ok("reconcile.fluxcd.io/requestedAt" in ann
             and "reconcile.fluxcd.io/forceAt" in ann, "force sets both annotations")
passes &= ok(list(body["metadata"].keys()) == ["annotations"] and "spec" not in body,
             "patch body is annotation-only (no spec mutation)")

# 403 from apiserver surfaces as permission_denied (so the agent escalates)
class _Forbidden:
    def patch_namespaced_custom_object(self, *a, **k):
        from kubernetes.client.rest import ApiException
        raise ApiException(status=403, reason="Forbidden")
mcp_k8s.custom = lambda: _Forbidden()
r = mcp_k8s.reconcile_flux_resource(kind="Kustomization", name="x", namespace="flux-system")
passes &= ok(r.get("error") == "permission_denied", "403 -> permission_denied", str(r)[:120])

print("\n" + "=" * 60)
print(f"OVERALL: {'ALL PASS' if passes else 'FAILURES PRESENT'}")
sys.exit(0 if passes else 1)
