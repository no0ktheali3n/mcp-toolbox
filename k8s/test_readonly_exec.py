"""Unit tests for read_only_exec — allowlisted, read-only in-container exec —
and its allowlist gate (_is_allowed_exec).

Pure-logic / monkeypatched (no live cluster), house style. read_only_exec
stays OUT of WRITE_TOOLS so it survives MCP_SAFETY_MODE=read-only; the
allowlist (not RBAC) is the wall, so the gate logic is the load-bearing part.
Run:
    KUBECONFIG=<any kubeconfig> python3 test_readonly_exec.py
or in-container:
    docker exec mcp-k8s python3 /app/test_readonly_exec.py
"""
import sys
sys.path.insert(0, "/app")  # in-container import path (no-op on host)
import mcp_k8s


def ok(cond, label, detail=""):
    marker = "PASS" if cond else "FAIL"
    print(f"  [{marker}] {label}" + (f"  -- {detail}" if detail else ""))
    return cond


passes = True


def allowed(cmd):
    a, _ = mcp_k8s._is_allowed_exec(cmd)
    return a


def reason(cmd):
    _, r = mcp_k8s._is_allowed_exec(cmd)
    return r


print("=== allowlist gate — allow read-only diagnostic commands ===")
passes &= ok(allowed(["etcdctl", "member", "list"]), "etcdctl member list")
passes &= ok(allowed(["etcdctl", "endpoint", "health"]), "etcdctl endpoint health")
passes &= ok(allowed(["etcdctl", "endpoint", "status"]), "etcdctl endpoint status")
passes &= ok(allowed(["etcdctl", "get", "/registry/x"]), "etcdctl get <key>")
passes &= ok(allowed(["pg_isready"]), "pg_isready")
passes &= ok(allowed(["pg_isready", "-h", "db", "-p", "5432"]), "pg_isready with flags")
passes &= ok(allowed(["redis-cli", "PING"]), "redis-cli PING")
passes &= ok(allowed(["redis-cli", "ping"]), "redis-cli ping (case-insensitive subcommand)")
passes &= ok(allowed(["redis-cli", "INFO"]), "redis-cli INFO")
passes &= ok(allowed(["redis-cli", "CLIENT", "LIST"]), "redis-cli CLIENT LIST (two-token)")
passes &= ok(allowed(["nginx", "-T"]), "nginx -T")
passes &= ok(allowed(["ss", "-tlnp"]), "ss -tlnp")
passes &= ok(allowed(["nslookup", "kubernetes.default"]), "nslookup")
passes &= ok(allowed(["/usr/local/bin/etcdctl", "member", "list"]),
             "absolute-path binary resolves by basename")

print("\n=== allowlist gate — reject writes / escapes ===")
passes &= ok(not allowed(["etcdctl", "put", "k", "v"]), "etcdctl put rejected (write subcommand)")
passes &= ok(not allowed(["etcdctl", "member", "remove", "abc"]),
             "etcdctl member remove rejected (write under allowed top-token)")
passes &= ok(not allowed(["redis-cli", "FLUSHALL"]), "redis-cli FLUSHALL rejected")
passes &= ok(not allowed(["redis-cli", "SET", "k", "v"]), "redis-cli SET rejected")
passes &= ok(not allowed(["rm", "-rf", "/"]), "rm rejected (binary not allowlisted)")
passes &= ok(not allowed(["kubectl", "get", "pods"]), "kubectl rejected (not allowlisted)")
passes &= ok(reason(["sh", "-c", "etcdctl member list"]) == "shell_not_allowed",
             "sh -c rejected as shell (bypasses allowlist)")
passes &= ok(reason(["bash", "-c", "ls"]) == "shell_not_allowed", "bash -c rejected as shell")
passes &= ok(reason(["etcdctl", "get", "x; rm -rf /"]) == "shell_metacharacter",
             "shell metachar in arg rejected")
passes &= ok(reason(["etcdctl", "get", "$(whoami)"]) == "shell_metacharacter",
             "command-substitution metachar rejected")
passes &= ok(reason([]) == "empty_command", "empty command rejected")
passes &= ok(not allowed(["cat", "/var/run/secrets/x"]),
             "cat rejected (raw file read — secret boundary, excluded)")
passes &= ok(not allowed(["env"]), "env rejected (would dump injected secrets)")

print("\n=== tool classification ===")
passes &= ok("read_only_exec" in mcp_k8s.mcp._tool_manager._tools, "read_only_exec registered")
passes &= ok("read_only_exec" not in mcp_k8s.WRITE_TOOLS,
             "read_only_exec NOT in WRITE_TOOLS (survives read-only safety mode)")
passes &= ok("exec_command" in mcp_k8s.WRITE_TOOLS,
             "exec_command stays in WRITE_TOOLS (unchanged, operator-only)")


class _FakeCore:
    def __init__(self): self.exec_attr = "connect-sentinel"
    @property
    def connect_get_namespaced_pod_exec(self):
        return self.exec_attr


print("\n=== read_only_exec happy path (allowed -> streams) ===")
stream_calls = []


def _fake_stream(fn, pod, namespace, **kw):
    stream_calls.append((pod, namespace, kw.get("command")))
    return "etcd-1\netcd-2\netcd-3\n"


mcp_k8s.core = lambda: _FakeCore()
mcp_k8s.stream = _fake_stream
r = mcp_k8s.read_only_exec(pod_name="etcd-0", command=["etcdctl", "member", "list"],
                          namespace="kube-system")
passes &= ok(r.get("allowed") is True and "etcd-1" in r.get("stdout", ""),
             "allowed command returns stdout", str(r)[:90])
passes &= ok(len(stream_calls) == 1 and stream_calls[0][0] == "etcd-0",
             "stream() called once for allowed command", str(stream_calls))

print("\n=== read_only_exec reject path (does NOT stream) ===")
stream_calls.clear()
r = mcp_k8s.read_only_exec(pod_name="etcd-0", command=["etcdctl", "put", "k", "v"],
                          namespace="kube-system")
passes &= ok(r.get("error") == "command_not_allowed", "disallowed -> command_not_allowed", str(r)[:90])
passes &= ok(len(stream_calls) == 0, "stream() NEVER called for disallowed command (no side effect)")

print("\n" + "=" * 60)
print(f"OVERALL: {'ALL PASS' if passes else 'FAILURES PRESENT'}")
sys.exit(0 if passes else 1)
