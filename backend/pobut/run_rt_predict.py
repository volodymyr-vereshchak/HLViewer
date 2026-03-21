"""
run_rt_predict.py
Генерація RT предикту з виключенням нульових активних адрес.
  - summer_op_vpg=False (повернули — глобальне VPG погіршує результат через місто)
  - виключення активних нульових адрес (gas_off=NaN, billing=0 весь рік)
Зберігає grs_rt_predict.xlsx з листами GRS_month, Total_month, GRS_total.
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

# ── 1. Завантаження білінгу + нульові адреси ──────────────────────────────────
print("Завантаження білінгу...")
ap = pd.read_csv(SUBS_FILE, low_memory=False)
ap['account_id'] = pd.to_numeric(ap['account_id'], errors='coerce')
ap = ap[ap['account_id'] != 222046].copy()

month_cols = [c for c in ap.columns if c.endswith('_2025') and c.split('_')[0] in MONTH_NAME]
for c in month_cols:
    ap[c] = pd.to_numeric(ap[c], errors='coerce').fillna(0)

zero_active_ids = set(
    ap[ap['gas_off'].isna() & ap[month_cols].eq(0).all(axis=1)]['account_id'].dropna().astype(int)
)
print(f"  Нульових активних адрес виключено: {len(zero_active_ids):,}")

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

# ── 2. Запуск предикту ────────────────────────────────────────────────────────
print("Запуск RT предикту...")
p = PobUtPredictor(MODEM_FILE, SUBS_FILE, mode='offline', summer_op_vpg=False)
df_all = p.predict(years=[2025])

# ── 3. Виключення нульових адрес ─────────────────────────────────────────────
df = df_all[~df_all['account_id'].isin(zero_active_ids)].copy()
excl_vol = df_all[df_all['account_id'].isin(zero_active_ids)]['volume'].sum()
print(f"  Виключений обсяг нульових: {excl_vol:,.0f} м³/рік")
print()

# ── 4. Агрегація ──────────────────────────────────────────────────────────────
agg = aggregate_by_grs_month(df)
agg = agg.merge(billing_grs, on=['grs', 'month'], how='left')
agg['bias%'] = ((agg['total'] - agg['fact_billing']) / agg['fact_billing'] * 100).round(1)

# ── 5. По місяцях ─────────────────────────────────────────────────────────────
by_m = agg.groupby('month')[['fact', 'pred', 'total', 'fact_billing']].sum()
by_m['fact_share%'] = (by_m['fact'] / by_m['total'] * 100).round(1)
by_m['bias%']       = ((by_m['total'] - by_m['fact_billing']) / by_m['fact_billing'] * 100).round(1)

print("=== По місяцях (всі GRS) ===")
print(f"{'Міс':>4}  {'Fact':>12}  {'Pred':>12}  {'Total':>12}  {'Billing':>12}  {'Fact%':>6}  {'Bias%':>7}")
for m in range(1, 13):
    r = by_m.loc[m]
    print(f"{MONTH_UA[m]:>4}  {r['fact']:>12,.0f}  {r['pred']:>12,.0f}  "
          f"{r['total']:>12,.0f}  {r['fact_billing']:>12,.0f}  "
          f"{r['fact_share%']:>5.1f}%  {r['bias%']:>+6.1f}%")
tot = by_m.sum()
print(f"{'Рік':>4}  {tot['fact']:>12,.0f}  {tot['pred']:>12,.0f}  "
      f"{tot['total']:>12,.0f}  {tot['fact_billing']:>12,.0f}  "
      f"{tot['fact']/tot['total']*100:>5.1f}%  "
      f"{(tot['total']-tot['fact_billing'])/tot['fact_billing']*100:>+6.1f}%")
print()

# ── 6. По GRS річний ─────────────────────────────────────────────────────────
by_grs = agg.groupby('grs')[['fact','pred','total','fact_billing']].sum()
by_grs['bias%'] = ((by_grs['total'] - by_grs['fact_billing']) / by_grs['fact_billing'] * 100).round(1)
by_grs = by_grs.sort_values('total', ascending=False)

print("=== По GRS (річний, топ 15) ===")
print(by_grs.head(15)[['total','fact_billing','bias%']].to_string())
print()

# ── 7. Зберегти Excel ─────────────────────────────────────────────────────────
total_month = by_m.reset_index()
total_month.insert(0, 'month_name', total_month['month'].map(MONTH_UA))
grs_total = by_grs.reset_index()

out_path = 'data/grs_rt_predict.xlsx'
with pd.ExcelWriter(out_path, engine='openpyxl') as xw:
    agg.to_excel(xw, sheet_name='GRS_month', index=False)
    total_month.to_excel(xw, sheet_name='Total_month', index=False)
    grs_total.to_excel(xw, sheet_name='GRS_total', index=False)

print(f"Збережено: {out_path}")
