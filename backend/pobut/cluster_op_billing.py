"""
cluster_op_billing.py

K-Means кластеризація ОП-абонентів на billing-даних.
Ознаки: log10(heated_area), log10(specific)
Мета: знайти природні групи в scatter'і area vs specific.
"""
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import warnings
warnings.filterwarnings('ignore')

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.cm as cm
import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import StandardScaler

from pobut_predictor import GROUP_MERGE

SUBS_FILE = "data/input/all_pobut_enriched.csv"
OUT_SCATTER = "data/cluster_billing_scatter.png"
OUT_ELBOW   = "data/cluster_billing_elbow.png"

MONTH_COLS = ['jan_2025','feb_2025','mar_2025','apr_2025','may_2025','jun_2025',
              'jul_2025','aug_2025','sep_2025','oct_2025','nov_2025','dec_2025']
ABS_LOW   = 5.0
Y_CAP_PCT = 0.99
K_RANGE   = range(2, 9)

# ── 1. Дані (ті самі що в scatter_op_heating.py) ──────────────────────────────
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
ref = op.groupby(['grs','consumer_type'])['specific'].median().rename('ref_median').reset_index()
op = op.merge(ref, on=['grs','consumer_type'], how='left')
op['ratio_to_ref'] = op['specific'] / np.maximum(op['ref_median'], 1)

def classify(r):
    if r < 0.30: return 'Критичний'
    elif r < 0.40: return 'Середній'
    elif r < 0.50: return 'Низький'
    else: return 'Норма'
op['level'] = op['ratio_to_ref'].apply(classify)

print(f"  N={len(op):,}")

# ── 2. Ознаки для кластеризації ────────────────────────────────────────────────
print("\n=== 2. Підготовка ознак ===")

# Відсікаємо верхній хвіст перед кластеризацією (щоб викиди не тягнули кластери)
cap_specific = op['specific'].quantile(Y_CAP_PCT)
cap_area     = op['heated_area'].quantile(Y_CAP_PCT)
op_cl = op[(op['specific'] <= cap_specific) & (op['heated_area'] <= cap_area)].copy()
print(f"  Після відсікання хвостів (p99): N={len(op_cl):,} "
      f"(відсічено {len(op)-len(op_cl):,})")

X = np.column_stack([
    np.log10(op_cl['heated_area'].values),
    np.log10(op_cl['specific'].values),
])
scaler = StandardScaler()
X_sc = scaler.fit_transform(X)

# ── 3. Підбір k (elbow + silhouette) ──────────────────────────────────────────
print("\n=== 3. Підбір k ===")
inertias, sils = [], []
for k in K_RANGE:
    km = KMeans(n_clusters=k, n_init=20, random_state=42)
    labels = km.fit_predict(X_sc)
    inertias.append(km.inertia_)
    sil = silhouette_score(X_sc, labels, sample_size=10_000, random_state=42)
    sils.append(sil)
    print(f"  k={k}: inertia={km.inertia_:.0f}, silhouette={sil:.4f}")

best_k = list(K_RANGE)[int(np.argmax(sils))]
print(f"\n  Оптимальний k = {best_k} (silhouette={max(sils):.4f})")

# Elbow + silhouette plot
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 4))
ks = list(K_RANGE)
ax1.plot(ks, inertias, 'o-', color='steelblue')
ax1.set_xlabel('k'); ax1.set_ylabel('Inertia'); ax1.set_title('Elbow')
ax1.grid(alpha=0.3)
ax2.plot(ks, sils, 'o-', color='tomato')
ax2.axvline(best_k, ls='--', color='gray', lw=1)
ax2.set_xlabel('k'); ax2.set_ylabel('Silhouette'); ax2.set_title('Silhouette score')
ax2.grid(alpha=0.3)
plt.tight_layout()
fig.savefig(OUT_ELBOW, dpi=130, bbox_inches='tight')
plt.close(fig)
print(f"  Збережено: {OUT_ELBOW}")

# ── 4. Фінальна кластеризація ──────────────────────────────────────────────────
print(f"\n=== 4. Кластеризація k={best_k} ===")
km_final = KMeans(n_clusters=best_k, n_init=40, random_state=42)
op_cl['cluster'] = km_final.fit_predict(X_sc)

# ── 5. Статистика кластерів ────────────────────────────────────────────────────
print("\n=== 5. Статистика кластерів ===")
LEVEL_ORDER = ['Критичний', 'Середній', 'Низький', 'Норма']
rows = []
for cid in sorted(op_cl['cluster'].unique()):
    cd = op_cl[op_cl['cluster'] == cid]
    level_pcts = {lvl: (cd['level'] == lvl).sum() / len(cd) * 100 for lvl in LEVEL_ORDER}
    rows.append({
        'Кластер': cid,
        'N': len(cd),
        'Медіана area, м²': cd['heated_area'].median(),
        'Медіана specific': cd['specific'].median(),
        'Медіана ratio':    cd['ratio_to_ref'].median(),
        **{f'% {lvl}': level_pcts[lvl] for lvl in LEVEL_ORDER},
    })

stats = pd.DataFrame(rows).set_index('Кластер')
print(stats.round(1).to_string())

# ── 6. Scatter plot ────────────────────────────────────────────────────────────
print("\n=== 6. Scatter plot ===")
PALETTE = ['#2196F3','#FF5722','#4CAF50','#9C27B0','#FF9800','#00BCD4','#E91E63','#607D8B']

fig, axes = plt.subplots(1, 2, figsize=(16, 7))
titles = ['Приватний сектор', 'Багатоквартирний сектор']

for ax, ct in zip(axes, titles):
    ct_data = op_cl[op_cl['consumer_type'] == ct]
    ct_cap  = ct_data['specific'].quantile(Y_CAP_PCT)

    for cid in sorted(ct_data['cluster'].unique()):
        cd = ct_data[ct_data['cluster'] == cid]
        color = PALETTE[cid % len(PALETTE)]
        med_sp = cd['specific'].median()
        med_ar = cd['heated_area'].median()
        pct_crit = (cd['level'] == 'Критичний').mean() * 100
        label = (f'К{cid}  N={len(cd):,}\n'
                 f'area={med_ar:.0f}м²  sp={med_sp:.1f}\n'
                 f'крит={pct_crit:.0f}%')
        ax.scatter(cd['heated_area'], cd['specific'],
                   c=color, alpha=0.25, s=7, linewidths=0,
                   label=label, zorder=2)

    ax.axhline(ABS_LOW, color='red', lw=1.5, ls=':', zorder=5,
               label=f'Критерій D = {ABS_LOW}')
    ax.axhline(ct_data['specific'].median(), color='white', lw=1.5, ls='--',
               alpha=0.7, zorder=5,
               label=f'Медіана = {ct_data["specific"].median():.1f}')

    ax.set_facecolor('#0D1626')
    ax.set_xscale('log')
    ax.set_ylim(0, ct_cap)
    ax.set_xlabel('Площа, м² (лог.)', color='#AABBCC', fontsize=11)
    ax.set_ylabel('м³/м²/рік', color='#AABBCC', fontsize=11)
    ax.set_title(f'{ct}  (N={len(ct_data):,})', color='white', fontsize=12)
    ax.tick_params(colors='#8899AA', labelsize=9)
    for sp in ax.spines.values(): sp.set_color('#2A3A5A')
    ax.grid(alpha=0.12, color='#445577')
    ax.legend(fontsize=7.5, framealpha=0.4, facecolor='#1A2340',
              edgecolor='#445577', labelcolor='white', loc='upper right',
              ncol=1)

fig.patch.set_facecolor('#0D1626')
fig.suptitle(f'K-Means кластеризація ОП-абонентів  (k={best_k}, log-ознаки)',
             color='white', fontsize=13, fontweight='bold')
plt.tight_layout()
fig.savefig(OUT_SCATTER, dpi=150, bbox_inches='tight', facecolor='#0D1626')
plt.close(fig)
print(f"  Збережено: {OUT_SCATTER}")
print("\nГотово.")
