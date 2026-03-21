"""
compare_village_approaches.py
Порівняння різних підходів предикту для сільських ГРС.

Варіанти:
  A. Поточна RT модель (базовий)
  B. Пряме масштабування: avg_modem × cnt_all
  C. k-LOO корекція: avg_modem × cnt_all × k_loo (k з ін. місяців, leave-one-out)
  D. Волод* кластер: пул всіх Волод* модемів → cluster_avg × cnt_all для Волод* секторів
  E. Стабільні GRS (std_k < 0.5): direct scaling; решта → RT
  F. Зважений k по кількості модемів: лише GRS ≥ 4 модеми (без викидів)
"""
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(r"D:\Projects\HLViewer\HLViewer\backend\pobut")

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

MONTH_UA = {1:'Січ',2:'Лют',3:'Бер',4:'Кві',5:'Тра',6:'Чер',
            7:'Лип',8:'Сер',9:'Вер',10:'Жов',11:'Лис',12:'Гру'}

# ── Завантаження ──────────────────────────────────────────────────────────────
sc = pd.read_excel('data/village_scatter_data.xlsx')
# Cols: grs, month, avg_modem, avg_all, cnt_modem, cnt_all, coef

rt = pd.read_excel('data/grs_rt_predict.xlsx', sheet_name='GRS_month')
village_grs = sc['grs'].unique()
rt_v = rt[rt['grs'].isin(village_grs)][['grs','month','fact','pred','total','fact_billing']].copy()

# Злити scatter + RT
df = sc.merge(rt_v, on=['grs','month'], how='inner')

# Стандартне відхилення коефіцієнта по GRS (з scatter)
grs_std = sc.groupby('grs')['coef'].std().rename('std_k').round(3)
df = df.merge(grs_std.reset_index(), on='grs', how='left')

# Прапорець Волод*
df['is_volod'] = df['grs'].str.contains('Волод', na=False)

print(f"Village GRS: {df['grs'].nunique()}, точок: {len(df)}")
print()

# ── Варіант A: RT модель ──────────────────────────────────────────────────────
# df['total'] — вже є (fact + pred)

# ── Варіант B: Пряме масштабування ───────────────────────────────────────────
df['pred_B'] = df['avg_modem'] * df['cnt_all']

# ── Варіант C: k-LOO корекція ─────────────────────────────────────────────────
# Для кожної GRS: k_loo(month M) = median(coef) по всіх місяцях крім M
loo_k = {}
for grs, dg in df.groupby('grs'):
    for m, row in dg.iterrows():
        other_k = dg.loc[dg.index != m, 'coef']
        loo_k[(dg.loc[m,'grs'], dg.loc[m,'month'])] = other_k.median() if len(other_k) >= 3 else np.nan

df['k_loo'] = df.apply(lambda r: loo_k.get((r['grs'], r['month']), np.nan), axis=1)
df['pred_C'] = df['avg_modem'] * df['cnt_all'] * df['k_loo']

# ── Варіант D: Волод* кластер ─────────────────────────────────────────────────
# Для кожного місяця: cluster_avg = Σ(avg_modem×cnt_modem) / Σ(cnt_modem) по Волод*
volod_monthly = (
    df[df['is_volod']]
    .groupby('month')
    .apply(lambda g: (g['avg_modem'] * g['cnt_modem']).sum() / g['cnt_modem'].sum())
    .rename('cluster_avg_volod')
)
df = df.merge(volod_monthly.reset_index(), on='month', how='left')

# Для Волод* — cluster avg × cnt_all; для решти — пряме scaling
df['pred_D'] = np.where(
    df['is_volod'],
    df['cluster_avg_volod'] * df['cnt_all'],
    df['avg_modem'] * df['cnt_all']   # fallback: прямий
)

# ── Варіант E: Стабільні GRS (std_k < 0.5) — direct, решта → RT ──────────────
df['pred_E'] = np.where(
    df['std_k'] < 0.5,
    df['avg_modem'] * df['cnt_all'],
    df['total']  # RT fallback
)

# ── Варіант F: Тільки GRS з ≥ 4 модемами (виключаємо одиночні) ───────────────
# Для GRS з ≥4 — direct scaling; для 1-3 і 0 — RT
df['pred_F'] = np.where(
    df['cnt_modem'] >= 4,
    df['avg_modem'] * df['cnt_all'],
    df['total']  # RT fallback
)

# ── Метрики ───────────────────────────────────────────────────────────────────
true = df['fact_billing']

def metrics(pred, name):
    valid = ~(pred.isna() | true.isna())
    p, t = pred[valid], true[valid]
    bias_m3  = (p - t).sum()
    bias_pct = bias_m3 / t.sum() * 100
    mae      = (p - t).abs().mean()
    rmse     = np.sqrt(((p - t)**2).mean())
    return {
        'Варіант': name,
        'Bias м³': round(bias_m3, 0),
        'Bias %': round(bias_pct, 2),
        'MAE': round(mae, 0),
        'RMSE': round(rmse, 0),
        'n': valid.sum()
    }

summary = pd.DataFrame([
    metrics(df['total'],  'A: RT модель'),
    metrics(df['pred_B'], 'B: avg_modem × cnt_all'),
    metrics(df['pred_C'], 'C: k-LOO × avg_modem × cnt_all'),
    metrics(df['pred_D'], 'D: Волод*-кластер + direct'),
    metrics(df['pred_E'], 'E: std<0.5 → direct, решта RT'),
    metrics(df['pred_F'], 'F: ≥4 модеми → direct, решта RT'),
])

print("=== Загальні метрики (village GRS × місяць) ===")
print(summary.to_string(index=False))
print()

# ── По місяцях ────────────────────────────────────────────────────────────────
print("=== Bias % по місяцях ===")
month_rows = []
for m in sorted(df['month'].unique()):
    dm = df[df['month'] == m]
    t = dm['fact_billing']
    row = {'Міс': MONTH_UA[m]}
    for col, lbl in [('total','A:RT'),('pred_B','B:Scl'),('pred_C','C:LOO'),
                     ('pred_D','D:Vol'),('pred_E','E:Stb'),('pred_F','F:4+')]:
        b = (dm[col] - t).sum() / t.sum() * 100
        row[lbl] = round(b, 1)
    month_rows.append(row)
print(pd.DataFrame(month_rows).to_string(index=False))
print()

# ── По GRS ────────────────────────────────────────────────────────────────────
print("=== Bias % по GRS ===")
grs_rows = []
for grs, dg in df.groupby('grs'):
    t = dg['fact_billing']
    row = {
        'GRS': grs[-32:],
        'Мод': dg['cnt_modem'].iloc[0],
        'std_k': dg['std_k'].iloc[0],
        'bill': round(t.sum()/1000, 0),
    }
    for col, lbl in [('total','A:RT'),('pred_B','B:Scl'),('pred_C','C:LOO'),
                     ('pred_D','D:Vol'),('pred_E','E:Stb'),('pred_F','F:4+')]:
        b = (dg[col] - t).sum() / t.sum() * 100
        row[lbl] = round(b, 1)
    grs_rows.append(row)
gdf = pd.DataFrame(grs_rows).sort_values('Мод', ascending=False)
print(gdf.to_string(index=False))
print()

# ── Коефіцієнти по місяцях і GRS (детальна табличка для кращого варіанту) ─────
best = 'pred_E'  # замінити якщо інший найкращий
print(f"=== Деталі по місяцях для кращого варіанту E (bias m³) ===")
for m in sorted(df['month'].unique()):
    dm = df[df['month'] == m]
    total_pred = dm[best].sum()
    total_fact = dm['fact_billing'].sum()
    print(f"  {MONTH_UA[m]:3s}: pred={total_pred:>10,.0f}  fact={total_fact:>10,.0f}  bias={total_pred-total_fact:>+9,.0f}  ({(total_pred-total_fact)/total_fact*100:+.1f}%)")
print()

# ── Зберегти ──────────────────────────────────────────────────────────────────
with pd.ExcelWriter('data/village_approaches.xlsx', engine='openpyxl') as xw:
    summary.to_excel(xw, sheet_name='Summary', index=False)
    pd.DataFrame(month_rows).to_excel(xw, sheet_name='By_month', index=False)
    gdf.to_excel(xw, sheet_name='By_GRS', index=False)
    df.to_excel(xw, sheet_name='Detail', index=False)
print("Збережено: data/village_approaches.xlsx")

# ── Графік: bias% по місяцях для всіх варіантів ──────────────────────────────
mdf = pd.DataFrame(month_rows).set_index('Міс')
fig, ax = plt.subplots(figsize=(12, 5))
colors = ['#888','#2196F3','#FF9800','#4CAF50','#E91E63','#9C27B0']
for (col, c) in zip(['A:RT','B:Scl','C:LOO','D:Vol','E:Stb','F:4+'], colors):
    ax.plot(mdf.index, mdf[col], marker='o', label=col, color=c, lw=1.5)
ax.axhline(0, color='k', lw=0.8, linestyle='--')
ax.set_title('Bias % по місяцях — village GRS (6 підходів)', fontsize=11)
ax.set_ylabel('Bias %')
ax.legend(fontsize=8)
ax.grid(True, alpha=0.3)
plt.tight_layout()
plt.savefig('data/village_approaches_bias.png', dpi=130, bbox_inches='tight')
print("Збережено: data/village_approaches_bias.png")
