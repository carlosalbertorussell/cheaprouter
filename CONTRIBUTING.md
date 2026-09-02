# Contributing to cheaprouter

Thanks for your interest in improving cheaprouter. This is a community tool —
contributions that add providers, fix pricing, or improve routing are welcome.

## Ways to contribute

- **Report a bug** — open an issue with reproduction steps
- **Update pricing** — provider prices change often; PRs that correct `providers.py` are especially valuable
- **Add a provider** — see below
- **Improve docs** — README, MCPIZE_DOCS, or inline docstrings

## Development setup

```bash
git clone https://github.com/carlosalbertorussell/cheaprouter.git
cd cheaprouter
pip install -r requirements.txt
cp .env.example .env   # add keys for local testing
python server.py --stdio   # run locally for Claude Desktop
```

## Adding a provider

Providers live in `providers.py` as entries in the `PROVIDERS` dict. Each needs:

1. A `ProviderConfig` with `name`, `region` (`us`/`eu`/`cn`), `base_url`, `api_key_env`, and `protocol` (`anthropic`/`openai`/`gemini`)
2. Three `ModelConfig` entries — one per tier (`tier_fast`, `tier_balanced`, `tier_powerful`) — with current per-1M-token pricing
3. If the provider uses an OpenAI-compatible API (most do), no client changes are needed. Native protocols require a handler in `client.py`.

After adding, update:
- `PROVIDER_IDS` in `server.py`
- The provider table in `README.md` and `MCPIZE_DOCS.md`
- `.env.example` with the new key variable
- `CHANGELOG.md` under `[Unreleased]`

## Pull request guidelines

- Keep PRs focused — one provider or one fix per PR
- Verify the code compiles: `python -m py_compile *.py`
- Run the test suite: `python -m pytest`
- Never commit real API keys — `.env` is gitignored; double-check your diff
- Use conventional commit messages: `feat:`, `fix:`, `docs:`, `deps:`, `ci:`, `refactor:`, `chore:`
- Update `CHANGELOG.md` under `[Unreleased]`

## Pricing accuracy

Pricing in `providers.py` reflects approximate list prices at the time of writing.
If you spot a stale price, a one-line PR correcting it — with a link to the
provider's pricing page in the description — is a genuinely useful contribution.

## Code of conduct

Be respectful and constructive. This is a small project maintained in spare time;
patience with review turnaround is appreciated.
