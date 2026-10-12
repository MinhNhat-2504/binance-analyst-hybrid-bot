# CARRY-7d go-live gate report

Evaluated as of **2026-10-12** (generated 2026-10-12 07:47 UTC by gate_report.py).

## VERDICT: NOT-YET

Earliest evaluable date: **2026-10-22** (10 days). By then:

- needs 10 more COMPLETE testnet runs

## Conditions

| status | condition | measured | threshold | note |
|---|---|---|---|---|
| PASS | paper days | 69 | >= 60 | day 69; recommended 90 |
| PASS | paper Sharpe (ann.) | +2.63 | > +0.50 |  |
| PASS | paper total return | +9.00% | > +0.00% |  |
| PASS | paper max drawdown | -2.9% | > -20% | current DD -2.7%; worst month 2026-10 +2.1% |
| PASS | paper record current | last fill 2026-10-10 (2d ago) | <= 2d |  |
| NOT-YET | testnet COMPLETE runs | 10 | >= 20 | since 2026-08-25; 10 more at one/day -> 2026-10-22; non-COMPLETE: 2026-09-05:UNRESOLVED_EXPOSURE |
| PASS | testnet exposure clean | last run 2026-10-12 COMPLETE | no HALTED_*/MISMATCH |  |
| PASS | testnet reconcile exit 0 | 10 COMPLETE run(s), all reconcile 0 | 0 non-zero |  |
| PASS | tracking error | cum -0.34% over 3d (~-0.80%/wk), 1 wk | no 3 consecutive weeks |div| > 1%/wk | no breaching week (partial: 3d in 2026-W41) |
| PASS | fill shortfall | 119 legs mean +2.9 med +9.1 p90 +68.9 bps | mean <= 10 bps |  |
| PASS | signal canary | 2026-10-11 (1d ago) Binance +2.29 vs Bybit +0.70 | fresh (<= 8d), no ALERT in last 4 | clear |
| PASS | no blocking markers | none | none |  |
| PASS | incidents closed | 7 recorded, all closed | all closed |  |

## What would flip this

- paper Sharpe (ann.): -> FAIL if drops below +0.50
- paper total return: -> FAIL if drops below +0.00%
- paper max drawdown: -> FAIL if equity falls another 17.8% from here
- paper record current: -> FAIL if paper task stops booking
- testnet COMPLETE runs: -> PASS when 10 more COMPLETE runs
- testnet exposure clean: -> FAIL if any run ending in a hand-off state
- tracking error: -> FAIL if 3 more consecutive week(s) beyond 1%
- fill shortfall: -> FAIL if mean exceeds 10 bps
- signal canary: -> FAIL if any ALERT row
- no blocking markers: -> FAIL if ATTENTION/canary_ALERT/lock appears
- incidents closed: -> FAIL if a new ATTENTION incident

## Facts

- paper: day 69, equity 1.0900 (+9.00%), Sharpe +2.63, maxDD -2.9%, current DD -2.7%, worst month 2026-10 +2.1%, last fill day 2026-10-10
- testnet: 10 COMPLETE day(s) since 2026-08-25, last run 2026-10-12, non-COMPLETE: 2026-09-05:UNRESOLVED_EXPOSURE
- tracking: 3 days, cumulative -0.34% of budget (~-0.80%/week), budget 2019 USD

| week | days | divergence USD | % of budget | breach |
|---|---|---|---|---|
| 2026-W41 | 3 | -6.86 | -0.34% |  |

- fills: 119 legs, shortfall mean +2.9 / median +9.1 / p90 +68.9 bps
- canary: last 2026-10-11 (1d ago), alerts in last 4: 0
- markers: none

Exit codes: 0 GO, 1 NO-GO, 2 NOT-YET. Thresholds come from carry_paper_config_v1.json[go_live_gate]; the 20-run and tracking rules from GO_LIVE_CHECKLIST.md.
