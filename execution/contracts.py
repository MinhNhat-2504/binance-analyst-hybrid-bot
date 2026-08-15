"""Shared arithmetic for the persisted execution-position contract."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping


import hashlib
import json
from pathlib import Path

# ABSOLUTE sanity bound baked into code. No ceilings file may set anything above this;
# it exists so that a bad edit to the JSON cannot authorise an arbitrary number.
ABSOLUTE_GROSS_UPPER_BOUND_USD = 5_000.0

CEILINGS_PATH = Path(__file__).resolve().parent.parent / "execution_ceilings_v1.json"


def load_ceilings(path: Path | None = None) -> tuple[dict[str, float], str]:
    """Return ({environment: ceiling_usd}, sha256 of the file).

    The sha256 is written into every execution contract so a reconciler can prove which
    ceiling set governed a run. Any value above ABSOLUTE_GROSS_UPPER_BOUND_USD is refused
    at load time - the file can lower the code's bound, never raise it.
    """
    path = Path(path) if path else CEILINGS_PATH
    raw = path.read_bytes()
    payload = json.loads(raw)
    ceilings = {str(k): float(v) for k, v in payload["ceilings_usd"].items()}
    for env, value in ceilings.items():
        if value < 0 or value > ABSOLUTE_GROSS_UPPER_BOUND_USD:
            raise ValueError(
                f"ceilings file sets {env}={value} outside [0, {ABSOLUTE_GROSS_UPPER_BOUND_USD}]"
            )
    return ceilings, hashlib.sha256(raw).hexdigest()


def frozen_ceiling(environment: str = "testnet", path: Path | None = None) -> float:
    ceilings, _ = load_ceilings(path)
    if environment not in ceilings:
        raise ValueError(f"no ceiling declared for environment {environment!r}")
    return ceilings[environment]


# Back-compat name used across the engine, CLI and reconciler. Resolved from the file at
# import; a missing/invalid file fails the import loudly rather than defaulting.
FROZEN_TESTNET_GROSS_CEILING_USD = frozen_ceiling("testnet")
CEILINGS_SHA256 = load_ceilings()[1]


def quantity_tolerance(budget: Mapping[str, Any], verification_price: Any) -> Decimal:
    """Convert a hashed per-symbol tolerance budget on one verification-price basis."""

    price = Decimal(str(verification_price))
    if price <= 0:
        return Decimal("0")
    step = Decimal(str(budget.get("step_size", 0)))
    rounding_steps = Decimal(str(budget.get("rounding_steps", 0)))
    min_notional = Decimal(str(budget.get("min_notional", 0)))
    min_notional_fraction = Decimal(str(budget.get("min_notional_fraction", 0)))
    return max(
        step * rounding_steps,
        (min_notional / price) * min_notional_fraction,
    )
