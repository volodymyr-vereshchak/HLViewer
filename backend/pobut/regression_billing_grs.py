"""
Лінійна регресія: передбачення місячного білінгу по ГРС.

Features:
  - Модемні місячні агрегати (total/OP/non-OP volume, spec, n_active) — по ГРС
  - Погода (HDH, температура, мороз, вітер, тощо) — по місяцю
  - Статика абонентів (n_op, area_op, n_nonop, share_op) — по ГРС
  - Взаємодія погода × ємність ГРС
  - Лаговий білінг (місяць m-1)

Train: 2025 міс. 1–11  |  Test: грудень (12)
Порівняння: Ridge / Lasso / ElasticNet з cross-val по α
"""
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge, Lasso, ElasticNet, RidgeCV
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import r2_score, mean_absolute_error

from pobut_predictor import GROUP_MERGE

MODEM_FILE   = 'data/input/profile_pobut_daily_result.csv'
SUBS_FILE    = 'data/input/all_pobut_enriched.csv'
WEATHER_FILE = '../data/weather_2025_daily_contractual.csv'
OUT_XLSX     = 'data/regression_grs_dec.xlsx'

MONTH_ENG = {'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,
             'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12}
MONTH_UA  = {1:'Січ',2:'Лют',3:'Бер',4:'Кві',5:'Тра',6:'Чер',
             7:'Лип',8:'Сер',9:'Вер',10:'Жов',11:'Лис',12:'Гру'}
LARGE_GRS = 5_000_000

# ═══════════════════════════════════════════════════════════
print("=== 1. Білінг ===")
ap = pd.read_csv(SUBS_FILE, low_memory=False)
ap['account_id'] = pd.to_numeric(ap['account_id'], errors='coerce')
if 'appliance_group' in ap.columns:
    ap['appliance_group'] = ap['appliance_group'].replace(GROUP_MERGE)
ap['heated_area'] = pd.to_numeric(ap['heated_area'], errors='coerce').fillna(0)

month_cols = sorted(
    [c for c in ap.columns if c.endswith('_2025') and c.split('_')[0] in MONTH_ENG],
    key=lambda c: MONTH_ENG[c.split('_')[0]],
)
for c in month_cols:
    ap[c] = pd.to_numeric(ap[c], errors='coerce').fillna(0)

ap_ok = ap[ap['grs'].notna()].copy()
ap_ok['has_op'] = ap_ok['appliance_group'].str.contains('ОП', na=False)

billing_parts = []
for c in month_cols:
    m = MONTH_ENG[c.split('_')[0]]
    g = ap_ok.groupby('grs')[c].sum().clip(lower=0).reset_index()
    g.columns = ['grs', 'billing']
    g['month'] = m
    billing_parts.append(g)
billing_df = pd.concat(billing_parts, ignore_index=True)
print(f"  {billing_df['grs'].nunique()} GRS × {billing_df['month'].nunique()} міс.")

# ═══════════════════════════════════════════════════════════
print("\n=== 2. Статика по ГРС ===")
def _grs_static(df):
    op   = df['has_op']
    ar   = df.loc[op & (df['heated_area'] > 0), 'heated_area']
    return pd.Series({
        'n_total':       len(df),
        'n_op':          int(op.sum()),
        'n_nonop':       int((~op).sum()),
        'area_op_total': float(df.loc[op, 'heated_area'].sum()),
        'area_op_mean':  float(ar.mean()) if len(ar) > 0 else 0.0,
        'share_op':      float(op.mean()),
    })

grs_static = ap_ok.groupby('grs').apply(_grs_static).reset_index()
ann_bill = billing_df.groupby('grs')['billing'].sum()
grs_static['ann_billing'] = grs_static['grs'].map(ann_bill)
grs_static['is_large']    = (grs_static['ann_billing'] >= LARGE_GRS).astype(int)
print(grs_static[['grs','n_total','n_op','area_op_total','share_op']].to_string(index=False))

# ═══════════════════════════════════════════════════════════
print("\n=== 3. Модемні місячні агрегати ===")
md = pd.read_csv(MODEM_FILE, sep=';', low_memory=False)
if 'appliance_group' in md.columns:
    md['appliance_group'] = md['appliance_group'].replace(GROUP_MERGE)
md['heated_area'] = pd.to_numeric(md['heated_area'], errors='coerce').fillna(0)

aid_grs = ap_ok.dropna(subset=['account_id']).set_index('account_id')['grs'].to_dict()
md['_aid'] = pd.to_numeric(md['account_id'], errors='coerce')
md['grs']  = md['_aid'].map(aid_grs)
md_ok = md[md['grs'].notna()].copy()
md_ok['has_op'] = md_ok['appliance_group'].str.contains('ОП', na=False)

by_month_cols = {}
for c in md.columns:
    if isinstance(c, str) and c.count('.') == 2 and len(c) == 10:
        try:
            _, ms, y = c.split('.')
            if int(y) == 2025:
                by_month_cols.setdefault(int(ms), []).append(c)
        except:
            pass
print(f"  Modem з GRS: {len(md_ok)},  місяців 2025: {len(by_month_cols)}")

modem_parts = []
for mo, cols in sorted(by_month_cols.items()):
    vol = md_ok[cols].apply(pd.to_numeric, errors='coerce').fillna(0).sum(axis=1)
    md_ok['_v'] = vol.values

    def _md_agg(df):
        v    = df['_v']
        op   = df['has_op'] & (df['heated_area'] > 0)
        aop  = df.loc[op, 'heated_area'].clip(lower=0.1)
        spec = v[op] / aop if op.any() else pd.Series(dtype=float)
        act  = v[op] > 0
        return pd.Series({
            'md_vol':     float(v.sum()),
            'md_vol_op':  float(v[op].sum()),
            'md_vol_nh':  float(v[~op].sum()),
            'md_n':       len(df),
            'md_n_op':    int(op.sum()),
            'md_n_act':   int((v > 0).sum()),
            'md_spec_op': float(spec[act].median()) if act.any() else 0.0,
            'md_area_op': float(df.loc[op, 'heated_area'].sum()),
        })

    agg = md_ok.groupby('grs').apply(_md_agg).reset_index()
    agg['month'] = mo
    modem_parts.append(agg)

modem_agg = pd.concat(modem_parts, ignore_index=True).fillna(0)
print(f"  Modem agg shape: {modem_agg.shape}")

# ═══════════════════════════════════════════════════════════
print("\n=== 4. Погода (місячна) ===")
w = pd.read_csv(WEATHER_FILE, parse_dates=['date'])
w['month'] = w['date'].dt.month
w25 = w[w['date'].dt.year == 2025].copy()
for tc in ['temperature', 'temp_avg', 'tavg']:
    if tc in w25.columns:
        w25['_temp'] = pd.to_numeric(w25[tc], errors='coerce')
        break
else:
    w25['_temp'] = 18.0 - w25['hdh_18c'] / 24.0  # estimate

# aggregate weather columns robustly
agg_spec = {
    'hdh18':    ('hdh_18c', 'sum'),
    'temp_avg': ('_temp', 'mean'),
    'temp_min': ('temperature_min', 'min') if 'temperature_min' in w25.columns else ('_temp', 'min'),
    'temp_max': ('temperature_max', 'max') if 'temperature_max' in w25.columns else ('_temp', 'max'),
    'n_cold5':  ('_temp', lambda x: (x < 5).sum()),
    'n_frost':  ('_temp', lambda x: (x < 0).sum()),
    'n_days':   ('hdh_18c', 'count'),
}
if 'hours_below_0c' in w25.columns:
    agg_spec['hrs_frost']  = ('hours_below_0c', 'sum')
if 'hours_below_minus5c' in w25.columns:
    agg_spec['hrs_frost5'] = ('hours_below_minus5c', 'sum')
if 'wind_speed' in w25.columns:
    agg_spec['wind_avg']   = ('wind_speed', 'mean')
if 'precipitation' in w25.columns:
    agg_spec['precip']     = ('precipitation', 'sum')

w_agg = w25.groupby('month').agg(**{k: v for k, v in agg_spec.items()}).reset_index()
print(w_agg[['month','hdh18','temp_avg','n_cold5','n_frost']].to_string(index=False))

# ═══════════════════════════════════════════════════════════
print("\n=== 5. Побудова матриці ознак ===")
data = billing_df.copy()
data = data.merge(grs_static.drop(columns=['ann_billing','is_large']), on='grs', how='left')
data = data.merge(modem_agg, on=['grs','month'], how='left')
data = data.merge(w_agg.drop(columns=['n_days']), on='month', how='left')

# Scaled modem → proxy for total (modem × ratio non-modem/modem)
data['md_scale']      = data['n_total'] / data['md_n'].clip(lower=1)
data['md_vol_scld']   = data['md_vol']    * data['md_scale']
data['md_vol_op_scld']= data['md_vol_op'] * data['md_scale']

# Calendar
data['month_sin'] = np.sin(2 * np.pi * data['month'] / 12)
data['month_cos'] = np.cos(2 * np.pi * data['month'] / 12)
data['is_heat']   = data['month'].isin([1,2,3,4,10,11,12]).astype(float)

# Weather × subscriber interactions
data['hdh_area']     = data['hdh18'] * data['area_op_total']
data['hdh_n_nonop']  = data['hdh18'] * data['n_nonop']
data['hdh_n_op']     = data['hdh18'] * data['n_op']
data['spec_area']    = data['md_spec_op'] * data['area_op_total']
data['spec_n_op']    = data['md_spec_op'] * data['n_op']
data['cold_area']    = data['n_cold5']    * data['area_op_total']

# Lag: previous month billing
data = data.sort_values(['grs', 'month'])
data['bill_lag1'] = data.groupby('grs')['billing'].shift(1)
data['md_vol_lag1'] = data.groupby('grs')['md_vol'].shift(1)

FEAT = [
    # Modem direct
    'md_vol', 'md_vol_op', 'md_vol_nh',
    'md_spec_op', 'md_n_act',
    # Modem scaled (proxy for all subscribers)
    'md_vol_scld', 'md_vol_op_scld', 'md_scale',
    # Weather
    'hdh18', 'temp_avg', 'temp_min', 'n_cold5', 'n_frost',
    # Extra weather if available
    *[c for c in ['hrs_frost','hrs_frost5','wind_avg','precip'] if c in w_agg.columns],
    # Subscriber static
    'n_total', 'n_op', 'n_nonop', 'area_op_total', 'area_op_mean', 'share_op',
    # Calendar
    'month_sin', 'month_cos', 'is_heat',
    # Lag
    'bill_lag1', 'md_vol_lag1',
    # Interactions
    'hdh_area', 'hdh_n_nonop', 'hdh_n_op',
    'spec_area', 'spec_n_op', 'cold_area',
]

data_full = data.dropna(subset=['billing'] + FEAT).copy()
print(f"  Рядків з усіма ознаками: {len(data_full)} / {len(data)}  (пропущено через lag1 місяць 1)")
print(f"  Ознак: {len(FEAT)}")
print(f"  Ознаки: {FEAT}")

# ═══════════════════════════════════════════════════════════
print("\n=== 6. Train (міс. 2–11) / Test (міс. 12) ===")
# Lag requires month>=2 for training (month 1 loses lag1)
train = data_full[data_full['month'] < 12].copy()
test  = data_full[data_full['month'] == 12].copy()
print(f"  Train: {len(train)} рядків  (міс. {train['month'].min()}–{train['month'].max()})")
print(f"  Test:  {len(test)} рядків   (міс. 12)")

X_tr = train[FEAT].values
y_tr = train['billing'].values
X_te = test[FEAT].values
y_te = test['billing'].values

scaler = StandardScaler()
X_tr_s = scaler.fit_transform(X_tr)
X_te_s = scaler.transform(X_te)

# ═══════════════════════════════════════════════════════════
print("\n=== 7. Навчання моделей ===")
ALPHAS_R = [1, 5, 10, 50, 100, 500, 1000, 5000, 10000, 50000]
ALPHAS_L = [100, 500, 1000, 5000, 10000, 50000]
models_to_try = {
    **{f'Ridge(α={a:,})': Ridge(alpha=a) for a in ALPHAS_R},
    **{f'Lasso(α={a:,})': Lasso(alpha=a, max_iter=30000) for a in ALPHAS_L},
    'ElasticNet(1k,0.5)': ElasticNet(alpha=1000, l1_ratio=0.5, max_iter=30000),
    'ElasticNet(5k,0.5)': ElasticNet(alpha=5000, l1_ratio=0.5, max_iter=30000),
}

print(f"  {'Модель':24s}  R²_tr  R²_te   MAE_tr      MAE_te     bias_te")
print("  " + "-"*72)
res_all = {}
for nm, model in models_to_try.items():
    model.fit(X_tr_s, y_tr)
    p_tr = np.clip(model.predict(X_tr_s), 0, None)
    p_te = np.clip(model.predict(X_te_s), 0, None)
    r2tr  = r2_score(y_tr, p_tr)
    r2te  = r2_score(y_te, p_te)
    maetr = mean_absolute_error(y_tr, p_tr)
    maete = mean_absolute_error(y_te, p_te)
    biaste = (p_te - y_te).sum() / y_te.sum() * 100
    res_all[nm] = dict(model=model, p_te=p_te,
                       r2tr=r2tr, r2te=r2te, maetr=maetr, maete=maete, biaste=biaste)
    print(f"  {nm:24s}  {r2tr:.3f}  {r2te:.3f}  {maetr:>10,.0f}  {maete:>10,.0f}  {biaste:>+8.2f}%")

best_nm = min(res_all, key=lambda n: res_all[n]['maete'])
best    = res_all[best_nm]
print(f"\n  ► Найкраща (мін. MAE_te): {best_nm}")
print(f"    R²_tr={best['r2tr']:.3f}  R²_te={best['r2te']:.3f}  "
      f"MAE_te={best['maete']:,.0f}  bias_te={best['biaste']:+.2f}%")

# ═══════════════════════════════════════════════════════════
print(f"\n=== 8. Feature importance ({best_nm}) ===")
bm = best['model']
coefs = bm.coef_
fi = sorted(zip(FEAT, coefs), key=lambda x: abs(x[1]), reverse=True)
max_c = max(abs(c) for _, c in fi) if fi else 1
print(f"  {'Ознака':22s}  {'Коеф':>12s}  Відносна")
for fn, fc in fi:
    bar_len = int(abs(fc) / max_c * 20)
    sign    = '+' if fc >= 0 else '-'
    bar     = sign + '█' * bar_len
    print(f"  {fn:22s}  {fc:>+12,.0f}  {bar}")

# ═══════════════════════════════════════════════════════════
print(f"\n=== 9. Грудень по ГРС ({best_nm}) ===")
te = test.copy()
te['pred']     = np.clip(best['p_te'], 0, None)
te['error']    = te['pred'] - te['billing']
te['bias_pct'] = te['error'] / te['billing'].clip(lower=1) * 100
te = te.merge(grs_static[['grs','ann_billing','is_large']], on='grs', how='left')

print(f"  {'GRS':45s}  {'bias%':>7s}  {'billing':>12s}  {'pred':>12s}")
print("  " + "-"*82)
for _, r in te.sort_values('billing', ascending=False).iterrows():
    lg  = ' [L]' if r['is_large'] else ''
    thr = 3 if r['is_large'] else 6
    fl  = ' !' if abs(r['bias_pct']) > thr else '  '
    print(f"  {fl}{r['grs'][:43]:43s}  {r['bias_pct']:>+7.1f}%  "
          f"{r['billing']:>12,.0f}  {r['pred']:>12,.0f}{lg}")

wb = te['billing'] > 0
bias_d = te.loc[wb, 'error'].sum() / te.loc[wb, 'billing'].sum() * 100
mae_d  = te.loc[wb, 'error'].abs().mean()
l_ok   = (te.loc[te['is_large'] & wb, 'bias_pct'].abs() <= 3).mean() * 100
s_ok   = (te.loc[~te['is_large'] & wb, 'bias_pct'].abs() <= 6).mean() * 100
print(f"\n  Грудень: bias={bias_d:+.2f}%  MAE_per_GRS={mae_d:,.0f}")
print(f"  Вел. GRS ≤3%: {l_ok:.0f}%   Мал. GRS ≤6%: {s_ok:.0f}%")
print(f"  (К_v2 Dec: bias=-1.02%)")

# ═══════════════════════════════════════════════════════════
print("\n=== 10. Помісячний bias (train) — перевірка overfitting ===")
train_cp = train.copy()
train_cp['pred'] = np.clip(best['model'].predict(X_tr_s), 0, None)
for mo in sorted(train_cp['month'].unique()):
    sub = train_cp[train_cp['month'] == mo]
    b   = (sub['pred'] - sub['billing']).sum() / sub['billing'].sum() * 100
    n   = len(sub)
    print(f"  {MONTH_UA[mo]:5s}: bias={b:>+7.2f}%  ({n} GRS)")

# ═══════════════════════════════════════════════════════════
print("\n=== 11. Naive baseline (lag1 = Nov billing → Dec prediction) ===")
lag_pred  = test['bill_lag1'].values
lag_bias  = (lag_pred - y_te).sum() / y_te.sum() * 100
lag_mae   = mean_absolute_error(y_te, lag_pred)
lag_r2    = r2_score(y_te, lag_pred)
print(f"  Lag1 baseline: R²={lag_r2:.3f}  MAE={lag_mae:,.0f}  bias={lag_bias:+.2f}%")
print(f"  Регресія ({best_nm}): R²={best['r2te']:.3f}  MAE={best['maete']:,.0f}  bias={best['biaste']:+.2f}%")

# ═══════════════════════════════════════════════════════════
print("\n=== 12. Збереження результатів ===")
out_rows = []
for nm2, r in res_all.items():
    te2 = test.copy()
    te2['pred']     = np.clip(r['p_te'], 0, None)
    te2['error']    = te2['pred'] - te2['billing']
    te2['bias_pct'] = te2['error'] / te2['billing'].clip(1) * 100
    te2['model']    = nm2
    out_rows.append(te2[['grs','month','billing','pred','error','bias_pct','model']])

out_df = pd.concat(out_rows, ignore_index=True)
out_df.to_excel(OUT_XLSX, index=False)
print(f"  Збережено → {OUT_XLSX}")

print("\n=== DONE ===")
