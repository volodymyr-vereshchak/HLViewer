"""
Real-time daily calibration — всі групи (ОП і без ОП), всі місяці.

Логіка по групах:
  ОП, опалювальний місяць:
      op_m2(grp, day) = [Σ actual_ref - Σ sb_day_ref] / Σ area_ref
      pred(c, day)    = max(0, op_m2 × area) + sb_day(c)

  ОП, літній місяць + не-ОП:
      daily_ref_mean(grp, day) = Σ actual_ref / n_ref
      pred(c, day) = daily_ref_mean × scale(c)
      де scale(c) = personal_median(c) / group_median

Порівняння з OLD (статична регресія для ОП, групова медіана для не-ОП).
"""
import sys, io, os, calendar
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(r"D:\Projects\HLViewer\HLViewer\backend\pobut")
import numpy as np, pandas as pd
from sklearn.linear_model import LinearRegression

HEATING_MONTHS = {1, 2, 3, 4, 10, 11, 12}
RANDOM_STATE   = 42
MIN_REF        = 5   # мінімум референсів для (group, date)

# ── Завантаження ──────────────────────────────────────────────────────────────
raw = pd.read_csv("data/input/profile_pobut_daily_result.csv",
                  sep=';', low_memory=False, encoding='utf-8')
META = ['account_id','gas_off','alternative','dacha','appliance_group','consumer_type',
        'profile_no','heated_area','total_area','residents','residents_all','serial_no']
raw.columns = META + list(raw.columns[12:])
date_cols = raw.columns[12:].tolist()

raw['account_id']      = pd.to_numeric(raw['account_id'], errors='coerce')
raw['appliance_group'] = raw['appliance_group'].astype(str).str.strip()
raw['heated_area']     = pd.to_numeric(raw['heated_area'].astype(str).str.strip(), errors='coerce').fillna(55.0)
raw['residents']       = pd.to_numeric(raw['residents'], errors='coerce').fillna(2.0)
raw['heated_area']     = raw['heated_area'].where(raw['heated_area'] > 0, 55.0)
raw['residents']       = raw['residents'].where(raw['residents'] > 0, 2.0)
raw['has_OP']          = raw['appliance_group'].str.contains('ОП', na=False)
raw['is_mkd']          = (raw['consumer_type'] == 'Багатоквартирний сектор').astype(float)

# ── Об'єднання ОП,ВПГ → ОП,ПГ,ВПГ ───────────────────────────────────────────
GROUP_MERGE = {'ОП,ВПГ': 'ОП,ПГ,ВПГ'}
raw['appliance_group'] = raw['appliance_group'].replace(GROUP_MERGE)

# ── Long format (всі дати) ────────────────────────────────────────────────────
long = raw.melt(
    id_vars=['account_id','appliance_group','is_mkd','heated_area','residents','has_OP'],
    value_vars=date_cols, var_name='date_str', value_name='consumption')
long['date']        = pd.to_datetime(long['date_str'], format='%d.%m.%Y', errors='coerce')
long['consumption'] = pd.to_numeric(long['consumption'], errors='coerce')
long = long.dropna(subset=['date', 'consumption']).copy()
long['year']  = long['date'].dt.year
long['month'] = long['date'].dt.month

# ── Monthly ───────────────────────────────────────────────────────────────────
monthly = (long
    .groupby(['account_id','appliance_group','is_mkd','heated_area','residents','has_OP','year','month'])
    .agg(cons_sum=('consumption','sum'), n_days=('date','count'))
    .reset_index())
monthly = monthly[monthly['n_days'] >= 20].copy()
monthly['days_in_m'] = monthly.apply(
    lambda r: calendar.monthrange(int(r['year']), int(r['month']))[1], axis=1)
monthly['consumption'] = monthly['cons_sum'] / monthly['n_days'] * monthly['days_in_m']

zero_share = (monthly
    .groupby('account_id', group_keys=False)
    .apply(lambda x: pd.Series({'zero_share': (x['cons_sum'] <= 0).mean()}),
           include_groups=False)['zero_share'])
monthly = monthly.join(zero_share, on='account_id')
monthly = monthly[monthly['zero_share'] < 0.5].copy()
q99 = monthly.groupby(['appliance_group','month'])['consumption'].transform('quantile', 0.99)
monthly = monthly[(monthly['consumption'] > 0) & (monthly['consumption'] <= q99)].copy()
monthly['is_heating'] = monthly['month'].isin(HEATING_MONTHS)

# ── 80/20 стратифікований split по групах ─────────────────────────────────────
np.random.seed(RANDOM_STATE)
ref_ids, test_ids = set(), set()
for grp, grp_df in monthly.groupby('appliance_group'):
    consumers = grp_df['account_id'].unique()
    mask = np.random.rand(len(consumers)) < 0.20
    test_ids.update(consumers[mask])
    ref_ids.update(consumers[~mask])

print(f"Референсні: {len(ref_ids)}  |  Тест: {len(test_ids)}")
for grp in sorted(monthly['appliance_group'].unique()):
    n_ref  = monthly[(monthly['account_id'].isin(ref_ids))  & (monthly['appliance_group']==grp)]['account_id'].nunique()
    n_test = monthly[(monthly['account_id'].isin(test_ids)) & (monthly['appliance_group']==grp)]['account_id'].nunique()
    print(f"  {grp:<14}: ref={n_ref:4d}  test={n_test:4d}")

train  = monthly[monthly['account_id'].isin(ref_ids)].copy()
test_m = monthly[monthly['account_id'].isin(test_ids)].copy()

op_groups = [g for g in monthly['appliance_group'].unique() if 'ОП' in g]
OP_FB     = {'ОП,ПГ':'ОП,ПГ', 'ОП,ПГ,ВПГ':'ОП,ПГ,ВПГ', 'ОП,ВПГ':'ОП,ПГ,ВПГ', 'ОП':'ОП,ПГ'}

# ── Особисті baseline (sb) для ОП ────────────────────────────────────────────
summer_op  = monthly[monthly['month'].isin([6,7,8]) & monthly['has_OP']]
consumer_sb = summer_op.groupby('account_id')['consumption'].median().to_dict()
group_sb_op = summer_op.groupby('appliance_group')['consumption'].median().to_dict()

sr_df  = pd.read_csv('data/profiles/pg_seasonal_ratios.csv')
pg_sr  = {(r['group'], int(r['month'])): r['seasonal_ratio'] for _, r in sr_df.iterrows()}
OP_TO_SR = {'ОП,ПГ':'ПГ', 'ОП,ВПГ':'ПГ,ВПГ', 'ОП,ПГ,ВПГ':'ПГ,ВПГ', 'ОП':'ПГ'}

def get_sr(grp, m):
    return pg_sr.get((OP_TO_SR.get(grp, 'ПГ'), m), 1.0)

def get_sb(aid, grp):
    return consumer_sb.get(aid, group_sb_op.get(grp, 0.0))

# ── Особисті медіани для не-ОП (scale factor) ─────────────────────────────────
consumer_med = monthly.groupby('account_id')['consumption'].median().to_dict()
group_med    = monthly.groupby('appliance_group')['consumption'].median().to_dict()

# ── OLD: статична модель для ОП (для порівняння) ──────────────────────────────
op_coefs_old = {}
for grp in op_groups:
    for m in HEATING_MONTHS:
        gd = train[(train['appliance_group']==grp) & (train['month']==m) & train['is_heating']]
        gd = gd.dropna(subset=['consumption'])
        gd = gd[(gd['heated_area']>0) & (gd['residents']>0)]
        if len(gd) < 10: continue
        lr = LinearRegression(fit_intercept=False)
        lr.fit(gd[['heated_area','residents']].values, gd['consumption'].values)
        op_coefs_old[(grp,m)] = (max(0.0, lr.coef_[0]), max(0.0, lr.coef_[1]))

# OLD для не-ОП: групова медіана по (group, month)
group_month_med = (train.groupby(['appliance_group','month'])['consumption'].median().to_dict())

# ── Денні дані: всі споживачі ─────────────────────────────────────────────────
daily_all = long.copy()
daily_all['days_in_m'] = daily_all.apply(
    lambda r: calendar.monthrange(int(r['year']), int(r['month']))[1], axis=1)
daily_all['sr']     = daily_all.apply(lambda r: get_sr(r['appliance_group'], int(r['month'])), axis=1)
daily_all['sb_m']   = daily_all.apply(lambda r: get_sb(r['account_id'], r['appliance_group']), axis=1)
daily_all['sb_day'] = daily_all['sb_m'] * daily_all['sr'] / daily_all['days_in_m']

ref_daily  = daily_all[daily_all['account_id'].isin(ref_ids)].copy()
test_daily = daily_all[daily_all['account_id'].isin(test_ids)].copy()

# ── RT профілі з референсних моdemів ─────────────────────────────────────────

# 1. ОП, опалювальні місяці: op_m2(grp, date) = Σheat / Σarea
ref_op_heat = ref_daily[ref_daily['has_OP'] & ref_daily['month'].isin(HEATING_MONTHS)].copy()
ref_op_heat['heat_day'] = ref_op_heat['consumption'] - ref_op_heat['sb_day']

op_m2_map = (ref_op_heat[ref_op_heat['heated_area'] > 0]
    .groupby(['appliance_group','date'])
    .apply(lambda g: pd.Series({
        'op_m2': g['heat_day'].sum() / g['heated_area'].sum(),
        'n':     len(g)
    }), include_groups=False)
    .reset_index())
op_m2_map = op_m2_map[op_m2_map['n'] >= MIN_REF]
op_m2_lkp = op_m2_map.set_index(['appliance_group','date'])['op_m2'].to_dict()

# Fallback по (fb_group, date)
def get_op_m2(grp, date):
    v = op_m2_lkp.get((grp, date))
    if v is not None: return v
    fb = OP_FB.get(grp, 'ОП,ПГ,ВПГ')
    return op_m2_lkp.get((fb, date), np.nan)

# 2. ОП-літо + не-ОП: нормалізований rate = Σ actual_ref / Σ personal_baseline_ref
# personal_baseline:
#   ОП            → personal_sb (літній baseline)
#   не-ОП         → consumer_med (річна медіана)
def personal_baseline(aid, grp):
    if 'ОП' in grp:
        return get_sb(aid, grp)
    return consumer_med.get(aid, group_med.get(grp, 1.0))

ref_daily['pbl'] = ref_daily.apply(
    lambda r: personal_baseline(r['account_id'], r['appliance_group']), axis=1)

# Для ОП-опалення вже є op_m2; для решти рахуємо rate
ref_nonheat = ref_daily[~(ref_daily['has_OP'] & ref_daily['month'].isin(HEATING_MONTHS))].copy()

rate_map = (ref_nonheat[ref_nonheat['pbl'] > 0]
    .groupby(['appliance_group','date'])
    .apply(lambda g: pd.Series({
        'rate': g['consumption'].sum() / g['pbl'].sum(),
        'n':    len(g)
    }), include_groups=False)
    .reset_index())
rate_map = rate_map[rate_map['n'] >= MIN_REF]
rate_lkp = rate_map.set_index(['appliance_group','date'])['rate'].to_dict()

def get_rate(grp, date):
    return rate_lkp.get((grp, date), np.nan)

print(f"\nRT профілів op_m2(grp,date):    {len(op_m2_lkp)}")
print(f"RT профілів rate(grp,date):     {len(rate_lkp)}")

# ── Денний предикт для тестових споживачів ───────────────────────────────────
test_daily['pbl'] = test_daily.apply(
    lambda r: personal_baseline(r['account_id'], r['appliance_group']), axis=1)

def predict_day(row):
    grp  = row['appliance_group']
    date = row['date']
    m    = int(row['month'])

    # ОП + опалювальний місяць: area-based op_m2
    if row['has_OP'] and m in HEATING_MONTHS:
        om2 = get_op_m2(grp, date)
        if not np.isnan(om2):
            return max(0.0, om2 * row['heated_area']) + row['sb_day']
        return np.nan

    # ОП-літо + не-ОП: rate × personal_baseline
    rate = get_rate(grp, date)
    if np.isnan(rate):
        return np.nan
    pbl = row['pbl']
    if pbl <= 0:
        return np.nan
    return rate * pbl

print("\nОбчислення денних предиктів...")
test_daily['pred_rt_day'] = test_daily.apply(predict_day, axis=1)
test_daily = test_daily.dropna(subset=['pred_rt_day']).copy()

# ── OLD місячний предикт ──────────────────────────────────────────────────────
def old_monthly(row):
    grp = row['appliance_group']
    m   = int(row['month'])
    if 'ОП' in grp and m in HEATING_MONTHS:
        fb  = OP_FB.get(grp, 'ОП,ПГ,ВПГ')
        key = (grp, m) if (grp, m) in op_coefs_old else (fb, m)
        if key in op_coefs_old:
            ca, cr = op_coefs_old[key]
            return max(0.0, ca*row['heated_area'] + cr*row['residents'])
        return np.nan
    # не-ОП і ОП-літо: групова медіана по місяцю
    return group_month_med.get((grp, m), np.nan)

test_m['pred_old'] = test_m.apply(old_monthly, axis=1)

# ── Акумуляція: денний RT → місячний ─────────────────────────────────────────
monthly_rt = (test_daily
    .groupby(['account_id','appliance_group','year','month'])
    .agg(actual_m=('consumption','sum'), pred_rt_m=('pred_rt_day','sum'), n_days=('consumption','count'))
    .reset_index())

monthly_rt = monthly_rt.merge(
    test_m[['account_id','year','month','consumption','pred_old']].rename(columns={'consumption':'actual_check'}),
    on=['account_id','year','month'], how='inner')
monthly_rt = monthly_rt.dropna(subset=['pred_old','pred_rt_m']).copy()

# ── Метрики ───────────────────────────────────────────────────────────────────
def r2(a, p):
    ss_res = np.sum((a - p)**2)
    ss_tot = np.sum((a - a.mean())**2)
    return 1 - ss_res / max(ss_tot, 1e-9)

def bias_pct(a, p):
    return (p.sum() / a.sum() - 1) * 100

print("\n" + "=" * 72)
print("ЗАГАЛЬНИЙ АГРЕГАТ ПО МІСЯЦЯХ — всі групи")
print("=" * 72)
print(f"  {'Місяць':<9} {'Actual':>10}  {'RT':>9}{'%':>6}  {'OLD':>9}{'%':>6}  {'n':>5}")
print("  " + "-"*60)

rows_all = []
for (y, m), sub in monthly_rt.groupby(['year','month']):
    a_ = sub['actual_m'].values
    pr = sub['pred_rt_m'].values
    po = sub['pred_old'].values
    rows_all.append((y, m, a_.sum(), pr.sum(), po.sum()))
    print(f"  {y}-{m:02d}   {a_.sum():>10.0f}"
          f"  {pr.sum():>9.0f}{bias_pct(a_, pr):>+5.1f}%"
          f"  {po.sum():>9.0f}{bias_pct(a_, po):>+5.1f}%"
          f"  {len(sub):>5}")

print("  " + "-"*60)
acts = np.array([r[2] for r in rows_all])
rts  = np.array([r[3] for r in rows_all])
olds = np.array([r[4] for r in rows_all])
print(f"  {'РАЗОМ':<9} {acts.sum():>10.0f}"
      f"  {rts.sum():>9.0f}{bias_pct(acts, rts):>+5.1f}%"
      f"  {olds.sum():>9.0f}{bias_pct(acts, olds):>+5.1f}%")
print()
print(f"  Agg R²:   RT={r2(acts,rts):.4f}   OLD={r2(acts,olds):.4f}")
a_all = monthly_rt['actual_m'].values
print(f"  R² (per-consumer):  RT={r2(a_all, monthly_rt['pred_rt_m'].values):.4f}"
      f"   OLD={r2(a_all, monthly_rt['pred_old'].values):.4f}")

# ── По групах ─────────────────────────────────────────────────────────────────
print()
print("По групах (всього за тестовий період):")
print(f"  {'Група':<14} {'Actual':>10}  {'RT':>9}{'%':>6}  {'OLD':>9}{'%':>6}  {'n_cons':>7}")
print("  " + "-"*62)
for grp in sorted(monthly_rt['appliance_group'].unique()):
    s  = monthly_rt[monthly_rt['appliance_group']==grp]
    a_ = s['actual_m'].values
    pr = s['pred_rt_m'].values
    po = s['pred_old'].values
    nc = s['account_id'].nunique()
    print(f"  {grp:<14} {a_.sum():>10.0f}"
          f"  {pr.sum():>9.0f}{bias_pct(a_, pr):>+5.1f}%"
          f"  {po.sum():>9.0f}{bias_pct(a_, po):>+5.1f}%"
          f"  {nc:>7}")

# ── Окремо: ОП по місяцях ─────────────────────────────────────────────────────
print()
print("ОП-групи, опалювальні місяці:")
print(f"  {'Місяць':<9} {'Actual':>10}  {'RT':>9}{'%':>6}  {'OLD':>9}{'%':>6}")
print("  " + "-"*52)
op_rows = []
for (y, m), sub in monthly_rt[
        monthly_rt['appliance_group'].isin(op_groups) &
        monthly_rt['month'].isin(HEATING_MONTHS)].groupby(['year','month']):
    a_ = sub['actual_m'].values
    pr = sub['pred_rt_m'].values
    po = sub['pred_old'].values
    op_rows.append((y, m, a_.sum(), pr.sum(), po.sum()))
    print(f"  {y}-{m:02d}   {a_.sum():>10.0f}"
          f"  {pr.sum():>9.0f}{bias_pct(a_, pr):>+5.1f}%"
          f"  {po.sum():>9.0f}{bias_pct(a_, po):>+5.1f}%")

if op_rows:
    a2 = np.array([r[2] for r in op_rows])
    r2_ = np.array([r[3] for r in op_rows])
    o2 = np.array([r[4] for r in op_rows])
    print("  " + "-"*52)
    print(f"  {'РАЗОМ':<9} {a2.sum():>10.0f}"
          f"  {r2_.sum():>9.0f}{bias_pct(a2, r2_):>+5.1f}%"
          f"  {o2.sum():>9.0f}{bias_pct(a2, o2):>+5.1f}%")
    print(f"  Agg R²: RT={r2(a2,r2_):.4f}  OLD={r2(a2,o2):.4f}")

# ── Детально: ПГ і ПГ,ВПГ по місяцях ────────────────────────────────────────
for non_op_grp in ['ПГ', 'ПГ,ВПГ']:
    sub_grp = monthly_rt[monthly_rt['appliance_group'] == non_op_grp]
    if len(sub_grp) == 0:
        continue
    n_cons = sub_grp['account_id'].nunique()
    print()
    print(f"{non_op_grp} — {n_cons} споживачів тесту, всі місяці:")
    print(f"  {'Місяць':<9} {'Actual':>9}  {'RT':>8}{'%':>6}  {'OLD':>8}{'%':>6}  {'n':>4}")
    print("  " + "-"*50)
    grp_rows = []
    for (y, m), s in sub_grp.groupby(['year', 'month']):
        a_ = s['actual_m'].values
        pr = s['pred_rt_m'].values
        po = s['pred_old'].values
        grp_rows.append((y, m, a_.sum(), pr.sum(), po.sum()))
        print(f"  {y}-{m:02d}   {a_.sum():>9.0f}"
              f"  {pr.sum():>8.0f}{bias_pct(a_, pr):>+5.1f}%"
              f"  {po.sum():>8.0f}{bias_pct(a_, po):>+5.1f}%"
              f"  {len(s):>4}")
    print("  " + "-"*50)
    ga = np.array([r[2] for r in grp_rows])
    gr = np.array([r[3] for r in grp_rows])
    go = np.array([r[4] for r in grp_rows])
    print(f"  {'РАЗОМ':<9} {ga.sum():>9.0f}"
          f"  {gr.sum():>8.0f}{bias_pct(ga, gr):>+5.1f}%"
          f"  {go.sum():>8.0f}{bias_pct(ga, go):>+5.1f}%")
    print(f"  Agg R²: RT={r2(ga, gr):.4f}  OLD={r2(ga, go):.4f}")
    a_c = sub_grp['actual_m'].values
    print(f"  R² (per-consumer): RT={r2(a_c, sub_grp['pred_rt_m'].values):.4f}"
          f"  OLD={r2(a_c, sub_grp['pred_old'].values):.4f}")
