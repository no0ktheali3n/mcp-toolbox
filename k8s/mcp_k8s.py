import asyncio
import datetime
import json
import logging
import os
import re
import subprocess
import tempfile

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream
from mcp.server.fastmcp import FastMCP

from mutation_guard import (
    ACTIVE_DENYLIST,
    denial_response,
    guard as _guard_kind,
    is_kind_denied,
    load_denylist_from_env,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp-k8s")
logger.info("mutation_guard active denylist: %s", sorted(ACTIVE_DENYLIST))

# Safety mode configuration
MCP_SAFETY_MODE = os.environ.get("MCP_SAFETY_MODE", "full")  # full, read-only, non-destructive

# Define tool categories for safety mode filtering
WRITE_TOOLS = {'apply_manifest', 'delete_resource', 'restart_deployment', 'scale_deployment', 'exec_command', 'patch_resource_limits', 'rollback_deployment'}
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
def restart_deployment(name: str, namespace: str = "default"):
    """Perform a rolling restart of a deployment.

    Honors the operator mutation denylist (MCP_K8S_DENYLIST). If
    "Deployment" is denied, returns a structured mutation_denied dict
    without calling the k8s API.
    """
    denied = _guard_kind("Deployment", "restart_deployment")
    if denied:
        return denied
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
def list_services(
    namespace: str = "default",
    limit: int = 100,
    continue_token: str = None,
) -> str:
    """List services in a namespace with type and port mappings.

    Pagination: pass `limit` to cap page size and `continue_token` to resume
    from a previous call's `continue` value. Return shape is
    `{"items": [...], "continue": next_token_or_None}`.
    """
    kwargs = {"limit": limit}
    if continue_token:
        kwargs["_continue"] = continue_token
    svcs = core().list_namespaced_service(namespace, **kwargs)
    items = [
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
    next_token = None
    if hasattr(svcs.metadata, "_continue") and svcs.metadata._continue:
        next_token = svcs.metadata._continue
    response = {"items": items, "continue": next_token}
    return json.dumps(response, indent=2)


@mcp.tool(annotations={"idempotent": True, "destructive": False, "read_only": True})
def list_configmaps(
    namespace: str = "default",
    limit: int = 100,
    continue_token: str = None,
) -> str:
    """List configmaps in a namespace and their keys.

    Pagination: pass `limit` to cap page size and `continue_token` to resume
    from a previous call's `continue` value. Return shape is
    `{"items": [...], "continue": next_token_or_None}`.
    """
    kwargs = {"limit": limit}
    if continue_token:
        kwargs["_continue"] = continue_token
    cms = core().list_namespaced_config_map(namespace, **kwargs)
    items = [
        {"name": cm.metadata.name, "keys": list(cm.data.keys()) if cm.data else []}
        for cm in cms.items
    ]
    next_token = None
    if hasattr(cms.metadata, "_continue") and cms.metadata._continue:
        next_token = cms.metadata._continue
    response = {"items": items, "continue": next_token}
    return json.dumps(response, indent=2)


@mcp.tool(annotations={"idempotent": True, "destructive": False, "read_only": True})
def get_events(
    namespace: str = "default",
    limit: int = 20,
    involved_object_name: str = None,
    involved_object_kind: str = None,
    event_type: str = None,
    field_selector: str = None,
) -> str:
    """Get recent events in a namespace — useful for debugging failing resources.

    The structured `involved_object_name`, `involved_object_kind`, and
    `event_type` params are convenience wrappers. For more control (e.g. to
    combine multiple selectors or use the raw Kubernetes field-selector
    syntax like `involvedObject.kind=Pod,involvedObject.name=foo`), pass
    `field_selector` directly — it is merged with the structured params.
    """
    field_selectors = []
    if involved_object_name:
        field_selectors.append(f"involvedObject.name={involved_object_name}")
    if involved_object_kind:
        field_selectors.append(f"involvedObject.kind={involved_object_kind}")
    if event_type:
        field_selectors.append(f"type={event_type}")
    if field_selector:
        field_selectors.append(field_selector)

    combined_selector = ",".join(field_selectors) if field_selectors else None

    if combined_selector:
        events = core().list_namespaced_event(namespace, field_selector=combined_selector)
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


# ── Resource limit patching (guarded) ─────────────────────────────────────────
#
# patch_resource_limits adjusts spec.template.spec.containers[*].resources on a
# Deployment / StatefulSet / DaemonSet. Adding this tool unblocks autonomous
# remediation of OOMKill and CPU-throttling scenarios. Safety depends on the
# guardrails below — without them, an agent that decides "raise memory" on
# every OOMKill would happily eat node capacity.
#
# Guardrails enforced server-side:
#   1. kind restricted to Deployment / StatefulSet / DaemonSet (no raw Pods).
#   2. container name must match an existing container in the spec.
#   3. new limit <= max_multiplier * current limit (default 2.0).
#   4. new limit <= absolute ceiling (default 2Gi memory / 4 CPU).
#   5. audit log entry for every call (accepted or rejected).
#   6. dry_run mode returns the proposed patch without applying.
#
# Authorization (whether the agent is ALLOWED to call this at all) is
# enforced upstream in the detector's prompt and/or MCP_SAFETY_MODE; this
# tool only enforces what a call is allowed to do.

_PATCH_SUPPORTED_KINDS = {"Deployment", "StatefulSet", "DaemonSet"}

# k8s memory quantity suffixes (lowercase "ki" etc. are invalid in k8s, only
# uppercase Ki/Mi/Gi/Ti for powers of 2 and K/M/G/T for decimal are valid).
_MEMORY_SUFFIX_MULTIPLIERS = {
    "": 1,
    "K": 10 ** 3, "M": 10 ** 6, "G": 10 ** 9, "T": 10 ** 12, "P": 10 ** 15,
    "Ki": 2 ** 10, "Mi": 2 ** 20, "Gi": 2 ** 30, "Ti": 2 ** 40, "Pi": 2 ** 50,
}
_MEMORY_RE = re.compile(r"^(\d+(?:\.\d+)?)([KMGTP]i?)?$")
_CPU_RE = re.compile(r"^(\d+(?:\.\d+)?)(m?)$")


def _parse_memory_bytes(q: str | None) -> int | None:
    """Parse a k8s memory quantity string to bytes. Returns None on None input."""
    if q is None:
        return None
    s = str(q).strip()
    m = _MEMORY_RE.match(s)
    if not m:
        raise ValueError(f"cannot parse memory quantity: {q!r}")
    qty, suf = m.groups()
    return int(float(qty) * _MEMORY_SUFFIX_MULTIPLIERS[suf or ""])


def _parse_cpu_millicores(q: str | None) -> int | None:
    """Parse a k8s CPU quantity string to millicores. '1'=1000, '500m'=500. None→None."""
    if q is None:
        return None
    s = str(q).strip()
    m = _CPU_RE.match(s)
    if not m:
        raise ValueError(f"cannot parse cpu quantity: {q!r}")
    qty, suf = m.groups()
    return int(float(qty) * (1 if suf == "m" else 1000))


_AUDIT_LOG_PATH = os.environ.get(
    "PATCH_RESOURCE_LIMITS_AUDIT_LOG", "/var/log/mcp-k8s-audit.log"
)


def _audit(event: dict) -> None:
    """Write a structured audit event to stdout (always) and the audit log file
    (best effort). The stdout copy survives in container logs even if the
    mounted log path isn't writable."""
    payload = {**event, "ts": datetime.datetime.utcnow().isoformat() + "Z"}
    line = json.dumps(payload)
    logger.info("[patch_resource_limits audit] %s", line)
    try:
        with open(_AUDIT_LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception as e:
        logger.warning("audit log write failed (%s): %s", _AUDIT_LOG_PATH, e)


def _fetch_target(kind: str, name: str, namespace: str):
    """Return the raw Deployment/StatefulSet/DaemonSet object from AppsV1Api."""
    a = apps()
    if kind == "Deployment":
        return a.read_namespaced_deployment(name, namespace)
    if kind == "StatefulSet":
        return a.read_namespaced_stateful_set(name, namespace)
    if kind == "DaemonSet":
        return a.read_namespaced_daemon_set(name, namespace)
    raise ValueError(f"unsupported kind: {kind}")


def _patch_target(kind: str, name: str, namespace: str, body: dict):
    """Apply a strategic-merge patch to the target resource."""
    a = apps()
    if kind == "Deployment":
        return a.patch_namespaced_deployment(name, namespace, body)
    if kind == "StatefulSet":
        return a.patch_namespaced_stateful_set(name, namespace, body)
    if kind == "DaemonSet":
        return a.patch_namespaced_daemon_set(name, namespace, body)
    raise ValueError(f"unsupported kind: {kind}")


@mcp.tool(annotations={"idempotent": False, "destructive": True, "read_only": False})
def patch_resource_limits(
    kind: str,
    name: str,
    container: str,
    limits: dict | None = None,
    requests: dict | None = None,
    namespace: str = "default",
    max_multiplier: float = 2.0,
    absolute_ceiling_memory: str = "2Gi",
    absolute_ceiling_cpu: str = "4",
    dry_run: bool = False,
) -> dict:
    """Patch container resource limits and/or requests on a workload.

    Supported kinds: Deployment, StatefulSet, DaemonSet. Raw Pods are
    intentionally not supported — mutating a Pod directly only lasts until
    its next recreate and hides problems from the controller.

    Guardrails (enforced server-side):
      - Container name must match an existing container in the target spec.
      - Each requested limit must be <= max_multiplier * current limit
        (when a current limit exists) AND <= the absolute ceiling.
        When no current limit exists (BestEffort / Burstable without limits),
        the multiplier check is skipped and only the absolute ceiling applies.
      - Requests, if provided, must be <= the new limit for the same resource.

    Args:
        kind: Deployment | StatefulSet | DaemonSet.
        name: Resource name in `namespace`.
        container: Container name within spec.template.spec.containers.
        limits: Optional dict {"memory": "256Mi", "cpu": "500m"}.
            Omit either key to leave that dimension unchanged.
        requests: Optional dict {"memory": "128Mi", "cpu": "250m"}.
        namespace: Kubernetes namespace. Default "default".
        max_multiplier: Max ratio of new limit to current limit. Default 2.0.
        absolute_ceiling_memory: Hard ceiling regardless of multiplier.
            Default "2Gi". k8s quantity string.
        absolute_ceiling_cpu: Hard ceiling regardless of multiplier.
            Default "4" (= 4000m). k8s quantity string.
        dry_run: If True, validate and return the proposed patch without
            applying it. Audit log still records the attempt.

    Returns:
        dict with:
          - applied (bool): True if the patch was sent to the API server.
          - dry_run (bool): Echo of the input.
          - kind, name, namespace, container: Echo of the inputs.
          - before (dict | None): Current limits/requests on the target
            container, or None if it couldn't be read.
          - after (dict): Proposed (or applied) limits/requests.
          - reason_rejected (str | None): Short reason code if the call was
            rejected, else None.
    """
    audit_base = {
        "tool": "patch_resource_limits",
        "kind": kind,
        "name": name,
        "namespace": namespace,
        "container": container,
        "requested_limits": limits,
        "requested_requests": requests,
        "max_multiplier": max_multiplier,
        "absolute_ceiling_memory": absolute_ceiling_memory,
        "absolute_ceiling_cpu": absolute_ceiling_cpu,
        "dry_run": dry_run,
    }

    def _reject(reason: str, detail: str = "", before=None, after=None) -> dict:
        _audit({**audit_base, "outcome": "rejected", "reason_rejected": reason, "detail": detail})
        return {
            "applied": False,
            "dry_run": dry_run,
            "kind": kind,
            "name": name,
            "namespace": namespace,
            "container": container,
            "before": before,
            "after": after or {},
            "reason_rejected": reason,
            "detail": detail,
        }

    # Guard 1: kind
    if kind not in _PATCH_SUPPORTED_KINDS:
        return _reject(
            "unsupported_kind",
            detail=f"kind must be one of {sorted(_PATCH_SUPPORTED_KINDS)}, got {kind!r}",
        )
    # Guard 1b: operator mutation denylist
    if is_kind_denied(kind, ACTIVE_DENYLIST):
        _audit({**audit_base, "outcome": "denied_by_operator", "reason_rejected": "mutation_denied"})
        return denial_response(kind, "patch_resource_limits", extra={
            "name": name,
            "namespace": namespace,
            "container": container,
            "dry_run": dry_run,
        })
    if not limits and not requests:
        return _reject(
            "nothing_to_patch",
            detail="at least one of `limits` or `requests` must be provided",
        )

    # Fetch target
    try:
        target = _fetch_target(kind, name, namespace)
    except ApiException as e:
        return _reject("target_not_found", detail=f"{e.status}: {e.reason}")

    # Locate container in spec.template.spec.containers
    containers = (target.spec.template.spec.containers or [])
    existing = next((c for c in containers if c.name == container), None)
    if existing is None:
        return _reject(
            "container_not_found",
            detail=f"no container named {container!r} in {kind}/{name}; have: "
                   f"{[c.name for c in containers]}",
        )

    # Capture before-state (what's currently set on the container)
    cur = existing.resources
    before = {
        "limits": dict(cur.limits) if cur and cur.limits else None,
        "requests": dict(cur.requests) if cur and cur.requests else None,
    }

    # Parse ceilings
    try:
        ceil_mem = _parse_memory_bytes(absolute_ceiling_memory)
        ceil_cpu = _parse_cpu_millicores(absolute_ceiling_cpu)
    except ValueError as e:
        return _reject("bad_ceiling", detail=str(e), before=before)

    # Validate requested limits
    try:
        req_mem = _parse_memory_bytes((limits or {}).get("memory"))
        req_cpu = _parse_cpu_millicores((limits or {}).get("cpu"))
        req_req_mem = _parse_memory_bytes((requests or {}).get("memory"))
        req_req_cpu = _parse_cpu_millicores((requests or {}).get("cpu"))
    except ValueError as e:
        return _reject("bad_quantity", detail=str(e), before=before)

    cur_limits = before["limits"] or {}
    try:
        cur_mem = _parse_memory_bytes(cur_limits.get("memory"))
        cur_cpu = _parse_cpu_millicores(cur_limits.get("cpu"))
    except ValueError as e:
        return _reject("bad_current_quantity", detail=str(e), before=before)

    # Absolute ceilings
    if req_mem is not None and req_mem > ceil_mem:
        return _reject(
            "memory_exceeds_ceiling",
            detail=f"requested {req_mem}B > ceiling {ceil_mem}B "
                   f"({absolute_ceiling_memory})",
            before=before,
        )
    if req_cpu is not None and req_cpu > ceil_cpu:
        return _reject(
            "cpu_exceeds_ceiling",
            detail=f"requested {req_cpu}m > ceiling {ceil_cpu}m "
                   f"({absolute_ceiling_cpu})",
            before=before,
        )

    # Multiplier checks (only when a current limit exists)
    if req_mem is not None and cur_mem is not None and req_mem > max_multiplier * cur_mem:
        return _reject(
            "memory_exceeds_multiplier",
            detail=f"requested {req_mem}B > {max_multiplier}x current "
                   f"{cur_mem}B (= {int(max_multiplier * cur_mem)}B)",
            before=before,
        )
    if req_cpu is not None and cur_cpu is not None and req_cpu > max_multiplier * cur_cpu:
        return _reject(
            "cpu_exceeds_multiplier",
            detail=f"requested {req_cpu}m > {max_multiplier}x current "
                   f"{cur_cpu}m (= {int(max_multiplier * cur_cpu)}m)",
            before=before,
        )

    # Requests must be <= new limit for same resource (both existing and new)
    effective_mem_limit = req_mem if req_mem is not None else cur_mem
    effective_cpu_limit = req_cpu if req_cpu is not None else cur_cpu
    if req_req_mem is not None and effective_mem_limit is not None and req_req_mem > effective_mem_limit:
        return _reject(
            "memory_request_exceeds_limit",
            detail=f"requests.memory {req_req_mem}B > effective limit {effective_mem_limit}B",
            before=before,
        )
    if req_req_cpu is not None and effective_cpu_limit is not None and req_req_cpu > effective_cpu_limit:
        return _reject(
            "cpu_request_exceeds_limit",
            detail=f"requests.cpu {req_req_cpu}m > effective limit {effective_cpu_limit}m",
            before=before,
        )

    # Build the after-state for return value and the strategic-merge patch body
    new_limits = dict(before["limits"] or {})
    if limits:
        for k, v in limits.items():
            if k in ("memory", "cpu") and v is not None:
                new_limits[k] = v
    new_requests = dict(before["requests"] or {})
    if requests:
        for k, v in requests.items():
            if k in ("memory", "cpu") and v is not None:
                new_requests[k] = v

    after = {
        "limits": new_limits or None,
        "requests": new_requests or None,
    }

    patch_body = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": container,
                            "resources": {
                                "limits": new_limits or None,
                                "requests": new_requests or None,
                            },
                        }
                    ]
                }
            }
        }
    }

    if dry_run:
        _audit({**audit_base, "outcome": "accepted_dry_run", "before": before, "after": after})
        return {
            "applied": False,
            "dry_run": True,
            "kind": kind,
            "name": name,
            "namespace": namespace,
            "container": container,
            "before": before,
            "after": after,
            "reason_rejected": None,
        }

    try:
        _patch_target(kind, name, namespace, patch_body)
    except ApiException as e:
        return _reject(
            "api_error",
            detail=f"patch failed: {e.status} {e.reason}: {e.body}",
            before=before,
            after=after,
        )

    _audit({**audit_base, "outcome": "applied", "before": before, "after": after})
    return {
        "applied": True,
        "dry_run": False,
        "kind": kind,
        "name": name,
        "namespace": namespace,
        "container": container,
        "before": before,
        "after": after,
        "reason_rejected": None,
    }


# ── rollback_deployment ────────────────────────────────────────────────────────

@mcp.tool(annotations={"idempotent": False, "destructive": True, "read_only": False})
def rollback_deployment(
    name: str,
    namespace: str = "default",
    to_revision: int | None = None,
) -> dict:
    """Roll a Deployment back to a previous revision.

    Finds the ReplicaSet owned by this Deployment at the target revision
    and patches the Deployment's pod template back to that ReplicaSet's
    template. This mirrors kubectl rollout undo semantics.

    Args:
        name: Deployment name.
        namespace: Kubernetes namespace (default "default").
        to_revision: Target revision number. If None, rolls back to the
            immediately-previous revision (current - 1). If specified,
            the revision must exist in the Deployment's ReplicaSet history.

    Returns:
        dict with:
          - rolled_back (bool): True if a patch was applied.
          - from_revision (str): Revision before the rollback.
          - to_revision (str): Revision selected as the target.
          - error (str, optional): Short error code on failure.
          - reason (str, optional): Human-readable error detail.

    Denied-by-operator behaviour mirrors other mutation tools: returns
    a structured mutation_denied dict instead of calling the API.
    """
    denied = _guard_kind("Deployment", "rollback_deployment")
    if denied:
        return denied

    a = apps()
    try:
        dep = a.read_namespaced_deployment(name, namespace)
    except ApiException as e:
        return {
            "rolled_back": False,
            "error": "deployment_not_found",
            "reason": f"{e.status}: {e.reason}",
            "from_revision": None,
            "to_revision": None,
        }

    annotations = (dep.metadata.annotations or {})
    current_rev = annotations.get("deployment.kubernetes.io/revision")

    # Gather owned ReplicaSets with their revisions.
    match_labels = dep.spec.selector.match_labels or {}
    label_selector = ",".join(f"{k}={v}" for k, v in match_labels.items())
    rs_list = a.list_namespaced_replica_set(namespace, label_selector=label_selector)

    owned = []
    for rs in rs_list.items:
        owner_refs = rs.metadata.owner_references or []
        is_owned = any(
            ref.kind == "Deployment" and ref.name == name and ref.controller
            for ref in owner_refs
        )
        if not is_owned:
            continue
        rs_rev = (rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision")
        if rs_rev is None:
            continue
        try:
            owned.append((int(rs_rev), rs))
        except ValueError:
            continue

    if not owned:
        return {
            "rolled_back": False,
            "error": "no_revisions_found",
            "reason": "no owned ReplicaSets with revision annotations",
            "from_revision": current_rev,
            "to_revision": None,
        }

    owned.sort(key=lambda t: t[0])  # ascending

    # Pick target.
    try:
        current_rev_int = int(current_rev) if current_rev is not None else None
    except ValueError:
        current_rev_int = None

    if to_revision is None:
        # Previous = largest revision strictly below current.
        candidates = [r for r, _ in owned if current_rev_int is None or r < current_rev_int]
        if not candidates:
            return {
                "rolled_back": False,
                "error": "no_previous_revision",
                "reason": "no revision strictly below current; only this revision exists",
                "from_revision": current_rev,
                "to_revision": None,
            }
        target_rev = max(candidates)
    else:
        target_rev = int(to_revision)
        if target_rev not in [r for r, _ in owned]:
            return {
                "rolled_back": False,
                "error": "target_revision_not_found",
                "reason": f"revision {target_rev} not present; available: {sorted(r for r,_ in owned)}",
                "from_revision": current_rev,
                "to_revision": None,
            }
        if current_rev_int is not None and target_rev == current_rev_int:
            return {
                "rolled_back": False,
                "error": "target_revision_is_current",
                "reason": f"revision {target_rev} is already the current revision",
                "from_revision": current_rev,
                "to_revision": str(target_rev),
            }

    target_rs = next(rs for rev, rs in owned if rev == target_rev)
    target_template = target_rs.spec.template

    # Build the patch body: clear the current revision annotation from the
    # pod template (the controller assigns it fresh) and replace the spec
    # template with the old ReplicaSet's template. kubectl rollout undo
    # does the same thing under the hood.
    template_dict = a.api_client.sanitize_for_serialization(target_template)
    # Drop the pod-template-hash label - k8s will recompute it from the new
    # template so the rollback revision gets a new ReplicaSet.
    try:
        labels = template_dict.get("metadata", {}).get("labels", {}) or {}
        labels.pop("pod-template-hash", None)
        if "metadata" in template_dict:
            template_dict["metadata"]["labels"] = labels
    except Exception:
        pass

    patch_body = {"spec": {"template": template_dict}}

    try:
        a.patch_namespaced_deployment(name, namespace, patch_body)
    except ApiException as e:
        return {
            "rolled_back": False,
            "error": "api_error",
            "reason": f"{e.status}: {e.reason}",
            "from_revision": current_rev,
            "to_revision": str(target_rev),
        }

    return {
        "rolled_back": True,
        "from_revision": current_rev,
        "to_revision": str(target_rev),
    }

# ── Cluster summary + search (M10) ─────────────────────────────────────────────


@mcp.tool(annotations={"idempotent": True, "destructive": False, "read_only": True})
def get_cluster_summary() -> dict:
    """Single-call cluster-wide health snapshot.

    Returns aggregate counts for nodes, pods, deployments, and namespaces so
    the agent can form a top-level picture without chaining 5+ list calls.
    """
    # Nodes
    nodes = core().list_node()
    node_ready = 0
    node_not_ready = 0
    for n in nodes.items:
        ready_cond = next(
            (c for c in (n.status.conditions or []) if c.type == "Ready"),
            None,
        )
        if ready_cond and ready_cond.status == "True":
            node_ready += 1
        else:
            node_not_ready += 1

    # Pods (all namespaces)
    pods = core().list_pod_for_all_namespaces()
    pod_counts = {"running": 0, "pending": 0, "failed": 0, "succeeded": 0, "crashloop": 0}
    for p in pods.items:
        phase = (p.status.phase or "").lower()
        if phase in pod_counts:
            pod_counts[phase] += 1
        for cs in (p.status.container_statuses or []):
            if cs.state and cs.state.waiting and cs.state.waiting.reason == "CrashLoopBackOff":
                pod_counts["crashloop"] += 1
                break

    # Deployments (all namespaces)
    deps = apps().list_deployment_for_all_namespaces()
    dep_available = 0
    dep_progressing = 0
    dep_stuck = 0
    for d in deps.items:
        conds = (d.status.conditions or []) if d.status else []
        is_available = any(c.type == "Available" and c.status == "True" for c in conds)
        is_progressing = any(c.type == "Progressing" and c.status == "True" for c in conds)
        is_stuck = any(
            c.type == "Progressing" and c.reason == "ProgressDeadlineExceeded"
            for c in conds
        )
        if is_stuck:
            dep_stuck += 1
        elif is_available:
            dep_available += 1
        elif is_progressing:
            dep_progressing += 1

    nss = core().list_namespace()

    return {
        "nodes": {"ready": node_ready, "not_ready": node_not_ready, "total": len(nodes.items)},
        "pods": pod_counts,
        "deployments": {
            "available": dep_available,
            "progressing": dep_progressing,
            "stuck": dep_stuck,
            "total": len(deps.items),
        },
        "namespace_count": len(nss.items),
    }


# Mapping: kind → (list_namespaced_fn_name, list_all_ns_fn_name, api_getter)
_SEARCH_KIND_DISPATCH = {
    "pod": ("list_namespaced_pod", "list_pod_for_all_namespaces", "core"),
    "service": ("list_namespaced_service", "list_service_for_all_namespaces", "core"),
    "configmap": ("list_namespaced_config_map", "list_config_map_for_all_namespaces", "core"),
    "secret": ("list_namespaced_secret", "list_secret_for_all_namespaces", "core"),
    "deployment": ("list_namespaced_deployment", "list_deployment_for_all_namespaces", "apps"),
    "daemonset": ("list_namespaced_daemon_set", "list_daemon_set_for_all_namespaces", "apps"),
    "statefulset": ("list_namespaced_stateful_set", "list_stateful_set_for_all_namespaces", "apps"),
    "job": ("list_namespaced_job", "list_job_for_all_namespaces", "batch"),
}


def _get_api(name: str):
    if name == "core":
        return core()
    if name == "apps":
        return apps()
    if name == "batch":
        return client.BatchV1Api()
    raise ValueError(f"unknown api: {name}")


@mcp.tool(annotations={"idempotent": True, "destructive": False, "read_only": True})
def search_resources(
    kind: str,
    label_selector: str | None = None,
    field_selector: str | None = None,
    namespace: str | None = None,
    limit: int = 100,
    continue_token: str | None = None,
) -> dict:
    """Label/field-selector search across a given resource kind.

    Supported kinds: pod, deployment, service, configmap, secret, job,
    daemonset, statefulset. Pass `namespace=None` to search cluster-wide.
    Pagination via `limit` + `continue_token`.
    """
    kind_lower = kind.lower().rstrip("s") if kind.lower().endswith("sets") else kind.lower()
    if kind_lower not in _SEARCH_KIND_DISPATCH:
        return {
            "error": "unsupported_kind",
            "kind": kind,
            "supported": sorted(_SEARCH_KIND_DISPATCH.keys()),
        }

    ns_fn, all_ns_fn, api_name = _SEARCH_KIND_DISPATCH[kind_lower]
    api = _get_api(api_name)

    kwargs = {"limit": limit}
    if label_selector:
        kwargs["label_selector"] = label_selector
    if field_selector:
        kwargs["field_selector"] = field_selector
    if continue_token:
        kwargs["_continue"] = continue_token

    try:
        if namespace:
            result = getattr(api, ns_fn)(namespace, **kwargs)
        else:
            result = getattr(api, all_ns_fn)(**kwargs)
    except ApiException as e:
        return {"error": "api_error", "status": e.status, "reason": e.reason}

    items = [
        {
            "kind": kind_lower,
            "name": r.metadata.name,
            "namespace": r.metadata.namespace,
            "created": r.metadata.creation_timestamp.isoformat() if r.metadata.creation_timestamp else None,
        }
        for r in result.items
    ]
    next_token = None
    if hasattr(result.metadata, "_continue") and result.metadata._continue:
        next_token = result.metadata._continue

    return {"items": items, "continue": next_token, "count": len(items)}


# Mapping: kind → (read_fn_name, api_getter, is_namespaced)
_YAML_KIND_DISPATCH = {
    "pod": ("read_namespaced_pod", "core", True),
    "service": ("read_namespaced_service", "core", True),
    "configmap": ("read_namespaced_config_map", "core", True),
    "secret": ("read_namespaced_secret", "core", True),
    "namespace": ("read_namespace", "core", False),
    "node": ("read_node", "core", False),
    "persistentvolume": ("read_persistent_volume", "core", False),
    "persistentvolumeclaim": ("read_namespaced_persistent_volume_claim", "core", True),
    "deployment": ("read_namespaced_deployment", "apps", True),
    "daemonset": ("read_namespaced_daemon_set", "apps", True),
    "statefulset": ("read_namespaced_stateful_set", "apps", True),
    "replicaset": ("read_namespaced_replica_set", "apps", True),
    "job": ("read_namespaced_job", "batch", True),
}


@mcp.tool(annotations={"idempotent": True, "destructive": False, "read_only": True})
def get_resource_yaml(kind: str, name: str, namespace: str | None = None) -> dict:
    """Return the raw manifest (as a dict) for any supported resource.

    Useful when the agent needs to reason about drift — the full spec
    is returned with managed-fields stripped.
    """
    kind_lower = kind.lower()
    if kind_lower not in _YAML_KIND_DISPATCH:
        return {
            "error": "unsupported_kind",
            "kind": kind,
            "supported": sorted(_YAML_KIND_DISPATCH.keys()),
        }
    fn_name, api_name, is_namespaced = _YAML_KIND_DISPATCH[kind_lower]
    api = _get_api(api_name)

    try:
        if is_namespaced:
            if not namespace:
                return {"error": "namespace_required", "kind": kind_lower}
            obj = getattr(api, fn_name)(name, namespace)
        else:
            obj = getattr(api, fn_name)(name)
    except ApiException as e:
        return {"error": "api_error", "status": e.status, "reason": e.reason, "kind": kind, "name": name}

    manifest = strip_managed_fields(obj.to_dict())
    return {
        "kind": kind_lower,
        "name": name,
        "namespace": namespace,
        "manifest": manifest,
    }

# ── Loki log queries (M11) ─────────────────────────────────────────────────────
#
# Loki is queried via the Kubernetes API server's service-proxy endpoint rather
# than direct HTTP. This works regardless of Docker networking between mcp-k8s
# and the cluster — the kubeconfig already grants reach into the cluster. The
# tradeoff is ~5-10ms extra latency per query (proxied through kube-apiserver).
#
# Override defaults via env vars if Loki is installed in a different namespace
# or under a different service name:
#   LOKI_SERVICE=loki  LOKI_PORT=3100  LOKI_NAMESPACE=monitoring


LOKI_SERVICE = os.environ.get("LOKI_SERVICE", "loki")
LOKI_PORT = int(os.environ.get("LOKI_PORT", "3100"))
LOKI_NAMESPACE = os.environ.get("LOKI_NAMESPACE", "monitoring")


@mcp.tool(annotations={"idempotent": True, "destructive": False, "read_only": True})
def query_loki(
    query: str,
    start: str | None = None,
    end: str | None = None,
    limit: int = 100,
) -> dict:
    """LogQL query via the k8s API server's service-proxy to Loki.

    `start`/`end` accept Loki's range formats (nanosecond epochs or RFC3339).
    When omitted, Loki defaults to its server-side window (typically 1h).
    Target is governed by `LOKI_SERVICE` / `LOKI_PORT` / `LOKI_NAMESPACE`
    env vars (defaults: `loki` / `3100` / `monitoring`).
    """
    query_params: list[tuple[str, str]] = [("query", query), ("limit", str(limit))]
    if start:
        query_params.append(("start", start))
    if end:
        query_params.append(("end", end))

    resource_path = (
        f"/api/v1/namespaces/{LOKI_NAMESPACE}"
        f"/services/{LOKI_SERVICE}:{LOKI_PORT}/proxy/loki/api/v1/query_range"
    )

    try:
        api_client = core().api_client
        raw = api_client.call_api(
            resource_path,
            "GET",
            path_params={},
            query_params=query_params,
            header_params={"Accept": "application/json"},
            body=None,
            response_type=None,
            auth_settings=["BearerToken"],
            _preload_content=False,
        )
        # _preload_content=False returns the urllib3 HTTPResponse
        resp = raw[0] if isinstance(raw, tuple) else raw
        payload = resp.data.decode("utf-8") if hasattr(resp, "data") else str(resp)
        data = json.loads(payload).get("data", {})
    except ApiException as e:
        return {"error": "loki_api_error", "status": e.status, "reason": e.reason}
    except Exception as e:
        return {"error": "loki_request_failed", "detail": str(e)}

    streams = [
        {"labels": s.get("stream", {}), "values": s.get("values", [])}
        for s in data.get("result", [])
    ]
    return {
        "streams": streams,
        "result_type": data.get("resultType"),
        "stream_count": len(streams),
    }


@mcp.tool(annotations={"idempotent": True, "destructive": False, "read_only": True})
def search_logs(
    namespace: str,
    pattern: str,
    time_range_seconds: int = 3600,
    limit: int = 100,
) -> dict:
    """Regex search across all pod logs in a namespace via Loki.

    Convenience wrapper around `query_loki` that builds the LogQL label
    selector and time window. Fails fast if Loki is unreachable.
    """
    import time

    end_ns = int(time.time() * 1e9)
    start_ns = end_ns - (time_range_seconds * int(1e9))

    escaped = pattern.replace('"', r'\"')
    logql = f'{{namespace="{namespace}"}} |~ "{escaped}"'

    return query_loki(logql, start=str(start_ns), end=str(end_ns), limit=limit)

# ── Entrypoint ───────────────────────────────────────────────────────────────────

# Apply safety mode restrictions after all tools are registered
apply_safety_mode()


# Health endpoint — lightweight probe for stack-health monitoring.
from starlette.requests import Request
from starlette.responses import JSONResponse

@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    try:
        v1 = client.CoreV1Api()
        v1.list_namespace(limit=1)
        cluster_reachable = True
    except Exception:
        cluster_reachable = False
    return JSONResponse({
        "status": "ok" if cluster_reachable else "degraded",
        "safety_mode": MCP_SAFETY_MODE,
        "cluster_reachable": cluster_reachable,
    })


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
