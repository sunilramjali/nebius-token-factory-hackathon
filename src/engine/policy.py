"""Load the policy values the engine needs from config/policy.yaml (single source of truth)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "policy.yaml"


@dataclass(frozen=True)
class Policy:
    band_c_threshold: int
    cost_cap_gbp: int
    lock_in_deadline: date
    epc_validity_years: int
    # Improvement types whose spend doesn't count toward the cap (fossil-fuel heating).
    fossil_heating_types: frozenset[str] = frozenset()
    # Below this property value the cap is a % of value (not modelled: values unknown).
    low_value_threshold_gbp: int = 100_000
    compliance_deadline: date = date(2030, 10, 1)


def load_policy(path: Path | str = DEFAULT_PATH) -> Policy:
    raw = yaml.safe_load(Path(path).read_text())
    return Policy(
        band_c_threshold=int(raw["band_c_threshold"]["value"]),
        cost_cap_gbp=int(raw["cost_cap"]["amount_gbp"]),
        lock_in_deadline=raw["lock_in"]["deadline_inclusive"],
        epc_validity_years=int(raw["lock_in"]["epc_validity_years"]),
        fossil_heating_types=frozenset(raw["cost_cap"]["fossil_heating_improvement_types"]),
        compliance_deadline=raw["compliance_deadline"]["value"],
        low_value_threshold_gbp=int(
            raw["cost_cap"]["low_value_rule"]["applies_below_property_value_gbp"]
        ),
    )
