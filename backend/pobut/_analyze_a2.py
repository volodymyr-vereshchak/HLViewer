import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np
from pobut_predictor import GROUP_MERGE, META_COLS

MONTHS = list(range(1,13))
WINTER = [1,2,3,10,11,12]
SUMMER = [6,7,8,9]
UA = {1:'Jan',2:'Feb',3:'Mar',4:'Apr',5:'May',6:'Jun',
      7:'Jul',8:'Aug',9:'Sep',10:'Oct',11:'Nov',12:'Dec'}

# == modem profiles ==
md = pd.read_csv('data/input/profile_pobut_daily_result.csv', sep=';', low_memory=False)
md.columns = META_COLS + list(md.columns[len(META_COLS):])
md = md[md['gas_off'].isna() & md['alternative'].isna() & md['dacha'].isna()]
md['appliance_group'] = md['appliance_group'].astype(str).str.strip().replace(GROUP_MERGE)

# Ukrainian OP symbol
OP = '\u041e\u041f'
md['has_op'] = md['appliance_group'].str.contains(OP, na=False)
print(f"has_op: {md['has_op'].sum()} heating, {(~md['has_op']).sum()} cooking")

day_cols = [c for c in md.columns if '.' in c and len(c)==10]
dates = pd.to_datetime([c for c in day_cols], format='%d.%m.%Y')
heat_daily = pd.Series(md[md['has_op']][day_cols].sum().values, index=dates).sort_index()
cook_daily = pd.Series(md[~md['has_op']][day_cols].sum().values, index=dates).sort_index()
heat_m = heat_daily['2025'].resample('MS').sum()
cook_m = cook_daily['2025'].resample('MS').sum()
w_heat = (heat_m / heat_m.sum()).values
w_cook = (cook_m / cook_m.sum()).values

# == billing pivot ==
dc = pd.read_excel('data/annual_decompose_result.xlsx', sheet_name='All_GRS_monthly')
dc['grs'] = dc['grs'].astype(str)
dc['month'] = dc['month'].astype(int)
billing_pivot = dc.pivot(index='month', columns='grs', values='billing')
decomp_pivot  = dc.pivot(index='month', columns='grs', values='total_pred')
annual_billing = dc.groupby('grs')['billing'].sum()
annual_decomp  = dc.groupby('grs')['total_pred'].sum()

MONTH_ENG = {'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,
             'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12}
ap = pd.read_csv('data/input/all_pobut_enriched.csv', low_memory=False)
ap['account_id'] = pd.to_numeric(ap['account_id'], errors='coerce')
ap['appliance_group'] = ap['appliance_group'].astype(str).str.strip().replace(GROUP_MERGE)
ap['has_op'] = ap['appliance_group'].str.contains(OP, na=False)
for eng, m in MONTH_ENG.items():
    col = f'{eng}_2025'
    if col in ap.columns:
        ap[col] = pd.to_numeric(ap[col], errors='coerce').fillna(0)
mc_ap = [f'{e}_2025' for e in MONTH_ENG if f'{e}_2025' in ap.columns]
ap['annual'] = ap[mc_ap].sum(axis=1)

modem_ids = set(pd.to_numeric(md.iloc[:,0], errors='coerce').dropna().astype(int))
nm = ap[~ap['account_id'].isin(modem_ids) & ap['grs'].notna()].copy()

# heating_share method 1: appliance_group
hs_appl = nm.groupby('grs').apply(
    lambda g: g.loc[g['has_op'], 'annual'].sum() / g['annual'].sum()
    if g['annual'].sum() > 0 else 0.5, include_groups=False
).rename('hs_appl')

# heating_share method 2: from winter billing %
# heating W%~91%, cooking W%~65% => hs = (actual_W% - 0.65)/(0.91-0.65)
winter_cols = [f'{e}_2025' for e, mv in MONTH_ENG.items() if mv in WINTER and f'{e}_2025' in ap.columns]
nm['winter_bill'] = nm[winter_cols].sum(axis=1)
hs_winter = nm.groupby('grs').apply(
    lambda g: (g['winter_bill'].sum() / g['annual'].sum() - 0.65) / (0.91 - 0.65)
    if g['annual'].sum() > 0 else 0.5, include_groups=False
).clip(0, 1).rename('hs_winter')

# target GRS (exclude 2 big problematic)
SKIP_STR = ['\u043a\u0456\u043b\u044c\u0446\u0435', '\u043c.\u0417\u0430\u043f\u043e\u0440\u0456\u0436']
target_grs = [g for g in annual_billing.index if not any(s in g for s in SKIP_STR)]
print(f"Target GRS: {len(target_grs)}")

hs_a = dict(zip(hs_appl.index, hs_appl.values))
hs_w = dict(zip(hs_winter.index, hs_winter.values))

print("\nSanity check heating_share (appl vs winter):")
for grs in list(target_grs)[:5]:
    print(f"  {grs[:40]}: appl={hs_a.get(grs,0):.2f}  winter={hs_w.get(grs,0):.2f}")

# == MAPE function ==
def mape_f(fact_piv, grs_list, ann_dict, hs_dict, wh, wc, months=None, k_m=None):
    if months is None: months = MONTHS
    if k_m is None: k_m = {m: 1.0 for m in MONTHS}
    mapes = []
    for grs in grs_list:
        ann = ann_dict.get(grs, 0)
        if ann <= 0 or grs not in fact_piv.columns: continue
        hs = hs_dict.get(grs, 0.5)
        errs = []
        for m in months:
            f = fact_piv.loc[m, grs] if m in fact_piv.index else np.nan
            if np.isnan(f) or f == 0: continue
            p = ann * (hs * wh[m-1] + (1-hs) * wc[m-1]) * k_m[m]
            errs.append(abs(p-f)/abs(f)*100)
        if errs: mapes.append(np.mean(errs))
    return np.nanmean(mapes)

# == 1. Monthly systematic bias ==
print("\n=== 1. Monthly bias A2 baseline (billing, hs_appl) ===")
ann_b = dict(annual_billing)
month_preds = {m: [] for m in MONTHS}
month_facts = {m: [] for m in MONTHS}
for grs in target_grs:
    ann = ann_b.get(grs, 0)
    if ann <= 0 or grs not in billing_pivot.columns: continue
    hs = hs_a.get(grs, 0.5)
    for m in MONTHS:
        f = billing_pivot.loc[m, grs] if m in billing_pivot.index else np.nan
        if np.isnan(f) or f == 0: continue
        p = ann * (hs * w_heat[m-1] + (1-hs) * w_cook[m-1])
        month_preds[m].append(p)
        month_facts[m].append(f)

print("  Month    sum_pred    sum_fact   bias%   mean_abs_bias%")
for m in MONTHS:
    sp = sum(month_preds[m])
    sf = sum(month_facts[m])
    if sf == 0: continue
    bias = (sp-sf)/sf*100
    abs_biases = [abs(p-f)/abs(f)*100 for p,f in zip(month_preds[m], month_facts[m]) if f!=0]
    mab = np.mean(abs_biases) if abs_biases else np.nan
    print(f"  {UA[m]:5}  {sp/1e3:10.1f}  {sf/1e3:10.1f}  {bias:>+7.1f}%  {mab:>14.1f}%")

# == 2. Compare variants ==
print("\n=== 2. Comparison of improvements ===")
m_base   = mape_f(billing_pivot, target_grs, ann_b,               hs_a, w_heat, w_cook)
m_hs_w   = mape_f(billing_pivot, target_grs, ann_b,               hs_w, w_heat, w_cook)
m_dec    = mape_f(decomp_pivot,  target_grs, dict(annual_decomp), hs_a, w_heat, w_cook)
m_dec_w  = mape_f(decomp_pivot,  target_grs, dict(annual_decomp), hs_w, w_heat, w_cook)
mw_base  = mape_f(billing_pivot, target_grs, ann_b,               hs_a, w_heat, w_cook, WINTER)
mw_hs_w  = mape_f(billing_pivot, target_grs, ann_b,               hs_w, w_heat, w_cook, WINTER)
mw_dec   = mape_f(decomp_pivot,  target_grs, dict(annual_decomp), hs_a, w_heat, w_cook, WINTER)
mw_dec_w = mape_f(decomp_pivot,  target_grs, dict(annual_decomp), hs_w, w_heat, w_cook, WINTER)

print(f"  {'Variant':<42} {'MAPE_all':>10} {'MAPE_winter':>12}")
print(f"  {'-'*66}")
print(f"  {'baseline: billing + hs_appl':<42} {m_base:>10.1f}% {mw_base:>12.1f}%")
print(f"  {'billing + hs_winter':<42} {m_hs_w:>10.1f}% {mw_hs_w:>12.1f}%")
print(f"  {'decompose + hs_appl':<42} {m_dec:>10.1f}% {mw_dec:>12.1f}%")
print(f"  {'decompose + hs_winter':<42} {m_dec_w:>10.1f}% {mw_dec_w:>12.1f}%")

# == 3. Monthly correction factors ==
print("\n=== 3. Monthly correction factors k[m] = median(fact/pred) ===")
k_bill = {}
for m in MONTHS:
    ratios = []
    for grs in target_grs:
        ann = ann_b.get(grs, 0)
        if ann <= 0 or grs not in billing_pivot.columns: continue
        hs = hs_a.get(grs, 0.5)
        f = billing_pivot.loc[m, grs] if m in billing_pivot.index else np.nan
        if np.isnan(f) or f == 0: continue
        p = ann * (hs * w_heat[m-1] + (1-hs) * w_cook[m-1])
        if p > 0: ratios.append(f/p)
    k_bill[m] = np.median(ratios) if ratios else 1.0

k_dec = {}
ann_d = dict(annual_decomp)
for m in MONTHS:
    ratios = []
    for grs in target_grs:
        ann = ann_d.get(grs, 0)
        if ann <= 0 or grs not in decomp_pivot.columns: continue
        hs = hs_a.get(grs, 0.5)
        f = decomp_pivot.loc[m, grs] if m in decomp_pivot.index else np.nan
        if np.isnan(f) or f == 0: continue
        p = ann * (hs * w_heat[m-1] + (1-hs) * w_cook[m-1])
        if p > 0: ratios.append(f/p)
    k_dec[m] = np.median(ratios) if ratios else 1.0

print(f"  Month  k_billing  k_decompose")
for m in MONTHS:
    print(f"  {UA[m]:5}  {k_bill[m]:9.3f}  {k_dec[m]:11.3f}")

mc_b  = mape_f(billing_pivot, target_grs, ann_b,   hs_a, w_heat, w_cook, k_m=k_bill)
mc_d  = mape_f(decomp_pivot,  target_grs, ann_d,   hs_a, w_heat, w_cook, k_m=k_dec)
mwc_b = mape_f(billing_pivot, target_grs, ann_b,   hs_a, w_heat, w_cook, WINTER, k_bill)
mwc_d = mape_f(decomp_pivot,  target_grs, ann_d,   hs_a, w_heat, w_cook, WINTER, k_dec)
print(f"\n  billing + k_m correction:   MAPE_all={mc_b:.1f}%  winter={mwc_b:.1f}%")
print(f"  decompose + k_m correction: MAPE_all={mc_d:.1f}%  winter={mwc_d:.1f}%")

# == 4. GRS-level constant bias: candidate for per-GRS calibration ==
print("\n=== 4. GRS-level winter/summer bias (stable offset = calibration candidate) ===")
print(f"  {'GRS':<35} {'hs_ap':>6} {'hs_w':>6}  {'W_bias':>8} {'S_bias':>8}  {'MAPE':>6}")
for grs in sorted(target_grs):
    ann = ann_b.get(grs, 0)
    if ann <= 0 or grs not in billing_pivot.columns: continue
    hs = hs_a.get(grs, 0.5)
    wv, sv, av = [], [], []
    for m in MONTHS:
        f = billing_pivot.loc[m, grs] if m in billing_pivot.index else np.nan
        if np.isnan(f) or f == 0: continue
        p = ann * (hs * w_heat[m-1] + (1-hs) * w_cook[m-1])
        bias = (f-p)/abs(p)*100
        av.append(abs(p-f)/abs(f)*100)
        if m in WINTER: wv.append(bias)
        if m in SUMMER: sv.append(bias)
    if not av: continue
    wb_ = np.mean(wv) if wv else np.nan
    sb_ = np.mean(sv) if sv else np.nan
    hw = hs_w.get(grs, np.nan)
    print(f"  {grs[:34]:<35} {hs:>6.2f} {hw:>6.2f}  {wb_:>+8.1f}% {sb_:>+8.1f}%  {np.mean(av):>6.1f}%")

# == 5. What if we calibrate per GRS (scale by winter fact/pred) ==
print("\n=== 5. Per-GRS calibration (scale by annual fact/pred) ===")
mapes_calib = []
for grs in target_grs:
    ann = ann_b.get(grs, 0)
    if ann <= 0 or grs not in billing_pivot.columns: continue
    hs = hs_w.get(grs, 0.5)  # use hs_winter
    # calibration factor from winter months only (more stable)
    w_preds, w_facts = [], []
    for m in WINTER:
        f = billing_pivot.loc[m, grs] if m in billing_pivot.index else np.nan
        if np.isnan(f) or f == 0: continue
        p = ann * (hs * w_heat[m-1] + (1-hs) * w_cook[m-1])
        w_preds.append(p); w_facts.append(f)
    if not w_preds: continue
    calib = sum(w_facts) / sum(w_preds) if sum(w_preds) > 0 else 1.0
    errs = []
    for m in MONTHS:
        f = billing_pivot.loc[m, grs] if m in billing_pivot.index else np.nan
        if np.isnan(f) or f == 0: continue
        p = ann * (hs * w_heat[m-1] + (1-hs) * w_cook[m-1]) * calib
        errs.append(abs(p-f)/abs(f)*100)
    if errs: mapes_calib.append(np.mean(errs))

print(f"  A2 + hs_winter + per-GRS winter calib:  MAPE={np.nanmean(mapes_calib):.1f}%")

# == Summary ==
print("\n=== SUMMARY ===")
print(f"  {'Variant':<50} {'MAPE_all':>10} {'MAPE_winter':>12}")
print(f"  {'-'*74}")
rows = [
    ('baseline: billing + hs_appl',                         m_base,  mw_base),
    ('billing + hs_winter',                                  m_hs_w,  mw_hs_w),
    ('decompose + hs_appl',                                  m_dec,   mw_dec),
    ('decompose + hs_winter',                                m_dec_w, mw_dec_w),
    ('billing + k[m] correction',                            mc_b,    mwc_b),
    ('decompose + k[m] correction',                          mc_d,    mwc_d),
    ('billing + hs_winter + per-GRS winter calib',           np.nanmean(mapes_calib), np.nan),
]
for label, ma, mw in rows:
    mw_s = f'{mw:12.1f}%' if not np.isnan(mw) else '         n/a'
    print(f"  {label:<50} {ma:>10.1f}%{mw_s}")
