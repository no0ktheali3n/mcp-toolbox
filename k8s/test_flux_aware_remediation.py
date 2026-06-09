"""Unit tests for Flux-aware remediation:
  - _flux_provenance: detect GitOps ownership from a manifest + frame durability
    (live patch = stopgap on a Flux-managed resource; durable fix is the git source).
    Names the Flux OWNER (HelmRelease/Kustomization) from labels.
  - propose_patch surfaces that provenance.
  - suspend_flux_resource / resume_flux_resource: pause/resume reconciliation by
    patching spec.suspend (rides existing Flux patch RBAC; no new grant).

Pure-logic / monkeypatched. Run:
    KUBECONFIG=<any kubeconfig> python3 test_flux_aware_remediation.py
or in-container:
    docker exec mcp-k8s python3 /app/test_flux_aware_remediation.py
"""
import sys
sys.path.insert(0, "/app")
import mcp_k8s


def ok(cond, label, detail=""):
    marker = "PASS" if cond else "FAIL"
    print(f"  [{marker}] {label}" + (f"  -- {detail}" if detail else ""))
    return cond


passes = True

print("=== _flux_provenance ===")
m_kust = {"metadata": {"name": "app", "namespace": "ns", "labels": {
    "kustomize.toolkit.fluxcd.io/name": "deployments",
    "kustomize.toolkit.fluxcd.io/namespace": "flux-system"}}}
p = mcp_k8s._flux_provenance(m_kust)
passes &= ok(p["flux_managed"] is True, "kustomize label -> flux_managed")
passes &= ok(p["flux_source"] == "Kustomization flux-system/deployments",
             "names the Kustomization owner", p.get("flux_source"))
passes &= ok("STOPGAP" in p["durability"], "durability flags stopgap")
passes &= ok("DEFERRED" in p.get("traceability", ""), "repo resolution marked deferred")
passes &= ok("suspend_flux_resource" in p.get("recommendation", ""),
             "recommendation points at suspend for the hold pattern")

m_helm = {"metadata": {"labels": {
    "helm.toolkit.fluxcd.io/name": "my-app",
    "helm.toolkit.fluxcd.io/namespace": "my-ns"}}}
passes &= ok(mcp_k8s._flux_provenance(m_helm)["flux_source"] == "HelmRelease my-ns/my-app",
             "names the HelmRelease owner")

passes &= ok(mcp_k8s._flux_provenance({"metadata": {"managedFields": [{"manager": "helm-controller"}]}})["flux_managed"] is True,
             "managedFields helm-controller -> flux_managed")
passes &= ok(mcp_k8s._flux_provenance({"metadata": {"managed_fields": [{"manager": "kustomize-controller"}]}})["flux_managed"] is True,
             "managed_fields (snake) kustomize-controller -> flux_managed")
passes &= ok(mcp_k8s._flux_provenance({"metadata": {"labels": {"app": "x"}}}) == {"flux_managed": False},
             "no flux signal -> {flux_managed: False}")

print("\n=== propose_patch surfaces provenance ===")
CURRENT = {"kind": "Deployment", "metadata": {"name": "app", "namespace": "ns", "labels": {
    "helm.toolkit.fluxcd.io/name": "my-app", "helm.toolkit.fluxcd.io/namespace": "my-ns"}},
    "spec": {"replicas": 1}}
mcp_k8s.get_resource_yaml = lambda kind, name, namespace=None: {"manifest": CURRENT}
mcp_k8s._dry_run_patch = lambda kind, name, namespace, patch: {**CURRENT, "spec": {"replicas": 3}}
mcp_k8s._guard_kind = lambda kind, op: None
r = mcp_k8s.propose_patch(kind="Deployment", name="app", namespace="ns", patch={"spec": {"replicas": 3}})
passes &= ok(r.get("flux", {}).get("flux_managed") is True, "propose_patch surfaces flux_managed", str(r.get("flux"))[:80])
passes &= ok("HelmRelease" in r["flux"]["flux_source"], "propose_patch names the flux owner")

print("\n=== suspend/resume classification ===")
for t in ("suspend_flux_resource", "resume_flux_resource"):
    passes &= ok(t in mcp_k8s.mcp._tool_manager._tools, f"{t} registered")
    passes &= ok(t in mcp_k8s.WRITE_TOOLS, f"{t} in WRITE_TOOLS (mutating, stripped in read-only)")


class _FakeCustom:
    def __init__(self): self.calls = []
    def patch_namespaced_custom_object(self, g, v, ns, plural, name, body):
        self.calls.append((g, v, ns, plural, name, body)); return {}


print("\n=== suspend/resume behavior ===")
fc = _FakeCustom()
mcp_k8s.custom = lambda: fc
mcp_k8s._flux_served_version = lambda group: "v2"
r = mcp_k8s.suspend_flux_resource(kind="HelmRelease", name="hr", namespace="ns")
passes &= ok(r.get("suspended") is True, "suspend -> suspended=True", str(r)[:80])
passes &= ok(fc.calls and fc.calls[0][5] == {"spec": {"suspend": True}},
             "suspend patches spec.suspend=True", str(fc.calls[0][5]) if fc.calls else "no call")
passes &= ok(fc.calls[0][3] == "helmreleases", "patches helmreleases plural")
fc.calls.clear()
r = mcp_k8s.resume_flux_resource(kind="HelmRelease", name="hr", namespace="ns")
passes &= ok(r.get("suspended") is False, "resume -> suspended=False")
passes &= ok(fc.calls[0][5] == {"spec": {"suspend": False}}, "resume patches spec.suspend=False")

print("\n=== suspend/resume guards ===")
r = mcp_k8s.suspend_flux_resource(kind="Pod", name="x", namespace="ns")
passes &= ok(r.get("error") == "unsupported_kind", "non-Flux kind -> unsupported_kind", str(r)[:70])


class _Forbidden:
    def patch_namespaced_custom_object(self, *a, **k):
        from kubernetes.client.rest import ApiException
        raise ApiException(status=403, reason="Forbidden")


mcp_k8s.custom = lambda: _Forbidden()
r = mcp_k8s.suspend_flux_resource(kind="HelmRelease", name="hr", namespace="ns")
passes &= ok(r.get("error") == "permission_denied", "403 -> permission_denied", str(r)[:70])

print("\n" + "=" * 60)
print(f"OVERALL: {'ALL PASS' if passes else 'FAILURES PRESENT'}")
sys.exit(0 if passes else 1)
