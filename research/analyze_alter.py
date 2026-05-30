"""Анализ: потребители с альтернативным отоплением / дача / газ откл."""

import re
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')
pd.set_option('display.max_columns', 30)
pd.set_option('display.width', 250)

SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR / 'data'

# --- Load alter ---
alter = pd.read_excel(DATA_DIR / 'alter.xlsx')
alter.columns = ['account_id', 'gas_off', 'alternative', 'dacha']

gas_off_ids = set(alter[alter['gas_off'].notna()]['account_id'])
alt_ids = set(alter[alter['alternative'].notna()]['account_id'])
dacha_ids = set(alter[alter['dacha'].notna()]['account_id'])
flagged_ids = gas_off_ids | alt_ids | dacha_ids

print(f"Газ откл: {len(gas_off_ids)}")
print(f"Альтернатива: {len(alt_ids)}")
print(f"Дача: {len(dacha_ids)}")
print(f"Пересечение альт+дача: {len(alt_ids & dacha_ids)}")
print(f"Всего с любым флагом: {len(flagged_ids)}")

# --- Load main data ---
raw = pd.read_csv(DATA_DIR / 'profile_pobut_daily_result.csv', sep=';', encoding='utf-8')
meta_cols = ['account_id', 'appliance_group', 'profile_no', 'heated_area',
             'total_area', 'residents', 'residents_all', 'device_serial']
raw.columns = meta_cols + list(raw.columns[len(meta_cols):])
raw['heated_area'] = pd.to_numeric(raw['heated_area'].astype(str).str.strip(), errors='coerce')
raw['residents'] = pd.to_numeric(raw['residents'], errors='coerce')

date_cols = [c for c in raw.columns if re.match(r'\d{2}\.\d{2}\.\d{4}$', str(c))]
df = raw.melt(id_vars=meta_cols, value_vars=date_cols, var_name='date_str', value_name='consumption')
df['date'] = pd.to_datetime(df['date_str'], format='%d.%m.%Y')
df['consumption'] = pd.to_numeric(df['consumption'], errors='coerce')
df = df.dropna(subset=['consumption'])
df['year_month'] = df['date'].dt.to_period('M')
df['month'] = df['date'].dt.month

# Monthly aggregation
monthly = df.groupby(['account_id', 'year_month']).agg(
    consumption_sum=('consumption', 'sum'),
    days_count=('consumption', 'count'),
).reset_index()
monthly = monthly.merge(raw[['account_id', 'appliance_group', 'heated_area', 'residents']],
                        on='account_id', how='left')
monthly['month_num'] = monthly['year_month'].dt.month
monthly['days_in_month'] = monthly['year_month'].apply(lambda p: p.days_in_month)
monthly['consumption_norm'] = monthly['consumption_sum'] / monthly['days_count'] * monthly['days_in_month']
monthly = monthly[monthly['days_count'] >= 20]
monthly['has_OP'] = monthly['appliance_group'].str.contains('ОП', na=False)

# Flags
monthly['is_gas_off'] = monthly['account_id'].isin(gas_off_ids)
monthly['is_alternative'] = monthly['account_id'].isin(alt_ids)
monthly['is_dacha'] = monthly['account_id'].isin(dacha_ids)
monthly['is_flagged'] = monthly['account_id'].isin(flagged_ids)

# ===================================================================
# Сколько ОП-потребителей с флагами
# ===================================================================
op_monthly = monthly[monthly['has_OP']].copy()
op_ids = set(op_monthly['account_id'].unique())

print(f"\n{'='*80}")
print(f"ОП потребители: {len(op_ids)}")
print(f"  + газ откл:     {len(gas_off_ids & op_ids)}")
print(f"  + альтернатива: {len(alt_ids & op_ids)}")
print(f"  + дача:         {len(dacha_ids & op_ids)}")
print(f"  + любой флаг:   {len(flagged_ids & op_ids)}")

# Не-ОП с флагами
non_op_ids = set(monthly[~monthly['has_OP']]['account_id'].unique())
print(f"\nНе-ОП потребители: {len(non_op_ids)}")
print(f"  + альтернатива: {len(alt_ids & non_op_ids)}")
print(f"  + дача:         {len(dacha_ids & non_op_ids)}")

# ===================================================================
# Сравнение: средний расход по группам
# ===================================================================
print(f"\n{'='*80}")
print("Сравнение среднего расхода: Обычные vs Альтернатива vs Дача")
print(f"{'='*80}")

header = f"{'Группа':<15} {'Тип':<15} {'N потр':>8} {'Ср.площ':>10} {'Ср.прож':>8} {'Ср.расх':>10} {'Расх/м2':>10}"
print(header)
print("-" * 85)

for grp in sorted(op_monthly['appliance_group'].unique()):
    gd = op_monthly[op_monthly['appliance_group'] == grp]

    for label, mask in [('Обычные', ~gd['is_flagged']),
                        ('Альтернат.', gd['is_alternative']),
                        ('Дача', gd['is_dacha']),
                        ('Газ откл', gd['is_gas_off'])]:
        sub = gd[mask]
        n = sub['account_id'].nunique()
        if n == 0:
            continue
        avg_area = sub.groupby('account_id')['heated_area'].first().mean()
        avg_res = sub.groupby('account_id')['residents'].first().mean()
        avg_cons = sub['consumption_norm'].mean()
        cons_per_m2 = avg_cons / avg_area if avg_area > 0 else 0
        print(f"{grp:<15} {label:<15} {n:>8} {avg_area:>10.1f} {avg_res:>8.1f} "
              f"{avg_cons:>10.1f} {cons_per_m2:>10.4f}")

# ===================================================================
# Помесячное сравнение для ОП,ПГ,ВПГ
# ===================================================================
print(f"\n{'='*80}")
print("Помесячно: ОП,ПГ,ВПГ - Обычные vs Альтернатива")
print(f"{'='*80}")

grp_name = 'ОП,ПГ,ВПГ'
gd = op_monthly[op_monthly['appliance_group'] == grp_name]
normal = gd[~gd['is_flagged']]
altern = gd[gd['is_alternative']]

print(f"{'Месяц':<10} {'Об.(N)':>8} {'Об.расх':>10} {'Об/м2':>8} "
      f"{'Ал.(N)':>8} {'Ал.расх':>10} {'Ал/м2':>8} {'Разн%':>8}")
print("-" * 80)

for ym in sorted(gd['year_month'].unique()):
    n_data = normal[normal['year_month'] == ym]
    a_data = altern[altern['year_month'] == ym]
    if len(n_data) == 0:
        continue

    n_avg = n_data['consumption_norm'].mean()
    n_area = n_data['heated_area'].mean()
    n_per_m2 = n_avg / n_area if n_area > 0 else 0

    if len(a_data) > 0:
        a_avg = a_data['consumption_norm'].mean()
        a_area = a_data['heated_area'].mean()
        a_per_m2 = a_avg / a_area if a_area > 0 else 0
        diff = (a_avg - n_avg) / n_avg * 100
        print(f"{str(ym):<10} {len(n_data):>8} {n_avg:>10.1f} {n_per_m2:>8.4f} "
              f"{len(a_data):>8} {a_avg:>10.1f} {a_per_m2:>8.4f} {diff:>7.1f}%")
    else:
        print(f"{str(ym):<10} {len(n_data):>8} {n_avg:>10.1f} {n_per_m2:>8.4f} "
              f"{'--':>8} {'--':>10} {'--':>8} {'--':>8}")

# ===================================================================
# То же для ОП,ПГ
# ===================================================================
grp_name2 = 'ОП,ПГ'
gd2 = op_monthly[op_monthly['appliance_group'] == grp_name2]
normal2 = gd2[~gd2['is_flagged']]
altern2 = gd2[gd2['is_alternative']]

if altern2['account_id'].nunique() > 0:
    print(f"\n{'='*80}")
    print("Помесячно: ОП,ПГ - Обычные vs Альтернатива")
    print(f"{'='*80}")

    print(f"{'Месяц':<10} {'Об.(N)':>8} {'Об.расх':>10} {'Об/м2':>8} "
          f"{'Ал.(N)':>8} {'Ал.расх':>10} {'Ал/м2':>8} {'Разн%':>8}")
    print("-" * 80)

    for ym in sorted(gd2['year_month'].unique()):
        n_data = normal2[normal2['year_month'] == ym]
        a_data = altern2[altern2['year_month'] == ym]
        if len(n_data) == 0:
            continue
        n_avg = n_data['consumption_norm'].mean()
        n_area = n_data['heated_area'].mean()
        n_per_m2 = n_avg / n_area if n_area > 0 else 0
        if len(a_data) > 0:
            a_avg = a_data['consumption_norm'].mean()
            a_area = a_data['heated_area'].mean()
            a_per_m2 = a_avg / a_area if a_area > 0 else 0
            diff = (a_avg - n_avg) / n_avg * 100
            print(f"{str(ym):<10} {len(n_data):>8} {n_avg:>10.1f} {n_per_m2:>8.4f} "
                  f"{len(a_data):>8} {a_avg:>10.1f} {a_per_m2:>8.4f} {diff:>7.1f}%")
        else:
            print(f"{str(ym):<10} {len(n_data):>8} {n_avg:>10.1f} {n_per_m2:>8.4f} "
                  f"{'--':>8} {'--':>10} {'--':>8} {'--':>8}")

# ===================================================================
# Детально: каждый потребитель с альтернативой
# ===================================================================
print(f"\n{'='*80}")
print("Детально: каждый потребитель с альтернативой (ОП группы)")
print(f"{'='*80}")

alt_op = op_monthly[op_monthly['is_alternative']].copy()
if len(alt_op) > 0:
    cons_agg = alt_op.groupby('account_id').agg(
        grp=('appliance_group', 'first'),
        area=('heated_area', 'first'),
        residents=('residents', 'first'),
        n_months=('year_month', 'nunique'),
        avg_cons=('consumption_norm', 'mean'),
    ).reset_index()

    # Winter/summer separately
    winter = alt_op[alt_op['month_num'].isin([12, 1, 2])].groupby('account_id')['consumption_norm'].mean()
    summer = alt_op[alt_op['month_num'].isin([6, 7, 8])].groupby('account_id')['consumption_norm'].mean()
    cons_agg = cons_agg.merge(winter.rename('winter'), on='account_id', how='left')
    cons_agg = cons_agg.merge(summer.rename('summer'), on='account_id', how='left')
    cons_agg['per_m2'] = cons_agg['avg_cons'] / cons_agg['area']
    cons_agg['w_s_ratio'] = cons_agg['winter'] / cons_agg['summer'].replace(0, np.nan)

    print(f"{'acc_id':<12} {'Группа':<15} {'Площ':>6} {'Пр':>4} {'Мес':>4} "
          f"{'Ср.расх':>8} {'Р/м2':>7} {'Зима':>8} {'Лето':>8} {'З/Л':>6}")
    print("-" * 90)
    for _, r in cons_agg.iterrows():
        ws = f"{r['w_s_ratio']:.1f}" if pd.notna(r['w_s_ratio']) else '--'
        w = f"{r['winter']:.1f}" if pd.notna(r['winter']) else '--'
        s = f"{r['summer']:.1f}" if pd.notna(r['summer']) else '--'
        area = f"{r['area']:.0f}" if pd.notna(r['area']) else '--'
        res = f"{r['residents']:.0f}" if pd.notna(r['residents']) else '--'
        print(f"{r['account_id']:<12} {r['grp']:<15} {area:>6} {res:>4} {r['n_months']:>4} "
              f"{r['avg_cons']:>8.1f} {r['per_m2']:>7.4f} {w:>8} {s:>8} {ws:>6}")

    # Normal reference
    normal_op = op_monthly[~op_monthly['is_flagged']]
    for grp_ref in sorted(cons_agg['grp'].unique()):
        ng = normal_op[normal_op['appliance_group'] == grp_ref]
        if len(ng) == 0:
            continue
        n_area = ng.groupby('account_id')['heated_area'].first().mean()
        nw = ng[ng['month_num'].isin([12, 1, 2])]['consumption_norm'].mean()
        ns = ng[ng['month_num'].isin([6, 7, 8])]['consumption_norm'].mean()
        n_avg = ng['consumption_norm'].mean()
        print(f"\nСправка {grp_ref} обычные ({ng['account_id'].nunique()} потр.): "
              f"ср.площ={n_area:.0f}, ср.расх={n_avg:.1f}, р/м2={n_avg/n_area:.4f}, "
              f"зима={nw:.1f}, лето={ns:.1f}, з/л={nw/ns:.1f}")

# ===================================================================
# Детально: каждый потребитель с дачей
# ===================================================================
print(f"\n{'='*80}")
print("Детально: каждый потребитель-дача (ОП группы)")
print(f"{'='*80}")

dacha_op = op_monthly[op_monthly['is_dacha']].copy()
if len(dacha_op) > 0:
    cons_d = dacha_op.groupby('account_id').agg(
        grp=('appliance_group', 'first'),
        area=('heated_area', 'first'),
        residents=('residents', 'first'),
        n_months=('year_month', 'nunique'),
        avg_cons=('consumption_norm', 'mean'),
    ).reset_index()

    winter_d = dacha_op[dacha_op['month_num'].isin([12, 1, 2])].groupby('account_id')['consumption_norm'].mean()
    summer_d = dacha_op[dacha_op['month_num'].isin([6, 7, 8])].groupby('account_id')['consumption_norm'].mean()
    cons_d = cons_d.merge(winter_d.rename('winter'), on='account_id', how='left')
    cons_d = cons_d.merge(summer_d.rename('summer'), on='account_id', how='left')
    cons_d['per_m2'] = cons_d['avg_cons'] / cons_d['area'].replace(0, np.nan)
    cons_d['w_s_ratio'] = cons_d['winter'] / cons_d['summer'].replace(0, np.nan)

    print(f"{'acc_id':<12} {'Группа':<15} {'Площ':>6} {'Пр':>4} {'Мес':>4} "
          f"{'Ср.расх':>8} {'Р/м2':>7} {'Зима':>8} {'Лето':>8} {'З/Л':>6}")
    print("-" * 90)
    for _, r in cons_d.iterrows():
        ws = f"{r['w_s_ratio']:.1f}" if pd.notna(r['w_s_ratio']) else '--'
        w = f"{r['winter']:.1f}" if pd.notna(r['winter']) else '--'
        s = f"{r['summer']:.1f}" if pd.notna(r['summer']) else '--'
        area = f"{r['area']:.0f}" if pd.notna(r['area']) else '--'
        res = f"{r['residents']:.0f}" if pd.notna(r['residents']) else '--'
        pm2 = f"{r['per_m2']:.4f}" if pd.notna(r['per_m2']) else '--'
        print(f"{r['account_id']:<12} {r['grp']:<15} {area:>6} {res:>4} {r['n_months']:>4} "
              f"{r['avg_cons']:>8.1f} {pm2:>7} {w:>8} {s:>8} {ws:>6}")
