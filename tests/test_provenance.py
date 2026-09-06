"""Tests for the provenance model (SP1a)."""

import pytest
import provenance as prov
from provenance import Tier, Provenance, trust_rank, is_citable, can_promote


# ─── tiers & trust ordering ───────────────────────────────────────────────────

def test_three_tiers_exist():
    assert {t.value for t in Tier} == {"verified", "proxy", "observed"}


def test_trust_ordering():
    assert trust_rank(Tier.VERIFIED) > trust_rank(Tier.PROXY) > trust_rank(Tier.OBSERVED)


def test_only_verified_is_citable():
    assert is_citable(Tier.VERIFIED) is True
    assert is_citable(Tier.PROXY) is False
    assert is_citable(Tier.OBSERVED) is False


def test_promotion_always_forbidden():
    # The cardinal rule: nothing auto-promotes to a higher tier — that's the human
    # act of verification. can_promote is always False, by design.
    for a in Tier:
        for b in Tier:
            assert can_promote(a, b) is False


def test_tier_accepts_string():
    # str-enum: passing the raw string works everywhere a Tier does
    assert trust_rank("verified") == trust_rank(Tier.VERIFIED)
    assert is_citable("verified") is True


# ─── Provenance record ────────────────────────────────────────────────────────

def test_provenance_verified_citable():
    p = Provenance(Tier.VERIFIED, source="https://x/pricing", verified_at="2026-09-05",
                   verified_by="human")
    assert p.citable is True
    d = p.to_dict()
    assert d["tier"] == "verified" and d["citable"] is True
    assert d["source"] == "https://x/pricing"
    assert d["verified_at"] == "2026-09-05"


def test_provenance_proxy_not_citable():
    p = Provenance(Tier.PROXY, source="openrouter", as_of="2026-09-05")
    assert p.citable is False
    d = p.to_dict()
    assert d["tier"] == "proxy" and d["citable"] is False
    # no verified_* fields on a proxy record
    assert "verified_at" not in d


def test_provenance_observed():
    p = Provenance(Tier.OBSERVED, source="router", as_of="2026-09-05T10:00:00Z")
    assert p.tier == Tier.OBSERVED
    assert p.citable is False


def test_to_dict_omits_none_fields():
    p = Provenance(Tier.PROXY)
    d = p.to_dict()
    assert d == {"tier": "proxy", "citable": False}


# ─── table provenance (the committed table is VERIFIED) ───────────────────────

def test_table_provenance_is_verified():
    table = {"verified_at": "2026-09-05", "verified_by": "human note", "max_age_days": 45}
    p = prov.table_provenance(table)
    assert p.tier == Tier.VERIFIED
    assert p.citable is True
    assert p.verified_at == "2026-09-05"


def test_pricing_table_exposes_verified_provenance():
    # SP1a wiring: pricing_table.table_provenance() reflects the shipped table.
    import pricing_table
    p = pricing_table.table_provenance()
    assert p.tier == Tier.VERIFIED
    assert p.citable is True


def test_status_block_carries_tier():
    import pricing_table
    st = pricing_table.price_table_status()
    assert st["tier"] == "verified"
