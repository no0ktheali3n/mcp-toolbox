#!/usr/bin/env bash
# fixtures/test_scenario_1_tag_mismatch.sh
#
# M12 Scenario 1 — image tag-format mismatch between Helm chart and registry.
#
# What this sets up
# -----------------
#   Registry (registry:2 @ http://registry.localhost):
#     demo/whoami:1.2.1
#     demo/whoami:1.2.2
#     demo/whoami:1.2.3     <-- target tag, but NO "v" prefix
#
#   Gitea chart (http://gitea.localhost/giteaadmin/demo-chart):
#     charts/demo-app/values.yaml  ->  image.tag: v1.2.3   <-- expects the v prefix
#
#   Cluster (default ns):
#     Deployment demo-app
#       image: registry:5000/demo/whoami:v1.2.3   <-- cannot be pulled (NotFound)
#
# The mismatch surfaces as ImagePullBackOff. The detector receives the
# KubePodNotReady / KubeContainerWaiting alert (normalized to PodNotReady
# per FOLLOWUP #42), dispatches the agent, and the agent autonomously
# walks:
#   1. k8s.get_pod_detail(<pod>)          — extracts image ref + event msg
#   2. ext.harbor_check_tag("demo/whoami", "v1.2.3")  -> exists:False
#   3. ext.harbor_list_tags("demo/whoami")            -> ["1.2.1","1.2.2","1.2.3"]
#   4. ext.gitea_get_chart_metadata(
#        "giteaadmin", "demo-chart", "charts/demo-app")
#                                         -> image.tag = "v1.2.3"
#   5. RCA — tag-format mismatch, suggests dropping the "v" prefix in
#      values.yaml OR re-tagging in the registry.
#
# Demo orientation (what to show in the split-screen demo):
#   - Left monitor:  http://registry-ui.localhost  — tags 1.2.1/1.2.2/1.2.3
#   - Center:        http://gitea.localhost/giteaadmin/demo-chart
#                    /src/branch/main/charts/demo-app/values.yaml  (shows v1.2.3)
#   - Right:         http://detector.localhost  — incident + Trace tab
#                    for the agent's autonomous investigation path
#
# Prereqs (one-time):
#   - registry + registry-ui up (../registry/docker-compose.yaml)
#   - k3d registry wiring applied (../k3d/setup-registry-certs.sh)
#   - gitea admin user + demo-chart repo seeded (see Stream I bootstrap)
#   - mcp-external registered with agent0 (see agent-zero/cutover.py)
#
# Usage:
#   ./test_scenario_1_tag_mismatch.sh
#   ./test_scenario_1_tag_mismatch.sh --cleanup    # removes the Deployment

set -euo pipefail

REGISTRY_HOST="${REGISTRY_HOST:-localhost:5000}"
REPO="${REPO:-demo/whoami}"
DEPLOY_TAG="v1.2.3"                       # what the Helm chart says
REGISTRY_TAGS=("1.2.1" "1.2.2" "1.2.3")   # what's actually pushed (no "v")
BASE_IMAGE="${BASE_IMAGE:-traefik/whoami:v1.11}"
SCENARIO_LABEL="demo=m12-scenario-1"

if [[ "${1:-}" == "--cleanup" ]]; then
  kubectl delete deployment demo-app --ignore-not-found
  exit 0
fi

echo "[1/4] Pushing ${REPO} tags to ${REGISTRY_HOST} (no 'v' prefix)..."
docker pull "${BASE_IMAGE}" >/dev/null
for tag in "${REGISTRY_TAGS[@]}"; do
  docker tag "${BASE_IMAGE}" "${REGISTRY_HOST}/${REPO}:${tag}"
  docker push "${REGISTRY_HOST}/${REPO}:${tag}" >/dev/null
  echo "  pushed: ${REGISTRY_HOST}/${REPO}:${tag}"
done

echo "[2/4] Deploying demo-app expecting tag ${DEPLOY_TAG} (which is NOT in the registry)..."
kubectl apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: demo-app
  namespace: default
  labels:
    app: demo-app
    ${SCENARIO_LABEL}
  annotations:
    chart-source: "http://gitea.localhost/giteaadmin/demo-chart"
    chart-path: "charts/demo-app"
spec:
  replicas: 1
  selector:
    matchLabels:
      app: demo-app
  template:
    metadata:
      labels:
        app: demo-app
    spec:
      containers:
      - name: app
        image: registry:5000/${REPO}:${DEPLOY_TAG}
        imagePullPolicy: IfNotPresent
        ports:
        - containerPort: 80
        resources:
          requests: {cpu: 10m, memory: 16Mi}
          limits:   {cpu: 100m, memory: 64Mi}
EOF

echo "[3/4] Waiting for pod to land in ImagePullBackOff..."
for _ in {1..10}; do
  sleep 2
  reason="$(kubectl get pods -l app=demo-app -o jsonpath='{.items[0].status.containerStatuses[0].state.waiting.reason}' 2>/dev/null || true)"
  if [[ "${reason}" == "ImagePullBackOff" || "${reason}" == "ErrImagePull" ]]; then
    echo "  pod state: ${reason} (expected)"
    break
  fi
done

kubectl get pods -l app=demo-app

echo ""
echo "[4/4] Fixture ready. The KubePodNotReady alert will fire after ~5m."
echo "       To dispatch immediately (bypass the Prometheus 'for' timer),"
echo "       POST a synthetic alert to the detector at:"
echo ""
pod_name="$(kubectl get pods -l app=demo-app -o jsonpath='{.items[0].metadata.name}')"
cat <<SYNTH
  curl -X POST -H "Content-Type: application/json" http://localhost:8001/alerts \\
    -d '{
      "version":"4",
      "status":"firing",
      "receiver":"detector-webhook",
      "groupKey":"{alertname=\"PodNotReady\",pod=\"${pod_name}\"}",
      "groupLabels":{"alertname":"PodNotReady"},
      "commonLabels":{"alertname":"PodNotReady","namespace":"default"},
      "alerts":[{
        "status":"firing",
        "labels":{"alertname":"PodNotReady","namespace":"default","pod":"${pod_name}","severity":"warning","app":"demo-app"},
        "annotations":{"summary":"Pod ${pod_name} ImagePullBackOff","description":"Tag v1.2.3 not found in registry"},
        "startsAt":"$(date -u +%Y-%m-%dT%H:%M:%SZ)",
        "fingerprint":"demo-scenario-1-${pod_name}"
      }]
    }'
SYNTH
echo ""
echo "Observe the incident at http://detector.localhost — Trace tab shows the"
echo "agent's autonomous harbor_list_tags + gitea_get_chart_metadata walk."
