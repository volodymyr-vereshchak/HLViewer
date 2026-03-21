import sys, io, os, calendar
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(r"D:\Projects\HLViewer\HLViewer\backend\pobut")
import numpy as np, pandas as pd
from sklearn.linear_model import LinearRegression

HEATING_MONTHS = {1,2,3,4,10,11,12}
RANDOM_STATE = 42

# ── load & prep ──────────────────────────────────────────────────────────────
raw = pd.read_csv("data/input/profile_pobut_daily_result.csv", sep=';', low_memory=False, encoding='utf-8')
META = ['account_id','gas_off','alternative','dacha','appliance_group','consumer_type','profile_no',
        'heated_area','total_area','residents','residents_all','serial_no']
raw.columns = META + list(raw.columns[12:])
date_cols = raw.columns[12:].tolist()
raw['account_id'] = pd.to_numeric(raw['account_id'], errors='coerce')
raw['appliance_group'] = raw['appliance_group'].astype(str).str.strip()
raw['consumer_type']   = raw['consumer_type'].astype(str).str.strip()
raw['heated_area'] = pd.to_numeric(raw['heated_area'].astype(str).str.strip(), errors='coerce').fillna(55.0)
raw['residents']   = pd.to_numeric(raw['residents'], errors='coerce').fillna(2.0)
raw['heated_area'] = raw['heated_area'].where(raw['heated_area'] > 0, 55.0)
raw['residents']   = raw['residents'].where(raw['residents'] > 0, 2.0)
raw['has_OP'] = raw['appliance_group'].str.contains('ОП', na=False)
raw['is_mkd']  = (raw['consumer_type'] == 'Багатоквартирний сектор').astype(float)

long = raw.melt(id_vars=['account_id','appliance_group','is_mkd','heated_area','residents','has_OP'],
    value_vars=date_cols, var_name='date_str', value_name='consumption')
long['date'] = pd.to_datetime(long['date_str'], format='%d.%m.%Y', errors='coerce')
long['consumption'] = pd.to_numeric(long['consumption'], errors='coerce')
long = long.dropna(subset=['date']).copy()
long['year'] = long['date'].dt.year
long['month'] = long['date'].dt.month

monthly = (long.groupby(
    ['account_id','appliance_group','is_mkd','heated_area','residents','has_OP','year','month'])
    .agg(cons_sum=('consumption','sum'), n_days=('date','count')).reset_index())
monthly = monthly[monthly['n_days'] >= 20].copy()
monthly['days_in_m'] = monthly.apply(
    lambda r: calendar.monthrange(int(r['year']), int(r['month']))[1], axis=1)
monthly['consumption'] = monthly['cons_sum'] / monthly['n_days'] * monthly['days_in_m']

zero_share = (monthly.groupby('account_id', group_keys=False)
    .apply(lambda x: pd.Series({'zero_share': (x['cons_sum']<=0).mean()}))['zero_share'])
monthly = monthly.join(zero_share, on='account_id')
monthly = monthly[monthly['zero_share'] < 0.5].copy()
q99 = monthly.groupby(['appliance_group','month'])['consumption'].transform('quantile', 0.99)
monthly = monthly[(monthly['consumption']>0) & (monthly['consumption']<=q99)].copy()
monthly['is_heating'] = monthly['month'].isin(HEATING_MONTHS)

# ── consumer-level 80/20 split (тільки ОП) ──────────────────────────────────
op_consumers = monthly[monthly['has_OP']]['account_id'].unique()
np.random.seed(RANDOM_STATE)
test_mask = np.random.rand(len(op_consumers)) < 0.20
test_ids  = set(op_consumers[test_mask])
train_ids = set(op_consumers[~test_mask])

train = monthly[monthly['account_id'].isin(train_ids)].copy()
test  = monthly[monthly['account_id'].isin(test_ids) & monthly['has_OP']].copy()
print(f"Train: {len(train_ids)} ОП споживачів  |  Test: {len(test_ids)} ОП споживачів")

op_groups = train['appliance_group'].unique()
OP_FB = {'ОП,ПГ':'ОП,ПГ', 'ОП,ПГ,ВПГ':'ОП,ПГ,ВПГ', 'ОП,ВПГ':'ОП,ПГ,ВПГ', 'ОП':'ОП,ПГ'}

# ── OLD: coef_area*area + coef_res*residents ─────────────────────────────────
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

# ── NEW: op_per_m2 * area + personal_summer_baseline ────────────────────────
summer_all = monthly[monthly['month'].isin([6,7,8]) & monthly['has_OP']].copy()
consumer_sb = summer_all.groupby('account_id')['consumption'].median().to_dict()
group_sb = {}
for grp in op_groups:
    vals = summer_all[summer_all['appliance_group']==grp]['consumption']
    if len(vals) >= 5:
        group_sb[grp] = vals.median()

train_w = train[train['is_heating']].copy()
train_w['sb'] = train_w.apply(
    lambda r: consumer_sb.get(r['account_id'], group_sb.get(r['appliance_group'], 0.0)), axis=1)
train_w['op_heat'] = np.maximum(0.0, train_w['consumption'] - train_w['sb'])
train_w['op_m2']   = np.where(train_w['heated_area'] > 0,
                               train_w['op_heat'] / train_w['heated_area'], np.nan)

op_m2_profile = {}
for grp in op_groups:
    for m in HEATING_MONTHS:
        gd = train_w[(train_w['appliance_group']==grp) & (train_w['month']==m)].dropna(subset=['op_m2'])
        if len(gd) >= 10:
            op_m2_profile[(grp, m)] = gd['op_m2'].median()

# ── MODEL D: op_per_m2 з чистим ПГ/ВПГ baseline (медіана чистих груп) ───────
# Базелайн = медіана споживання чистих ПГ або ПГ,ВПГ груп за той самий місяць
# (вони не мають опалення → чисте ПГ+ВПГ споживання, включаючи сезонність)
all_monthly = monthly.copy()  # всі споживачі для медіани ПГ/ВПГ
pg_monthly_med  = {}  # month -> median ПГ
pgvpg_monthly_med = {}  # month -> median ПГ,ВПГ
for m in range(1, 13):
    pg_d = all_monthly[(all_monthly['appliance_group']=='ПГ') & (all_monthly['month']==m)]
    if len(pg_d) >= 5:
        pg_monthly_med[m] = pg_d['consumption'].median()
    pgvpg_d = all_monthly[(all_monthly['appliance_group']=='ПГ,ВПГ') & (all_monthly['month']==m)]
    if len(pgvpg_d) >= 5:
        pgvpg_monthly_med[m] = pgvpg_d['consumption'].median()

# Маппінг: яку базелайн брати для кожної ОП-групи
def get_clean_baseline(grp, m):
    if 'ВПГ' in grp:
        return pgvpg_monthly_med.get(m, pg_monthly_med.get(m, 14.0))
    else:
        return pg_monthly_med.get(m, 8.0)

train_d = train[train['is_heating']].copy()
train_d['sb_clean'] = train_d.apply(
    lambda r: get_clean_baseline(r['appliance_group'], int(r['month'])), axis=1)
train_d['op_heat_d'] = np.maximum(0.0, train_d['consumption'] - train_d['sb_clean'])
train_d['op_m2_d']   = np.where(train_d['heated_area'] > 0,
                                  train_d['op_heat_d'] / train_d['heated_area'], np.nan)

op_m2_profile_d = {}
for grp in op_groups:
    for m in HEATING_MONTHS:
        gd = train_d[(train_d['appliance_group']==grp) & (train_d['month']==m)].dropna(subset=['op_m2_d'])
        if len(gd) >= 10:
            op_m2_profile_d[(grp, m)] = gd['op_m2_d'].median()

print(f"  op_m2_profile_d: {len(op_m2_profile_d)} (group,month) профілів")

# ── MODEL E: total_op_heating / total_area (агрегат-несміщений) ──────────────
# Замість median(op_per_m2): sum(op_heating) / sum(area) — гарантує bias≈0 на трейні
op_m2_profile_e = {}
for grp in op_groups:
    for m in HEATING_MONTHS:
        gd = train_w[(train_w['appliance_group']==grp) & (train_w['month']==m)].dropna(subset=['op_heat'])
        gd = gd[gd['heated_area'] > 0]
        if len(gd) >= 10:
            op_m2_profile_e[(grp, m)] = gd['op_heat'].sum() / gd['heated_area'].sum()

print(f"  op_m2_profile_e (sum/sum): {len(op_m2_profile_e)} профілів")

# ── MODEL G: OLD + is_mkd як бінарний признак ────────────────────────────────
# coef_area × area + coef_res × residents + coef_mkd × is_mkd
op_coefs_g = {}
for grp in op_groups:
    for m in HEATING_MONTHS:
        gd = train[(train['appliance_group']==grp) & (train['month']==m) & train['is_heating']]
        gd = gd.dropna(subset=['consumption'])
        gd = gd[(gd['heated_area']>0) & (gd['residents']>0)]
        if len(gd) < 10: continue
        lr = LinearRegression(fit_intercept=False)
        lr.fit(gd[['heated_area','residents','is_mkd']].values, gd['consumption'].values)
        op_coefs_g[(grp,m)] = (max(0.0, lr.coef_[0]), max(0.0, lr.coef_[1]), lr.coef_[2])

print(f"  op_coefs_g (OLD+ctype): {len(op_coefs_g)} профілів")

# ── MODEL H: окремі sum/sum профілі по (group, month, ctype) ─────────────────
# Fallback: якщо для ctype < MIN_N → використовуємо об'єднаний (group, month)
MIN_N_CTYPE = 8
op_m2_profile_h = {}   # key: (grp, m, is_mkd_int)  або  (grp, m)  для fallback
for grp in op_groups:
    for m in HEATING_MONTHS:
        for mkd_val in [0, 1]:
            gd = train_w[(train_w['appliance_group']==grp) &
                         (train_w['month']==m) &
                         (train_w['is_mkd']==mkd_val)].dropna(subset=['op_heat'])
            gd = gd[gd['heated_area'] > 0]
            if len(gd) >= MIN_N_CTYPE:
                op_m2_profile_h[(grp, m, mkd_val)] = gd['op_heat'].sum() / gd['heated_area'].sum()

print(f"  op_m2_profile_h (split by ctype): {len(op_m2_profile_h)} профілів")
print(f"    МКД профілів:    {sum(1 for k in op_m2_profile_h if k[2]==1)}")
print(f"    Приват профілів: {sum(1 for k in op_m2_profile_h if k[2]==0)}")

# ── MODEL F: seasonal ratio для ПГ/ВПГ ───────────────────────────────────────
# Seasonal ratio з чистих груп (ПГ, ПГ,ВПГ): ratio = median(m) / median(July)
pg_monthly_all  = monthly[monthly['appliance_group']=='ПГ'].groupby('month')['consumption'].median()
vpg_monthly_all = monthly[monthly['appliance_group']=='ПГ,ВПГ'].groupby('month')['consumption'].median()

pg_ref  = pg_monthly_all.get(7, pg_monthly_all.mean())
vpg_ref = vpg_monthly_all.get(7, vpg_monthly_all.mean())

pg_ratio  = (pg_monthly_all / pg_ref).to_dict()  if pg_ref > 0 else {}
vpg_ratio = (vpg_monthly_all / vpg_ref).to_dict() if vpg_ref > 0 else {}

def get_seasonal_ratio(grp, m):
    if 'ВПГ' in grp:
        return vpg_ratio.get(m, 1.0)
    else:
        return pg_ratio.get(m, 1.0)

print(f"  pg_ratio:  { {m: round(v,3) for m,v in sorted(pg_ratio.items())} }")
print(f"  vpg_ratio: { {m: round(v,3) for m,v in sorted(vpg_ratio.items())} }")

# ── Seasonal ratios з файлу (для MODEL I) ─────────────────────────────────────
sr_df = pd.read_csv('data/profiles/pg_seasonal_ratios.csv')
pg_sr = {(r['group'], int(r['month'])): r['seasonal_ratio']
         for _, r in sr_df.iterrows()}
OP_TO_SR = {'ОП,ПГ': 'ПГ', 'ОП,ВПГ': 'ПГ,ВПГ',
            'ОП,ПГ,ВПГ': 'ПГ,ВПГ', 'ОП': 'ПГ'}

def get_sr(grp, m):
    return pg_sr.get((OP_TO_SR.get(grp, 'ПГ'), m), 1.0)

# ── MODEL I: op_heat > 0 фільтр + seasonal ПГ/ВПГ ────────────────────────────
op_m2_profile_i = {}
for grp in op_groups:
    for m in HEATING_MONTHS:
        gd = train_w[(train_w['appliance_group']==grp) &
                     (train_w['month']==m)].dropna(subset=['op_heat'])
        gd = gd[(gd['heated_area'] > 0) & (gd['op_heat'] > 0)]  # тільки де грів
        if len(gd) >= 10:
            op_m2_profile_i[(grp, m)] = gd['op_heat'].sum() / gd['heated_area'].sum()

print(f"  op_m2_profile_i (op_heat>0 + seasonal_sr): {len(op_m2_profile_i)} профілів")

# ── MODEL I_adj: усунення подвійного рахунку ─────────────────────────────────
# op_heat_adj = cons - sb × seasonal_ratio(grp, m)  ← зимовий ПГ/ВПГ рівень
# Це математично коректна декомпозиція: тренуємо на "чистому опаленні"
train_w_adj = train_w.copy()
train_w_adj['sr'] = train_w_adj.apply(
    lambda r: get_sr(r['appliance_group'], int(r['month'])), axis=1)
train_w_adj['sb'] = train_w_adj.apply(
    lambda r: consumer_sb.get(r['account_id'], group_sb.get(r['appliance_group'], 0.0)), axis=1)
train_w_adj['op_heat_adj'] = train_w_adj['consumption'] - train_w_adj['sb'] * train_w_adj['sr']

op_m2_profile_iadj = {}
for grp in op_groups:
    for m in HEATING_MONTHS:
        gd = train_w_adj[(train_w_adj['appliance_group']==grp) &
                         (train_w_adj['month']==m)].dropna(subset=['op_heat_adj'])
        gd = gd[(gd['heated_area'] > 0) & (gd['op_heat_adj'] > 0)]  # тільки де грів
        if len(gd) >= 10:
            op_m2_profile_iadj[(grp, m)] = gd['op_heat_adj'].sum() / gd['heated_area'].sum()

print(f"  op_m2_profile_iadj (adj: cons - sb×sr): {len(op_m2_profile_iadj)} профілів")

# ── Predict ───────────────────────────────────────────────────────────────────
test_w = test[test['is_heating']].copy()
test_w['pred_old'] = np.nan
test_w['pred_new'] = np.nan
test_w['pred_d']   = np.nan
test_w['pred_e']   = np.nan
test_w['pred_f']   = np.nan
test_w['pred_g']   = np.nan
test_w['pred_h']   = np.nan
test_w['pred_i']    = np.nan
test_w['pred_iadj'] = np.nan

for idx, row in test_w.iterrows():
    grp = row['appliance_group']
    m   = int(row['month'])
    fb  = OP_FB.get(grp, 'ОП,ПГ,ВПГ')

    # OLD
    key = (grp,m) if (grp,m) in op_coefs_old else (fb,m)
    if key in op_coefs_old:
        ca, cr = op_coefs_old[key]
        test_w.at[idx, 'pred_old'] = max(0.0, ca*row['heated_area'] + cr*row['residents'])

    # NEW (особистий літній baseline)
    key2 = (grp,m) if (grp,m) in op_m2_profile else (fb,m)
    if key2 in op_m2_profile:
        sb = consumer_sb.get(row['account_id'], group_sb.get(grp, 0.0))
        test_w.at[idx, 'pred_new'] = max(0.0, op_m2_profile[key2] * row['heated_area']) + sb

    # MODEL D (чистий ПГ/ВПГ baseline по місяцю)
    key3 = (grp,m) if (grp,m) in op_m2_profile_d else (fb,m)
    if key3 in op_m2_profile_d:
        sb_d = get_clean_baseline(grp, m)
        test_w.at[idx, 'pred_d'] = max(0.0, op_m2_profile_d[key3] * row['heated_area']) + sb_d

    # MODEL E (sum/sum op_m2 + особистий літній baseline)
    key4 = (grp,m) if (grp,m) in op_m2_profile_e else (fb,m)
    if key4 in op_m2_profile_e:
        sb = consumer_sb.get(row['account_id'], group_sb.get(grp, 0.0))
        test_w.at[idx, 'pred_e'] = max(0.0, op_m2_profile_e[key4] * row['heated_area']) + sb

    # MODEL F (sum/sum op_m2 + особистий літній baseline × seasonal_ratio)
    key5 = (grp,m) if (grp,m) in op_m2_profile_e else (fb,m)
    if key5 in op_m2_profile_e:
        sb = consumer_sb.get(row['account_id'], group_sb.get(grp, 0.0))
        sr = get_seasonal_ratio(grp, m)
        test_w.at[idx, 'pred_f'] = (
            max(0.0, op_m2_profile_e[key5] * row['heated_area'])
            + max(0.0, sb * sr)
        )

    mkd = int(row['is_mkd'])
    fb_mkd = OP_FB.get(grp, 'ОП,ПГ,ВПГ')

    # MODEL G (OLD + is_mkd бінарний признак)
    key6 = (grp,m) if (grp,m) in op_coefs_g else (fb_mkd,m)
    if key6 in op_coefs_g:
        ca, cr, cm = op_coefs_g[key6]
        test_w.at[idx, 'pred_g'] = max(0.0,
            ca * row['heated_area'] + cr * row['residents'] + cm * mkd)

    # MODEL H (окремі sum/sum профілі по ctype, fallback → MODEL E)
    key7 = (grp,m,mkd) if (grp,m,mkd) in op_m2_profile_h else \
           (fb_mkd,m,mkd) if (fb_mkd,m,mkd) in op_m2_profile_h else None
    # fallback до MODEL E якщо ctype-профіль відсутній
    key7_fb = (grp,m) if (grp,m) in op_m2_profile_e else (fb_mkd,m)
    coef_h = op_m2_profile_h.get(key7) if key7 else None
    if coef_h is None:
        coef_h = op_m2_profile_e.get(key7_fb)
    if coef_h is not None:
        sb = consumer_sb.get(row['account_id'], group_sb.get(grp, 0.0))
        test_w.at[idx, 'pred_h'] = max(0.0, coef_h * row['heated_area']) + sb

    # MODEL I (op_heat>0 фільтр + seasonal ПГ/ВПГ)
    key_i = (grp, m) if (grp, m) in op_m2_profile_i else (fb, m)
    if key_i in op_m2_profile_i:
        sb = consumer_sb.get(row['account_id'], group_sb.get(grp, 0.0))
        test_w.at[idx, 'pred_i'] = (
            max(0.0, op_m2_profile_i[key_i] * row['heated_area'])
            + sb * get_sr(grp, m)
        )

    # MODEL I_adj (без подвійного рахунку: op_heat_adj = cons - sb×sr)
    key_iadj = (grp, m) if (grp, m) in op_m2_profile_iadj else (fb, m)
    if key_iadj in op_m2_profile_iadj:
        sb = consumer_sb.get(row['account_id'], group_sb.get(grp, 0.0))
        test_w.at[idx, 'pred_iadj'] = (
            max(0.0, op_m2_profile_iadj[key_iadj] * row['heated_area'])
            + sb * get_sr(grp, m)
        )

# ── Aggregate R² по місяцях ───────────────────────────────────────────────────
def r2(a, p):
    ss_res = np.sum((a-p)**2)
    ss_tot = np.sum((a-a.mean())**2)
    return 1 - ss_res / max(ss_tot, 1e-9)

def mape(a, p):
    return np.mean(np.abs((a-p)/a)) * 100

print()
print("ОП ЗИМА — АГРЕГАТ ПО МІСЯЦЯХ (20% test споживачів)  (* = найкращий)")
print(f"{'Місяць':<9} {'Actual':>9}  {'OLD':>7}{'%':>6}  {'E':>7}{'%':>6}  {'G(+ctype)':>9}{'%':>6}  {'H(split)':>9}{'%':>6}  {'I':>7}{'%':>6}  {'Iadj':>8}{'%':>6}")
print("="*120)

rows = []
for (y, m), sub in test_w.groupby(['year','month']):
    s = sub.dropna(subset=['consumption','pred_old','pred_e','pred_g','pred_h','pred_i','pred_iadj'])
    if len(s) == 0: continue
    a    = s['consumption'].sum()
    po   = s['pred_old'].sum()
    pe   = s['pred_e'].sum()
    pg   = s['pred_g'].sum()
    ph   = s['pred_h'].sum()
    pi   = s['pred_i'].sum()
    piad = s['pred_iadj'].sum()
    rows.append((y, m, a, po, pe, pg, ph, pi, piad))
    errs = [abs((po/a-1)*100), abs((pe/a-1)*100), abs((pg/a-1)*100),
            abs((ph/a-1)*100), abs((pi/a-1)*100), abs((piad/a-1)*100)]
    best = min(errs)
    marks = ["*" if abs(e-best)<1e-9 else " " for e in errs]
    print(f"{y}-{m:02d}   {a:>9.0f}"
          f"  {po:>7.0f}{(po/a-1)*100:>+5.1f}%{marks[0]}"
          f"  {pe:>7.0f}{(pe/a-1)*100:>+5.1f}%{marks[1]}"
          f"  {pg:>9.0f}{(pg/a-1)*100:>+5.1f}%{marks[2]}"
          f"  {ph:>9.0f}{(ph/a-1)*100:>+5.1f}%{marks[3]}"
          f"  {pi:>7.0f}{(pi/a-1)*100:>+5.1f}%{marks[4]}"
          f"  {piad:>8.0f}{(piad/a-1)*100:>+5.1f}%{marks[5]}")

print("="*120)
acts = np.array([r[2] for r in rows])
olds = np.array([r[3] for r in rows])
es   = np.array([r[4] for r in rows])
gs   = np.array([r[5] for r in rows])
hs   = np.array([r[6] for r in rows])
is_  = np.array([r[7] for r in rows])
iads = np.array([r[8] for r in rows])

print(f"{'РАЗОМ':<9} {acts.sum():>9.0f}"
      f"  {olds.sum():>7.0f}{(olds.sum()/acts.sum()-1)*100:>+5.1f}%"
      f"  {es.sum():>7.0f}{(es.sum()/acts.sum()-1)*100:>+5.1f}%"
      f"  {gs.sum():>9.0f}{(gs.sum()/acts.sum()-1)*100:>+5.1f}%"
      f"  {hs.sum():>9.0f}{(hs.sum()/acts.sum()-1)*100:>+5.1f}%"
      f"  {is_.sum():>7.0f}{(is_.sum()/acts.sum()-1)*100:>+5.1f}%"
      f"  {iads.sum():>8.0f}{(iads.sum()/acts.sum()-1)*100:>+5.1f}%")
print()
print(f"  Aggregate R²:  OLD={r2(acts,olds):.4f}  E={r2(acts,es):.4f}"
      f"  G={r2(acts,gs):.4f}  H={r2(acts,hs):.4f}  I={r2(acts,is_):.4f}  Iadj={r2(acts,iads):.4f}")
print(f"  Agg MAPE:      OLD={mape(acts,olds):.1f}%  E={mape(acts,es):.1f}%"
      f"  G={mape(acts,gs):.1f}%  H={mape(acts,hs):.1f}%  I={mape(acts,is_):.1f}%  Iadj={mape(acts,iads):.1f}%")
print(f"  Total bias:    OLD={(olds.sum()/acts.sum()-1)*100:+.1f}%"
      f"  E={(es.sum()/acts.sum()-1)*100:+.1f}%"
      f"  G={(gs.sum()/acts.sum()-1)*100:+.1f}%"
      f"  H={(hs.sum()/acts.sum()-1)*100:+.1f}%"
      f"  I={(is_.sum()/acts.sum()-1)*100:+.1f}%"
      f"  Iadj={(iads.sum()/acts.sum()-1)*100:+.1f}%")
print()

print("По ОП-групах:")
for grp in sorted(test_w['appliance_group'].unique()):
    s = test_w[test_w['appliance_group']==grp].dropna(
        subset=['consumption','pred_old','pred_e','pred_g','pred_h','pred_i','pred_iadj'])
    if len(s) == 0: continue
    a = s['consumption'].sum()
    print(f"  {grp:<14}: actual={a:>9.0f}"
          f"  OLD={s['pred_old'].sum():>9.0f}({(s['pred_old'].sum()/a-1)*100:>+5.1f}%)"
          f"  E={s['pred_e'].sum():>9.0f}({(s['pred_e'].sum()/a-1)*100:>+5.1f}%)"
          f"  G={s['pred_g'].sum():>9.0f}({(s['pred_g'].sum()/a-1)*100:>+5.1f}%)"
          f"  H={s['pred_h'].sum():>9.0f}({(s['pred_h'].sum()/a-1)*100:>+5.1f}%)"
          f"  I={s['pred_i'].sum():>9.0f}({(s['pred_i'].sum()/a-1)*100:>+5.1f}%)"
          f"  Iadj={s['pred_iadj'].sum():>9.0f}({(s['pred_iadj'].sum()/a-1)*100:>+5.1f}%)")

print()
print("По типу споживача (МКД vs Приват):")
for ct_val, ct_name in [(1,'МКД'), (0,'Приват')]:
    s = test_w[test_w['is_mkd']==ct_val].dropna(
        subset=['consumption','pred_old','pred_e','pred_g','pred_h','pred_i','pred_iadj'])
    if len(s) == 0: continue
    a = s['consumption'].sum()
    print(f"  {ct_name:<8}: actual={a:>9.0f}"
          f"  OLD={s['pred_old'].sum():>9.0f}({(s['pred_old'].sum()/a-1)*100:>+5.1f}%)"
          f"  E={s['pred_e'].sum():>9.0f}({(s['pred_e'].sum()/a-1)*100:>+5.1f}%)"
          f"  G={s['pred_g'].sum():>9.0f}({(s['pred_g'].sum()/a-1)*100:>+5.1f}%)"
          f"  H={s['pred_h'].sum():>9.0f}({(s['pred_h'].sum()/a-1)*100:>+5.1f}%)"
          f"  I={s['pred_i'].sum():>9.0f}({(s['pred_i'].sum()/a-1)*100:>+5.1f}%)"
          f"  Iadj={s['pred_iadj'].sum():>9.0f}({(s['pred_iadj'].sum()/a-1)*100:>+5.1f}%)")
