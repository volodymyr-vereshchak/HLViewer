"""
test_summer_vpg.py
Порівняння RT моделі до і після summer_op_vpg=True.
"""
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(r"D:\Projects\HLViewer\HLViewer\backend\pobut")

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from pobut_predictor import PobUtPredictor, aggregate_by_grs_month

MONTH_UA = {1:'Січ',2:'Лют',3:'Бер',4:'Кві',5:'Тра',6:'Чер',
            7:'Лип',8:'Сер',9:'Вер',10:'Жов',11:'Лис',12:'Гру'}

MODEM_FILE = 'data/input/profile_pobut_daily_result.csv'
SUBS_FILE  = 'data/input/all_pobut_enriched.csv'

# ── Базовий предикт ───────────────────────────────────────────────────────────
print("Запуск базового предикту (summer_op_vpg=False)...")
p_base = PobUtPredictor(MODEM_FILE, SUBS_FILE, mode='offline')
df_base = p_base.predict(years=[2025])
agg_base = aggregate_by_grs_month(df_base)
print(f"  grp_sb['ОП,ПГ']    = {p_base._grp_sb.get('ОП,ПГ', 'N/A'):.2f}")
print(f"  grp_sb['ОП,ПГ,ВПГ']= {p_base._grp_sb.get('ОП,ПГ,ВПГ', 'N/A'):.2f}")
print()

# ── Предикт з summer_op_vpg=True ─────────────────────────────────────────────
print("Запуск предикту з summer_op_vpg=True...")
p_vpg = PobUtPredictor(MODEM_FILE, SUBS_FILE, mode='offline', summer_op_vpg=True)
df_vpg = p_vpg.predict(years=[2025])
agg_vpg = aggregate_by_grs_month(df_vpg)
print()

# ── Завантаження білінгу ──────────────────────────────────────────────────────
MONTH_NAME = {'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,
              'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12}
ap = pd.read_csv(SUBS_FILE, low_memory=False)
ap['account_id'] = pd.to_numeric(ap['account_id'], errors='coerce')
month_cols = [c for c in ap.columns if c.endswith('_2025') and c.split('_')[0] in MONTH_NAME]
for c in month_cols:
    ap[c] = pd.to_numeric(ap[c], errors='coerce').fillna(0)
ap = ap[ap['account_id'] != 222046].copy()

billing_long = []
for c in month_cols:
    m = MONTH_NAME[c.split('_')[0]]
    tmp = ap[['account_id','grs', c]].copy()
    tmp.columns = ['account_id','grs','bill']
    tmp['month'] = m
    billing_long.append(tmp)
billing = pd.concat(billing_long, ignore_index=True)
billing_grs = billing.groupby(['grs','month'])['bill'].sum().reset_index()
billing_grs.columns = ['grs','month','fact_billing']

# ── Зведення по місяцях (загально) ────────────────────────────────────────────
def monthly_summary(agg, billing_grs, label):
    merged = agg.merge(billing_grs, on=['grs','month'], how='left')
    by_m = merged.groupby('month')[['total','fact_billing']].sum()
    by_m['bias%'] = ((by_m['total'] - by_m['fact_billing']) / by_m['fact_billing'] * 100).round(1)
    by_m['label'] = label
    return by_m

sm_base = monthly_summary(agg_base, billing_grs, 'Base')
sm_vpg  = monthly_summary(agg_vpg,  billing_grs, 'VPG')

print("=== Загальне споживання по місяцях (всі GRS) ===")
print(f"{'Міс':>4}  {'Base total':>12}  {'VPG total':>12}  {'Billing':>12}  {'Bias% Base':>10}  {'Bias% VPG':>10}  {'Delta':>10}")
for m in range(1, 13):
    b_row   = sm_base.loc[m]
    v_row   = sm_vpg.loc[m]
    bill    = b_row['fact_billing']
    b_total = b_row['total']
    v_total = v_row['total']
    delta   = v_total - b_total
    print(f"{MONTH_UA[m]:>4}  {b_total:>12,.0f}  {v_total:>12,.0f}  {bill:>12,.0f}  "
          f"{b_row['bias%']:>+9.1f}%  {v_row['bias%']:>+9.1f}%  {delta:>+10,.0f}")

total_b = sm_base['fact_billing'].sum()
total_base_t = sm_base['total'].sum()
total_vpg_t  = sm_vpg['total'].sum()
print(f"\n{'Рік':>4}  {total_base_t:>12,.0f}  {total_vpg_t:>12,.0f}  {total_b:>12,.0f}  "
      f"{(total_base_t-total_b)/total_b*100:>+9.1f}%  "
      f"{(total_vpg_t-total_b)/total_b*100:>+9.1f}%  "
      f"{total_vpg_t-total_base_t:>+10,.0f}")
print()

# ── По ГРС (тільки літо — місяці 5-9) ────────────────────────────────────────
summer_m = [5, 6, 7, 8, 9]
print("=== Bias% по GRS (літо, місяці 5-9) ===")

def grs_summer(agg, billing_grs):
    a = agg[agg['month'].isin(summer_m)].copy()
    b = billing_grs[billing_grs['month'].isin(summer_m)].copy()
    m = a.merge(b, on=['grs','month'], how='left')
    g = m.groupby('grs')[['total','fact_billing']].sum()
    g['bias%'] = ((g['total'] - g['fact_billing']) / g['fact_billing'] * 100).round(1)
    return g

gs_base = grs_summer(agg_base, billing_grs)
gs_vpg  = grs_summer(agg_vpg,  billing_grs)
gs_cmp = gs_base[['total','fact_billing','bias%']].copy()
gs_cmp.columns = ['base_total','billing','base_bias%']
gs_cmp['vpg_total'] = gs_vpg['total']
gs_cmp['vpg_bias%'] = gs_vpg['bias%']
gs_cmp['delta_m3'] = (gs_cmp['vpg_total'] - gs_cmp['base_total']).round(0)
gs_cmp = gs_cmp.sort_values('base_bias%')
print(gs_cmp[['billing','base_total','vpg_total','base_bias%','vpg_bias%','delta_m3']].to_string())
print()

# ── Зберегти ──────────────────────────────────────────────────────────────────
# Додати fact_billing до обох агрегацій і зберегти
def add_billing(agg, billing_grs):
    m = agg.merge(billing_grs, on=['grs','month'], how='left')
    m['bias%'] = ((m['total'] - m['fact_billing']) / m['fact_billing'] * 100).round(1)
    return m

res_base = add_billing(agg_base, billing_grs)
res_vpg  = add_billing(agg_vpg,  billing_grs)

with pd.ExcelWriter('data/summer_vpg_compare.xlsx', engine='openpyxl') as xw:
    res_base.to_excel(xw, sheet_name='Base',    index=False)
    res_vpg.to_excel(xw,  sheet_name='VPG',     index=False)
    gs_cmp.reset_index().to_excel(xw, sheet_name='GRS_summer', index=False)

print("Збережено: data/summer_vpg_compare.xlsx")

# ── Графік bias% по місяцях ───────────────────────────────────────────────────
fig, ax = plt.subplots(figsize=(11, 5))
months_ua = [MONTH_UA[m] for m in range(1,13)]
bias_base = [sm_base.loc[m,'bias%'] for m in range(1,13)]
bias_vpg  = [sm_vpg.loc[m,'bias%']  for m in range(1,13)]
ax.plot(months_ua, bias_base, 'o-', color='#2196F3', lw=2, label='Base (без VPG)')
ax.plot(months_ua, bias_vpg,  's-', color='#E91E63', lw=2, label='VPG (ОП,ПГ→ОП,ПГ,ВПГ влітку)')
ax.axhline(0, color='k', lw=0.8, ls='--')
for m_idx, m in enumerate(range(1,13)):
    ax.annotate(f"{bias_base[m_idx]:+.0f}%", (months_ua[m_idx], bias_base[m_idx]),
                fontsize=7, ha='center', va='bottom', color='#2196F3')
    ax.annotate(f"{bias_vpg[m_idx]:+.0f}%", (months_ua[m_idx], bias_vpg[m_idx]),
                fontsize=7, ha='center', va='top', color='#E91E63')
ax.set_title('Bias % по місяцях: Base vs summer_op_vpg (всі GRS, 2025)', fontsize=11)
ax.set_ylabel('Bias % (pred vs billing)')
ax.legend()
ax.grid(True, alpha=0.3)
plt.tight_layout()
plt.savefig('data/summer_vpg_bias.png', dpi=130, bbox_inches='tight')
print("Збережено: data/summer_vpg_bias.png")
