#!/usr/bin/env bash
# test_scenario_1_missing_image_tag.sh
#
# M12 PoC Scenario 1 — Missing image tag.
# Seeds a Pod that references a non-existent tag in the local
# registry, then exercises the agent's ImagePullBackOff pivot path:
#   k8s.get_pod_detail -> harbor_check_tag -> harbor_list_tags.
#
# STATUS: placeholder. Fixture is wired up in the follow-on M12
# stream once the harbor_* tools in mcp-external are implemented.
#
# See plans/ADD_ON_A_EXTERNAL_SYSTEMS.md for the full scenario spec.

set -euo pipefail

# TODO: push a real image to the local registry (registry:5000)
# TODO: kubectl apply -f fixtures/scenario-1-pod.yaml with a broken tag
# TODO: wait for ImagePullBackOff state
# TODO: trigger an alert so the detector dispatches the agent
# TODO: assert RCA cites harbor_check_tag / harbor_list_tags output

echo '[scenario-1] placeholder — fixture not yet implemented (M12 follow-on)'
exit 0
