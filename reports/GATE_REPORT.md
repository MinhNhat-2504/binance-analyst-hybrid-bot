# CARRY-7d go-live gate report

Evaluated as of **2026-10-20** (generated 2026-10-20 02:16 UTC by gate_report.py).

## VERDICT: NOT-YET

Earliest evaluable date: **2026-10-27** (7 days). By then:

- needs 7 more COMPLETE testnet runs

## Conditions

| status | condition | measured | threshold | note |
|---|---|---|---|---|
| PASS | paper days | 77 | >= 60 | day 77; recommended 90 |
| PASS | paper Sharpe (ann.) | +2.80 | > +0.50 |  |
| PASS | paper total return | +10.89% | > +0.00% |  |
| PASS | paper max drawdown | -4.1% | > -20% | current DD -1.0%; worst month 2026-08 +2.2% |
| PASS | paper record current | last fill 2026-10-18 (2d ago) | <= 2d |  |
| NOT-YET | testnet COMPLETE runs | 13 | >= 20 | since 2026-08-25; 7 more at one/day -> 2026-10-27; non-COMPLETE: 2026-09-05:UNRESOLVED_EXPOSURE, 2026-10-15:HALTED_MID_BOOK |
| PASS | testnet exposure clean | last run 2026-10-20 COMPLETE | no HALTED_*/MISMATCH |  |
| PASS | testnet reconcile exit 0 | 13 COMPLETE run(s), all reconcile 0 | 0 non-zero |  |
| PASS | tracking error | cum -0.19% over 6d (~-0.23%/wk), 2 wk | no 3 consecutive weeks |div| > 1%/wk | no breaching week (partial: 3d in 2026-W42) |
| PASS | fill shortfall | 338 legs mean +9.0 med +9.1 p90 +78.8 bps | mean <= 10 bps |  |
| PASS | signal canary | 2026-10-18 (2d ago) Binance +2.44 vs Bybit +0.70 | fresh (<= 8d), no ALERT in last 4 | clear |
| PASS | no blocking markers | none | none |  |
| PASS | incidents closed | 8 recorded, all closed | all closed |  |

## What would flip this

- paper Sharpe (ann.): -> FAIL if drops below +0.50
- paper total return: -> FAIL if drops below +0.00%
- paper max drawdown: -> FAIL if equity falls another 19.2% from here
- paper record current: -> FAIL if paper task stops booking
- testnet COMPLETE runs: -> PASS when 7 more COMPLETE runs
- testnet exposure clean: -> FAIL if any run ending in a hand-off state
- tracking error: -> FAIL if 3 more consecutive week(s) beyond 1%
- fill shortfall: -> FAIL if mean exceeds 10 bps
- signal canary: -> FAIL if any ALERT row
- no blocking markers: -> FAIL if ATTENTION/canary_ALERT/lock appears
- incidents closed: -> FAIL if a new ATTENTION incident

## Facts

- paper: day 77, equity 1.1089 (+10.89%), Sharpe +2.80, maxDD -4.1%, current DD -1.0%, worst month 2026-08 +2.2%, last fill day 2026-10-18
- testnet: 13 COMPLETE day(s) since 2026-08-25, last run 2026-10-20, non-COMPLETE: 2026-09-05:UNRESOLVED_EXPOSURE, 2026-10-15:HALTED_MID_BOOK
- tracking: 6 days, cumulative -0.19% of budget (~-0.23%/week), budget 2019 USD

| week | days | divergence USD | % of budget | breach |
|---|---|---|---|---|
| 2026-W41 | 3 | -6.86 | -0.34% |  |
| 2026-W42 | 3 | +3.00 | +0.15% |  |

- fills: 338 legs, shortfall mean +9.0 / median +9.1 / p90 +78.8 bps
- canary: last 2026-10-18 (2d ago), alerts in last 4: 0
- markers: none

Exit codes: 0 GO, 1 NO-GO, 2 NOT-YET. Thresholds come from carry_paper_config_v1.json[go_live_gate]; the 20-run and tracking rules from GO_LIVE_CHECKLIST.md.
