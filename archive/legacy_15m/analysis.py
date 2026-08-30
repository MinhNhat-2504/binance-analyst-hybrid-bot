import pandas as pd
import numpy as np

f1 = 'shadow_ledger_candidates_v4.csv'
f2 = 'shadow_ledger_candidates_v4_pre_strict_symbol_prior_1778861831.csv'

df1 = pd.read_csv(f1)
df2 = pd.read_csv(f2)

def summarize(df, label):
    print(f"\n--- {label} ---")
    for col in ['Outcome_PnL', 'Provisional_PnL']:
        v = df[col].dropna()
        if len(v) > 0:
            print(f"{col}: Count={len(v)}, WinRate={(v>0).mean():.3f}, Mean={v.mean():.6f}, Total={v.sum():.6f}")

summarize(df1, "Current")
summarize(df2, "Backup")

print("\n--- PnL by Side & Regime (Current) ---")
print(df1.groupby('side')[['Outcome_PnL', 'Provisional_PnL']].sum())
print(df1.groupby('regime')[['Outcome_PnL', 'Provisional_PnL']].sum())

if 'symbol_prior_multiplier' in df1.columns:
    print("\n--- PnL by symbol_prior_multiplier (Current) ---")
    print(df1.groupby('symbol_prior_multiplier')[['Outcome_PnL', 'Provisional_PnL']].sum())

print("\n--- PnL/Count by optimizer_candidate (Backup) ---")
print(df2.groupby('optimizer_candidate').agg({'Outcome_PnL': ['count', 'sum'], 'Provisional_PnL': ['count', 'sum']}))

print("\n--- Removed Rows (optimizer_candidate True in Backup, but not in Current) ---")
# Since we don't have candidate_id, we infer from general counts.
# In Backup: 6 rows are True for optimizer_candidate (Outcome_PnL sum -0.0018).
# In Current: 0 rows are True.
# These 6 filtered rows represent the impact.
removed_backup = df2[df2['optimizer_candidate'] == True]
print(f"Removed Rows Count: {len(removed_backup)}")
print(f"Removed Outcome_PnL Sum: {removed_backup['Outcome_PnL'].sum():.6f}")
print(f"Net-Impact: {'Negative' if removed_backup['Outcome_PnL'].sum() < 0 else 'Positive'}")

# ----------------------------
# Additional analyses (A-D)
# ----------------------------
def colci(df, name):
    for c in df.columns:
        if c.lower() == name.lower():
            return c
    return None

out_col = colci(df1, 'Outcome_PnL')
prov_col = colci(df1, 'Provisional_PnL')
proba_col = colci(df1, 'final_proba') or colci(df1, 'proba') or colci(df1, 'Calib_Proba')
side_col = colci(df1, 'side')
reg_col = colci(df1, 'regime')
opt_col = colci(df1, 'optimizer_candidate')
exit_col = colci(df1, 'Exit_Reason') or colci(df1, 'exit_reason')

print('\n--- A: Top-10 worst symbols (LONG, regime 0) ---')
if out_col and side_col and reg_col:
    dfA = df1.dropna(subset=[out_col, side_col, reg_col]).copy()
    dfA[out_col] = pd.to_numeric(dfA[out_col], errors='coerce')
    mask = (dfA[side_col].astype(str).str.upper() == 'LONG') & (pd.to_numeric(dfA[reg_col], errors='coerce') == 0)
    if 'symbol' in dfA.columns:
        grp = dfA[mask].groupby('symbol')[out_col].agg(['count','mean','sum']).sort_values('sum')
        print(grp.head(10).to_string())
    else:
        print('symbol column not present')
else:
    print(' Missing columns for A')

print('\n--- B: EV after fees (net PnL) ---')
fee_col = colci(df1, 'Fee') or colci(df1, 'fee')
inv_col = colci(df1, 'invested_usdt') or colci(df1, 'size_usd')
if out_col and fee_col and fee_col in df1.columns:
    dfB = df1.copy()
    dfB[out_col] = pd.to_numeric(dfB[out_col], errors='coerce')
    dfB[fee_col] = pd.to_numeric(dfB[fee_col], errors='coerce')
    max_fee = dfB[fee_col].abs().max()
    fee_pct = None
    if pd.notna(max_fee) and max_fee < 1.0:
        fee_pct = dfB[fee_col]
    elif inv_col and inv_col in dfB.columns:
        dfB[inv_col] = pd.to_numeric(dfB[inv_col], errors='coerce')
        fee_pct = dfB[fee_col] / dfB[inv_col]
    if fee_pct is not None:
        dfB['net_outcome'] = dfB[out_col] - fee_pct
        s = dfB['net_outcome'].dropna()
        print(f"Net Outcome: n={len(s)} mean={s.mean()*100:.3f}% total={s.sum()*100:.2f}%")
    else:
        print('Fee present but could not interpret as pct; skipping net calc')
else:
    print('No Fee column found or missing outcome; skipping B')

print('\n--- C: Size distribution and PnL for LONG/regime0 ---')
size_col = colci(df1, 'size_usd') or colci(df1, 'invested_usdt')
if side_col and reg_col and out_col and size_col and size_col in df1.columns:
    dfC = df1.dropna(subset=[out_col, side_col, reg_col, size_col]).copy()
    dfC[out_col] = pd.to_numeric(dfC[out_col], errors='coerce')
    dfC[size_col] = pd.to_numeric(dfC[size_col], errors='coerce')
    mask = (dfC[side_col].astype(str).str.upper()=='LONG') & (pd.to_numeric(dfC[reg_col], errors='coerce')==0)
    if 'symbol' in dfC.columns:
        grpC = dfC[mask].groupby('symbol').agg(count=(out_col,'count'), sum_pnl=(out_col,'sum'), mean_size=(size_col,'mean'))
        print('Top symbols by mean size:')
        print(grpC.sort_values('mean_size', ascending=False).head(10).to_string())
        print('\nTop loss symbols by PnL:')
        print(grpC.sort_values('sum_pnl').head(10).to_string())
    else:
        print('symbol column not present for C')
else:
    print('Missing columns for C')

print('\n--- D: TP1_RUNNER_TP vs other Exit_Reasons ---')
if exit_col and out_col and exit_col in df1.columns:
    dfD = df1.dropna(subset=[out_col, exit_col]).copy()
    dfD[out_col] = pd.to_numeric(dfD[out_col], errors='coerce')
    grpD = dfD.groupby(exit_col)[out_col].agg(['count','mean','sum'])
    print(grpD.apply(lambda x: (int(x['count']), x['mean']*100, x['sum']*100), axis=1).to_string())
    key = 'TP1_RUNNER_TP'
    if key in dfD[exit_col].astype(str).unique():
        tp = dfD[dfD[exit_col].astype(str)==key]
        pos_frac = (tp[out_col]>0).mean()
        print(f"\nTP1_RUNNER_TP positive fraction: {pos_frac*100:.1f}% (n={len(tp)})")
else:
    print('Missing exit reason or outcome for D')

print('\nAll analyses done')
