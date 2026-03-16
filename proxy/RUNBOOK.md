# MCP Proxy/Gateway — Local Development & Azure Deployment Guide

> Covers: running and testing locally, and deploying to Azure using managed services.

---

## Part 1 — Local Development

### Prerequisites

| Tool | Version | Purpose |
|---|---|---|
| Python | ≥ 3.10 | Runtime |
| `uv` | latest | Package management (`pip` is not used) |
| Docker Desktop | latest | Container builds and compose stack |
| `curl` or Bruno/Postman | any | Manual HTTP testing |

### 1.1 Install proxy dependencies

```bash
cd /path/to/ka-mcp-atlassian/proxy
uv sync --all-extras
```

This creates a `.venv` inside `proxy/` and installs all dependencies including dev/test extras.

### 1.2 Run unit tests

```bash
# From the proxy/ directory
uv run pytest tests/unit/ -xvs
```

Expected output: `42 passed in ~0.4s`

Run with coverage:

```bash
uv run pytest tests/unit/ --cov=src/mcp_proxy --cov-report=term-missing
```

### 1.3 Run the proxy locally (without Docker)

The proxy needs an upstream mcp-atlassian server to forward to. Start the upstream first, then start the proxy.

**Step 1 — Start mcp-atlassian in HTTP mode:**

```bash
# From repo root
# No Atlassian env vars needed — users provide credentials per-request
# via X-Atlassian-* headers through the proxy.

uv run mcp-atlassian \
  --transport streamable-http \
  --port 8080 \
  --host 127.0.0.1 \
  --path /mcp
```

**Step 2 — Start the proxy:**

```bash
cd proxy

# Configure via environment variables
export PROXY_UPSTREAM_URL=http://127.0.0.1:8080
export PROXY_READ_ONLY=true
export PROXY_JIRA_PROJECTS_WHITELIST=DS
export PROXY_CONFLUENCE_SPACES_WHITELIST=ENG
export PROXY_AUDIT_LOG_ENABLED=true

uv run uvicorn mcp_proxy.main:app \
  --host 127.0.0.1 \
  --port 8000 \
  --reload          # hot-reload on file changes during development
```

The proxy is now listening at `http://127.0.0.1:8000`.

### 1.4 Run the full stack with Docker Compose

```bash
# From repo root
cp .env.example .env   # fill in PROXY_* whitelist and read-only settings

# Build both images and start
docker compose -f docker-compose.proxy.yml up --build

# Background mode
docker compose -f docker-compose.proxy.yml up --build -d

# Tear down
docker compose -f docker-compose.proxy.yml down
```

Services:
- `mcp-proxy` → `http://localhost:8000` (public)
- `mcp-atlassian` → internal only (port 8080 not accessible from host)

### 1.5 Verify the proxy is running

```bash
# Health check
curl http://localhost:8000/healthz
# → {"status":"ok"}
```

### 1.6 Manual test scenarios

All examples use `curl`. Each request includes `X-Atlassian-*` headers to identify the user and target instance. Use only the Jira headers for Jira-only tests, only the Confluence headers for Confluence-only tests, or all four for mixed workloads.

**Test 1 — tools/list (always passes through)**

```bash
curl -s -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "X-Atlassian-Jira-Url: https://jira.mgm-tp.com/jira" \
  -H "X-Atlassian-Jira-Personal-Token: YOUR_JIRA_PAT" \
  -H "X-Atlassian-Confluence-Url: https://wiki.mgm-tp.com/confluence" \
  -H "X-Atlassian-Confluence-Personal-Token: YOUR_CONFLUENCE_PAT" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | jq .
```

Expected: list of tools from mcp-atlassian.

**Test 2 — Allowed read tool call (Jira only)**

```bash
curl -s -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "X-Atlassian-Jira-Url: https://jira.mgm-tp.com/jira" \
  -H "X-Atlassian-Jira-Personal-Token: YOUR_JIRA_PAT" \
  -d '{
    "jsonrpc":"2.0","id":2,"method":"tools/call",
    "params":{"name":"jira_get_issue","arguments":{"issue_key":"PROJ-1"}}
  }' | jq .
```

Expected: issue details (forwarded to upstream, HTTP 200).

**Test 3 — Denied: project not in whitelist**

```bash
# Assumes PROXY_JIRA_PROJECTS_WHITELIST=PROJ (OTHER is not allowed)
curl -s -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "X-Atlassian-Jira-Url: https://jira.mgm-tp.com/jira" \
  -H "X-Atlassian-Jira-Personal-Token: YOUR_JIRA_PAT" \
  -d '{
    "jsonrpc":"2.0","id":3,"method":"tools/call",
    "params":{"name":"jira_get_issue","arguments":{"issue_key":"OTHER-42"}}
  }' | jq .
```

Expected HTTP 403:
```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "error": {
    "code": -32001,
    "message": "Tool 'jira_get_issue' references Jira project(s) ['OTHER'] which are not in the whitelist ['PROJ']."
  }
}
```

**Test 4 — Denied: write tool in read-only mode**

```bash
# Assumes PROXY_READ_ONLY=true
curl -s -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "X-Atlassian-Jira-Url: https://jira.mgm-tp.com/jira" \
  -H "X-Atlassian-Jira-Personal-Token: YOUR_JIRA_PAT" \
  -d '{
    "jsonrpc":"2.0","id":4,"method":"tools/call",
    "params":{"name":"jira_create_issue","arguments":{"project_key":"PROJ","summary":"Test"}}
  }' | jq .
```

Expected HTTP 403:
```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "error": {
    "code": -32001,
    "message": "Tool 'jira_create_issue' is a write operation; server is in read-only mode."
  }
}
```

**Test 5 — Denied: JQL with out-of-scope project**

```bash
curl -s -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "X-Atlassian-Jira-Url: https://jira.mgm-tp.com/jira" \
  -H "X-Atlassian-Jira-Personal-Token: YOUR_JIRA_PAT" \
  -d '{
    "jsonrpc":"2.0","id":5,"method":"tools/call",
    "params":{"name":"jira_search","arguments":{"jql":"project in (PROJ, SENSITIVE) AND status = Open"}}
  }' | jq .
```

Expected HTTP 403 — `SENSITIVE` is blocked.

> **Note:** Different users can point at different Jira/Confluence instances by
> changing their own `X-Atlassian-Jira-Url` and `X-Atlassian-Confluence-Url`
> header values. The proxy forwards these headers to the upstream server, which
> connects to the specified instance using the accompanying personal token.

### 1.7 Reading audit logs locally

```bash
# Docker Compose — follow proxy logs
docker compose -f docker-compose.proxy.yml logs -f mcp-proxy

# Filter to audit entries only (structured JSON lines)
docker compose -f docker-compose.proxy.yml logs mcp-proxy \
  | grep '"decision"' | jq .
```

---

## Part 2 — Azure Deployment

### 2.1 Architecture overview

```
Internet
   │
   │  HTTPS (port 443)
   ▼
Azure Application Gateway  (WAF v2)
   │  TLS termination, path-based routing
   │  Route /mcp → mcp-proxy backend pool
   ▼
Azure Container Apps  ─────────────────────────────────┐
   │                                                    │
   │  mcp-proxy  (ingress: external, port 8000)        │
   │    ├─ PROXY_READ_ONLY from Key Vault              │
   │    ├─ PROXY_JIRA_PROJECTS_WHITELIST from Key Vault│
   │    └─ PROXY_UPSTREAM_URL → mcp-atlassian internal │
   │                                                    │
   │  mcp-atlassian  (ingress: internal, port 8080)    │
   │    ├─ JIRA_URL, JIRA_PERSONAL_TOKEN from Key Vault│
   │    └─ only reachable from mcp-proxy               │
   │                                                    │
   └────────────────────────────────────────────────────┘
           │ Managed Identity
           ▼
   Azure Key Vault  (secrets: PATs, whitelist config)
           │
   Azure Monitor + Log Analytics  (audit logs, metrics)
```

### 2.2 Azure services used

| Service | Tier | Purpose |
|---|---|---|
| **Azure Container Registry (ACR)** | Basic | Stores `ka-mcp-proxy` and `ka-mcp-atlassian` Docker images |
| **Azure Container Apps (ACA)** | Consumption | Runs both containers in the same environment; handles scaling, health checks, secrets injection |
| **Azure Key Vault** | Standard | Stores all secrets (`JIRA_PERSONAL_TOKEN`, `CONFLUENCE_PERSONAL_TOKEN`, whitelist env vars) |
| **Azure Application Gateway** | WAF v2 | TLS termination, WAF rules, routes external HTTPS to mcp-proxy; blocks direct access to mcp-atlassian |
| **Azure Monitor / Log Analytics** | Pay-per-use | Ingests structured audit logs from the proxy; dashboards and alerts |
| **Azure Virtual Network (VNet)** | — | Isolates Container Apps environment; mcp-atlassian is not reachable from outside the VNet |
| **Azure Managed Identity** | — | Grants Container Apps access to Key Vault without storing credentials |

### 2.3 Step-by-step deployment

#### Step 1 — Create ACR and push images

```bash
REGISTRY=myacr.azurecr.io
az acr login --name myacr

# Build and push mcp-atlassian
docker build -t $REGISTRY/ka-mcp-atlassian:latest .
docker push $REGISTRY/ka-mcp-atlassian:latest

# Build and push mcp-proxy
docker build -t $REGISTRY/ka-mcp-proxy:latest proxy/
docker push $REGISTRY/ka-mcp-proxy:latest
```

#### Step 2 — Create Key Vault and store secrets

```bash
KV=my-mcp-kv
az keyvault create --name $KV --resource-group my-rg --location westeurope

# Atlassian credentials
az keyvault secret set --vault-name $KV --name jira-personal-token    --value "YOUR_JIRA_PAT"
az keyvault secret set --vault-name $KV --name confluence-personal-token --value "YOUR_CONFLUENCE_PAT"

# Proxy access control config
az keyvault secret set --vault-name $KV --name proxy-read-only                  --value "true"
az keyvault secret set --vault-name $KV --name proxy-jira-projects-whitelist    --value "PROJ,DEMO"
az keyvault secret set --vault-name $KV --name proxy-confluence-spaces-whitelist --value "ENG,HR"
```

#### Step 3 — Create Container Apps environment with VNet integration

```bash
# Create VNet
az network vnet create \
  --name mcp-vnet --resource-group my-rg \
  --address-prefix 10.0.0.0/16

az network vnet subnet create \
  --vnet-name mcp-vnet --resource-group my-rg \
  --name aca-subnet --address-prefixes 10.0.0.0/23

# Create Container Apps environment (VNet-injected)
az containerapp env create \
  --name mcp-env \
  --resource-group my-rg \
  --location westeurope \
  --infrastructure-subnet-resource-id \
    $(az network vnet subnet show \
      --vnet-name mcp-vnet --resource-group my-rg \
      --name aca-subnet --query id -o tsv)
```

#### Step 4 — Create Managed Identity and grant Key Vault access

```bash
# Create user-assigned managed identity
az identity create --name mcp-identity --resource-group my-rg

IDENTITY_CLIENT_ID=$(az identity show --name mcp-identity --resource-group my-rg --query clientId -o tsv)
IDENTITY_ID=$(az identity show --name mcp-identity --resource-group my-rg --query id -o tsv)

# Grant Key Vault Secrets User role
KV_ID=$(az keyvault show --name $KV --query id -o tsv)
az role assignment create \
  --role "Key Vault Secrets User" \
  --assignee $IDENTITY_CLIENT_ID \
  --scope $KV_ID
```

#### Step 5 — Deploy mcp-atlassian (internal only)

```bash
az containerapp create \
  --name mcp-atlassian \
  --resource-group my-rg \
  --environment mcp-env \
  --image $REGISTRY/ka-mcp-atlassian:latest \
  --registry-server $REGISTRY \
  --user-assigned $IDENTITY_ID \
  --ingress internal \            # <-- not reachable from internet
  --target-port 9000 \
  --args "--transport" "streamable-http" "--port" "9000" "--host" "0.0.0.0" "--path" "/mcp" \
  --secrets \
    "jira-pat=keyvaultref:https://$KV.vault.azure.net/secrets/jira-personal-token,identityref:$IDENTITY_ID" \
    "confluence-pat=keyvaultref:https://$KV.vault.azure.net/secrets/confluence-personal-token,identityref:$IDENTITY_ID" \
  --env-vars \
    "JIRA_URL=https://jira.example.com/jira" \
    "JIRA_PERSONAL_TOKEN=secretref:jira-pat" \
    "JIRA_SSL_VERIFY=true" \
    "CONFLUENCE_URL=https://wiki.example.com/confluence" \
    "CONFLUENCE_PERSONAL_TOKEN=secretref:confluence-pat" \
    "CONFLUENCE_SSL_VERIFY=true" \
    "READ_ONLY_MODE=true" \
  --min-replicas 1 \
  --max-replicas 3
```

#### Step 6 — Deploy mcp-proxy (external ingress)

```bash
ATLASSIAN_FQDN=$(az containerapp show \
  --name mcp-atlassian --resource-group my-rg \
  --query properties.configuration.ingress.fqdn -o tsv)

az containerapp create \
  --name mcp-proxy \
  --resource-group my-rg \
  --environment mcp-env \
  --image $REGISTRY/ka-mcp-proxy:latest \
  --registry-server $REGISTRY \
  --user-assigned $IDENTITY_ID \
  --ingress external \            # <-- reachable from internet via App Gateway
  --target-port 8080 \
  --secrets \
    "proxy-read-only=keyvaultref:https://$KV.vault.azure.net/secrets/proxy-read-only,identityref:$IDENTITY_ID" \
    "proxy-jira-wl=keyvaultref:https://$KV.vault.azure.net/secrets/proxy-jira-projects-whitelist,identityref:$IDENTITY_ID" \
    "proxy-conf-wl=keyvaultref:https://$KV.vault.azure.net/secrets/proxy-confluence-spaces-whitelist,identityref:$IDENTITY_ID" \
  --env-vars \
    "PROXY_UPSTREAM_URL=https://$ATLASSIAN_FQDN" \
    "PROXY_READ_ONLY=secretref:proxy-read-only" \
    "PROXY_JIRA_PROJECTS_WHITELIST=secretref:proxy-jira-wl" \
    "PROXY_CONFLUENCE_SPACES_WHITELIST=secretref:proxy-conf-wl" \
    "PROXY_AUDIT_LOG_ENABLED=true" \
  --min-replicas 1 \
  --max-replicas 5
```

#### Step 7 — Configure Application Gateway with WAF

```bash
# Create public IP
az network public-ip create \
  --name mcp-appgw-ip --resource-group my-rg \
  --sku Standard --allocation-method Static

# Create Application Gateway (WAF v2)
PROXY_FQDN=$(az containerapp show \
  --name mcp-proxy --resource-group my-rg \
  --query properties.configuration.ingress.fqdn -o tsv)

az network application-gateway create \
  --name mcp-appgw \
  --resource-group my-rg \
  --location westeurope \
  --sku WAF_v2 \
  --capacity 2 \
  --vnet-name mcp-vnet \
  --subnet appgw-subnet \
  --public-ip-address mcp-appgw-ip \
  --http-settings-cookie-based-affinity Disabled \
  --http-settings-port 443 \
  --http-settings-protocol Https \
  --frontend-port 443 \
  --routing-rule-type Basic \
  --servers $PROXY_FQDN \
  --cert-file ./tls.pfx \
  --cert-password "YOUR_CERT_PASSWORD"

# Enable WAF in Prevention mode
az network application-gateway waf-config set \
  --gateway-name mcp-appgw \
  --resource-group my-rg \
  --enabled true \
  --firewall-mode Prevention \
  --rule-set-type OWASP \
  --rule-set-version 3.2
```

#### Step 8 — Configure Azure Monitor for audit logs

Container Apps stdout is automatically ingested by Log Analytics. Query audit log entries with KQL:

```kql
// All access control decisions in the last 24 hours
ContainerAppConsoleLogs_CL
| where ContainerName_s == "mcp-proxy"
| where Log_s contains "\"decision\""
| extend parsed = parse_json(Log_s)
| project
    TimeGenerated,
    decision    = parsed.decision,
    tool        = parsed.tool,
    user        = parsed.user,
    reason      = parsed.reason,
    request_id  = parsed.request_id
| order by TimeGenerated desc

// Denied requests only — useful for alerting
ContainerAppConsoleLogs_CL
| where ContainerName_s == "mcp-proxy"
| where Log_s contains "\"decision\":\"deny\""
| extend parsed = parse_json(Log_s)
| summarize deny_count = count() by bin(TimeGenerated, 5m), tool = tostring(parsed.tool)
| order by TimeGenerated desc
```

Create an alert rule on the deny query to notify via email or Teams when deny rate exceeds a threshold.

### 2.4 Updating the whitelist without restarting mcp-atlassian

The proxy reads config at startup via `get_config()` (cached with `lru_cache`). To update the whitelist in production:

1. Update the Key Vault secret:
   ```bash
   az keyvault secret set --vault-name $KV \
     --name proxy-jira-projects-whitelist \
     --value "PROJ,DEMO,NEWPROJECT"
   ```

2. Restart only the proxy container app (mcp-atlassian is unaffected):
   ```bash
   az containerapp revision restart \
     --name mcp-proxy \
     --resource-group my-rg \
     --revision $(az containerapp revision list \
         --name mcp-proxy --resource-group my-rg \
         --query "[0].name" -o tsv)
   ```

Active MCP sessions on the upstream server are not disrupted.

### 2.5 CI/CD integration (GitHub Actions sketch)

```yaml
name: Deploy proxy to Azure

on:
  push:
    paths:
      - "proxy/**"
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - run: cd proxy && uv sync --all-extras
      - run: cd proxy && uv run pytest tests/unit/ -xvs

  deploy:
    needs: test
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: azure/login@v2
        with:
          creds: ${{ secrets.AZURE_CREDENTIALS }}
      - uses: azure/docker-login@v2
        with:
          login-server: myacr.azurecr.io
          username: ${{ secrets.ACR_USERNAME }}
          password: ${{ secrets.ACR_PASSWORD }}
      - run: |
          docker build -t myacr.azurecr.io/ka-mcp-proxy:${{ github.sha }} proxy/
          docker push myacr.azurecr.io/ka-mcp-proxy:${{ github.sha }}
      - run: |
          az containerapp update \
            --name mcp-proxy \
            --resource-group my-rg \
            --image myacr.azurecr.io/ka-mcp-proxy:${{ github.sha }}
```

### 2.6 Security checklist for Azure

- [ ] `mcp-atlassian` Container App has `--ingress internal` — not reachable from internet
- [ ] Application Gateway WAF is in Prevention mode (not Detection)
- [ ] Key Vault secrets are not stored as plain env vars in Container App definitions
- [ ] Managed Identity is used for Key Vault access — no service principal passwords
- [ ] ACR Admin account is disabled; CI/CD uses a service principal with `AcrPush` role only
- [ ] Log Analytics workspace retention is set (90 days minimum for audit compliance)
- [ ] Azure Monitor alert rule fires when deny rate exceeds threshold
- [ ] VNet subnet for Container Apps environment has a Network Security Group blocking unexpected outbound
- [ ] TLS certificate on Application Gateway is from a trusted CA (not self-signed)
- [ ] `PROXY_READ_ONLY` and whitelist vars stored in Key Vault, not hardcoded in compose or pipelines
