"""
test_exclude_zero.py
Виключення активних нульових адрес (gas_off=NaN, billing=0 весь рік) з предикту.
"""
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(r"D:\Projects\HLViewer\HLViewer\backend\pobut")

import pandas as pd
import numpy as np
from pobut_predictor import PobUtPredictor, aggregate_by_grs_month

MONTH_UA = {1:'Січ',2:'Лют',3:'Бер',4:'Кві',5:'Тра',6:'Чер',
            7:'Лип',8:'Сер',9:'Вер',10:'Жов',11:'Лис',12:'Гру'}
MONTH_NAME = {'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,
              'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12}

MODEM_FILE = 'data/input/profile_pobut_daily_result.csv'
SUBS_FILE  = 'data/input/all_pobut_enriched.csv'

# ── 1. Знайти нульові активні адреси ─────────────────────────────────────────
ap = pd.read_csv(SUBS_FILE, low_memory=False)
ap['account_id'] = pd.to_numeric(ap['account_id'], errors='coerce')
ap = ap[ap['account_id'] != 222046].copy()

month_cols = [c for c in ap.columns if c.endswith('_2025') and c.split('_')[0] in MONTH_NAME]
for c in month_cols:
    ap[c] = pd.to_numeric(ap[c], errors='coerce').fillna(0)

zero_active_ids = set(
    ap[ap['gas_off'].isna() & ap[month_cols].eq(0).all(axis=1)]['account_id'].dropna().astype(int)
)
print(f"Нульові активні адреси для виключення: {len(zero_active_ids):,}")

# ── 2. Запустити базовий предикт ──────────────────────────────────────────────
print("Запуск базового предикту...")
p = PobUtPredictor(MODEM_FILE, SUBS_FILE, mode='offline')
df_base = p.predict(years=[2025])
agg_base = aggregate_by_grs_month(df_base)
print(f"  Всього рядків: {len(df_base):,}")

# ── 3. Виключити нульові адреси ───────────────────────────────────────────────
df_excl = df_base[~df_base['account_id'].isin(zero_active_ids)].copy()
agg_excl = aggregate_by_grs_month(df_excl)
print(f"  Після виключення: {len(df_excl):,} рядків")
print(f"  Виключено предикт-рядків: {len(df_base) - len(df_excl):,}")
print()

# ── 4. Завантажити білінг ─────────────────────────────────────────────────────
billing_long = []
for c in month_cols:
    m = MONTH_NAME[c.split('_')[0]]
    tmp = ap[['account_id', 'grs', c]].copy()
    tmp.columns = ['account_id', 'grs', 'bill']
    tmp['month'] = m
    billing_long.append(tmp)
billing = pd.concat(billing_long, ignore_index=True)
billing_grs = billing.groupby(['grs', 'month'])['bill'].sum().reset_index()
billing_grs.columns = ['grs', 'month', 'fact_billing']
billing_total_m = billing.groupby('month')['bill'].sum()

# ── 5. Порівняння по місяцях ──────────────────────────────────────────────────
def month_totals(agg):
    return agg.merge(billing_grs, on=['grs', 'month'], how='left').groupby('month')[['total', 'fact_billing']].sum()

mt_base = month_totals(agg_base)
mt_excl = month_totals(agg_excl)

print("=== Порівняння по місяцях (всі GRS) ===")
print(f"{'Міс':>4}  {'Base':>12}  {'Excl':>12}  {'Billing':>12}  {'Bias% Base':>10}  {'Bias% Excl':>10}  {'Delta':>10}")
total_base = total_excl = total_bill = 0
for m in range(1, 13):
    base_t  = mt_base.loc[m, 'total']
    excl_t  = mt_excl.loc[m, 'total']
    bill_t  = mt_base.loc[m, 'fact_billing']
    delta   = excl_t - base_t
    b_bias  = (base_t - bill_t) / bill_t * 100
    e_bias  = (excl_t - bill_t) / bill_t * 100
    total_base += base_t; total_excl += excl_t; total_bill += bill_t
    print(f"{MONTH_UA[m]:>4}  {base_t:>12,.0f}  {excl_t:>12,.0f}  {bill_t:>12,.0f}  "
          f"{b_bias:>+9.1f}%  {e_bias:>+9.1f}%  {delta:>+10,.0f}")

print(f"{'Рік':>4}  {total_base:>12,.0f}  {total_excl:>12,.0f}  {total_bill:>12,.0f}  "
      f"{(total_base-total_bill)/total_bill*100:>+9.1f}%  "
      f"{(total_excl-total_bill)/total_bill*100:>+9.1f}%  "
      f"{total_excl-total_base:>+10,.0f}")
print()

# ── 6. По GRS де найбільший вплив ─────────────────────────────────────────────
print("=== Вплив по GRS (топ за delta) ===")
m_base_grs = agg_base.merge(billing_grs, on=['grs','month'], how='left')
m_excl_grs = agg_excl.merge(billing_grs, on=['grs','month'], how='left')

grs_base = m_base_grs.groupby('grs')[['total','fact_billing']].sum()
grs_excl = m_excl_grs.groupby('grs')[['total','fact_billing']].sum()

grs_cmp = grs_base.copy()
grs_cmp.columns = ['base_total', 'billing']
grs_cmp['excl_total'] = grs_excl['total']
grs_cmp['delta_m3']   = (grs_cmp['excl_total'] - grs_cmp['base_total']).round(0)
grs_cmp['base_bias%'] = ((grs_cmp['base_total'] - grs_cmp['billing']) / grs_cmp['billing'] * 100).round(1)
grs_cmp['excl_bias%'] = ((grs_cmp['excl_total'] - grs_cmp['billing']) / grs_cmp['billing'] * 100).round(1)
grs_cmp = grs_cmp.sort_values('delta_m3')

# Показати тільки GRS де є різниця
changed = grs_cmp[grs_cmp['delta_m3'] != 0].copy()
print(f"GRS зі змінами: {len(changed)}")
print(changed[['billing', 'base_total', 'excl_total', 'delta_m3', 'base_bias%', 'excl_bias%']].to_string())
print()

# ── 7. Що саме виключили ──────────────────────────────────────────────────────
excl_rows = df_base[df_base['account_id'].isin(zero_active_ids)]
print(f"=== Виключені рядки: {len(excl_rows):,} ===")
print("По типу (fact/pred):")
print(excl_rows['type'].value_counts().to_string())
print()
by_m = excl_rows.groupby('month')['volume'].sum()
print("Виключений обсяг по місяцях:")
for m in range(1, 13):
    v = by_m.get(m, 0)
    print(f"  {MONTH_UA[m]:>4}: {v:>10,.0f} м³")
print(f"  {'Рік':>4}: {by_m.sum():>10,.0f} м³")
print()

# ── 8. Зберегти результат ─────────────────────────────────────────────────────
res_excl = agg_excl.merge(billing_grs, on=['grs','month'], how='left')
res_excl['bias%'] = ((res_excl['total'] - res_excl['fact_billing']) / res_excl['fact_billing'] * 100).round(1)

with pd.ExcelWriter('data/excl_zero_compare.xlsx', engine='openpyxl') as xw:
    res_excl.to_excel(xw, sheet_name='GRS_month', index=False)
    changed.reset_index().to_excel(xw, sheet_name='GRS_delta', index=False)

print("Збережено: data/excl_zero_compare.xlsx")
