# DS-2031: Access Control Solutions for mcp-atlassian

## Problem Summary

`mcp-atlassian` built-in whitelist filters (`JIRA_PROJECTS_FILTER`, `CONFLUENCE_SPACES_FILTER`) are not real access controls:

- **Jira**: only 2/50 tools check the filter (`get_issue`, `search`)
- **Confluence**: only 1/25 tools checks the filter (`search`)
- All other tools (create, delete, transitions, worklogs, etc.) ignore the filter
- Filters can be bypassed with crafted JQL/CQL queries (e.g. explicit `project=` clauses)

This creates a **data privacy risk** — users could access projects outside their approved scope.

---

## Planned Access Control Layers

1. **Per-user auth** — each user provides their own PAT; permissions scoped to that user
2. **Per-project whitelist** — server-side env vars restrict accessible projects (post data privacy approval)
3. **Read-only mode** — `READ_ONLY_MODE=true` initially

---

## Solutions

### Solution 1: Decorator-based filter enforcement (patch upstream)

Wrap all tool handlers with a decorator that validates project/space against the whitelist before executing. Extends what the LibreChat deployment already does partially.

**Pros**
- Minimal, reusable pattern — one decorator covers many tools
- Can be contributed back to `sooperset/mcp-atlassian`
- Fast to implement

**Cons**
- Must be applied carefully to all 73 tools — easy to miss edge cases
- Maintenance burden as upstream adds new tools
- JQL/CQL injection still possible if decorator only checks declared args, not query content

---

### Solution 2: Full fork with proper enforcement (current approach)

Maintain an mgm-internal fork (`ka-mcp-atlassian`) with access control enforced across all tools, including JQL/CQL parsing to block cross-project queries.

**Pros**
- Complete control; highest security guarantee
- Can add mgm-specific features (audit logging, custom toolsets)
- JQL/CQL bypass can be addressed at the query level

**Cons**
- High maintenance cost — must rebase on upstream regularly
- JQL/CQL parsing is complex and error-prone
- Risk of diverging significantly from the community project

---

### Solution 3: Rely solely on per-user PAT (no server-side filter)

Remove the server-side whitelist entirely and trust that each user's Jira/Confluence permissions already restrict access appropriately.

**Pros**
- Zero implementation effort
- No false sense of security — permissions enforced by Jira/Confluence natively
- No maintenance burden

**Cons**
- No defense-in-depth — if a user has broad Jira access, so does the AI
- May not satisfy data privacy requirements for AI-specific access controls

---

### Solution 4: Proxy/gateway layer in front of mcp-atlassian

Run a lightweight HTTP proxy that intercepts all MCP tool calls and enforces whitelists before forwarding to `mcp-atlassian`.

**Pros**
- Clean separation of concerns — mcp-atlassian stays unmodified
- Audit logging, rate limiting, and ACL in one place
- Can be updated independently from mcp-atlassian

**Cons**
- Additional infrastructure component to build and maintain
- Requires HTTP transport (not stdio) — more complex deployment
- Proxy itself becomes a security-critical component

---

## Recommendation

**Short-term**: Solution 1 (decorator patch) — fastest path, leverages existing LibreChat work. Apply with `READ_ONLY_MODE=true`.

**Long-term**: Solution 2 (fork) is the right direction since `ka-mcp-atlassian` already exists. Key additions needed:
- Universal filter decorator applied to **all** tools
- JQL/CQL sanitization to block cross-project queries
- Per-user audit logging

---

## Open Questions

- [ ] Where to deploy the shared instance? (infra TBD)
- [ ] Single instance for Jira + Confluence, or separate?
- [ ] Which projects get data privacy approval first?
- [ ] Who maintains the fork long-term?
