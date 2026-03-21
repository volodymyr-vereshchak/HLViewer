"""
Предикт всіх споживачів з RT моделлю + агрегація по ГРС × місяць.

Логіка:
  - Моdemні споживачі (2544): факт (сума щоденних зчитувань)
  - Всі інші (~219K):  RT модель (групові коефіцієнти з моdemних даних):
      ОП + опалення → area × heat_m2_sum(grp,m) + grp_sb[grp] × sr
      ОП + літо     → grp_sb[grp] × rate_sum(grp,m)
      не-ОП         → grp_med[grp] × rate_sum(grp,m)
  де grp_sb / grp_med — групові медіани з моdemних даних (monthly_long)
     heat_m2_sum / rate_sum — місячні суми денних коефіцієнтів із моdemних даних
"""
import sys, io, os, calendar
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(r"D:\Projects\HLViewer\HLViewer\backend\pobut")
import numpy as np, pandas as pd

HEATING_MONTHS = {1, 2, 3, 4, 10, 11, 12}
SUMMER_MONTHS  = {5, 6, 7, 8, 9}
SKIP_GROUPS    = {'---', '222046', 'ВПГ', 'nan'}
GROUP_MERGE    = {'ОП,ВПГ': 'ОП,ПГ,ВПГ', 'ОП': 'ОП,ПГ'}
OP_FB          = {'ОП,ПГ': 'ОП,ПГ', 'ОП,ПГ,ВПГ': 'ОП,ПГ,ВПГ'}
OP_TO_SR       = {'ОП,ПГ': 'ПГ', 'ОП,ПГ,ВПГ': 'ПГ,ВПГ'}
MIN_REF        = 5

# ── 1. Моdemні дані (2544 споживачів) ────────────────────────────────────────
print("Завантаження моdemних даних...")
raw = pd.read_csv("../data/profile_pobut_daily_result.csv",
                  sep=';', low_memory=False, encoding='utf-8')
META = ['account_id','gas_off','alternative','dacha','appliance_group','consumer_type',
        'profile_no','heated_area','total_area','residents','residents_all','serial_no']
raw.columns = META + list(raw.columns[12:])
date_cols = raw.columns[12:].tolist()

raw['account_id']      = pd.to_numeric(raw['account_id'], errors='coerce')
raw['appliance_group'] = raw['appliance_group'].astype(str).str.strip().replace(GROUP_MERGE)
raw['heated_area']     = pd.to_numeric(raw['heated_area'].astype(str).str.strip(), errors='coerce').fillna(55.0)
raw['heated_area']     = raw['heated_area'].where(raw['heated_area'] > 0, 55.0)
raw['has_OP']          = raw['appliance_group'].str.contains('ОП', na=False)
modem_ids              = set(raw['account_id'].dropna().astype(int))
print(f"  Моdemних споживачів: {len(modem_ids)}")

# Long format моdemних даних
long = raw.melt(
    id_vars=['account_id','appliance_group','heated_area','has_OP'],
    value_vars=date_cols, var_name='date_str', value_name='consumption')
long['date']        = pd.to_datetime(long['date_str'], format='%d.%m.%Y', errors='coerce')
long['consumption'] = pd.to_numeric(long['consumption'], errors='coerce')
long = long.dropna(subset=['date','consumption']).copy()
long['year']  = long['date'].dt.year
long['month'] = long['date'].dt.month

# ── 2. Групові baseline з моdemних даних (місячний рівень) ────────────────────
print("Обчислення групових baseline...")

# monthly_long: місячне споживання моdemних споживачів
long['days_in_m'] = long.apply(
    lambda r: calendar.monthrange(int(r['year']), int(r['month']))[1], axis=1)
monthly_long = (long
    .groupby(['account_id','appliance_group','has_OP','year','month'])
    .agg(cons_sum=('consumption','sum'), n_days=('date','count'),
         days_in_m=('days_in_m','first')).reset_index())
monthly_long = monthly_long[monthly_long['n_days'] >= 20].copy()
monthly_long['consumption'] = (monthly_long['cons_sum']
                               / monthly_long['n_days']
                               * monthly_long['days_in_m'])

# grp_sb[grp]  = медіана місячного літнього споживання для ОП груп (MONTHLY)
summer_monthly_op = monthly_long[monthly_long['month'].isin([6,7,8]) & monthly_long['has_OP']]
grp_sb = summer_monthly_op.groupby('appliance_group')['consumption'].median().to_dict()

# grp_med[grp] = медіана місячного споживання для не-ОП груп (MONTHLY)
grp_med = (monthly_long[~monthly_long['has_OP']]
           .groupby('appliance_group')['consumption'].median().to_dict())

print("  Групові summer baseline (ОП):")
for g, v in sorted(grp_sb.items()):
    print(f"    {g}: {v:.1f} м³/міс")
print("  Групові медіани (не-ОП):")
for g, v in sorted(grp_med.items()):
    print(f"    {g}: {v:.1f} м³/міс")

# ── 3. Seasonal ratios ────────────────────────────────────────────────────────
sr_df  = pd.read_csv('data/profiles/pg_seasonal_ratios.csv')
pg_sr  = {(r['group'], int(r['month'])): r['seasonal_ratio'] for _, r in sr_df.iterrows()}

def get_sr(grp, m):
    key = OP_TO_SR.get(grp, 'ПГ')
    return pg_sr.get((key, m), 1.0)

# ── 4. RT профілі (всі моdemні = референси) ──────────────────────────────────
print("Обчислення RT профілів...")

# Підтягуємо consumer_type і is_alt для моdemних з all_pobut
_ap_meta = pd.read_csv("data/input/all_pobut_enriched.csv",
                        usecols=['account_id','consumer_type','alternative'],
                        low_memory=False)
_ap_meta['account_id'] = pd.to_numeric(_ap_meta['account_id'], errors='coerce')
_ap_meta['consumer_type'] = _ap_meta['consumer_type'].fillna('Приватний сектор')
_ap_meta['is_alt'] = _ap_meta['alternative'].notna()
ct_map  = _ap_meta.set_index('account_id')['consumer_type'].to_dict()
alt_map = _ap_meta.set_index('account_id')['is_alt'].to_dict()

long['consumer_type'] = long['account_id'].map(ct_map).fillna('Приватний сектор')
long['is_alt']        = long['account_id'].map(alt_map).fillna(False)

# pbl для кожного моdemного запису — ГРУПОВІ МІСЯЧНІ значення
long['pbl'] = long.apply(
    lambda r: grp_sb.get(r['appliance_group'], 1.0) if r['has_OP']
              else grp_med.get(r['appliance_group'], 1.0), axis=1)
long['sr']      = long.apply(lambda r: get_sr(r['appliance_group'], int(r['month'])), axis=1)
# sb_day: денна доля місячного baseline (з сезонною корекцією для ОП)
long['sb_day']  = long.apply(
    lambda r: r['pbl'] * r['sr'] / r['days_in_m'] if r['has_OP']
              else r['pbl'] / r['days_in_m'], axis=1)
long['heat_day']= long['consumption'] - long['sb_day']

# op_m2(grp, consumer_type, is_alt, date) — стратифікований по типу і alternative
MIN_REF_STRAT = 3   # менший поріг для малих страт
ref_op = long[long['has_OP'] & long['month'].isin(HEATING_MONTHS) & (long['heated_area'] > 0)]

def compute_hms(ref, groupby_cols):
    d = (ref.groupby(groupby_cols + ['date'])
         .apply(lambda g: pd.Series({
             'op_m2': g['heat_day'].sum() / g['heated_area'].sum(), 'n': len(g)
         }), include_groups=False)
         .reset_index())
    d = d[d['n'] >= MIN_REF_STRAT].copy()
    d['op_m2_pos'] = d['op_m2'].clip(lower=0)
    d['year']  = d['date'].dt.year
    d['month'] = d['date'].dt.month
    return (d.groupby(groupby_cols + ['year','month'])['op_m2_pos'].sum()
            .reset_index().rename(columns={'op_m2_pos': 'heat_m2_sum'}))

# Страта 1: (grp, consumer_type, is_alt)
hms_strat = compute_hms(ref_op, ['appliance_group','consumer_type','is_alt'])
# Страта 2: (grp, consumer_type) — без alt
hms_ct    = compute_hms(ref_op, ['appliance_group','consumer_type'])
# Страта 3: (grp) — загальний fallback
hms_grp   = compute_hms(ref_op, ['appliance_group'])

hms_strat_lkp = hms_strat.set_index(['appliance_group','consumer_type','is_alt','year','month'])['heat_m2_sum'].to_dict()
hms_ct_lkp    = hms_ct.set_index(['appliance_group','consumer_type','year','month'])['heat_m2_sum'].to_dict()
hms_grp_lkp   = hms_grp.set_index(['appliance_group','year','month'])['heat_m2_sum'].to_dict()

CT_PRIV = 'Приватний сектор'

def get_hms(grp, ct, is_alt, y, m):
    # 1: точна страта (grp, consumer_type, is_alt)
    v = hms_strat_lkp.get((grp, ct, is_alt, y, m))
    if v is not None: return v
    # 2: той самий тип, без alt-розрізнення
    v = hms_ct_lkp.get((grp, ct, y, m))
    if v is not None: return v
    # 3: Приватний сектор (основна маса модемів)
    v = hms_ct_lkp.get((grp, CT_PRIV, y, m))
    if v is not None: return v
    # 4: загальний груповий
    v = hms_grp_lkp.get((grp, y, m))
    if v is not None: return v
    # 5: fallback група
    fb = OP_FB.get(grp)
    return hms_grp_lkp.get((fb, y, m), np.nan) if fb else np.nan

# rate(grp, date) — для не-ОП і ОП-літо (не стратифікуємо — там проблем немає)
ref_nonheat = long[~(long['has_OP'] & long['month'].isin(HEATING_MONTHS)) & (long['pbl'] > 0)]
rate_daily = (ref_nonheat.groupby(['appliance_group','date'])
    .apply(lambda g: pd.Series({
        'rate': g['consumption'].sum() / g['pbl'].sum(), 'n': len(g)
    }), include_groups=False)
    .reset_index())
rate_daily = rate_daily[rate_daily['n'] >= MIN_REF].copy()
rate_daily['year']  = rate_daily['date'].dt.year
rate_daily['month'] = rate_daily['date'].dt.month

rate_sum = (rate_daily
    .groupby(['appliance_group','year','month'])['rate'].sum()
    .reset_index().rename(columns={'rate': 'rate_sum'}))

print(f"  heat_m2_sum: strat={len(hms_strat)} / ct={len(hms_ct)} / grp={len(hms_grp)}")
print(f"  rate_sum профілів: {len(rate_sum)}")

# Виводимо середній heat_m2_sum по страті для зимового місяця
print("\n  heat_m2_sum (grp × тип, місяць=1):")
h1 = hms_ct[hms_ct['month']==1].copy()
h1['hms_per_day'] = h1['heat_m2_sum'] / 31
print(h1[['appliance_group','consumer_type','heat_m2_sum']].to_string(index=False))

rs_lkp = rate_sum.set_index(['appliance_group','year','month'])['rate_sum'].to_dict()

def get_rs(grp, y, m):
    v = rs_lkp.get((grp, y, m))
    if v is not None: return v
    fb = OP_FB.get(grp)
    return rs_lkp.get((fb, y, m), np.nan) if fb else np.nan

# ── 5. Факт для моdemних споживачів ──────────────────────────────────────────
print("\nРахуємо факт для моdemних споживачів...")
modem_monthly = (monthly_long
    [['account_id','appliance_group','year','month','consumption']]
    .copy())

# Додаємо GRS із all_pobut_enriched
all_pobut = pd.read_csv("data/input/all_pobut_enriched.csv",
                         low_memory=False, encoding='utf-8')
all_pobut['account_id'] = pd.to_numeric(all_pobut['account_id'], errors='coerce')
grs_map = all_pobut.set_index('account_id')['grs'].to_dict()

modem_monthly['grs']  = modem_monthly['account_id'].map(grs_map)
modem_monthly['type'] = 'fact'
modem_monthly = modem_monthly.rename(columns={'consumption': 'volume'})

# ── Фактичне споживання з файлу (jan_2025..dec_2025) по всіх споживачах ──────
MONTH_NAME = {'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,
              'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12}
month_cols_2025 = [c for c in all_pobut.columns if c.endswith('_2025') and c.split('_')[0] in MONTH_NAME]

fact_billing_long = all_pobut[['account_id','grs'] + month_cols_2025].melt(
    id_vars=['account_id','grs'], var_name='col', value_name='volume')
fact_billing_long['month'] = fact_billing_long['col'].str.split('_').str[0].map(MONTH_NAME)
fact_billing_long['year']  = 2025
fact_billing_long['volume'] = pd.to_numeric(fact_billing_long['volume'], errors='coerce').fillna(0)
fact_billing_long = fact_billing_long[fact_billing_long['grs'].notna()].copy()

fact_billing_grs = (fact_billing_long
    .groupby(['grs','year','month'])['volume'].sum().reset_index()
    .rename(columns={'volume': 'fact_billing'}))
fact_billing_total = (fact_billing_long
    .groupby(['year','month'])['volume'].sum().reset_index()
    .rename(columns={'volume': 'fact_billing'}))

# ── 6. Предикт для всіх не-моdemних споживачів ───────────────────────────────
print("Обчислення предиктів для не-моdemних споживачів...")

# Фільтрація: прибираємо тільки газ відключений
# alternative — включаємо завжди
# dacha       — включаємо тільки для літніх місяців (обробляється в циклі)
non_modem = all_pobut[
    ~all_pobut['account_id'].isin(modem_ids) &
    all_pobut['gas_off'].isna()
].copy()
non_modem['appliance_group'] = non_modem['appliance_group'].astype(str).str.strip().replace(GROUP_MERGE)
non_modem = non_modem[~non_modem['appliance_group'].isin(SKIP_GROUPS)].copy()
non_modem['has_OP']      = non_modem['appliance_group'].str.contains('ОП', na=False)
non_modem['is_dacha']    = non_modem['dacha'].notna()
non_modem['is_alt']      = non_modem['alternative'].notna()
non_modem['consumer_type'] = non_modem['consumer_type'].fillna('Приватний сектор')
non_modem['heated_area'] = pd.to_numeric(
    non_modem['heated_area'].astype(str).str.strip(), errors='coerce').fillna(55.0)
non_modem['heated_area'] = non_modem['heated_area'].where(non_modem['heated_area'] > 0, 55.0)

# Груповий baseline з моdemних даних (без індивідуальних даних з файлу)
non_modem['pbl'] = non_modem.apply(
    lambda r: grp_sb.get(r['appliance_group'], grp_sb.get('ОП,ПГ', 10.0)) if r['has_OP']
              else grp_med.get(r['appliance_group'], grp_med.get('ПГ', 5.0)), axis=1)

print(f"  Не-моdemних споживачів: {len(non_modem):,}")
print(f"    з них дача:       {non_modem['is_dacha'].sum():,}")
print(f"    з них alternative: {non_modem['alternative'].notna().sum():,}")
print("  pbl по групах (не-модемні):")
print(non_modem.groupby('appliance_group')['pbl'].agg(['mean','count']).to_string())

# Визначаємо всі (year, month) з моdemних даних
periods = sorted(set(zip(monthly_long['year'], monthly_long['month'])))
print(f"\n  Період: {periods[0]} → {periods[-1]}  ({len(periods)} місяців)")

# Векторизований предикт по кожному місяцю
records = []
for (y, m) in periods:
    is_heat = m in HEATING_MONTHS

    for _, row in non_modem.iterrows():
        grp  = row['appliance_group']
        area = row['heated_area']
        pbl  = row['pbl']
        grs  = row['grs']
        aid  = row['account_id']
        ct   = row['consumer_type']
        ia   = row['is_alt']

        # Дача — тільки влітку
        if row['is_dacha'] and m not in SUMMER_MONTHS:
            continue

        if row['has_OP'] and is_heat:
            hms = get_hms(grp, ct, ia, y, m)
            if np.isnan(hms): continue
            sr  = get_sr(grp, m)
            vol = max(0.0, hms * area) + pbl * sr
        else:
            rs = get_rs(grp, y, m)
            if np.isnan(rs): continue
            vol = pbl * rs

        records.append({'account_id': aid, 'appliance_group': grp,
                        'year': y, 'month': m, 'volume': vol, 'grs': grs, 'type': 'pred'})

pred_monthly = pd.DataFrame(records)
print(f"  Предиктів: {len(pred_monthly):,}")

# ── 7. Об'єднання факту і предикту ───────────────────────────────────────────
combined = pd.concat([modem_monthly, pred_monthly], ignore_index=True)
combined = combined[combined['grs'].notna()].copy()

# ── 8. Агрегація по ГРС × місяць ─────────────────────────────────────────────
agg = (combined
    .groupby(['grs','year','month','type'])['volume']
    .sum().reset_index())

pivot = agg.pivot_table(
    index=['grs','year','month'],
    columns='type', values='volume', aggfunc='sum', fill_value=0).reset_index()
pivot['total']      = pivot.get('fact', 0) + pivot.get('pred', 0)
pivot['fact_share'] = (pivot.get('fact', 0) / pivot['total'].replace(0, np.nan) * 100).round(1)

# Підсумкова таблиця по місяцях
print()
print("=" * 95)
print("АГРЕГАТ ПО МІСЯЦЯХ (всі ГРС разом)  [м³]")
print("=" * 95)
by_month = (combined.groupby(['year','month','type'])['volume']
    .sum().reset_index()
    .pivot_table(index=['year','month'], columns='type', values='volume', aggfunc='sum', fill_value=0)
    .reset_index())
by_month['total']      = by_month.get('fact', 0) + by_month.get('pred', 0)
by_month['fact_share'] = (by_month.get('fact', 0) / by_month['total'].replace(0,np.nan) * 100).round(1)

by_month = by_month.merge(fact_billing_total, on=['year','month'], how='left')
by_month['fact_billing'] = by_month['fact_billing'].fillna(0)
by_month['bias%'] = ((by_month['total'] - by_month['fact_billing'])
                     / by_month['fact_billing'].replace(0, np.nan) * 100).round(1)

print(f"  {'Місяць':<9} {'Факт(модем)':>13} {'Предикт':>13} {'Разом(RT)':>13} {'Факт(файл)':>13} {'Bias%':>7}")
print("  " + "-"*80)
for _, r in by_month.iterrows():
    print(f"  {int(r['year'])}-{int(r['month']):02d}   "
          f"  {r.get('fact', 0):>12,.0f}"
          f"  {r.get('pred', 0):>12,.0f}"
          f"  {r['total']:>12,.0f}"
          f"  {r['fact_billing']:>12,.0f}"
          f"  {r.get('bias%', float('nan')):>6.1f}%")
print("  " + "-"*80)
print(f"  {'РАЗОМ':<9}   "
      f"  {by_month.get('fact', pd.Series([0])).sum():>12,.0f}"
      f"  {by_month.get('pred', pd.Series([0])).sum():>12,.0f}"
      f"  {by_month['total'].sum():>12,.0f}"
      f"  {by_month['fact_billing'].sum():>12,.0f}")

# Таблиця по ГРС (підсумок за весь період)
print()
print("=" * 80)
print("АГРЕГАТ ПО ГРС (весь тестовий період)")
print("=" * 80)
by_grs = (combined.groupby(['grs','type'])['volume']
    .sum().reset_index()
    .pivot_table(index='grs', columns='type', values='volume', aggfunc='sum', fill_value=0)
    .reset_index())
by_grs['total']      = by_grs.get('fact', 0) + by_grs.get('pred', 0)
by_grs['fact_share'] = (by_grs.get('fact', 0) / by_grs['total'].replace(0,np.nan) * 100).round(1)
by_grs = by_grs.sort_values('total', ascending=False)

fact_billing_grs_total = fact_billing_grs.groupby('grs')['fact_billing'].sum().reset_index()
by_grs = by_grs.merge(fact_billing_grs_total, on='grs', how='left')
by_grs['fact_billing'] = by_grs['fact_billing'].fillna(0)
by_grs['bias%'] = ((by_grs['total'] - by_grs['fact_billing'])
                   / by_grs['fact_billing'].replace(0, np.nan) * 100).round(1)

print(f"  {'ГРС':<45} {'Факт(мод)':>11} {'Предикт':>11} {'Разом(RT)':>11} {'Факт(файл)':>11} {'Bias%':>7}")
print("  " + "-"*105)
for _, r in by_grs.iterrows():
    print(f"  {str(r['grs']):<45}"
          f"  {r.get('fact', 0):>10,.0f}"
          f"  {r.get('pred', 0):>10,.0f}"
          f"  {r['total']:>10,.0f}"
          f"  {r['fact_billing']:>10,.0f}"
          f"  {r.get('bias%', float('nan')):>6.1f}%")

# Зберегти результат
out_path = 'data/grs_rt_predict.xlsx'
with pd.ExcelWriter(out_path, engine='openpyxl') as writer:
    # Sheet 1: RT по ГРС × місяць + факт з файлу
    rt_grs_month = (combined.groupby(['grs','year','month','type'])['volume']
        .sum().reset_index()
        .pivot_table(index=['grs','year','month'], columns='type',
                     values='volume', aggfunc='sum', fill_value=0).reset_index())
    rt_grs_month['total_rt'] = rt_grs_month.get('fact', 0) + rt_grs_month.get('pred', 0)
    rt_grs_month = rt_grs_month.merge(fact_billing_grs, on=['grs','year','month'], how='left')
    rt_grs_month['fact_billing'] = rt_grs_month['fact_billing'].fillna(0)
    rt_grs_month['bias%'] = ((rt_grs_month['total_rt'] - rt_grs_month['fact_billing'])
                              / rt_grs_month['fact_billing'].replace(0, np.nan) * 100).round(1)
    rt_grs_month.to_excel(writer, sheet_name='GRS_month', index=False)
    by_month.to_excel(writer, sheet_name='Total_month', index=False)
    by_grs.to_excel(writer, sheet_name='GRS_total', index=False)

print(f"\nЗбережено: {out_path}")
