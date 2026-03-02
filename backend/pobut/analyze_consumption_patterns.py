"""
analyze_consumption_patterns.py

Шаг 1-3: Паттерны споживання → матчинг біллінгу → декомпозиція по ГРС

1. Кластеризація нормованих місячних профілів модемів (оптимальна к)
2. Матчинг кожного абонента біллінгу до найближчого паттерну
3. Декомпозиція: expected_monthly(i,m) = annual_billing(i) × pattern(cluster,m)
4. Агрегат по ГРС
"""
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import normalize
from pobut_predictor import GROUP_MERGE, CT_PRIV, META_COLS

MODEM_FILE = 'data/input/profile_pobut_daily_result.csv'
SUBS_FILE  = 'data/input/all_pobut_enriched.csv'
OUT_XLSX   = 'data/consumption_clusters.xlsx'

MONTH_ENG = {'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,
             'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12}
UA = {1:'Січ',2:'Лют',3:'Бер',4:'Кві',5:'Тра',6:'Чер',
      7:'Лип',8:'Сер',9:'Вер',10:'Жов',11:'Лис',12:'Гру'}

# ── 1. Модемні місячні профілі ────────────────────────────────────────────────
print("=== 1. Модемні дані → місячні профілі ===")
md = pd.read_csv(MODEM_FILE, sep=';', low_memory=False)
md.columns = META_COLS + list(md.columns[len(META_COLS):])
md['appliance_group'] = md['appliance_group'].astype(str).str.strip().replace(GROUP_MERGE)
md['consumer_type']   = md['consumer_type'].fillna(CT_PRIV)
md['has_OP']          = md['appliance_group'].str.contains('\u041e\u041f', na=False)
md['account_id']      = pd.to_numeric(md['account_id'], errors='coerce')
md = md[md['gas_off'].isna() & md['alternative'].isna() & md['dacha'].isna()]

date_cols = sorted([c for c in md.columns if isinstance(c, str) and c.count('.') == 2
                    and len(c) == 10 and c.endswith('.2025')],
                   key=lambda c: (int(c[6:]), int(c[3:5]), int(c[:2])))
for c in date_cols:
    md[c] = pd.to_numeric(md[c], errors='coerce').fillna(0)

# Місячні суми
monthly_data = {}
for m in range(1, 13):
    cols_m = [c for c in date_cols if int(c[3:5]) == m]
    monthly_data[m] = md[cols_m].sum(axis=1).values

md_m = pd.DataFrame(monthly_data, index=md.index)
md['annual_modem'] = md_m.sum(axis=1)

# Фільтр: активні модеми (річне > 0, жоден місяць не домінує > 60%)
md_active = md[md['annual_modem'] > 0].copy()
md_m_active = md_m.loc[md_active.index].copy()
md_active['max_frac'] = md_m_active.max(axis=1) / md_active['annual_modem']
md_active = md_active[md_active['max_frac'] < 0.60]

modem_ids = set(md_active['account_id'].dropna().astype(int))
print(f"  Активних модемів (після фільтру): {len(md_active):,}")

# Нормовані профілі (Σ=1)
md_m_clean = md_m_active.loc[md_active.index]
annual_vec  = md_active['annual_modem'].values
shapes_md   = md_m_clean.values / annual_vec[:, None]  # (N, 12)

print(f"  Форма матриці профілів: {shapes_md.shape}")

# ── 2. Оптимальна кількість кластерів ────────────────────────────────────────
print("\n=== 2. Пошук оптимальної кількості кластерів ===")
K_MIN, K_MAX = 2, 12
inertias, silhouettes = [], []

for k in range(K_MIN, K_MAX + 1):
    km = KMeans(n_clusters=k, random_state=42, n_init=20, max_iter=500)
    labels = km.fit_predict(shapes_md)
    inertias.append(km.inertia_)
    sil = silhouette_score(shapes_md, labels, sample_size=min(len(shapes_md), 3000))
    silhouettes.append(sil)
    print(f"  k={k:2d}:  inertia={km.inertia_:8.4f}  silhouette={sil:.4f}")

# Elbow: знаходимо точку максимального перегину (2-а похідна)
inertias_arr = np.array(inertias)
d1 = np.diff(inertias_arr)
d2 = np.diff(d1)
elbow_k = K_MIN + 1 + np.argmax(np.abs(d2))

best_sil_k = K_MIN + np.argmax(silhouettes)
print(f"\n  Elbow → k={elbow_k}")
print(f"  Best silhouette → k={best_sil_k}  (score={max(silhouettes):.4f})")

# Вибір: силует пріоритетніший для інтерпретованості
OPTIMAL_K = best_sil_k
if abs(silhouettes[elbow_k - K_MIN] - max(silhouettes)) < 0.02:
    OPTIMAL_K = elbow_k  # якщо близькі — беремо elbow (менше кластерів = краще для інтерпретації)
print(f"  → Використовуємо k={OPTIMAL_K}")

# ── 3. Фінальна кластеризація ─────────────────────────────────────────────────
print(f"\n=== 3. KMeans k={OPTIMAL_K} ===")
km_final = KMeans(n_clusters=OPTIMAL_K, random_state=42, n_init=50, max_iter=1000)
labels_md = km_final.fit_predict(shapes_md)
centers   = km_final.cluster_centers_  # (K, 12)

# Впорядковуємо кластери за зимовим споживанням (зима = nov-feb)
winter_idx = [0, 1, 10, 11]  # Jan, Feb, Nov, Dec
winter_share = centers[:, winter_idx].sum(axis=1)
order = np.argsort(winter_share)[::-1]  # descending winter
centers_ord = centers[order]
remap = {old: new for new, old in enumerate(order)}
labels_md_ord = np.array([remap[l] for l in labels_md])

print(f"\n  Паттерни (відсортовані за зимовою часткою):")
print(f"  {'Кластер':>10} {'N':>6} {'Зима%':>7} {'Літо%':>7} | " +
      "  ".join(f"{UA[m]:>4}" for m in range(1, 13)))
for c in range(OPTIMAL_K):
    mask = labels_md_ord == c
    n = mask.sum()
    p = centers_ord[c]
    winter_pct = p[winter_idx].sum() * 100
    summer_pct = p[[4,5,6,7,8]].sum() * 100
    monthly_str = "  ".join(f"{v*100:4.1f}" for v in p)
    print(f"  Паттерн {c+1:>2}:   {n:5}  {winter_pct:6.1f}%  {summer_pct:6.1f}% | {monthly_str}")

# Додаємо метадані кластерів
md_active = md_active.copy()
md_active['cluster'] = labels_md_ord
print(f"\n  ОП-розподіл по кластерах:")
for c in range(OPTIMAL_K):
    mask = labels_md_ord == c
    has_op = md_active.iloc[np.where(mask)[0]]['has_OP']
    print(f"  Паттерн {c+1}: ОП={has_op.sum()}/{mask.sum()} ({has_op.mean()*100:.0f}%)")

# ── 4. Матчинг біллінгу → паттерни ───────────────────────────────────────────
print("\n=== 4. Матчинг біллінгу → паттерни ===")
ap = pd.read_csv(SUBS_FILE, low_memory=False)
ap['account_id']      = pd.to_numeric(ap['account_id'], errors='coerce')
ap['appliance_group'] = ap['appliance_group'].astype(str).str.strip().replace(GROUP_MERGE)
ap['consumer_type']   = ap['consumer_type'].fillna(CT_PRIV)
ap['has_OP']          = ap['appliance_group'].str.contains('\u041e\u041f', na=False)
ap = ap[ap['grs'].notna()].copy()

bill_cols = {}
for eng, m in MONTH_ENG.items():
    col = f'{eng}_2025'
    if col in ap.columns:
        ap[col] = pd.to_numeric(ap[col], errors='coerce').fillna(0)
        bill_cols[m] = col

mc = [bill_cols[m] for m in range(1, 13)]
ap['annual_billing'] = ap[mc].sum(axis=1)

# Фільтр: тільки ненульовий річний біллінг
ap_active = ap[ap['annual_billing'].abs() > 0].copy()
print(f"  Абонентів з ненульовим білінгом: {len(ap_active):,}")

# Нормовані паттерни біллінгу
bill_matrix = ap_active[mc].values.astype(float)
annual_vec_ap = ap_active['annual_billing'].values

# Для матчингу: використовуємо абсолютні значення (від'ємні = перерозрахунки)
bill_abs = np.abs(bill_matrix)
annual_abs = bill_abs.sum(axis=1)

# Абоненти з хоча б одним ненульовим місяцем
valid_mask = annual_abs > 0
shapes_ap = np.zeros((len(ap_active), 12))
shapes_ap[valid_mask] = bill_abs[valid_mask] / annual_abs[valid_mask, None]

# Cosine similarity до центроїдів кластерів
# Нормуємо центроїди та профілі абонентів
centers_norm = normalize(centers_ord, norm='l2')
shapes_norm  = normalize(shapes_ap, norm='l2')

# Cosine similarity = dot product після нормування
sim_matrix   = shapes_norm @ centers_norm.T   # (N_subs, K)
best_cluster = np.argmax(sim_matrix, axis=1)
best_sim     = sim_matrix[np.arange(len(ap_active)), best_cluster]

ap_active = ap_active.copy()
ap_active['cluster']    = best_cluster
ap_active['similarity'] = best_sim

print(f"  Розподіл по паттернах (матчинг):")
for c in range(OPTIMAL_K):
    mask = ap_active['cluster'] == c
    n = mask.sum()
    med_sim = ap_active.loc[mask, 'similarity'].median()
    vol = ap_active.loc[mask, 'annual_billing'].sum() / 1e6
    print(f"  Паттерн {c+1}: {n:6} абон. ({n/len(ap_active)*100:.1f}%)  "
          f"sim_med={med_sim:.3f}  vol={vol:.2f} млн м³")

# ── 5. Декомпозиція: annual_billing × pattern → monthly expected ──────────────
print("\n=== 5. Декомпозиція за паттернами ===")

# Для кожного абонента: expected_monthly = annual_billing × pattern(cluster, m)
# Для модемних: використовуємо фактичні місячні дані
decomp = np.zeros((len(ap_active), 12))
for c in range(OPTIMAL_K):
    mask = (ap_active['cluster'].values == c)
    decomp[mask] = (ap_active['annual_billing'].values[mask, None] *
                    centers_ord[c][None, :])

# Для модемних абонентів — замінюємо фактичними даними з модемів
md_monthly_dict = {}
for idx, row in md_active.iterrows():
    aid = row['account_id']
    if pd.isna(aid):
        continue
    aid = int(aid)
    monthly = md_m_clean.loc[idx].values
    md_monthly_dict[aid] = monthly

ap_ids = ap_active['account_id'].values
for i, aid in enumerate(ap_ids):
    if pd.notna(aid) and int(aid) in md_monthly_dict:
        decomp[i] = md_monthly_dict[int(aid)]

print(f"  Модемних абонентів з реальними даними: "
      f"{sum(1 for aid in ap_ids if pd.notna(aid) and int(aid) in md_monthly_dict):,}")

# Додаємо місячні прогнози до датафрейму
for m in range(1, 13):
    ap_active[f'pred_m{m}'] = decomp[:, m-1]

# ── 6. Агрегат по ГРС ────────────────────────────────────────────────────────
print("\n=== 6. Агрегат по ГРС ===")
pred_cols = [f'pred_m{m}' for m in range(1, 13)]
grs_pred = ap_active.groupby('grs')[pred_cols + ['annual_billing']].sum()
grs_pred.columns = [UA[m] if col == f'pred_m{m}' else col
                    for m, col in zip(list(range(1,13))+[0], grs_pred.columns)]
grs_pred.columns = list(UA.values()) + ['annual_billing']
grs_pred['annual_pred'] = grs_pred[list(UA.values())].sum(axis=1)
grs_pred['diff_mln']    = (grs_pred['annual_pred'] - grs_pred['annual_billing']) / 1e6

print(f"  ГРС: {len(grs_pred)}")
top5 = grs_pred.sort_values('annual_billing', ascending=False).head(5)
print(f"\n  Топ-5 ГРС (млн м³):")
for grs, row in top5.iterrows():
    print(f"    {grs[:35]:35} billing={row['annual_billing']/1e6:.2f}  pred={row['annual_pred']/1e6:.2f}")

# ── 7. Аномальні абоненти (низька схожість з будь-яким паттерном) ─────────────
print("\n=== 7. Аномальні паттерни (потенційно підозрілі) ===")
low_sim = ap_active[ap_active['similarity'] < 0.7].copy()
print(f"  Абонентів з cosine similarity < 0.7: {len(low_sim):,} "
      f"({len(low_sim)/len(ap_active)*100:.1f}%)")
for thresh in [0.5, 0.6, 0.7, 0.8]:
    n = (ap_active['similarity'] < thresh).sum()
    print(f"    sim < {thresh}: {n:6} ({n/len(ap_active)*100:.1f}%)")

# ── 8. Збереження ─────────────────────────────────────────────────────────────
print(f"\n=== 8. Збереження {OUT_XLSX} ===")

# Лист 1: центроїди паттернів
pattern_df = pd.DataFrame(
    centers_ord,
    columns=[UA[m] for m in range(1, 13)]
)
pattern_df.insert(0, 'pattern', [f'Паттерн {i+1}' for i in range(OPTIMAL_K)])
for c in range(OPTIMAL_K):
    mask = labels_md_ord == c
    pattern_df.loc[c, 'n_modems'] = int(mask.sum())
    pattern_df.loc[c, 'op_pct']   = round(md_active.iloc[np.where(mask)[0]]['has_OP'].mean() * 100, 1)
    pattern_df.loc[c, 'winter_pct'] = round(centers_ord[c, [0,1,10,11]].sum() * 100, 1)
    pattern_df.loc[c, 'summer_pct'] = round(centers_ord[c, [4,5,6,7,8]].sum() * 100, 1)

# Лист 2: абоненти з кластером і декомпозицією
subs_out = ap_active[['account_id', 'grs', 'consumer_type', 'appliance_group',
                       'has_OP', 'annual_billing', 'cluster', 'similarity'] +
                      pred_cols].copy()
subs_out.columns = (['account_id', 'grs', 'consumer_type', 'appliance_group',
                     'has_OP', 'annual_billing', 'cluster', 'similarity'] +
                    [UA[m] for m in range(1, 13)])
subs_out['cluster'] = subs_out['cluster'] + 1
subs_out['similarity'] = subs_out['similarity'].round(3)
subs_out['annual_billing'] = subs_out['annual_billing'].round(1)

# Лист 3: ГРС агрегат
grs_out = grs_pred.reset_index()

# Лист 4: аномальні (sim < 0.7)
anom_out = subs_out[subs_out['similarity'] < 0.7].sort_values('similarity')

with pd.ExcelWriter(OUT_XLSX, engine='openpyxl') as w:
    pattern_df.round(4).to_excel(w, sheet_name='Patterns', index=False)
    subs_out.to_excel(w, sheet_name='Subscribers', index=False)
    grs_out.round(3).to_excel(w, sheet_name='GRS_monthly', index=False)
    anom_out.to_excel(w, sheet_name='Anomalous', index=False)

print(f"  Паттернів: {OPTIMAL_K} → лист Patterns")
print(f"  Абонентів: {len(subs_out):,} → лист Subscribers")
print(f"  ГРС: {len(grs_out)} → лист GRS_monthly")
print(f"  Аномальних (sim<0.7): {len(anom_out):,} → лист Anomalous")
print("\n=== Готово ===")
