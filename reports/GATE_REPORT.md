# CARRY-7d go-live gate report

Evaluated as of **2026-09-03** (generated 2026-09-03 20:55 UTC by gate_report.py).

## VERDICT: NOT-YET

Earliest evaluable date: **2026-10-02** (29 days). By then:

- paper must book 30 more days without interruption (day 60 on 2026-10-02)
- needs 17 more COMPLETE testnet runs

## Conditions

| status | condition | measured | threshold | note |
|---|---|---|---|---|
| NOT-YET | paper days | 30 | >= 60 | day 60 books on 2026-10-02 (29d) |
| NOT-YET | paper Sharpe (ann.) | +1.43 | > +0.50 | so far would PASS; judged at day 60 |
| NOT-YET | paper total return | +1.51% | > +0.00% | so far would PASS; judged at day 60 |
| PASS | paper max drawdown | -2.5% | > -20% | current DD -1.4%; worst month 2026-09 -0.7% |
| PASS | paper record current | last fill 2026-09-01 (2d ago) | <= 2d |  |
| NOT-YET | testnet COMPLETE runs | 3 | >= 20 | since 2026-08-25; 17 more at one/day -> 2026-09-20; non-COMPLETE: none |
| PASS | testnet exposure clean | last run 2026-08-27 COMPLETE | no HALTED_*/MISMATCH |  |
| PASS | testnet reconcile exit 0 | 3 COMPLETE run(s), all reconcile 0 | 0 non-zero |  |
| PASS | tracking error | cum +0.48% over 3d (~+1.13%/wk), 1 wk | no 3 consecutive weeks |div| > 1%/wk | no breaching week (partial: 3d in 2026-W35) |
| PASS | fill shortfall | 90 legs mean -5.2 med +3.7 p90 +62.6 bps | mean <= 10 bps |  |
| PASS | signal canary | 2026-09-03 (0d ago) Binance +1.11 vs Bybit +1.26 | fresh (<= 8d), no ALERT in last 4 | clear |
| PASS | no blocking markers | none | none |  |
| PASS | incidents closed | 3 recorded, all closed | all closed |  |

## What would flip this

- paper days: -> PASS when 30 more booked days
- paper Sharpe (ann.): -> PASS when stays above +0.50 at day 60
- paper total return: -> PASS when stays above +0.00% at day 60
- paper max drawdown: -> FAIL if equity falls another 18.9% from here
- paper record current: -> FAIL if paper task stops booking
- testnet COMPLETE runs: -> PASS when 17 more COMPLETE runs
- testnet exposure clean: -> FAIL if any run ending in a hand-off state
- tracking error: -> FAIL if 3 more consecutive week(s) beyond 1%
- fill shortfall: -> FAIL if mean exceeds 10 bps
- signal canary: -> FAIL if any ALERT row
- no blocking markers: -> FAIL if ATTENTION/canary_ALERT/lock appears
- incidents closed: -> FAIL if a new ATTENTION incident

## Facts

- paper: day 30, equity 1.0151 (+1.51%), Sharpe +1.43, maxDD -2.5%, current DD -1.4%, worst month 2026-09 -0.7%, last fill day 2026-09-01
- testnet: 3 COMPLETE day(s) since 2026-08-25, last run 2026-08-27, non-COMPLETE: none
- tracking: 3 days, cumulative +0.48% of budget (~+1.13%/week), budget 2000 USD

| week | days | divergence USD | % of budget | breach |
|---|---|---|---|---|
| 2026-W35 | 3 | +9.68 | +0.48% |  |

- fills: 90 legs, shortfall mean -5.2 / median +3.7 / p90 +62.6 bps
- canary: last 2026-09-03 (0d ago), alerts in last 4: 0
- markers: none

Exit codes: 0 GO, 1 NO-GO, 2 NOT-YET. Thresholds come from carry_paper_config_v1.json[go_live_gate]; the 20-run and tracking rules from GO_LIVE_CHECKLIST.md.
