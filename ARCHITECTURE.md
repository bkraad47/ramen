# Ramen architecture (v0.1.0 — skeleton, expanded by the build)

Hierarchy: **Group → Environment → Zone → Worker**.

- **console/** FastAPI + Jinja2 + HTMX manager UI. Auth (email/password, OAuth pluggable), RBAC (super admin, group admin, viewer), groups/environments/zones, secret names, git→bucket sync, deploy, rebalance, logs, audit, API keys, config yaml, backup JSON.
- **node-rs/** Rust MCP server node. JSON-RPC 2.0 over MCP Streamable HTTP. Auth, IP allowlist, structured logging. Spawns the Python runtime sidecar on demand (1:1).
- **runtime-py/** Python 3.14 runtime. Loads `mcp/` from the group bucket, validates each `<name>.json` proto, executes callables, resolves `{{$group.secret}}` at call time.
- **deploy/** docker-compose (local), Helm chart, Terraform for GCP (GKE Autopilot, Firestore, GCS, Secret Manager, global HTTPS LB) and AWS (EKS, DynamoDB, S3, Secrets Manager, ALB — untested).
- **State** Firestore (GCP) or DynamoDB (AWS) behind one repository interface. Fernet-encrypted fields. No relational DB.
- **Zones** one zone first; every layer is multi-zone capable.
