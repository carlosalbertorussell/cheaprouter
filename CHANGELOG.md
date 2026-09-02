# Changelog

All notable changes to cheaprouter are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] — 2026-09-02

### Added
- Initial release of cheaprouter — a BYOK MCP server for LLM token cost arbitrage.
- Eight providers across three regions: Anthropic, OpenAI, Google Gemini, Groq,
  Mistral (EU), DeepSeek (CN), Alibaba Qwen (CN), and xAI Grok.
- Three capability tiers (`tier_fast`, `tier_balanced`, `tier_powerful`) mapping
  semantically equivalent models across all providers.
- Five MCP tools: `arbitrage_get_pricing`, `arbitrage_estimate_cost`,
  `arbitrage_route_completion`, `arbitrage_provider_status`, `arbitrage_get_history`.
- BYOK (Bring Your Own Keys) model — API keys are supplied per request, never
  stored or logged.
- Latency-aware routing with configurable threshold (`latency_sensitive` flag).
- Region filtering via `allowed_regions` and global `BLOCKED_REGIONS`.
- Provider exclusion via `excluded_providers`.
- JSONL routing history with cumulative spend and savings tracking.
- Three wire protocols: Anthropic native, OpenAI-compatible, Google Gemini native.
- MCPize cloud deployment via streamable-http transport.

[Unreleased]: https://github.com/carlosalbertorussell/cheaprouter/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/carlosalbertorussell/cheaprouter/releases/tag/v1.0.0
