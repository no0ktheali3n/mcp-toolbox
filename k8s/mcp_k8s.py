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

# Safety mode configuration
MCP_SAFETY_MODE = os.environ.get("MCP_SAFETY_MODE", "full")  # full, read-only, non-destructive

# Define tool categories for safety mode filtering
WRITE_TOOLS = {'apply_manifest', 'delete_resource', 'restart_deployment', 'scale_deployment', 'exec_command'}
DESTRUCTIVE_TOOLS = {'delete_resource'}

mcp = FastMCP("mcp-k8s", host="0.0.0.0", port=8000)

def apply_safety_mode():
    """Remove tools based on MCP_SAFETY_MODE environment variable"""
    mode = os.environ.get("MCP_SAFETY_MODE", "full")

    if mode == "read-only":
        # Remove all write tools
        for tool_name in WRITE_TOOLS:
            if tool_name in mcp._tool_manager._tools:
                del mcp._tool_manager._tools[tool_name]
    elif mode == "non-destructive":
        # Remove only destructive tools
        for tool_name in DESTRUCTIVE_TOOLS:
            if tool_name in mcp._tool_manager._tools:
                del mcp._tool_manager._tools[tool_name]


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

@mcp.tool(annotations={"idempotent": True, "destructive": False, "read_only": True})
def list_namespaces() -> str:
    """List all namespaces in the cluster"""
    nss = core().list_namespace()
    result = [{"name": n.metadata.name, "status": n.status.phase} for n in nss.items]
    return json.dumps(result, indent=2)


@mcp.tool(annotations={"idempotent": True, "destructive": False, "read_only": True})
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

@mcp.tool(annotations={"idempotent": True, "destructive": False, "read_only": True})
def list_pods(
    namespace: str = "default",
    label_selector: str = None,
    field_selector: str = None,
    limit: int = 50,
    all_namespaces: bool = False,
) -> str:
    """List pods in a namespace with their status and restart counts"""
    # Build kwargs for API call
    kwargs = {"limit": limit}
    if label_selector:
        kwargs["label_selector"] = label_selector
    if field_selector:
        kwargs["field_selector"] = field_selector

    # Call appropriate API method
    if all_namespaces:
        pods = core().list_pod_for_all_namespaces(**kwargs)
    else:
        pods = core().list_namespaced_pod(namespace, **kwargs)

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

    # Build response with metadata
    response = {
        "pods": result,
        "metadata": {
            "continue": pods.metadata._continue if hasattr(pods.metadata, '_continue') and pods.metadata._continue else None,
            "remaining_count": pods.metadata.remaining_items_count if hasattr(pods.metadata, 'remaining_items_count') and pods.metadata.remaining_items_count else 0,
        }
    }
    return json.dumps(response, indent=2)


def strip_managed_fields(obj: dict) -> dict:
    """Remove managedFields and last-applied-configuration from API responses"""
    if "metadata" in obj and "managedFields" in obj["metadata"]:
        del obj["metadata"]["managedFields"]
    if "metadata" in obj and obj["metadata"].get("annotations"):
        annotations = obj["metadata"]["annotations"]
        if annotations and "kubectl.kubernetes.io/last-applied-configuration" in annotations:
            del annotations["kubectl.kubernetes.io/last-applied-configuration"]
    return obj


def get_qos_class(pod) -> str:
    """Determine pod QoS class based on resource requests and limits"""
    containers = pod.spec.containers or []
    if not containers:
        return "BestEffort"

    all_guaranteed = True
    any_set = False

    for container in containers:
        resources = container.resources
        requests = resources.requests if resources else {}
        limits = resources.limits if resources else {}

        req_cpu = requests.get("cpu") if requests else None
        req_mem = requests.get("memory") if requests else None
        lim_cpu = limits.get("cpu") if limits else None
        lim_mem = limits.get("memory") if limits else None

        has_requests = req_cpu is not None or req_mem is not None
        has_limits = lim_cpu is not None or lim_mem is not None

        if has_requests or has_limits:
            any_set = True

        # Guaranteed requires all containers to have both limits and requests set and equal
        if not (req_cpu and req_mem and lim_cpu and lim_mem):
            all_guaranteed = False
        elif req_cpu != lim_cpu or req_mem != lim_mem:
            all_guaranteed = False

    if all_guaranteed:
        return "Guaranteed"
    elif any_set:
        return "Burstable"
    else:
        return "BestEffort"


@mcp.tool(annotations={"idempotent": True, "destructive": False, "read_only": True})
def get_pod_detail(pod_name: str, namespace: str = "default") -> str:
    """Get detailed info about a pod including conditions, container states, resources, and recent events"""
    pod = core().read_namespaced_pod(pod_name, namespace)
    pod_dict = strip_managed_fields(pod.to_dict())

    events = core().list_namespaced_event(
        namespace, field_selector=f"involvedObject.name={pod_name}"
    )

    containers_detail = []
    for c in pod.status.container_statuses or []:
        # Get resource requests and limits from pod spec
        container_spec = None
        for cs in pod.spec.containers or []:
            if cs.name == c.name:
                container_spec = cs
                break

        resources_info = {
            "requests": {"cpu": None, "memory": None},
            "limits": {"cpu": None, "memory": None},
        }
        volume_mounts = []

        if container_spec:
            res = container_spec.resources
            if res:
                requests = res.requests or {}
                limits = res.limits or {}
                resources_info["requests"]["cpu"] = requests.get("cpu")
                resources_info["requests"]["memory"] = requests.get("memory")
                resources_info["limits"]["cpu"] = limits.get("cpu")
                resources_info["limits"]["memory"] = limits.get("memory")

            if container_spec.volume_mounts:
                for vm in container_spec.volume_mounts:
                    volume_mounts.append({
                        "name": vm.name,
                        "mount_path": vm.mount_path,
                        "read_only": vm.read_only or False,
                    })

        containers_detail.append({
            "name": c.name,
            "image": c.image,
            "ready": c.ready,
            "restarts": c.restart_count,
            "state": {
                "running": {"started_at": str(c.state.running.started_at)} if c.state and c.state.running else None,
                "waiting": {"reason": c.state.waiting.reason, "message": c.state.waiting.message} if c.state and c.state.waiting else None,
                "terminated": {
                    "reason": c.state.terminated.reason,
                    "exit_code": c.state.terminated.exit_code,
                    "started_at": str(c.state.terminated.started_at) if c.state.terminated.started_at else None,
                    "finished_at": str(c.state.terminated.finished_at) if c.state.terminated.finished_at else None,
                } if c.state and c.state.terminated else None,
            },
            "resources": resources_info,
            "volume_mounts": volume_mounts,
        })

    result = {
        "name": pod.metadata.name,
        "namespace": pod.metadata.namespace,
        "node": pod.spec.node_name,
        "phase": pod.status.phase,
        "qos_class": get_qos_class(pod),
        "pod_ip": pod.status.pod_ip,
        "host_ip": pod.status.host_ip,
        "conditions": [
            {"type": c.type, "status": c.status, "reason": c.reason}
            for c in (pod.status.conditions or [])
        ],
        "containers": containers_detail,
        "events": [
            {"reason": e.reason, "message": e.message, "type": e.type}
            for e in sorted(events.items, key=lambda x: x.last_timestamp or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc), reverse=True)[:10]
        ],
    }
    return json.dumps(result, indent=2)


@mcp.tool(annotations={"idempotent": True, "destructive": False, "read_only": True})
async def find_unhealthy_pods(
    namespace: str = "",
    restart_threshold: int = 5,
    include_completed: bool = False,
    limit: int = 25,
    sort_by: str = "restarts",
) -> dict:
    """Find unhealthy pods across all namespaces or a specific namespace.

    Returns pods that are not Running/Succeeded, or Running pods with restarts >= threshold.
    """
    # List pods - either all namespaces or specific namespace
    if namespace == "":
        pod_list = core().list_pod_for_all_namespaces()
    else:
        pod_list = core().list_namespaced_pod(namespace)

    unhealthy_pods = []

    for pod in pod_list.items:
        phase = pod.status.phase or "Unknown"
        container_statuses = pod.status.container_statuses or []

        # Calculate total restarts and find last restart reason
        total_restarts = sum(c.restart_count or 0 for c in container_statuses)
        last_restart_reason = None
        for c in container_statuses:
            if c.last_state and c.last_state.terminated and c.last_state.terminated.reason:
                last_restart_reason = c.last_state.terminated.reason
                break

        # Determine if pod is unhealthy
        is_unhealthy = False

        # Phase != Running AND Phase != Succeeded -> always include
        if phase not in ("Running", "Succeeded"):
            is_unhealthy = True
        # Phase == Succeeded and include_completed -> include
        elif phase == "Succeeded" and include_completed:
            is_unhealthy = True
        # Phase == Running but restarts >= threshold -> include (only if threshold > 0)
        elif phase == "Running" and restart_threshold > 0 and total_restarts >= restart_threshold:
            is_unhealthy = True

        if not is_unhealthy:
            continue

        # Get age
        age = pod.metadata.creation_timestamp
        age_str = age.isoformat() if age else "unknown"

        unhealthy_pods.append({
            "name": pod.metadata.name,
            "namespace": pod.metadata.namespace or "",
            "phase": phase,
            "restart_count": total_restarts,
            "last_restart_reason": last_restart_reason,
            "age": age_str,
            "_age_dt": age,  # For sorting
        })

    # Sort
    if sort_by == "age":
        unhealthy_pods.sort(key=lambda p: p["_age_dt"] or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc), reverse=True)
    else:  # Default: sort by restarts descending
        unhealthy_pods.sort(key=lambda p: p["restart_count"], reverse=True)

    # Remove internal sort key and apply limit
    total_unhealthy = len(unhealthy_pods)
    for p in unhealthy_pods:
        del p["_age_dt"]

    returned_pods = unhealthy_pods[:limit]

    return {
        "pods": returned_pods,
        "total_unhealthy": total_unhealthy,
        "returned_count": len(returned_pods),
        "truncated": total_unhealthy > limit,
    }


@mcp.tool(annotations={"idempotent": True, "destructive": False, "read_only": True})
def get_pod_logs(
    pod_name: str,
    namespace: str = "default",
    container: str = "",
    tail_lines: int = 100,
    since_seconds: int = None,
    previous: bool = False,
) -> str:
    """Get logs from a pod. Specify container name if the pod has multiple containers"""
    kwargs = {"tail_lines": tail_lines, "previous": previous}
    if container:
        kwargs["container"] = container
    if since_seconds is not None:
        kwargs["since_seconds"] = since_seconds
    logs = core().read_namespaced_pod_log(pod_name, namespace, **kwargs)
    return logs or "(no logs)"


@mcp.tool(annotations={"idempotent": False, "destructive": False, "read_only": False})
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

@mcp.tool(annotations={"idempotent": True, "destructive": False, "read_only": True})
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


@mcp.tool(annotations={"idempotent": False, "destructive": True, "read_only": False})
def scale_deployment(name: str, replicas: int, namespace: str = "default") -> str:
    """Scale a deployment to a specified number of replicas (use 0 to stop)"""
    apps().patch_namespaced_deployment_scale(
        name, namespace, {"spec": {"replicas": replicas}}
    )
    return f"Scaled deployment/{name} to {replicas} replicas"


@mcp.tool(annotations={"idempotent": False, "destructive": True, "read_only": False})
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


@mcp.tool(annotations={"idempotent": True, "destructive": False, "read_only": True})
def get_deployment_health(name: str, namespace: str = "default") -> dict:
    """Get comprehensive health report for a deployment including replicasets, pods, and events"""

    def strip_managed_fields(obj: dict) -> dict:
        """Remove managedFields and last-applied-configuration from API responses"""
        if "metadata" in obj and "managedFields" in obj["metadata"]:
            del obj["metadata"]["managedFields"]
        if "metadata" in obj and obj["metadata"].get("annotations"):
            annotations = obj["metadata"]["annotations"]
            if annotations and "kubectl.kubernetes.io/last-applied-configuration" in annotations:
                del annotations["kubectl.kubernetes.io/last-applied-configuration"]
        return obj

    # Get deployment
    try:
        deployment = apps().read_namespaced_deployment(name, namespace)
    except ApiException as e:
        return {"error": f"Deployment {name} not found in namespace {namespace}", "status_code": e.status}

    deployment_dict = strip_managed_fields(deployment.to_dict())

    # Get label selector
    match_labels = deployment.spec.selector.match_labels or {}
    label_selector = ",".join(f"{k}={v}" for k, v in match_labels.items())

    # Get owned ReplicaSets
    replicasets = []
    if label_selector:
        rs_list = apps().list_namespaced_replica_set(namespace, label_selector=label_selector)
        for rs in rs_list.items:
            # Check if this RS is owned by this deployment
            owner_refs = rs.metadata.owner_references or []
            is_owned = any(
                ref.kind == "Deployment" and ref.name == name and ref.controller
                for ref in owner_refs
            )
            if is_owned:
                rs_dict = strip_managed_fields(rs.to_dict())
                replicasets.append({
                    "name": rs_dict["metadata"]["name"],
                    "desired": rs_dict.get("spec", {}).get("replicas", 0),
                    "ready": rs_dict.get("status", {}).get("ready_replicas", 0) or 0,
                    "available": rs_dict.get("status", {}).get("available_replicas", 0) or 0,
                })

    # Get owned Pods
    pods = []
    if label_selector:
        pod_list = core().list_namespaced_pod(namespace, label_selector=label_selector)
        for pod in pod_list.items:
            # Check if this pod is owned by this deployment (via replicaset)
            owner_refs = pod.metadata.owner_references or []
            is_owned = any(
                ref.kind == "ReplicaSet" and ref.controller
                for ref in owner_refs
            )
            if is_owned:
                pod_dict = strip_managed_fields(pod.to_dict())
                container_states = []
                for c in pod_dict.get("status", {}).get("container_statuses", []) or []:
                    state = "unknown"
                    if c.get("state", {}).get("running"):
                        state = "running"
                    elif c.get("state", {}).get("waiting"):
                        state = "waiting"
                    elif c.get("state", {}).get("terminated"):
                        state = "terminated"
                    container_states.append({
                        "name": c.get("name", "unknown"),
                        "state": state,
                    })
                pods.append({
                    "name": pod_dict["metadata"]["name"],
                    "phase": pod_dict.get("status", {}).get("phase", "Unknown"),
                    "restart_count": sum(
                        c.get("restart_count", 0)
                        for c in pod_dict.get("status", {}).get("container_statuses", []) or []
                    ),
                    "container_states": container_states,
                })

    # Get events for deployment
    events = []

    # Events for the deployment itself
    try:
        deploy_events = core().list_namespaced_event(
            namespace,
            field_selector=f"involvedObject.name={name},involvedObject.kind=Deployment"
        )
        events.extend(deploy_events.items)
    except ApiException:
        pass

    # Events for pods
    for pod in pods:
        try:
            pod_events = core().list_namespaced_event(
                namespace,
                field_selector=f"involvedObject.name={pod['name']},involvedObject.kind=Pod"
            )
            events.extend(pod_events.items)
        except ApiException:
            pass

    # Sort events by timestamp, take last 15
    sorted_events = sorted(
        events,
        key=lambda x: x.last_timestamp or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc),
        reverse=True
    )[:15]

    events_result = [
        {
            "reason": e.reason or "",
            "message": e.message or "",
            "type": e.type or "Unknown",
            "time": str(e.last_timestamp) if e.last_timestamp else "",
        }
        for e in sorted_events
    ]

    # Build rollout status
    status = deployment.status
    conditions = status.conditions or [] if status else []

    progressing = False
    stalled = False
    complete = False

    for cond in conditions:
        if cond.type == "Progressing":
            if cond.status == "True":
                progressing = True
                if cond.reason and "ProgressDeadlineExceeded" in cond.reason:
                    stalled = True
        if cond.type == "Available":
            if cond.status == "True":
                complete = True

    # If desired == available and not progressing, rollout is complete
    desired = deployment.spec.replicas or 0
    available = status.available_replicas or 0 if status else 0
    if desired > 0 and available >= desired and not progressing:
        complete = True

    # Get deployment conditions
    conditions_result = [
        {
            "type": c.type,
            "status": c.status,
            "reason": c.reason or "",
        }
        for c in conditions
    ]

    # Get strategy
    strategy = "RollingUpdate"
    if deployment.spec.strategy and deployment.spec.strategy.type:
        strategy = deployment.spec.strategy.type

    # Get image
    image = ""
    containers = deployment.spec.template.spec.containers or []
    if containers:
        image = containers[0].image or ""

    return {
        "deployment": {
            "name": name,
            "namespace": namespace,
            "desired_replicas": deployment.spec.replicas or 0,
            "ready_replicas": status.ready_replicas or 0 if status else 0,
            "available_replicas": available,
            "unavailable_replicas": status.unavailable_replicas or 0 if status else 0,
            "strategy": strategy,
            "image": image,
        },
        "replicasets": replicasets,
        "pods": pods,
        "events": events_result,
        "rollout_status": {
            "progressing": progressing,
            "stalled": stalled,
            "complete": complete,
        },
        "conditions": conditions_result,
    }


# ── Services & Config ────────────────────────────────────────────────────────────

@mcp.tool(annotations={"idempotent": True, "destructive": False, "read_only": True})
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


@mcp.tool(annotations={"idempotent": True, "destructive": False, "read_only": True})
def list_configmaps(namespace: str = "default") -> str:
    """List configmaps in a namespace and their keys"""
    cms = core().list_namespaced_config_map(namespace)
    result = [
        {"name": cm.metadata.name, "keys": list(cm.data.keys()) if cm.data else []}
        for cm in cms.items
    ]
    return json.dumps(result, indent=2)


@mcp.tool(annotations={"idempotent": True, "destructive": False, "read_only": True})
def get_events(
    namespace: str = "default",
    limit: int = 20,
    involved_object_name: str = None,
    involved_object_kind: str = None,
    event_type: str = None,
) -> str:
    """Get recent events in a namespace — useful for debugging failing resources"""
    field_selectors = []
    if involved_object_name:
        field_selectors.append(f"involvedObject.name={involved_object_name}")
    if involved_object_kind:
        field_selectors.append(f"involvedObject.kind={involved_object_kind}")
    if event_type:
        field_selectors.append(f"type={event_type}")

    field_selector = ",".join(field_selectors) if field_selectors else None

    if field_selector:
        events = core().list_namespaced_event(namespace, field_selector=field_selector)
    else:
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

@mcp.tool(annotations={"idempotent": False, "destructive": True, "read_only": False})
def apply_manifest(manifest_yaml: str) -> str:
    """Apply a Kubernetes YAML manifest to the cluster"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(manifest_yaml)
        tmp = f.name
    try:
        return kubectl("apply", "-f", tmp)
    finally:
        os.unlink(tmp)


@mcp.tool(annotations={"idempotent": False, "destructive": True, "read_only": False})
def delete_resource(resource_type: str, name: str, namespace: str = "default") -> str:
    """Delete a Kubernetes resource. resource_type examples: pod, deployment, service, configmap"""
    return kubectl("delete", resource_type, name, "-n", namespace)


# ── Entrypoint ───────────────────────────────────────────────────────────────────

# Apply safety mode restrictions after all tools are registered
apply_safety_mode()

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
