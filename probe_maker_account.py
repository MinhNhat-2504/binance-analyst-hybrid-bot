"""Read-only preflight for an isolated Binance Demo Futures maker experiment.

Credentials must come from an explicit, untracked probe file. The daily bot's
.env.testnet is never used. This script contains no order or cancel call.
"""
from __future__ import annotations

import argparse
import hmac
import os
from pathlib import Path

from execution.binance_futures import DEMO_TESTNET_BASE_URL, FuturesCredentials, FuturesREST

ROOT = Path(__file__).resolve().parent
REQUIRED = ("PROBE_API_KEY", "PROBE_API_SECRET", "ISOLATED_DEMO_ACCOUNT")


def parse_probe_file(path: Path) -> dict[str, str]:
    if not path.is_file() or path.resolve() == (ROOT / ".env.testnet").resolve():
        raise ValueError("provide a separate probe credentials file")
    values = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError("invalid probe credentials file")
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip().strip('"').strip("'")
        if name not in REQUIRED or name in values:
            raise ValueError("probe file contains an unknown or duplicate field")
        values[name] = value
    if any(not values.get(name) for name in REQUIRED):
        raise ValueError("probe file must contain PROBE_API_KEY, PROBE_API_SECRET, ISOLATED_DEMO_ACCOUNT")
    if values["ISOLATED_DEMO_ACCOUNT"] != "1":
        raise ValueError("ISOLATED_DEMO_ACCOUNT must be 1 after verifying account separation")
    return values


def primary_keys() -> list[str]:
    env_key = os.environ.get("BINANCE_TESTNET_API_KEY", "").strip()
    path = ROOT / ".env.testnet"
    file_key = ""
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            if raw.strip().startswith("BINANCE_TESTNET_API_KEY="):
                file_key = raw.split("=", 1)[1].strip().strip('"').strip("'")
                break
    return [key for key in (env_key, file_key) if key]


def check_not_primary(probe_key: str, primary: list[str]) -> None:
    if not primary:
        raise RuntimeError("cannot verify separation from the daily bot's API key")
    if any(hmac.compare_digest(probe_key, key) for key in primary):
        raise RuntimeError("probe key matches the daily bot key; refusing")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--credentials", type=Path, required=True)
    args = parser.parse_args()
    values = parse_probe_file(args.credentials)
    check_not_primary(values["PROBE_API_KEY"], primary_keys())
    # Explicitly select the demo host; never inherit a classic/live host setting.
    os.environ["BINANCE_TESTNET_HOST"] = "demo"
    client = FuturesREST("testnet", FuturesCredentials(values["PROBE_API_KEY"],
                                                 values["PROBE_API_SECRET"]))
    if client.base_url != DEMO_TESTNET_BASE_URL:
        raise RuntimeError("probe must use Binance Demo Futures")
    account = client.account()
    positions = [p for p in client.positions() if abs(float(p.get("positionAmt", 0))) > 0]
    orders = client.open_orders()
    dual_side = client.position_mode()
    if not account.get("canTrade") or dual_side or positions or orders:
        print(f"REFUSING: canTrade={account.get('canTrade')} dualSide={dual_side} "
              f"positions={len(positions)} openOrders={len(orders)}")
        return 2
    print("Isolated Demo Futures preflight: PASS; flat, no open orders, one-way mode")
    print("No orders were submitted. The separate maker experiment can be staged here.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
