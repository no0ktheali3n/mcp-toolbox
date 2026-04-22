# mcp-k8s

FastMCP-based MCP server for Kubernetes cluster management. Exposes tools for
AI agents (Agent Zero) to interact with Kubernetes clusters via streamable-http.

## Requirements
- Docker + Docker Compose
- kubectl on host (mounted into container)
- k3d cluster with kubeconfig at `k8s/kubeconfig-internal.yaml`

## Quick Start
```bash
# Copy kubeconfig from your cluster setup
cp ~path/to/your/k8s/kubeconfig-internal.yaml k8s/kubeconfig-internal.yaml

# Build and start
docker compose -f k8s/docker-compose.yml up -d --build

# Verify
docker logs mcp-k8s --tail 20
```

## Development Workflow
```bash
# After making changes to k8s/mcp_k8s.py
docker compose -f k8s/docker-compose.yml up -d --build
docker logs mcp-k8s --tail 20
```

## Tools

### Discovery Tools

#### `list_namespaces()`
List all namespaces in the cluster with their status (Active/Terminating).

**Returns:** Array of `{name, status}` objects.

---

#### `list_pods(namespace="default", label_selector=None, field_selector=None, limit=50, all_namespaces=False)`
List pods with filtering and pagination support.

**Parameters:**
- `namespace` — Target namespace (ignored if `all_namespaces=True`)
- `label_selector` — Kubernetes label selector (e.g., `"app=nginx,tier=frontend"`)
- `field_selector` — Kubernetes field selector (e.g., `"status.phase!=Running"`)
- `limit` — Maximum pods to return (default: 50)
- `all_namespaces` — List across all namespaces (default: False)

**Returns:**
```json
{
  "pods": [{
    "name": "nginx-abc123",
    "status": "Running",
    "ready": "1/1",
    "restarts": 0,
    "node": "k3d-mcp-cluster-server-0",
    "age": "2026-03-24T10:00:00Z"
  }],
  "metadata": {
    "continue": "ey...",  // Pagination token for next page
    "remaining_count": 0  // Approximate remaining items
  }
}
```

**Why it matters:** Field selectors enable powerful queries like "find all non-Running pods cluster-wide" without client-side filtering.

---

#### `list_deployments(namespace="default")`
List deployments with replica counts and container image.

**Returns:** Array of `{name, desired, ready, available, image}` objects.

---

#### `list_services(namespace="default", limit=100, continue_token=None)`
List services with type, cluster IP, and port mappings. Paginated.

**Parameters:**
- `namespace` — Target namespace
- `limit` — Maximum services per page (default: 100)
- `continue_token` — Resume token from a previous call's `continue` field

**Returns:**
```json
{
  "items": [{"name": "nginx", "type": "ClusterIP", "cluster_ip": "10.43.0.1", "ports": [...]}],
  "continue": "eyJ2IjoibWV0YS5rOHMu..." // or null on the last page
}
```

**Backward compat:** calling `list_services(namespace)` with no new params still works — the default `limit=100` is enough to return every service in a typical namespace.

---

#### `list_configmaps(namespace="default", limit=100, continue_token=None)`
List configmaps and their data keys (not values). Paginated.

**Parameters:**
- `namespace` — Target namespace
- `limit` — Maximum configmaps per page (default: 100)
- `continue_token` — Resume token from a previous call's `continue` field

**Returns:**
```json
{
  "items": [{"name": "kube-root-ca.crt", "keys": ["ca.crt"]}],
  "continue": null
}
```

**Why it matters:** Large namespaces (kube-system, monitoring) can have 50+ configmaps — paging keeps responses bounded.

---

#### `get_cluster_summary()`
Single-call cluster-wide health snapshot — aggregates nodes, pods, deployments, and namespaces into one structured response.

**Returns:**
```json
{
  "nodes": {"ready": 3, "not_ready": 0, "total": 3},
  "pods": {"running": 47, "pending": 0, "failed": 0, "succeeded": 2, "crashloop": 0},
  "deployments": {"available": 15, "progressing": 0, "stuck": 0, "total": 15},
  "namespace_count": 8
}
```

**Why it matters:** Lets the agent form a top-level cluster picture with one tool call instead of chaining 5+ list calls.

---

#### `search_resources(kind, label_selector=None, field_selector=None, namespace=None, limit=100, continue_token=None)`
Label/field-selector search across any supported resource kind. Pass `namespace=None` to search cluster-wide.

**Supported kinds:** `pod`, `service`, `configmap`, `secret`, `deployment`, `daemonset`, `statefulset`, `job`

**Returns:**
```json
{
  "items": [{"kind": "deployment", "name": "nginx", "namespace": "default", "created": "2026-04-22T..."}],
  "continue": "eyJ2...",
  "count": 1
}
```

**Why it matters:** Replaces `kubectl get {kind} -l label=value -A` with a structured, paginated API — essential for the agent when hunting for resources by label without knowing the namespace.

---

#### `get_resource_yaml(kind, name, namespace=None)`
Return the full manifest (as a dict) for any supported resource with `managedFields` stripped.

**Supported kinds (namespaced):** `pod`, `service`, `configmap`, `secret`, `deployment`, `daemonset`, `statefulset`, `replicaset`, `job`, `persistentvolumeclaim`

**Supported kinds (cluster-scoped):** `namespace`, `node`, `persistentvolume`

**Returns:** `{"kind": "deployment", "name": "nginx", "namespace": "default", "manifest": { ... full spec ... }}`

**Why it matters:** Drift diagnosis — the agent can pull the live spec and reason about divergence from source-of-truth without needing a separate kubectl wrapper.

---

### Diagnosis Tools

#### `get_node_status()`
Get status and info for all cluster nodes.

**Returns:** Array of `{name, ready, roles, version, os}` objects.

**Why it matters:** Quick cluster health check — identifies node-level issues before diving into pod diagnosis.

---

#### `get_pod_detail(pod_name, namespace="default")`
Get comprehensive pod details including conditions, container states, resources, and recent events.

**Returns:**
```json
{
  "name": "web-app-abc123",
  "namespace": "default",
  "node": "k3d-mcp-cluster-server-0",
  "phase": "Running",
  "qos_class": "Burstable",
  "pod_ip": "10.42.0.15",
  "host_ip": "172.18.0.2",
  "conditions": [{"type": "Ready", "status": "True", "reason": ""}],
  "containers": [{
    "name": "app",
    "image": "myapp:v1.2.3",
    "ready": true,
    "restarts": 5,
    "state": {
      "running": {"started_at": "2026-03-24T10:00:00Z"},
      "waiting": null,
      "terminated": null
    },
    "resources": {
      "requests": {"cpu": "100m", "memory": "128Mi"},
      "limits": {"cpu": "500m", "memory": "512Mi"}
    },
    "volume_mounts": [{"name": "config", "mount_path": "/etc/config", "read_only": true}]
  }],
  "events": [{"reason": "Started", "message": "...", "type": "Normal"}]
}
```

**QoS Class explanation:**
| Value | Meaning |
|-------|---------|
| `Guaranteed` | All containers have equal requests and limits — highest priority, last to be evicted |
| `Burstable` | Some resources specified — medium priority |
| `BestEffort` | No resources specified — first to be killed under node pressure |

**Why it matters:** Single-call diagnosis — replaces `kubectl describe pod` + `kubectl get events` with structured JSON optimized for LLM consumption.

---

#### `find_unhealthy_pods(namespace="", restart_threshold=5, include_completed=False, limit=25, sort_by="restarts")`
Find unhealthy pods across all namespaces or a specific namespace.

**Parameters:**
- `namespace` — Empty string = all namespaces
- `restart_threshold` — Flag pods with restarts >= this value (default: 5)
- `include_completed` — Include Succeeded/Failed pods (default: False)
- `limit` — Maximum pods to return (default: 25)
- `sort_by` — Sort key: `"restarts"` or `"age"` (default: "restarts")

**Returns:**
```json
{
  "pods": [{
    "name": "crash-abc123",
    "namespace": "default",
    "phase": "CrashLoopBackOff",
    "restart_count": 10,
    "last_restart_reason": "Error",
    "age": "2026-03-24T10:00:00Z"
  }],
  "total_unhealthy": 47,    // Total found before limit
  "returned_count": 1,      // == len(pods)
  "truncated": true         // True if total_unhealthy > limit
}
```

**Why it matters:** Entry point for autonomous diagnosis — the agent calls this first to identify which pods need investigation.

---

#### `get_deployment_health(name, namespace="default")`
Get comprehensive deployment health report including replicasets, pods, events, and rollout status.

**Returns:**
```json
{
  "deployment": {
    "name": "web-app",
    "namespace": "default",
    "desired_replicas": 3,
    "ready_replicas": 2,
    "available_replicas": 2,
    "unavailable_replicas": 1,
    "strategy": "RollingUpdate",
    "image": "myapp:v1.2.3"
  },
  "replicasets": [{
    "name": "web-app-abc123",
    "desired": 3,
    "ready": 2,
    "available": 2
  }],
  "pods": [{
    "name": "web-app-abc123-xyz",
    "phase": "Running",
    "restart_count": 0,
    "container_states": [{"name": "app", "state": "running"}]
  }],
  "events": [...],
  "rollout_status": {
    "progressing": false,
    "stalled": false,
    "complete": true
  },
  "conditions": [{"type": "Available", "status": "True", "reason": ""}]
}
```

**Why it matters:** Single-call deployment diagnosis — aggregates deployment + replicasets + pods + events into one structured response.

---

#### `get_pod_logs(pod_name, namespace="default", container="", tail_lines=100, since_seconds=None, previous=False)`
Get logs from a pod's container.

**Parameters:**
- `pod_name` — Pod name (required)
- `namespace` — Namespace (default: "default")
- `container` — Container name (required if pod has multiple containers)
- `tail_lines` — Number of lines from end (default: 100)
- `since_seconds` — Time window for logs (e.g., 60 = last 60 seconds)
- `previous` — Get logs from previously terminated container (default: False)

**Why it matters:** `previous=True` is essential for crash-loop diagnosis — retrieves logs from the container instance that crashed.

---

#### `get_events(namespace="default", limit=20, involved_object_name=None, involved_object_kind=None, event_type=None, field_selector=None)`
Get recent events with optional filtering.

**Parameters:**
- `namespace` — Target namespace
- `limit` — Maximum events to return (default: 20)
- `involved_object_name` — Filter by resource name (e.g., "nginx-abc123")
- `involved_object_kind` — Filter by resource kind (e.g., "Pod", "Deployment")
- `event_type` — Filter by type: "Normal" or "Warning"
- `field_selector` — Raw Kubernetes field-selector string (e.g., `"involvedObject.kind=Pod,involvedObject.name=foo"`). Merged with the structured params above — use whichever is more convenient.

**Why it matters:** Field-selector filtering pushes queries to etcd — efficient for "show me only Warning events for this pod." The raw `field_selector` param is an escape hatch for selector combinations the structured params cannot express.

---

#### `exec_command(pod_name, command, namespace="default", container="")`
Execute a command inside a running pod's container.

**Parameters:**
- `pod_name` — Pod name (required)
- `command` — Command as list (e.g., `["ls", "-la"]`)
- `namespace` — Namespace (default: "default")
- `container` — Container name (required for multi-container pods)

**Returns:** Combined stdout + stderr.

---

### Remediation Tools

#### `scale_deployment(name, replicas, namespace="default")`
Scale a deployment to specified replicas.

**Returns:** `"Scaled deployment/{name} to {replicas} replicas"`

---

#### `restart_deployment(name, namespace="default")`
Perform a rolling restart of a deployment (adds restart annotation to trigger rollout).
Honors the operator mutation denylist (see below) - if `Deployment` is denied, returns
a structured `{"error": "mutation_denied", ...}` dict without calling the API.

**Returns:** `"Restarted deployment/{name}"` on success, or a mutation-denied dict.

---

#### `rollback_deployment(name, namespace="default", to_revision=None)`
Roll a Deployment back to a previous revision. Finds the ReplicaSet that owns
the target revision and patches the Deployment's pod template back to that
ReplicaSet's template - same semantics as `kubectl rollout undo`.

**Parameters:**
- `name` - Deployment name
- `namespace` - Namespace (default: "default")
- `to_revision` - Target revision number. If `None`, rolls back to the
  immediately-previous revision (largest revision strictly below current).

**Returns:**
```json
{
  "rolled_back": true,
  "from_revision": "8",
  "to_revision": "7"
}
```

**Error shapes (no raise):**
- `deployment_not_found` - Deployment doesn't exist.
- `no_previous_revision` - Only one revision recorded; nothing to roll back to.
- `target_revision_not_found` - Caller asked for a revision not in history.
- `target_revision_is_current` - Caller asked to roll back to the current revision.
- `api_error` - k8s API rejected the patch.
- `mutation_denied` - Operator denylist includes "Deployment".

**Why it matters:** After a bad deployment lands, the agent can surgically
return to a known-good revision without needing a manifest on disk.

---

### Operator mutation denylist (`MCP_K8S_DENYLIST`)

Operators can forbid the agent from mutating specific Kubernetes resource
kinds. Mutation tools (`patch_resource_limits`, `restart_deployment`,
`rollback_deployment`) route through the denylist before calling the API.
Denied requests return a structured error dict:

```json
{
  "error": "mutation_denied",
  "kind": "Secret",
  "tool": "restart_deployment",
  "reason": "operator denylist",
  "denylist_env_var": "MCP_K8S_DENYLIST"
}
```

**Default denylist:** `Secret`, `ClusterRole`, `ClusterRoleBinding`, `ServiceAccount`

**Override:** set `MCP_K8S_DENYLIST` to a comma-separated list of kinds
(PascalCase). The env var REPLACES the default; repeat the baseline kinds
to extend it rather than replace:

```bash
# Replace default with an empty denylist (DANGEROUS)
MCP_K8S_DENYLIST= docker compose up -d

# Add Pod on top of the default
MCP_K8S_DENYLIST=Secret,ClusterRole,ClusterRoleBinding,ServiceAccount,Pod docker compose up -d
```

Matching is case-insensitive. Denylist is read once at container start -
it is a deploy-time decision, not a runtime toggle.

Implementation: `mutation_guard.py` exposes `DEFAULT_DENYLIST`,
`ACTIVE_DENYLIST`, `is_kind_denied()`, `load_denylist_from_env()`, and
`guard()` as the single source of truth.

---

#### `apply_manifest(manifest_yaml)`
Apply a Kubernetes YAML manifest to the cluster.

**Parameters:**
- `manifest_yaml` — Full YAML string (can contain multiple documents)

**Returns:** kubectl apply output.

---

#### `delete_resource(resource_type, name, namespace="default")`
Delete a Kubernetes resource.

**Parameters:**
- `resource_type` — Resource kind (e.g., "pod", "deployment", "service", "configmap")
- `name` — Resource name
- `namespace` — Namespace (default: "default")

**Returns:** kubectl delete output.

---

## Utility Functions (Internal)

### `strip_managed_fields(obj: dict) -> dict`
Remove verbose Kubernetes metadata from API responses:
- `metadata.managedFields` — Internal field-manager bookkeeping (often 100+ lines)
- `metadata.annotations["kubectl.kubernetes.io/last-applied-configuration"]` — Full last-applied YAML

**Applied to:** All tools returning Kubernetes objects (`get_pod_detail`, `get_deployment_health`).

**Why it matters:** Reduces token consumption by 30-50% on typical responses.

---

### `get_qos_class(pod) -> str`
Calculate pod QoS (Quality of Service) class based on resource requests/limits.

**Returns:** `"Guaranteed"`, `"Burstable"`, or `"BestEffort"`

| Class | Criteria | OOM Kill Priority |
|-------|----------|-------------------|
| `Guaranteed` | All containers have requests == limits | Last |
| `Burstable` | Some resources specified | Medium |
| `BestEffort` | No resources specified | First |

**Why it matters:** When diagnosing why a pod was OOM killed or evicted, QoS class indicates whether the behavior was expected.

---

## Repos
- Infrastructure: https://github.com/no0ktheali3n/expert-k8
- MCP server: https://github.com/no0ktheali3n/mcp-toolbox
