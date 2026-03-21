"""
residual_op_analysis.py

Нормалізація питомого споживання через регресійний residual.
Замість ratio = specific / median — фітуємо лог-лог регресію:
    log10(specific) ~ log10(area)
Residual = відхилення від очікуваного споживання для даної площі.
Великий від'ємний residual → підозрілий абонент.
"""
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import warnings
warnings.filterwarnings('ignore')

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score

from pobut_predictor import GROUP_MERGE

SUBS_FILE  = "data/input/all_pobut_enriched.csv"
OUT_RESID  = "data/residual_scatter.png"
OUT_COMP   = "data/residual_vs_ratio.png"
OUT_CLUST  = "data/residual_cluster.png"

MONTH_COLS = ['jan_2025','feb_2025','mar_2025','apr_2025','may_2025','jun_2025',
              'jul_2025','aug_2025','sep_2025','oct_2025','nov_2025','dec_2025']
Y_CAP_PCT  = 0.99

# ── 1. Завантаження (ті самі дані) ────────────────────────────────────────────
print("=== 1. Завантаження ===")
ap = pd.read_csv(SUBS_FILE, low_memory=False)
ap['appliance_group'] = ap['appliance_group'].astype(str).str.strip().replace(GROUP_MERGE)
ap['has_OP'] = ap['appliance_group'].str.contains('ОП', na=False)
ap['heated_area'] = pd.to_numeric(ap['heated_area'], errors='coerce').fillna(55.0)
ap['heated_area'] = ap['heated_area'].where(ap['heated_area'] > 0, 55.0)
for c in MONTH_COLS:
    ap[c] = pd.to_numeric(ap[c], errors='coerce').fillna(0)
ap['annual'] = ap[MONTH_COLS].sum(axis=1)

op = ap[
    ap['has_OP'] & ap['gas_off'].isna() & ap['alternative'].isna() &
    ap['dacha'].isna() & ap['grs'].notna() & (ap['annual'] > 0)
].copy().reset_index(drop=True)

op['specific'] = op['annual'] / op['heated_area']

# Старий ratio (для порівняння)
ref = op.groupby(['grs','consumer_type'])['specific'].median().rename('ref_median').reset_index()
op = op.merge(ref, on=['grs','consumer_type'], how='left')
op['ratio_to_ref'] = op['specific'] / np.maximum(op['ref_median'], 1)

print(f"  N={len(op):,}")

# ── 2. Лог-лог регресія per consumer_type ────────────────────────────────────
print("\n=== 2. Регресія log(specific) ~ log(area) ===")

op['log_area']     = np.log10(op['heated_area'])
op['log_specific'] = np.log10(np.maximum(op['specific'], 0.01))
op['residual']     = np.nan

for ct in op['consumer_type'].unique():
    mask = op['consumer_type'] == ct
    x = op.loc[mask, 'log_area'].values
    y = op.loc[mask, 'log_specific'].values

    # Відсікаємо хвости перед регресією
    cap = np.quantile(y, Y_CAP_PCT)
    fit_mask = y <= cap
    slope, intercept, r, p, se = stats.linregress(x[fit_mask], y[fit_mask])

    predicted = slope * x + intercept
    resid = y - predicted
    op.loc[mask, 'residual'] = resid

    print(f"  {ct}:")
    print(f"    slope={slope:.3f}  intercept={intercept:.3f}  R²={r**2:.3f}")
    print(f"    Тлумачення: +10% площі → {(10**slope - 1) * 100:+.1f}% питомого")
    print(f"    N={mask.sum():,}")

# ── 3. Порівняння: residual vs ratio ─────────────────────────────────────────
print("\n=== 3. Кореляція residual vs log(ratio) ===")
op['log_ratio'] = np.log10(np.maximum(op['ratio_to_ref'], 0.001))
r, p = stats.pearsonr(op['residual'], op['log_ratio'])
print(f"  Pearson r={r:.4f} (p={p:.2e})")
print(f"  → residual пояснює {r**2*100:.1f}% дисперсії log(ratio)")
print(f"  → {(1-r**2)*100:.1f}% — те що residual бачить, а ratio — ні")

# ── 4. Пороги на residual (аналог рівнів підозрілості) ────────────────────────
print("\n=== 4. Розподіл residuals ===")
for q in [0.01, 0.05, 0.10, 0.20, 0.50, 0.80, 0.90]:
    print(f"  p{q*100:.0f}: {op['residual'].quantile(q):.3f}")

# Пороги: residual < -1 → критичний (10x нижче очікуваного)
# Відповідник до старих ratio-порогів, але в логарифмічній шкалі
R_CRIT = -1.0    # 10x нижче очікуваного
R_HIGH = -0.60   # ~4x нижче
R_LOW  = -0.35   # ~2.2x нижче

def classify_resid(r):
    if r < R_CRIT: return 'Критичний'
    elif r < R_HIGH: return 'Середній'
    elif r < R_LOW:  return 'Низький'
    else: return 'Норма'

op['level_resid'] = op['residual'].apply(classify_resid)

print("\n  Розподіл по рівнях (residual):")
for lvl in ['Норма','Низький','Середній','Критичний']:
    n = (op['level_resid'] == lvl).sum()
    print(f"  {lvl}: {n:,}  ({n/len(op)*100:.1f}%)")

# ── 5. Scatter: area vs residual (2 панелі) ───────────────────────────────────
print("\n=== 5. Scatter plot ===")
COLORS = {'Норма':'#888888','Низький':'#F5C518','Середній':'#FF8C00','Критичний':'#E32B2B'}
ZORDER = {'Норма':1,'Низький':2,'Середній':3,'Критичний':4}

fig, axes = plt.subplots(1, 2, figsize=(16, 7), facecolor='#0D1626')
for ax, ct in zip(axes, ['Приватний сектор','Багатоквартирний сектор']):
    ct_data = op[op['consumer_type'] == ct]
    ax.set_facecolor('#0D1626')

    for lvl in ['Норма','Низький','Середній','Критичний']:
        sub = ct_data[ct_data['level_resid'] == lvl]
        if sub.empty: continue
        n_lvl = len(sub)
        ax.scatter(sub['heated_area'], sub['residual'],
                   c=COLORS[lvl], alpha=0.25, s=7, linewidths=0,
                   label=f'{lvl}  (N={n_lvl:,})',
                   zorder=ZORDER[lvl])

    # Лінії порогів
    for thresh, lbl, clr in [(R_CRIT,'Критичний < -1.0','#E32B2B'),
                              (R_HIGH,'Середній < -0.60','#FF8C00'),
                              (R_LOW, 'Низький < -0.35', '#F5C518')]:
        ax.axhline(thresh, color=clr, lw=1.2, ls='--', alpha=0.7, zorder=5)

    ax.axhline(0, color='white', lw=1.0, ls=':', alpha=0.4, zorder=5,
               label='Очікуване (residual=0)')

    ax.set_xscale('log')
    ax.set_xlabel('Площа, м² (лог.)', color='#AABBCC', fontsize=11)
    ax.set_ylabel('Residual log(specific)', color='#AABBCC', fontsize=11)
    ax.set_title(f'{ct}  (N={len(ct_data):,})', color='white', fontsize=12)
    ax.tick_params(colors='#8899AA', labelsize=9)
    for sp in ax.spines.values(): sp.set_color('#2A3A5A')
    ax.grid(alpha=0.12, color='#445577')
    ax.legend(fontsize=8, framealpha=0.4, facecolor='#1A2340',
              edgecolor='#445577', labelcolor='white', loc='upper right')

fig.suptitle('Residual log(specific) vs площа — ефект розмаху прибраний',
             color='white', fontsize=13, fontweight='bold')
plt.tight_layout()
fig.savefig(OUT_RESID, dpi=150, bbox_inches='tight', facecolor='#0D1626')
plt.close(fig)
print(f"  Збережено: {OUT_RESID}")

# ── 6. Порівняння: хто змінив рівень? ────────────────────────────────────────
print("\n=== 6. Порівняння ratio vs residual ===")
LVL_OLD = {'Норма': op['ratio_to_ref'] >= 0.50,
            'Низький': (op['ratio_to_ref'] >= 0.40) & (op['ratio_to_ref'] < 0.50),
            'Середній': (op['ratio_to_ref'] >= 0.30) & (op['ratio_to_ref'] < 0.40),
            'Критичний': op['ratio_to_ref'] < 0.30}
for lvl, mask in LVL_OLD.items():
    op.loc[mask, 'level_ratio'] = lvl

cross = pd.crosstab(op['level_ratio'], op['level_resid'],
                    rownames=['ratio'], colnames=['residual'])
print(cross.to_string())

# Хто перейшов з Норми → Критичний (або навпаки)
new_crit = op[(op['level_ratio'] == 'Норма') & (op['level_resid'] == 'Критичний')]
new_ok   = op[(op['level_ratio'] == 'Критичний') & (op['level_resid'] == 'Норма')]
print(f"\n  Нові критичні (були Нормою): {len(new_crit):,}")
print(f"  Реабілітовані (були Критичними → тепер Норма): {len(new_ok):,}")

# ── 7. Scatter: ratio vs residual (порівняльний) ─────────────────────────────
fig, axes = plt.subplots(1, 2, figsize=(14, 6), facecolor='#0D1626')
for ax, ct in zip(axes, ['Приватний сектор','Багатоквартирний сектор']):
    ct_data = op[op['consumer_type'] == ct].copy()
    ax.set_facecolor('#0D1626')

    # Колір по розбіжності між методами
    same  = ct_data['level_ratio'] == ct_data['level_resid']
    worse = (ct_data['level_ratio'] == 'Норма') & (ct_data['level_resid'] == 'Критичний')
    better = (ct_data['level_ratio'] == 'Критичний') & (ct_data['level_resid'] == 'Норма')

    ax.scatter(ct_data.loc[same, 'ratio_to_ref'],
               ct_data.loc[same, 'residual'],
               c='#4488AA', alpha=0.2, s=5, linewidths=0, label=f'Збіг ({same.sum():,})', zorder=1)
    ax.scatter(ct_data.loc[worse, 'ratio_to_ref'],
               ct_data.loc[worse, 'residual'],
               c='#FF4444', alpha=0.6, s=15, linewidths=0,
               label=f'Нові критичні ({worse.sum():,})', zorder=3)
    ax.scatter(ct_data.loc[better, 'ratio_to_ref'],
               ct_data.loc[better, 'residual'],
               c='#44FF88', alpha=0.6, s=15, linewidths=0,
               label=f'Реабілітовані ({better.sum():,})', zorder=3)

    ax.axhline(R_CRIT, color='#FF4444', lw=1, ls='--', alpha=0.5)
    ax.axvline(0.30,   color='#FF4444', lw=1, ls='--', alpha=0.5)
    ax.set_xlabel('ratio_to_ref (старий метод)', color='#AABBCC', fontsize=10)
    ax.set_ylabel('residual (новий метод)', color='#AABBCC', fontsize=10)
    ax.set_title(f'{ct}', color='white', fontsize=11)
    ax.tick_params(colors='#8899AA', labelsize=8)
    for sp in ax.spines.values(): sp.set_color('#2A3A5A')
    ax.grid(alpha=0.12, color='#445577')
    ax.legend(fontsize=8, framealpha=0.4, facecolor='#1A2340',
              edgecolor='#445577', labelcolor='white')

fig.suptitle('Порівняння: ratio_to_ref vs residual\n'
             'Червоні — яких ratio пропустив, зелені — яких зайво підозрював',
             color='white', fontsize=12, fontweight='bold')
plt.tight_layout()
fig.savefig(OUT_COMP, dpi=150, bbox_inches='tight', facecolor='#0D1626')
plt.close(fig)
print(f"  Збережено: {OUT_COMP}")

# ── 8. Кластеризація на residuals ─────────────────────────────────────────────
print("\n=== 7. Кластеризація на residuals ===")
op_cl = op[op['specific'] <= op['specific'].quantile(Y_CAP_PCT)].copy()

X = op_cl[['log_area','residual']].values
from sklearn.preprocessing import StandardScaler
X_sc = StandardScaler().fit_transform(X)

best_k, best_sil = 2, -1
for k in range(2, 8):
    km = KMeans(n_clusters=k, n_init=20, random_state=42)
    lbl = km.fit_predict(X_sc)
    sil = silhouette_score(X_sc, lbl, sample_size=10_000, random_state=42)
    print(f"  k={k}: silhouette={sil:.4f}")
    if sil > best_sil:
        best_sil, best_k = sil, k

print(f"\n  Оптимальний k={best_k} (sil={best_sil:.4f})")
km_final = KMeans(n_clusters=best_k, n_init=40, random_state=42)
op_cl['cluster'] = km_final.fit_predict(X_sc)

print("\n  Статистика кластерів:")
PALETTE = ['#2196F3','#FF5722','#4CAF50','#9C27B0','#FF9800','#00BCD4','#E91E63']
rows = []
for cid in sorted(op_cl['cluster'].unique()):
    cd = op_cl[op_cl['cluster'] == cid]
    pct_crit_resid = (cd['level_resid'] == 'Критичний').mean() * 100
    pct_crit_ratio = (cd['level_ratio'] == 'Критичний').mean() * 100
    rows.append({
        'K': cid, 'N': len(cd),
        'Медіана area': cd['heated_area'].median(),
        'Медіана specific': cd['specific'].median(),
        'Медіана residual': cd['residual'].median(),
        '% крит(resid)': pct_crit_resid,
        '% крит(ratio)': pct_crit_ratio,
    })
    print(f"  К{cid}: N={len(cd):,}  "
          f"area={cd['heated_area'].median():.0f}  "
          f"specific={cd['specific'].median():.1f}  "
          f"residual={cd['residual'].median():.2f}  "
          f"крит(resid)={pct_crit_resid:.0f}%  крит(ratio)={pct_crit_ratio:.0f}%")

# Scatter кластерів
fig, axes = plt.subplots(1, 2, figsize=(16, 7), facecolor='#0D1626')
for ax, ct in zip(axes, ['Приватний сектор','Багатоквартирний сектор']):
    ct_data = op_cl[op_cl['consumer_type'] == ct]
    ax.set_facecolor('#0D1626')
    for cid in sorted(ct_data['cluster'].unique()):
        cd = ct_data[ct_data['cluster'] == cid]
        color = PALETTE[cid % len(PALETTE)]
        med_sp = cd['specific'].median()
        med_ar = cd['heated_area'].median()
        med_r  = cd['residual'].median()
        pct_c  = (cd['level_resid'] == 'Критичний').mean() * 100
        label  = f'К{cid} N={len(cd):,}\narea={med_ar:.0f} sp={med_sp:.1f}\nresid={med_r:.2f} крит={pct_c:.0f}%'
        ax.scatter(cd['heated_area'], cd['residual'],
                   c=color, alpha=0.25, s=7, linewidths=0,
                   label=label, zorder=2)

    ax.axhline(0, color='white', lw=1.0, ls=':', alpha=0.4, zorder=5)
    ax.axhline(R_CRIT, color='#FF4444', lw=1.2, ls='--', alpha=0.6, zorder=5)
    ax.set_xscale('log')
    ax.set_xlabel('Площа, м² (лог.)', color='#AABBCC', fontsize=11)
    ax.set_ylabel('Residual log(specific)', color='#AABBCC', fontsize=11)
    ax.set_title(f'{ct}  (N={len(ct_data):,})', color='white', fontsize=12)
    ax.tick_params(colors='#8899AA', labelsize=9)
    for sp in ax.spines.values(): sp.set_color('#2A3A5A')
    ax.grid(alpha=0.12, color='#445577')
    ax.legend(fontsize=7.5, framealpha=0.4, facecolor='#1A2340',
              edgecolor='#445577', labelcolor='white', loc='upper right')

fig.patch.set_facecolor('#0D1626')
fig.suptitle(f'K-Means (k={best_k}) на residuals — ефект розмаху прибраний',
             color='white', fontsize=13, fontweight='bold')
plt.tight_layout()
fig.savefig(OUT_CLUST, dpi=150, bbox_inches='tight', facecolor='#0D1626')
plt.close(fig)
print(f"  Збережено: {OUT_CLUST}")
print("\nГотово.")
