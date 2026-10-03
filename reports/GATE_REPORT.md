# CARRY-7d go-live gate report

Evaluated as of **2026-10-03** (generated 2026-10-03 05:36 UTC by gate_report.py).

## VERDICT: NOT-YET

Earliest evaluable date: **2026-10-19** (16 days). By then:

- needs 16 more COMPLETE testnet runs
- canary alerts must clear (edge fading / regime change)

## Conditions

| status | condition | measured | threshold | note |
|---|---|---|---|---|
| PASS | paper days | 60 | >= 60 | day 60; recommended 90 |
| PASS | paper Sharpe (ann.) | +2.81 | > +0.50 |  |
| PASS | paper total return | +7.82% | > +0.00% |  |
| PASS | paper max drawdown | -2.9% | > -20% | current DD -0.3%; worst month 2026-10 +1.0% |
| PASS | paper record current | last fill 2026-10-01 (2d ago) | <= 2d |  |
| NOT-YET | testnet COMPLETE runs | 4 | >= 20 | since 2026-08-25; 16 more at one/day -> 2026-10-19; non-COMPLETE: 2026-09-05:UNRESOLVED_EXPOSURE |
| PASS | testnet exposure clean | last run 2026-09-26 COMPLETE | no HALTED_*/MISMATCH |  |
| PASS | testnet reconcile exit 0 | 4 COMPLETE run(s), all reconcile 0 | 0 non-zero |  |
| PASS | tracking error | cum +0.48% over 3d (~+1.13%/wk), 1 wk | no 3 consecutive weeks |div| > 1%/wk | no breaching week (partial: 3d in 2026-W35) |
| PASS | fill shortfall | 90 legs mean -5.2 med +3.7 p90 +62.6 bps | mean <= 10 bps |  |
| FAIL | signal canary | 2026-09-27 (6d ago) Binance +1.04 vs Bybit +0.65 | fresh (<= 8d), no ALERT in last 4 | ALERT: 2026-09-06: SIGNAL: Binance edge (+0.84) converging to Bybit (+1.11) - fading, not spreading |
| PASS | no blocking markers | none | none |  |
| PASS | incidents closed | 6 recorded, all closed | all closed |  |

## What would flip this

- paper Sharpe (ann.): -> FAIL if drops below +0.50
- paper total return: -> FAIL if drops below +0.00%
- paper max drawdown: -> FAIL if equity falls another 19.8% from here
- paper record current: -> FAIL if paper task stops booking
- testnet COMPLETE runs: -> PASS when 16 more COMPLETE runs
- testnet exposure clean: -> FAIL if any run ending in a hand-off state
- tracking error: -> FAIL if 3 more consecutive week(s) beyond 1%
- fill shortfall: -> FAIL if mean exceeds 10 bps
- signal canary: -> PASS when 4 consecutive clear rows
- no blocking markers: -> FAIL if ATTENTION/canary_ALERT/lock appears
- incidents closed: -> FAIL if a new ATTENTION incident

## Facts

- paper: day 60, equity 1.0782 (+7.82%), Sharpe +2.81, maxDD -2.9%, current DD -0.3%, worst month 2026-10 +1.0%, last fill day 2026-10-01
- testnet: 4 COMPLETE day(s) since 2026-08-25, last run 2026-09-26, non-COMPLETE: 2026-09-05:UNRESOLVED_EXPOSURE
- tracking: 3 days, cumulative +0.48% of budget (~+1.13%/week), budget 2000 USD

| week | days | divergence USD | % of budget | breach |
|---|---|---|---|---|
| 2026-W35 | 3 | +9.68 | +0.48% |  |

- fills: 90 legs, shortfall mean -5.2 / median +3.7 / p90 +62.6 bps
- canary: last 2026-09-27 (6d ago), alerts in last 4: 1
- markers: none

Exit codes: 0 GO, 1 NO-GO, 2 NOT-YET. Thresholds come from carry_paper_config_v1.json[go_live_gate]; the 20-run and tracking rules from GO_LIVE_CHECKLIST.md.
