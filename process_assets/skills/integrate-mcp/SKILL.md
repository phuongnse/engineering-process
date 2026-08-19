---
name: integrate-mcp
description: Design, implement, verify, and maintain a typed Model Context Protocol tool surface backed by an authoritative application API. Use for operation coverage, tool schemas, authorization, mutation safety, runtime registration, or supported-client evidence.
---

# Integrate MCP

## Goal

Expose useful typed tools without creating a second domain authority or bypassing the
application's authentication, authorization, validation, tenancy, and concurrency.

## Workflow

1. Read the authoritative API contract, product acceptance criteria, integration
   architecture, current operation inventory, and supported client lifecycle.
2. Classify every applicable API operation as exposed, intentionally internal, or
   blocked with an owner. Do not silently omit gaps.
3. Map each exposed operation to a typed semantic tool. Preserve server-owned request,
   response, identity, authorization, idempotency, revision, and failure contracts.
4. Classify tools as read, write, or destructive. Do not retry mutations after an
   ambiguous timeout or server failure; bind confirmations to authoritative state.
5. Prove protocol shape, operation coverage, safety, authenticated integration, tool
   registry refresh, authorized invocation, and required read-back.
6. Stop at browser consent, credentials, permissions, client reload, or other
   user-controlled boundaries. Preserve the pending operation and exact evidence.

## Hard gates

- Do not let the tool adapter call internal storage or domain implementation directly.
- Do not accept caller-supplied identity that must come from authentication context.
- A protocol harness or raw API call cannot replace supported-client runtime evidence.

## Output

Return operation coverage, tool changes, auth and mutation decisions, protocol and
runtime evidence, client state, read-back result, and blockers.
