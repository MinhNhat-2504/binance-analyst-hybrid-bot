# Execution and data readiness

This repository has a **testnet-only** executor.  It refuses production endpoints,
requires separate `BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET`, and is
halted when its kill-switch file is absent or engaged.  It does not read the legacy
`BINANCE_API_*` names.

## Testnet rehearsal

1. Create a Binance Futures testnet key and put only the two `BINANCE_TESTNET_*`
   variables in your local environment (see `.env.execution.example`).
2. Export the exact frozen paper target:
   `python -B export_carry_targets.py`
3. Inspect it without a network call or API key:
   `python -B run_testnet_execution.py`
4. Only for a deliberate testnet rehearsal, release then execute:
   `python -B run_testnet_execution.py --release-kill-switch "testnet rehearsal YYYY-MM-DD"`
   `python -B run_testnet_execution.py --execute --confirm-testnet I_ACCEPT_TESTNET_ORDERS`
5. Review fills, slippage, and before/after positions (read-only):
   `python -B reconcile_paper_vs_testnet.py`
6. Halt it again:
   `python -B run_testnet_execution.py --engage-kill-switch "rehearsal complete"`

Every run and order leg is kept in `.execution/testnet_execution.sqlite3`. Targets older
than six hours are refused for execution. An unavailable or below-minimum target refuses
the whole portfolio rather than silently changing its composition; any existing affected
position is reduce-only closed where possible. Every exception, including Ctrl+C after
execution starts, engages the kill switch, cancels open orders, and attempts a reduce-only
market flatten of every current position. Sizing is based on `totalMarginBalance`, not
free margin; every order has a deterministic client order ID and is capped by
`max_order_notional_usd`.

The testnet account must be one-way mode. Set it deliberately (with no orders) before a
rehearsal: `python -B run_testnet_execution.py --set-one-way-mode --confirm-testnet I_ACCEPT_TESTNET_ORDERS`.

## Microstructure collection

The collector uses only public production-market WebSocket streams: no credentials and
no order capability.  Its configuration fixes 30 symbols, top-10 depth sampled at most
once per five seconds per symbol, all-market liquidation events, hourly gzip JSONL
rotation, and a 100 GB cap.

Run a local smoke test first:

`python -B run_market_data_collector.py --duration-seconds 60`

Then run persistently from your process manager:

`python -B run_market_data_collector.py`

Inspect `market_data_collector_state.json`; do not treat the collector as running until
`depth_written` is increasing. Counters persist across process restarts and each network
failure is written as a `kind: gap` record to both streams. The liquidation stream is an
exchange-throttled lower bound during cascades. Data is under `market_data/` and
deliberately ignored by git.

## Separate 8-hour research cell

`eight_hour_carry_protocol_v1.json` was created before its result and fixes the only
8-hour cell: settled trailing 7-day funding, 20% tails, next 8-hour open, 10/20 bps per
leg, one latency stress, fixed discovery/symbol holdout and time replay, HAC and joint
circular-shift null.  It cannot modify the daily paper route.

When public market-data access is available, run exactly once per frozen data snapshot:

`python -B run_8h_carry_oos.py --days 600 --n-perm 999`

The generated `eight_hour_carry_report.json` is research-only.  It can nominate a
separate paper-v2 trial, never promote the existing daily route.
