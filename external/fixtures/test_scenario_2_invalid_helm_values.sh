#!/usr/bin/env bash
# test_scenario_2_invalid_helm_values.sh
#
# M12 PoC Scenario 2 — Invalid Helm values.
# Seeds a Helm release that fails install because values-prod.yaml
# is missing a required field per Chart.yaml + values.schema.json.
# Exercises the agent's Helm-error pivot path:
#   k8s.get_events -> gitea_get_chart_metadata ->
#   gitea_get_file_content -> gitea_get_recent_commits.
#
# STATUS: placeholder. Fixture is wired up in the follow-on M12
# stream once the gitea_* tools in mcp-external are implemented.
#
# See plans/ADD_ON_A_EXTERNAL_SYSTEMS.md for the full scenario spec.

set -euo pipefail

# TODO: gitea API - create chart repo with Chart.yaml + values.schema.json
# TODO: gitea API - create values-prod.yaml missing db.password
# TODO: helm install against the broken values file
# TODO: let the failure propagate to Alertmanager -> detector
# TODO: assert RCA cites gitea_get_chart_metadata / gitea_get_file_content output

echo '[scenario-2] placeholder — fixture not yet implemented (M12 follow-on)'
exit 0
