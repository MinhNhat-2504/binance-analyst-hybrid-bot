"""Shared arithmetic for the persisted execution-position contract."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping


FROZEN_TESTNET_GROSS_CEILING_USD = 500.0


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
