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
14 tools currently available. See TRACK1_PLAN.md for v2.0 roadmap.

## Repos
- Infrastructure: https://github.com/no0ktheali3n/expert-k8
- MCP server: https://github.com/no0ktheali3n/mcp-toolbox
