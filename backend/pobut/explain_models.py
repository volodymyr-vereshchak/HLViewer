import sys, io, calendar
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import numpy as np, pandas as pd
from sklearn.linear_model import LinearRegression
import os
os.chdir(r"D:\Projects\HLViewer\HLViewer\backend\pobut")

HEATING_MONTHS = {1,2,3,4,10,11,12}

# ─── ДАНІ ────────────────────────────────────────────────────────────────────
raw = pd.read_csv('data/input/profile_pobut_daily_result.csv', sep=';', low_memory=False, encoding='utf-8')
META = ['account_id','gas_off','alternative','dacha','appliance_group','consumer_type','profile_no',
        'heated_area','total_area','residents','residents_all','serial_no']
raw.columns = META + list(raw.columns[12:])
date_cols = raw.columns[12:].tolist()
raw['account_id']      = pd.to_numeric(raw['account_id'], errors='coerce')
raw['appliance_group'] = raw['appliance_group'].astype(str).str.strip()
raw['consumer_type']   = raw['consumer_type'].astype(str).str.strip()
raw['heated_area']     = pd.to_numeric(raw['heated_area'].astype(str).str.strip(), errors='coerce').fillna(55.0)
raw['residents']       = pd.to_numeric(raw['residents'], errors='coerce').fillna(2.0)
raw['heated_area']     = raw['heated_area'].where(raw['heated_area'] > 0, 55.0)
raw['residents']       = raw['residents'].where(raw['residents'] > 0, 2.0)
raw['is_mkd']          = (raw['consumer_type'] == 'Багатоквартирний сектор').astype(float)
raw['has_OP']          = raw['appliance_group'].str.contains('ОП', na=False)

long = raw.melt(id_vars=['account_id','appliance_group','is_mkd','heated_area','residents','has_OP'],
    value_vars=date_cols, var_name='ds', value_name='consumption')
long['date']        = pd.to_datetime(long['ds'], format='%d.%m.%Y', errors='coerce')
long['consumption'] = pd.to_numeric(long['consumption'], errors='coerce')
long = long.dropna(subset=['date']).copy()
long['year']  = long['date'].dt.year
long['month'] = long['date'].dt.month

monthly = (long.groupby(
    ['account_id','appliance_group','is_mkd','heated_area','residents','has_OP','year','month'])
    .agg(cons_sum=('consumption','sum'), n_days=('date','count')).reset_index())
monthly = monthly[monthly['n_days'] >= 20].copy()
monthly['days_in_m'] = monthly.apply(
    lambda r: calendar.monthrange(int(r['year']), int(r['month']))[1], axis=1)
monthly['consumption'] = monthly['cons_sum'] / monthly['n_days'] * monthly['days_in_m']
q99 = monthly.groupby(['appliance_group','month'])['consumption'].transform('quantile', 0.99)
monthly = monthly[(monthly['consumption'] > 0) & (monthly['consumption'] <= q99)].copy()
monthly['is_heating'] = monthly['month'].isin(HEATING_MONTHS)

op_groups = monthly[monthly['has_OP']]['appliance_group'].unique()
OP_FB = {'ОП,ПГ':'ОП,ПГ','ОП,ПГ,ВПГ':'ОП,ПГ,ВПГ','ОП,ВПГ':'ОП,ПГ,ВПГ','ОП':'ОП,ПГ'}

# ─── ТРЕНУВАННЯ ──────────────────────────────────────────────────────────────
op_coefs_old, op_coefs_g, op_m2_profile_e = {}, {}, {}

summer = monthly[monthly['month'].isin([6,7,8]) & monthly['has_OP']]
consumer_sb = summer.groupby('account_id')['consumption'].median().to_dict()
group_sb = {g: summer[summer['appliance_group']==g]['consumption'].median()
            for g in op_groups if len(summer[summer['appliance_group']==g]) >= 5}

train_w = monthly[monthly['is_heating'] & monthly['has_OP']].copy()
train_w['sb']      = train_w['account_id'].map(consumer_sb).fillna(
    train_w['appliance_group'].map(group_sb)).fillna(0.0)
train_w['op_heat'] = np.maximum(0.0, train_w['consumption'] - train_w['sb'])

for grp in op_groups:
    for m in HEATING_MONTHS:
        gd = monthly[(monthly['appliance_group']==grp)&(monthly['month']==m)&monthly['is_heating']]
        gd = gd.dropna(subset=['consumption'])
        gd = gd[(gd['heated_area']>0)&(gd['residents']>0)]
        if len(gd) < 10: continue
        lr = LinearRegression(fit_intercept=False)
        lr.fit(gd[['heated_area','residents']].values, gd['consumption'].values)
        op_coefs_old[(grp,m)] = (max(0,lr.coef_[0]), max(0,lr.coef_[1]))
        lr2 = LinearRegression(fit_intercept=False)
        lr2.fit(gd[['heated_area','residents','is_mkd']].values, gd['consumption'].values)
        op_coefs_g[(grp,m)] = (max(0,lr2.coef_[0]), max(0,lr2.coef_[1]), lr2.coef_[2])
        gw = train_w[(train_w['appliance_group']==grp)&(train_w['month']==m)&
                     (train_w['heated_area']>0)].dropna(subset=['op_heat'])
        if len(gw) >= 10:
            op_m2_profile_e[(grp,m)] = gw['op_heat'].sum() / gw['heated_area'].sum()

non_op_profile = {}
for grp in monthly['appliance_group'].unique():
    if monthly[monthly['appliance_group']==grp]['has_OP'].any():
        continue
    for m in range(1, 13):
        gd = monthly[(monthly['appliance_group']==grp)&(monthly['month']==m)]
        if len(gd) >= 5:
            non_op_profile[(grp,m)] = gd['consumption'].median()

smart_ids = set(monthly['account_id'].unique())
SEP = '=' * 70

# ════════════════════════════════════════════════════════════════════════════
print(SEP)
print('КРОК 1: ВІД ДЕННИХ ПОКАЗІВ ДО МІСЯЧНОГО ФАКТУ (смартметер)')
print(SEP)

ex_row  = monthly[(monthly['appliance_group']=='ОП,ПГ,ВПГ')&
                  (monthly['month']==12)&(monthly['is_mkd']==0)].iloc[0]
ex_acc  = ex_row['account_id']
ex_year = int(ex_row['year'])
daily_ex = long[(long['account_id']==ex_acc)&(long['year']==ex_year)&
                (long['month']==12)].sort_values('date')
n_days   = int(daily_ex['consumption'].notna().sum())
s_total  = daily_ex['consumption'].sum()
days_m   = calendar.monthrange(2025,12)[1]
monthly_val = s_total / n_days * days_m if n_days > 0 else 0.0

print(f'\nСпоживач ID={ex_acc} | ОП,ПГ,ВПГ | Приват | грудень {ex_year}')
print(f'  Дата           Денне споживання')
print(f'  {"-"*35}')
for _, r in daily_ex.head(5).iterrows():
    print(f'  {r["date"].date()}     {r["consumption"]:>7.3f} м³/день')
print(f'  ...')
for _, r in daily_ex.tail(3).iterrows():
    print(f'  {r["date"].date()}     {r["consumption"]:>7.3f} м³/день')
print()
print(f'  Кроки нормалізації:')
print(f'    Сума за {n_days} фактичних днів  = {s_total:.2f} м³')
print(f'    Середнє на день             = {s_total:.2f} / {n_days} = {s_total/n_days:.4f} м³/день')
print(f'    Місяць має {days_m} днів          x {days_m}')
print(f'    Місячний факт               = {monthly_val:.1f} м³')

# ════════════════════════════════════════════════════════════════════════════
print()
print(SEP)
print('КРОК 2: ТРЕНУВАННЯ МОДЕЛЕЙ (раз для всіх місяців і груп)')
print(SEP)
m_ex = 12
for grp in ['ОП,ПГ','ОП,ПГ,ВПГ']:
    ca_o, cr_o = op_coefs_old.get((grp,m_ex),(0,0))
    ca_g, cr_g, cm_g = op_coefs_g.get((grp,m_ex),(0,0,0))
    op_m2 = op_m2_profile_e.get((grp,m_ex), 0)
    sb_grp = group_sb.get(grp, 0)
    n = len(monthly[(monthly['appliance_group']==grp)&(monthly['month']==m_ex)&monthly['is_heating']])
    print(f'\nГрупа {grp} | місяць 12 | N={n} споживачів:')
    print(f'  Груповий літній baseline sb = {sb_grp:.2f} м³')
    print()
    print(f'  OLD:   LinearRegression(area, residents) без intercept')
    print(f'         coef_area = {ca_o:.4f} м³/м²/міс')
    print(f'         coef_res  = {cr_o:.4f} м³/мешк/міс')
    print()
    print(f'  MODEL G: LinearRegression(area, residents, is_mkd) без intercept')
    print(f'         coef_area = {ca_g:.4f} м³/м²/міс')
    print(f'         coef_res  = {cr_g:.4f} м³/мешк/міс')
    print(f'         coef_mkd  = {cm_g:.4f} м³ (знижка для МКД)')
    print()
    print(f'  MODEL E: sum(op_heating) / sum(area)  — агрегатний коефіцієнт')
    print(f'         op_heating = consumption - sb_personal')
    print(f'         op_m2      = {op_m2:.4f} м³/м²/міс  (без ПГ/ВПГ компоненти)')

# ════════════════════════════════════════════════════════════════════════════
print()
print(SEP)
print('КРОК 3: ПЕРЕДБАЧЕННЯ ДЛЯ ОП-СПОЖИВАЧА (грудень)')
print(SEP)

SHOW_M = 12
configs = [
    ('ОП,ПГ,ВПГ', 0),
    ('ОП,ПГ,ВПГ', 1),
    ('ОП,ПГ',     0),
    ('ОП,ПГ',     1),
]
for grp, mkd in configs:
    fb = OP_FB.get(grp,'ОП,ПГ,ВПГ')
    sub = monthly[(monthly['appliance_group']==grp)&(monthly['is_mkd']==mkd)&
                  (monthly['month']==SHOW_M)].dropna(subset=['consumption'])
    if len(sub) == 0: continue
    med_area = sub['heated_area'].median()
    ex = sub.iloc[(sub['heated_area']-med_area).abs().argsort()[:1]].iloc[0]
    acc  = ex['account_id']
    area = ex['heated_area']
    res  = ex['residents']
    fact = ex['consumption']
    sb   = consumer_sb.get(acc, group_sb.get(grp, 10.0))
    ct   = 'МКД' if mkd==1 else 'Приват'

    ca_o, cr_o   = op_coefs_old.get((grp,SHOW_M), op_coefs_old.get((fb,SHOW_M),(0,0)))
    ca_g, cr_g, cm_g = op_coefs_g.get((grp,SHOW_M), op_coefs_g.get((fb,SHOW_M),(0,0,0)))
    op_m2        = op_m2_profile_e.get((grp,SHOW_M), op_m2_profile_e.get((fb,SHOW_M),0))

    p_old = max(0, ca_o*area + cr_o*res)
    p_g   = max(0, ca_g*area + cr_g*res + cm_g*mkd)
    p_e   = max(0, op_m2*area) + sb

    print(f'\n  {grp} | {ct} | площа={area:.0f}м² | мешканці={res:.0f} | sb={sb:.1f}м³')
    print(f'  Факт грудень: {fact:.1f} м³')
    print()
    print(f'  OLD:     {ca_o:.4f}*{area:.0f} + {cr_o:.4f}*{res:.0f}')
    print(f'           = {ca_o*area:.1f} + {cr_o*res:.1f} = {p_old:.1f} м³  ({p_old-fact:+.1f})')
    print()
    print(f'  MODEL G: {ca_g:.4f}*{area:.0f} + {cr_g:.4f}*{res:.0f} + {cm_g:.4f}*{mkd}')
    print(f'           = {ca_g*area:.1f} + {cr_g*res:.1f} + {cm_g*mkd:.1f} = {p_g:.1f} м³  ({p_g-fact:+.1f})')
    print()
    print(f'  MODEL E: op_m2={op_m2:.4f} * {area:.0f}м² + sb={sb:.1f}')
    print(f'           = {op_m2*area:.1f} (опалення) + {sb:.1f} (ПГ/ВПГ) = {p_e:.1f} м³  ({p_e-fact:+.1f})')
    print(f'  {"─"*55}')

# ════════════════════════════════════════════════════════════════════════════
print()
print(SEP)
print('КРОК 4: ПЕРЕДБАЧЕННЯ ДЛЯ НЕ-ОП СПОЖИВАЧА')
print(SEP)
print()
print('  Для не-ОП (ПГ, ПГ,ВПГ) — нема опалення, всі моделі однакові:')
print('  прогноз = median(група, місяць) зі смартметерних даних')
print()
print(f'  {"Група":<12} {"Місяць":<8} {"Медіан м³":>10}  {"N спостер.":>12}')
print(f'  {"─"*46}')
for grp_no in ['ПГ','ПГ,ВПГ']:
    for m in [7, 10, 12, 1]:
        p = non_op_profile.get((grp_no,m))
        n = len(monthly[(monthly['appliance_group']==grp_no)&(monthly['month']==m)])
        label = 'літо' if m in [6,7,8] else 'опал.'
        if p:
            print(f'  {grp_no:<12} М{m:02d}({label})  {p:>10.2f}  {n:>12}')

# ════════════════════════════════════════════════════════════════════════════
print()
print(SEP)
print('КРОК 5: АГРЕГАЦІЯ ПО ГРС — ГРУДЕНЬ 2025')
print(SEP)

ap = pd.read_csv('data/input/all_pobut_enriched.csv', low_memory=False,
                 usecols=['account_id','appliance_group','grs','heated_area',
                          'residents','consumer_type','summer_baseline'])
ap['account_id']      = pd.to_numeric(ap['account_id'], errors='coerce')
ap['appliance_group'] = ap['appliance_group'].astype(str).str.strip()
ap['is_mkd']          = (ap['consumer_type']=='Багатоквартирний сектор').astype(float)
ap['grs']             = ap['grs'].astype(str).str.strip()
ap['heated_area']     = pd.to_numeric(ap['heated_area'], errors='coerce').fillna(55.0)
ap['heated_area']     = ap['heated_area'].where(ap['heated_area'] > 0, 55.0)
ap['residents']       = pd.to_numeric(ap['residents'], errors='coerce').fillna(2.0)

for grs_show in ['С-1(Запоріж) ГРС-1,3 кільце', 'ГРС (2) м.Запоріжжя']:
    grs_df = ap[ap['grs']==grs_show]
    tot = {'OLD':0.0,'G':0.0,'E':0.0}
    cnt = {'smart':0,'op_pred':0,'non_op':0}

    for _, r in grs_df.iterrows():
        acc  = r['account_id']
        grp  = str(r['appliance_group']).strip()
        area = float(r['heated_area'])
        res  = float(r['residents'])
        mkd  = float(r['is_mkd'])
        fb   = OP_FB.get(grp,'ОП,ПГ,ВПГ')
        is_op = 'ОП' in grp
        sb_b  = float(r['summer_baseline']) if not pd.isna(r['summer_baseline']) else group_sb.get(grp,10.0)

        if acc in smart_ids:
            sm = monthly[(monthly['account_id']==acc)&(monthly['month']==12)]['consumption']
            val = float(sm.mean()) if len(sm) > 0 else np.nan
            if not np.isnan(val):
                tot['OLD']+=val; tot['G']+=val; tot['E']+=val
                cnt['smart']+=1
        elif is_op:
            sb = consumer_sb.get(acc, sb_b)
            k = (grp,12) if (grp,12) in op_coefs_old else (fb,12)
            if k in op_coefs_old:
                ca,cr = op_coefs_old[k]
                tot['OLD'] += max(0, ca*area + cr*res)
            k_g = (grp,12) if (grp,12) in op_coefs_g else (fb,12)
            if k_g in op_coefs_g:
                ca,cr,cm = op_coefs_g[k_g]
                tot['G'] += max(0, ca*area + cr*res + cm*mkd)
            k_e = (grp,12) if (grp,12) in op_m2_profile_e else (fb,12)
            if k_e in op_m2_profile_e:
                tot['E'] += max(0, op_m2_profile_e[k_e]*area) + sb
            cnt['op_pred']+=1
        else:
            p = non_op_profile.get((grp,12), non_op_profile.get((fb,12), 0))
            tot['OLD']+=p; tot['G']+=p; tot['E']+=p
            cnt['non_op']+=1

    print(f'\n  ГРС: {grs_show}')
    print(f'  Всього абонентів:      {len(grs_df):>8,}')
    print(f'    - смартметер (факт): {cnt["smart"]:>8,}')
    print(f'    - ОП прогноз:        {cnt["op_pred"]:>8,}')
    print(f'    - не-ОП профіль:     {cnt["non_op"]:>8,}')
    print(f'  Грудень 2025 (м³):')
    print(f'    OLD = {tot["OLD"]:>12,.0f}')
    print(f'    G   = {tot["G"]:>12,.0f}   ({(tot["G"]/tot["OLD"]-1)*100:>+.1f}% vs OLD)')
    print(f'    E   = {tot["E"]:>12,.0f}   ({(tot["E"]/tot["OLD"]-1)*100:>+.1f}% vs OLD)')
