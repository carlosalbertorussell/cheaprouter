# Review response — external review 2026-09-06

Disposition of the eight recommendations (CR-01…CR-08) from the 2026-09-06
external review. Recorded because auditable response to review is the same
provenance discipline the project holds elsewhere.

Overall: the review is sharp and mostly right. Two of its criticisms (CR-02 timing,
CR-03 liability contradiction) are things that should have been caught internally.
Its two blocking items (CR-01/CR-02) have since been partly overtaken by events —
SC1 shipped and the server routes again — but the *spirit* of CR-02 stands.

| # | Item | Status |
|---|------|--------|
| CR-01 | Take the five tier-mapping decisions; server refuses to route | **RESOLVED.** SC1 shipped (PR #30): decision taken (cheapest-current-per-tier), all 8 providers verified incl. Qwen+Grok, prices.json re-verified 2026-09-05, guard fresh, server routes, price-freshness CI green. |
| CR-02 | Freeze new design until SC1 closes | **Premise overtaken (SC1 shipped) but spirit ACCEPTED.** The critique — bottleneck was a decision, response was more design — is fair, and applies again to the SP1a–d build sprints run after the server came back up. Commitment: prefer external-signal moves (CR-07/08) over further building on unvalidated conviction. |
| CR-03 | Pull the liability question forward; it contradicts "build to certification depth from the start" | **FIXED (docs).** The self-contradiction was real. `PRICING_STRATEGY.md` now makes liability a build-*gating* decision for the attestation layer (read-only server may proceed; anything stamped "certified" waits on it). |
| CR-04 | Document the OpenRouter concentration risk (critical dependency = direct competitor) | **FIXED (docs).** Added as the load-bearing risk in `PIPELINE.md` Risks, with the dependency chain and the second-source mitigation (reframe SC7's cross-check as risk mitigation; identify a concrete second source). |
| CR-05 | Mark SC8's 95/5 as estimated, unmeasured (per your own Minefield standard) | **FIXED (docs).** The ratio is now labelled ESTIMATED/UNMEASURED in `PRICING_STRATEGY.md` and `SPRINTS.md`, with a note to *measure* it once SC8 runs (auto-verify vs. escalate fraction). |
| CR-06 | Define Pro price + free/Pro boundary | **DEFERRED (correctly, per review).** Real gap, not urgent — "when there is someone to charge." Remains an open question in `PIPELINE.md`. BYOK ⇒ no take-rate ⇒ Pro must be worth its analytics alone. |
| CR-07 | Publish to the official MCP registry (`registry.modelcontextprotocol.io`), Smithery ingests it | **RECORDED for post-live.** Actionable: publish once with the `io.github.*` namespace (NOT domain — endpoint on `mcpize.run` would fail domain verification); GitHub OIDC fits the existing CI. Unconfirmed: whether Smithery surfaces ingested *remote* entries prominently. Do after the server is discoverable-worthy. |
| CR-08 | Evaluate a free tier without the MCPize account/key friction | **RECORDED for post-live.** For a funnel/reference product, the "create an account to make the first call" barrier is where registry traffic dies — and would be *mis*-read as the channel failing rather than the barrier. Worth removing before measuring registry conversion. |

## The two items that gate further work

- **CR-03 (liability)** gates the *attestation layer* — decide what "certified"
  warrants before building anything that claims it. The read-only pricing server
  (SP1a–SP1e) is unaffected.
- **CR-02 (spirit)** is a standing reminder: the highest-leverage next move may be
  *external signal* (get discoverable — CR-07/08 — once the server is worth
  discovering), not more building.

## What the review got right that internal work missed

- The timing critique (CR-02): the bottleneck was one decision; the response was
  more design. True then, and true again of the post-SC1 build sprints.
- The liability contradiction (CR-03): filed as "later" while building to
  certification depth — genuinely incoherent, should have been caught when both
  sentences were written.

## The review's closing, worth keeping on the record

"Ayer esto era un router con backlog. Hoy hay un router, la cadena SC1–SC3,
cuatro sprints SC, un segundo servidor MCP, un negocio de atestación, un dataset
histórico… Clientes: cero." The zero-external-demand point stands (now recorded
as a first-class risk), even though the "server offline" half is resolved.
