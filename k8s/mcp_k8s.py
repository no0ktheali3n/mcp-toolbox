import asyncio
import datetime
import json
import logging
import os
import subprocess
import tempfile

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp-k8s")

mcp = FastMCP("mcp-k8s", host="0.0.0.0", port=8000)

# ── Config ─────────────────────────────────────────────────────────────────────

def load_config():
    kubeconfig = os.environ.get("KUBECONFIG", "/root/.kube/config")
    config.load_kube_config(config_file=kubeconfig)

load_config()

def core() -> client.CoreV1Api:
    return client.CoreV1Api()

def apps() -> client.AppsV1Api:
    return client.AppsV1Api()

def kubectl(*args: str) -> str:
    kubeconfig = os.environ.get("KUBECONFIG", "/root/.kube/config")
    result = subprocess.run(
        ["kubectl", "--kubeconfig", kubeconfig, *args],
        capture_output=True, text=True, timeout=30
    )
    return (result.stdout + result.stderr).strip()


# ── Cluster ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_namespaces() -> str:
    """List all namespaces in the cluster"""
    nss = core().list_namespace()
    result = [{"name": n.metadata.name, "status": n.status.phase} for n in nss.items]
    return json.dumps(result, indent=2)


@mcp.tool()
def get_node_status() -> str:
    """Get status and info for all nodes in the cluster"""
    nodes = core().list_node()
    result = []
    for n in nodes.items:
        conditions = {c.type: c.status for c in (n.status.conditions or [])}
        result.append({
            "name": n.metadata.name,
            "ready": conditions.get("Ready", "Unknown"),
            "roles": [k.replace("node-role.kubernetes.io/", "") for k in n.metadata.labels if "node-role" in k],
            "version": n.status.node_info.kubelet_version if n.status.node_info else "unknown",
            "os": n.status.node_info.os_image if n.status.node_info else "unknown",
        })
    return json.dumps(result, indent=2)


# ── Pods ────────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_pods(namespace: str = "default") -> str:
    """List pods in a namespace with their status and restart counts"""
    pods = core().list_namespaced_pod(namespace)
    result = []
    for p in pods.items:
        statuses = p.status.container_statuses or []
        result.append({
            "name": p.metadata.name,
            "status": p.status.phase,
            "ready": f"{sum(1 for c in statuses if c.ready)}/{len(statuses)}",
            "restarts": sum(c.restart_count for c in statuses),
            "node": p.spec.node_name,
            "age": str(p.metadata.creation_timestamp),
        })
    return json.dumps(result, indent=2)


@mcp.tool()
def describe_pod(pod_name: str, namespace: str = "default") -> str:
    """Get detailed info about a pod including conditions and recent events"""
    pod = core().read_namespaced_pod(pod_name, namespace)
    events = core().list_namespaced_event(
        namespace, field_selector=f"involvedObject.name={pod_name}"
    )
    result = {
        "name": pod.metadata.name,
        "namespace": pod.metadata.namespace,
        "node": pod.spec.node_name,
        "status": pod.status.phase,
        "conditions": [
            {"type": c.type, "status": c.status, "reason": c.reason}
            for c in (pod.status.conditions or [])
        ],
        "containers": [
            {
                "name": c.name,
                "image": c.image,
                "ready": c.ready,
                "restarts": c.restart_count,
                "state": {
    			"running": {"started_at": str(c.state.running.started_at)} if c.state.running else None,
    			"waiting": {"reason": c.state.waiting.reason, "message": c.state.waiting.message} if c.state.waiting else None,
    			"terminated": {"reason": c.state.terminated.reason, "exit_code": c.state.terminated.exit_code} if c.state.terminated else None,
		},
            }
            for c in (pod.status.container_statuses or [])
        ],
        "events": [
            {"reason": e.reason, "message": e.message, "type": e.type}
            for e in sorted(events.items, key=lambda x: x.last_timestamp or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc), reverse=True)[:10]
        ],
    }
    return json.dumps(result, indent=2)


@mcp.tool()
def get_pod_logs(
    pod_name: str,
    namespace: str = "default",
    container: str = "",
    tail_lines: int = 100,
) -> str:
    """Get logs from a pod. Specify container name if the pod has multiple containers"""
    kwargs = {"tail_lines": tail_lines}
    if container:
        kwargs["container"] = container
    logs = core().read_namespaced_pod_log(pod_name, namespace, **kwargs)
    return logs or "(no logs)"


@mcp.tool()
def exec_command(
    pod_name: str,
    command: list[str],
    namespace: str = "default",
    container: str = "",
) -> str:
    """Execute a command inside a running pod. Pass command as a list e.g. ['ls', '-la']"""
    kwargs = {"command": command}
    if container:
        kwargs["container"] = container
    resp = stream(
        core().connect_get_namespaced_pod_exec,
        pod_name, namespace,
        stdout=True, stderr=True, stdin=False, tty=False,
        **kwargs,
    )
    return resp


# ── Deployments ─────────────────────────────────────────────────────────────────

@mcp.tool()
def list_deployments(namespace: str = "default") -> str:
    """List deployments in a namespace with replica counts and image"""
    deps = apps().list_namespaced_deployment(namespace)
    result = [
        {
            "name": d.metadata.name,
            "desired": d.spec.replicas,
            "ready": d.status.ready_replicas or 0,
            "available": d.status.available_replicas or 0,
            "image": d.spec.template.spec.containers[0].image if d.spec.template.spec.containers else "unknown",
        }
        for d in deps.items
    ]
    return json.dumps(result, indent=2)


@mcp.tool()
def scale_deployment(name: str, replicas: int, namespace: str = "default") -> str:
    """Scale a deployment to a specified number of replicas (use 0 to stop)"""
    apps().patch_namespaced_deployment_scale(
        name, namespace, {"spec": {"replicas": replicas}}
    )
    return f"Scaled deployment/{name} to {replicas} replicas"


@mcp.tool()
def restart_deployment(name: str, namespace: str = "default") -> str:
    """Perform a rolling restart of a deployment"""
    patch = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubectl.kubernetes.io/restartedAt": datetime.datetime.utcnow().isoformat()
                    }
                }
            }
        }
    }
    apps().patch_namespaced_deployment(name, namespace, patch)
    return f"Restarted deployment/{name}"


# ── Services & Config ────────────────────────────────────────────────────────────

@mcp.tool()
def list_services(namespace: str = "default") -> str:
    """List services in a namespace with type and port mappings"""
    svcs = core().list_namespaced_service(namespace)
    result = [
        {
            "name": s.metadata.name,
            "type": s.spec.type,
            "cluster_ip": s.spec.cluster_ip,
            "ports": [
                {"port": p.port, "target_port": str(p.target_port), "protocol": p.protocol}
                for p in (s.spec.ports or [])
            ],
        }
        for s in svcs.items
    ]
    return json.dumps(result, indent=2)


@mcp.tool()
def list_configmaps(namespace: str = "default") -> str:
    """List configmaps in a namespace and their keys"""
    cms = core().list_namespaced_config_map(namespace)
    result = [
        {"name": cm.metadata.name, "keys": list(cm.data.keys()) if cm.data else []}
        for cm in cms.items
    ]
    return json.dumps(result, indent=2)


@mcp.tool()
def get_events(namespace: str = "default", limit: int = 20) -> str:
    """Get recent events in a namespace — useful for debugging failing resources"""
    events = core().list_namespaced_event(namespace)
    result = [
        {
            "reason": e.reason,
            "message": e.message,
            "object": f"{e.involved_object.kind}/{e.involved_object.name}",
            "type": e.type,
            "count": e.count,
            "time": str(e.last_timestamp),
        }
        for e in sorted(events.items, key=lambda x: x.last_timestamp or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc), reverse=True)[:limit]
    ]
    return json.dumps(result, indent=2)


# ── Manifest operations ──────────────────────────────────────────────────────────

@mcp.tool()
def apply_manifest(manifest_yaml: str) -> str:
    """Apply a Kubernetes YAML manifest to the cluster"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(manifest_yaml)
        tmp = f.name
    try:
        return kubectl("apply", "-f", tmp)
    finally:
        os.unlink(tmp)


@mcp.tool()
def delete_resource(resource_type: str, name: str, namespace: str = "default") -> str:
    """Delete a Kubernetes resource. resource_type examples: pod, deployment, service, configmap"""
    return kubectl("delete", resource_type, name, "-n", namespace)


# ── Entrypoint ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
