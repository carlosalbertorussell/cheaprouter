# Security Policy

## Supported Versions

cheaprouter is currently in active development. Security fixes are applied to the latest version only.

| Version | Supported |
|---------|-----------|
| latest (main) | ✅ |
| older commits | ❌ |

## Scope

This policy covers the cheaprouter MCP server codebase. It does **not** cover:

- Third-party LLM provider APIs (Anthropic, OpenAI, Google, Groq, Mistral, DeepSeek, Alibaba, xAI) — report vulnerabilities in those services directly to the respective provider
- MCPize hosting infrastructure — report platform issues to MCPize
- Security of your own API keys — cheaprouter never stores them, but you are responsible for how you manage and rotate them

## Security Model

cheaprouter is a BYOK (Bring Your Own Keys) routing server. A few design decisions are worth understanding:

**API keys are never stored.** Keys passed in `api_keys` are used for the duration of the request only. They are not logged, persisted, or retained in any form.

**Request content is never stored.** The `messages` payload sent to `arbitrage_route_completion` is forwarded to the chosen provider and immediately discarded. The routing history log records only metadata: timestamp, provider selected, token counts, cost, and latency.

**A hosted server never spends the operator's keys.** Over HTTP (the transport `python server.py` uses, and the one MCPize runs) only keys the caller passes in `api_keys` count. Provider key environment variables (`ANTHROPIC_API_KEY`, ...) are ignored, so an anonymous caller cannot route completions billed to whoever runs the server. The env-var fallback exists only for local use:

| How the server runs | Env-var keys used for providers missing from `api_keys`? |
|---|---|
| `python server.py --stdio` (Claude Desktop, local clients) | Yes — the process belongs to one local user |
| `python server.py` (streamable HTTP, MCPize) | **No** |
| HTTP with `ARBITRAGE_ALLOW_ENV_KEYS=1` | Yes — explicit opt-in for a private deployment you protect yourself. Never set it on a public endpoint. |

A key the caller passes always wins over an env var. Do not store provider keys as MCPize secrets (`.env.example` explains this).

**The server holds no provider credentials.** The MCPize deployment needs no provider API keys. The only server-side secret is the optional Upstash token for durable routing history.

## Reporting a Vulnerability

If you discover a security vulnerability in cheaprouter, please **do not open a public GitHub issue**.

Report it privately via GitHub's built-in security advisory feature:

1. Go to the [Security tab](../../security) of this repository
2. Click **Report a vulnerability**
3. Provide a clear description, reproduction steps, and potential impact

Alternatively, email the maintainer directly. The address is in the GitHub profile.

**Please include:**
- Description of the vulnerability and affected component
- Steps to reproduce
- Potential impact (data exposure, privilege escalation, key leakage, etc.)
- Your suggested fix, if you have one

## Response Timeline

| Stage | Target |
|-------|--------|
| Acknowledgement | Within 48 hours |
| Initial assessment | Within 5 business days |
| Fix or mitigation | Dependent on severity — critical issues prioritised |
| Public disclosure | Coordinated with reporter after fix is deployed |

## Responsible Disclosure

This project follows coordinated disclosure. Please give reasonable time for a fix before any public disclosure. Credit will be given to reporters in the release notes unless anonymity is requested.

## Known Non-Issues

The following are by design and not considered vulnerabilities:

- **Routing history is shared across users** — history records contain no content, no keys, and no PII; only aggregate routing metadata
- **History resets on redeploy** — ephemeral by design on MCPize; use `ARBITRAGE_HISTORY_FILE` override for persistence
- **No authentication on the MCP endpoint** — access control is delegated to the MCP client and MCPize platform layer
