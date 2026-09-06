"""
Provenance model for cheaprouter price data (SP1a).

Formalises the trust model that has been implicit in prices.json: every price
carries a *provenance tier* saying how much it can be trusted and how it was
obtained. This is the shared spine both the router and the future pricing server
build on (the "waist" of PRICING_SERVER_BLUEPRINT.md).

The three tiers are a HIERARCHY OF TRUST, never a blend. Lower tiers inform and
challenge higher ones (drift/validation signals) but NEVER auto-promote into a
higher tier — that promotion is the human act of verification/attestation. This
is the no-invented-prices rule expressed as a type.

    VERIFIED  — a human checked it against the provider's own pricing page,
                stamped verified_at + source. The authoritative, citable tier.
    PROXY     — machine-aggregated from a third-party feed (e.g. OpenRouter).
                Current and useful, but unverified — a proxy for the truth.
    OBSERVED  — empirical: what a real completion actually cost, reported by the
                router. Ground truth of a *single* transaction, but caller- and
                time-specific (may be a negotiated / off-peak / regional rate), so
                it is a validation & drift SIGNAL, never a publishable list price.

Nothing here stores keys or content — provenance is metadata about prices only.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional


class Tier(str, Enum):
    VERIFIED = "verified"
    PROXY = "proxy"
    OBSERVED = "observed"


# Trust ordering — higher value = more trustworthy. Used to decide precedence and
# to enforce "never auto-promote": a lower tier can flag a higher one, not replace it.
_TRUST_ORDER = {Tier.OBSERVED: 0, Tier.PROXY: 1, Tier.VERIFIED: 2}


def trust_rank(tier: Tier) -> int:
    """Higher = more trustworthy. VERIFIED (2) > PROXY (1) > OBSERVED (0)."""
    return _TRUST_ORDER[Tier(tier)]


def is_citable(tier: Tier) -> bool:
    """Only VERIFIED prices are citable as authoritative truth."""
    return Tier(tier) == Tier.VERIFIED


def can_promote(_from: Tier, _to: Tier) -> bool:
    """
    Automatic promotion between tiers is ALWAYS forbidden. Verification/attestation
    (moving anything up to VERIFIED) is a human act, by design. This function exists
    to make that rule explicit and testable — it always returns False.
    """
    return False


class Provenance:
    """
    Provenance metadata attached to a price. Immutable-by-convention record of how
    a number was obtained and how far it can be trusted.
    """

    __slots__ = ("tier", "source", "verified_at", "verified_by", "as_of", "confidence")

    def __init__(
        self,
        tier: Tier,
        *,
        source: Optional[str] = None,       # provider page URL, feed name, or "router"
        verified_at: Optional[str] = None,  # ISO date, VERIFIED tier only
        verified_by: Optional[str] = None,  # who attested, VERIFIED tier only
        as_of: Optional[str] = None,        # when this datum was obtained/observed
        confidence: Optional[float] = None, # 0..1, optional
    ):
        self.tier = Tier(tier)
        self.source = source
        self.verified_at = verified_at
        self.verified_by = verified_by
        self.as_of = as_of
        self.confidence = confidence

    @property
    def citable(self) -> bool:
        return is_citable(self.tier)

    def to_dict(self) -> dict:
        d = {"tier": self.tier.value, "citable": self.citable}
        for k in ("source", "verified_at", "verified_by", "as_of", "confidence"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        return d

    def __repr__(self) -> str:
        return f"Provenance(tier={self.tier.value}, source={self.source!r})"


def table_provenance(table: dict) -> Provenance:
    """
    Derive the whole-table provenance from prices.json's top-level fields. The
    committed table is the VERIFIED tier by construction (a human ran SC1 and
    stamped verified_at/verified_by).
    """
    return Provenance(
        Tier.VERIFIED,
        verified_at=table.get("verified_at"),
        verified_by=table.get("verified_by"),
        source="prices.json",
        as_of=table.get("verified_at"),
    )
