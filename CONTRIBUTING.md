# Contributing to cheaprouter

Thanks for your interest in improving cheaprouter. This is a community tool —
contributions that add providers, fix pricing, or improve routing are welcome.

## Ways to contribute

- **Report a bug** — open an issue with reproduction steps
- **Update pricing** — provider prices change often; PRs that correct `prices.json` are especially valuable
- **Add a provider** — see below
- **Improve docs** — README, MCPIZE_DOCS, or inline docstrings

## Development setup

```bash
git clone https://github.com/carlosalbertorussell/cheaprouter.git
cd cheaprouter
pip install -r requirements.txt
cp .env.example .env   # add keys for local testing (used over stdio only)
python server.py --stdio   # run locally for Claude Desktop
```

## Adding a provider

Providers live in `prices.json` under `providers`; `providers.py` builds the
`PROVIDERS` registry from it. Each entry needs:

1. `name`, `region` (`us`/`eu`/`cn`), `base_url`, `api_key_env`, `protocol` (`anthropic`/`openai`/`gemini`) and a `refresh` block
2. Three `models` entries — one per tier (`tier_fast`, `tier_balanced`, `tier_powerful`) — with `model_id`, current per-1M-token input/output prices, `context_window`, and `cached_input_price_per_1m` if the provider publishes a cache-read rate
3. A `pricing_urls` entry pointing at the provider's pricing page
3. If the provider uses an OpenAI-compatible API (most do), no client changes are needed. Native protocols require a handler in `client.py`.

After adding, update:
- `PROVIDER_IDS` in `server.py`
- The provider table in `README.md` and `MCPIZE_DOCS.md`
- `MCPIZE_LISTING.md`: run `python scripts/gen_listing.py` (a test fails if it drifts from `prices.json`)
- `.env.example` with the new key variable
- `CHANGELOG.md` under `[Unreleased]`

## Pull request guidelines

- Keep PRs focused — one provider or one fix per PR
- Verify the code compiles: `python -m py_compile *.py scripts/*.py`
- Run the test suite: `python -m pytest` (no network: `tests/conftest.py` fails any test that opens a real connection)
- Never commit real API keys — `.env` is gitignored; double-check your diff
- Use conventional commit messages: `feat:`, `fix:`, `docs:`, `deps:`, `ci:`, `refactor:`, `chore:`
- Update `CHANGELOG.md` under `[Unreleased]`

## Pricing accuracy

Prices live in `prices.json` with a `verified_at` date. Routing refuses once the
table is older than `max_age_days`, so when you correct a price, verify it against
the provider's pricing page, link that page in the PR description, and update
`verified_at` only if you re-verified the whole table. Regenerate the listing with
`python scripts/gen_listing.py`.

## Code of conduct

Be respectful and constructive. This is a small project maintained in spare time;
patience with review turnaround is appreciated.
