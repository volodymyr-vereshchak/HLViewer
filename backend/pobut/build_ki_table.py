"""
build_ki_table.py

Будуємо таблицю коефіцієнтів масштабу k_i для кожного ОП-споживача.

Логіка:
  P(m)   = глобальний удільний профіль [м³/м²/місяць] з модемів
  k_i    = actual_heating / (area_i × ΣP(m)) — наскільки реальне споживання
            споживача відрізняється від "типового" (k=1 → типовий)

  Для перевірки: нормалізована форма білінгового профілю
    p_norm_i(m) = billing_i(m) / mean(billing_i for heating months)
  порівнюємо з P_modem_norm = P(m) / mean(P(m) for heating months)
  → якщо corr ≥ 0.80, споживач "слідує" глобальному профілю

  Прогноз зима:
    vol_i(m) = k_i × area_i × P(m)

Виходи:
  data/profiles/ki_heating_consumers.csv   ← таблиця k_i per consumer (Config K input)
  data/winter_billing_profiles.xlsx        ← аналіз профілів
"""
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd

from pobut_predictor import (
    PobUtPredictor, CT_PRIV, HEATING_MONTHS, GROUP_MERGE, SKIP_GROUPS, META_COLS,
)

MODEM_FILE = 'data/input/profile_pobut_daily_result.csv'
SUBS_FILE  = 'data/input/all_pobut_enriched.csv'
KI_CSV     = 'data/profiles/ki_heating_consumers.csv'
OUT_EXCEL  = 'data/winter_billing_profiles.xlsx'

os.makedirs('data/profiles', exist_ok=True)

HEATING_M  = [10, 11, 12, 1, 2, 3, 4]  # опалювальний сезон Oct→Apr
MONTH_ENG  = {
    'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,
    'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12,
}
MONTH_UA   = {
    1:'Січ',2:'Лют',3:'Бер',4:'Кві',5:'Тра',6:'Чер',
    7:'Лип',8:'Сер',9:'Вер',10:'Жов',11:'Лис',12:'Гру',
}

MIN_AREA      = 10.0
MAX_AREA      = 500.0
CORR_THRESH   = 0.80    # профіль вважається "схожим"
K_MAX_CLIP    = 5.0     # к_i > 5 → аутлайєр (використовуємо GRS-fallback)
MIN_HEAT_BILL = 2       # мін. місяців з ненульовим білінгом для надійного k_i

# ──────────────────────────────────────────────────────────────────────────────
# Глобальний P(m) з модемних даних  (відтворюємо з попереднього аналізу)
# ──────────────────────────────────────────────────────────────────────────────
# Значення з analyze_winter_profiles.py (медіана по всіх ОП-модемах)
P_MODEM = {
    1: 4.3167,
    2: 4.3717,
    3: 2.7025,
    4: 1.3619,
    10: 1.1902,
    11: 2.7532,
    12: 3.9867,
}
P_SUM   = sum(P_MODEM[m] for m in HEATING_M)        # ~20.68 м³/м²/сезон
P_MEAN  = P_SUM / len(HEATING_M)                    # середнє по місяцях сезону
P_NORM  = {m: P_MODEM[m] / P_MEAN for m in HEATING_M}  # нормалізовано до mean=1

print("=" * 65)
print(f"P(m) modem [м³/м²]:  (mean={P_MEAN:.4f})")
for m in HEATING_M:
    print(f"  {MONTH_UA[m]:4s}: P={P_MODEM[m]:.4f}  P_norm={P_NORM[m]:.3f}")
print(f"  Σ P(m) за сезон = {P_SUM:.4f} м³/м²")


# ──────────────────────────────────────────────────────────────────────────────
# Завантаження білінгу
# ──────────────────────────────────────────────────────────────────────────────
print("\nЗавантаження all_pobut_enriched.csv ...")
ap = pd.read_csv(SUBS_FILE, low_memory=False)
ap['account_id']      = pd.to_numeric(ap['account_id'], errors='coerce')
ap['appliance_group'] = ap['appliance_group'].astype(str).str.strip().replace(GROUP_MERGE)
ap['consumer_type']   = ap['consumer_type'].fillna(CT_PRIV)
ap['heated_area']     = pd.to_numeric(
    ap['heated_area'].astype(str).str.strip(), errors='coerce'
).fillna(55.0).clip(lower=1.0)
ap['has_OP']          = ap['appliance_group'].str.contains('ОП', na=False)
ap['gas_off']         = ap['gas_off'].notna()

# Модемні ID
raw_md = pd.read_csv(MODEM_FILE, sep=';', low_memory=False, encoding='utf-8', nrows=5)
raw_md_full = pd.read_csv(MODEM_FILE, sep=';', low_memory=False, encoding='utf-8',
                          usecols=[0])
modem_ids = set(pd.to_numeric(raw_md_full.iloc[:, 0], errors='coerce').dropna().astype(int))
print(f"  Модемних ID: {len(modem_ids):,}")

# ОП не-модемні, активні, площа OK
op_all = ap[
    ap['has_OP'] &
    ~ap['account_id'].isin(modem_ids) &
    ~ap['gas_off'] &
    ap['grs'].notna() &
    ~ap['appliance_group'].isin(SKIP_GROUPS) &
    (ap['heated_area'] >= MIN_AREA) &
    (ap['heated_area'] <= MAX_AREA)
].copy()
print(f"  ОП не-модемних (активних, площа OK): {len(op_all):,}")

# Зчитуємо місячний білінг в масив (7 опалювальних місяців)
heat_months_cols = {}  # m → column name
for eng, m in MONTH_ENG.items():
    if m not in HEATING_M:
        continue
    col = f'{eng}_2025'
    if col in ap.columns:
        heat_months_cols[m] = col

# Матриця білінгу: rows = consumers, cols = months
bill_matrix = np.zeros((len(op_all), len(HEATING_M)), dtype=np.float32)
idx_map     = {m: i for i, m in enumerate(HEATING_M)}

aid_index = op_all.set_index('account_id')
for m, col in heat_months_cols.items():
    vals = pd.to_numeric(ap.set_index('account_id')[col], errors='coerce')\
             .reindex(op_all['account_id'].values).fillna(0).values
    bill_matrix[:, idx_map[m]] = vals.astype(np.float32)

areas    = op_all['heated_area'].values
aids     = op_all['account_id'].values
grps     = op_all['appliance_group'].values
cts      = op_all['consumer_type'].values
grs_vals = op_all['grs'].values

print(f"  Отримано білінгову матрицю: {bill_matrix.shape}")


# ──────────────────────────────────────────────────────────────────────────────
# Обчислення k_i та кореляції для кожного споживача
# ──────────────────────────────────────────────────────────────────────────────
print("\nОбчислення k_i та кореляції профілів ...")

P_norm_arr = np.array([P_NORM[m] for m in HEATING_M])  # shape (7,)
P_arr      = np.array([P_MODEM[m] for m in HEATING_M])  # shape (7,)

# Кількість місяців з ненульовим білінгом
n_nonzero = (bill_matrix > 0).sum(axis=1)

# Питоме споживання: м³/м²  [n_cons × n_months]
specific = bill_matrix / areas[:, None]

# k_i = mean(specific[m]) / P_mean = mean(specific) / P_mean
mean_specific = specific.mean(axis=1)
k_i_raw = mean_specific / P_MEAN  # shape (n_cons,)

# Нормалізований профіль споживача: p_norm_i(m) = billing_i(m) / mean(billing_i_heating)
# Для кореляції area скорочується → можна рахувати без area
mean_bill_heat = bill_matrix.mean(axis=1)  # mid-season average billing

# Де mean > 0, рахуємо нормалізований профіль
valid_mask = (mean_bill_heat > 0) & (n_nonzero >= MIN_HEAT_BILL) & (areas >= MIN_AREA)

# Векторна кореляція: corr між P_norm і p_norm_i для кожного споживача
# corr = (Σ (x-x̄)(y-ȳ)) / (std_x × std_y)
# P_norm вже нормалізований (mean=1), p_norm_i нормалізований до mean(billing)
p_norm_bill = np.zeros_like(bill_matrix)  # row = normalized billing profile

valid_idx = np.where(valid_mask)[0]
p_norm_bill[valid_idx] = (
    bill_matrix[valid_idx] / mean_bill_heat[valid_idx, None]
)

# Корреляція Пірсона vectorized
def batch_pearsonr(X, y):
    """corr between each row of X and vector y. Both shape (n, k) and (k,)."""
    y_mean = y.mean()
    y_std  = y.std()
    if y_std == 0:
        return np.zeros(len(X))
    X_mean = X.mean(axis=1, keepdims=True)
    X_std  = X.std(axis=1)
    cov    = ((X - X_mean) * (y - y_mean)).mean(axis=1)
    X_std_safe = np.where(X_std > 0, X_std, 1.0)
    corr   = cov / (X_std_safe * y_std)
    corr   = np.where(X_std > 0, corr, 0.0)
    return corr.astype(np.float32)

corr_arr = np.full(len(op_all), np.nan, dtype=np.float32)
corr_arr[valid_idx] = batch_pearsonr(p_norm_bill[valid_idx], P_norm_arr)

print(f"  valid (≥{MIN_HEAT_BILL} міс. білінгу): {valid_mask.sum():,}  "
      f"({valid_mask.sum()/len(op_all)*100:.1f}%)")


# ──────────────────────────────────────────────────────────────────────────────
# Статистика профілів
# ──────────────────────────────────────────────────────────────────────────────
print("\n=== Статистика кореляції профілів (billing vs modem P) ===")
for (grp, ct), idx_g in pd.Series(
    list(zip(grps, cts)), name='gc'
).groupby(list(zip(grps, cts))).groups.items() if False else []:
    pass

# Grouped stats
gc_arr = list(zip(grps, cts))
gc_df  = pd.DataFrame({
    'account_id': aids,
    'grs': grs_vals,
    'appliance_group': grps,
    'consumer_type': cts,
    'heated_area': areas,
    'k_i': k_i_raw,
    'corr': corr_arr,
    'n_nonzero': n_nonzero,
    'mean_bill_heat': mean_bill_heat,
    'valid': valid_mask,
})

print(f"\n  Кореляція білінгового профілю з P_modem (серед valid):")
grp_stats = []
for (g, c), sub in gc_df[gc_df['valid']].groupby(['appliance_group', 'consumer_type']):
    corr_good  = (sub['corr'] >= CORR_THRESH).mean() * 100
    corr_med   = sub['corr'].median()
    k_med      = sub['k_i'].median()
    k_mean     = sub['k_i'].mean()
    k_trim     = sub['k_i'].clip(upper=K_MAX_CLIP).mean()
    n          = len(sub)
    grp_stats.append({
        'appliance_group': g, 'consumer_type': c, 'n': n,
        'corr_median': round(corr_med, 3),
        'corr_pct_good': round(corr_good, 1),
        'k_median': round(k_med, 3),
        'k_mean': round(k_mean, 3),
        'k_trimmed_mean': round(k_trim, 3),
    })
    print(f"  ({g:12s}, {c[:15]:15s}) n={n:,}  "
          f"corr_med={corr_med:.3f}  corr≥{CORR_THRESH}: {corr_good:.1f}%  "
          f"k_med={k_med:.3f}  k_mean={k_mean:.3f}  k_trim={k_trim:.3f}")

grp_stats_df = pd.DataFrame(grp_stats)


# ──────────────────────────────────────────────────────────────────────────────
# Медіана k_i по GRS × group (для fallback)
# ──────────────────────────────────────────────────────────────────────────────
print("\n=== GRS-рівні медіани k_i (для non-billing fallback) ===")
valid_ki = gc_df[
    gc_df['valid'] &
    gc_df['k_i'].notna() &
    (gc_df['k_i'] > 0) &
    (gc_df['k_i'] <= K_MAX_CLIP)   # без аутлайєрів
].copy()

ki_grs = (
    valid_ki.groupby(['grs', 'appliance_group', 'consumer_type'])['k_i']
    .agg(
        n='count',
        median_k='median',
        mean_k='mean',
        p25_k=lambda x: x.quantile(0.25),
        p75_k=lambda x: x.quantile(0.75),
        p10_k=lambda x: x.quantile(0.10),
        p90_k=lambda x: x.quantile(0.90),
    )
    .reset_index()
)
print(f"  Комірок (grs, grp, ct): {len(ki_grs):,}  ГРС: {ki_grs['grs'].nunique():,}")

# Global group medians (fallback якщо мало даних по ГРС)
ki_global = (
    valid_ki.groupby(['appliance_group', 'consumer_type'])['k_i']
    .agg(n=('count'), median_k=('median'), mean_k=('mean'))
    .reset_index()
    .rename(columns={'median_k': 'global_median_k', 'mean_k': 'global_mean_k'})
)

FOCUS_GRS = [
    'С-1(Запоріж) ГРС-1,3 кільце',
    'ГРС (2) м.Запоріжжя',
    'С-2 (Вільнянс) ГРС Вільнянськ',
]
print("\n  Фокус-ГРС (медіана k_i):")
for grs in FOCUS_GRS:
    sub = ki_grs[ki_grs['grs'] == grs].sort_values('n', ascending=False)
    print(f"  {grs}:")
    for _, r in sub.head(4).iterrows():
        print(f"    ({r['appliance_group']:12s}, {r['consumer_type'][:15]:15s})  "
              f"n={r['n']:,}  k_med={r['median_k']:.3f}  "
              f"IQR=[{r['p25_k']:.2f},{r['p75_k']:.2f}]")


# ──────────────────────────────────────────────────────────────────────────────
# Billing profile median by group (shape comparison)
# ──────────────────────────────────────────────────────────────────────────────
print("\n=== Форма білінгового профілю vs P_modem ===")
# Для кожного місяця: медіана specific_m по групах
bill_profile_rows = []
for mi, m in enumerate(HEATING_M):
    spec_m = specific[:, mi]
    for (g, c), sub in gc_df[gc_df['valid']].groupby(['appliance_group', 'consumer_type']):
        idx = sub.index
        vals = spec_m[idx]
        vals = vals[vals > 0]
        if len(vals) < 10:
            continue
        bill_profile_rows.append({
            'appliance_group': g, 'consumer_type': c, 'month': m,
            'n': len(vals),
            'median_sp': np.median(vals),
            'mean_sp': np.mean(vals),
            'P_modem': P_MODEM[m],
            'ratio': np.median(vals) / P_MODEM[m],
        })

bill_profile_df = pd.DataFrame(bill_profile_rows)

# Normalize billing profile shape (mean=1) and compare with P_norm
for (g, c), sub in bill_profile_df.groupby(['appliance_group', 'consumer_type']):
    sub_m = sub.set_index('month')['median_sp']
    common = [m for m in HEATING_M if m in sub_m.index]
    if len(common) < 4:
        continue
    bill_mean = sub_m[common].mean()
    ratio_to_modem = bill_mean / P_MEAN
    print(f"  ({g:12s}, {c[:15]:15s}): "
          f"billing_mean={bill_mean:.4f}  modem_mean={P_MEAN:.4f}  "
          f"ratio={ratio_to_modem:.3f}")
    vals_line = "  ".join(
        f"{MONTH_UA[m]}:{sub_m.get(m,np.nan):.3f}(×{sub_m.get(m,np.nan)/P_MODEM[m]:.2f})"
        for m in HEATING_M if m in sub_m.index
    )
    print(f"    {vals_line}")


# ──────────────────────────────────────────────────────────────────────────────
# Формуємо k_i таблицю для Config K
# ──────────────────────────────────────────────────────────────────────────────
print("\nФормуємо ki_heating_consumers.csv ...")

# Якщо k_i > K_MAX_CLIP → outlier, буде окремо оброблятися або fallback
ki_table = gc_df[gc_df['valid']].copy()
ki_table['k_i_clipped'] = ki_table['k_i'].clip(upper=K_MAX_CLIP)
ki_table['is_outlier']  = ki_table['k_i'] > K_MAX_CLIP
ki_table['corr_ok']     = ki_table['corr'] >= CORR_THRESH
ki_table['source']      = 'billing'

# Merge GRS-fallback: consumers без valid k_i отримають GRS-median
all_op = gc_df.copy()
no_ki  = all_op[~all_op['valid'] | all_op['k_i'].isna()].copy()
no_ki['k_i'] = np.nan
no_ki = no_ki.merge(
    ki_grs[['grs', 'appliance_group', 'consumer_type', 'median_k']],
    on=['grs', 'appliance_group', 'consumer_type'],
    how='left',
)
no_ki = no_ki.merge(
    ki_global[['appliance_group', 'consumer_type', 'global_median_k']],
    on=['appliance_group', 'consumer_type'],
    how='left',
)
no_ki['k_i_clipped'] = no_ki['median_k'].fillna(no_ki['global_median_k']).fillna(0.85)
no_ki['is_outlier']  = False
no_ki['corr_ok']     = False
no_ki['source']      = 'grs_fallback'
no_ki = no_ki.drop(columns=['median_k', 'global_median_k'], errors='ignore')

# Consumers з valid k_i але без billing fallback — get GRS k for comparison
ki_table = ki_table.merge(
    ki_grs[['grs', 'appliance_group', 'consumer_type', 'median_k']].rename(
        columns={'median_k': 'k_grs_median'}),
    on=['grs', 'appliance_group', 'consumer_type'],
    how='left',
)

save_cols = ['account_id', 'grs', 'appliance_group', 'consumer_type',
             'heated_area', 'k_i', 'k_i_clipped', 'is_outlier', 'corr_ok',
             'n_nonzero', 'source']

ki_full = pd.concat([
    ki_table[save_cols],
    no_ki[[c for c in save_cols if c in no_ki.columns]],
], ignore_index=True)

ki_full.to_csv(KI_CSV, index=False, encoding='utf-8')
print(f"  Збережено: {KI_CSV}  ({len(ki_full):,} рядків)")
print(f"  billing k_i: {(ki_full['source']=='billing').sum():,}  "
      f"grs_fallback: {(ki_full['source']=='grs_fallback').sum():,}")
print(f"  outliers (k>{K_MAX_CLIP}): {ki_full['is_outlier'].sum():,}  "
      f"({ki_full['is_outlier'].mean()*100:.2f}%)")
n_corr_ok = (ki_full['corr_ok'] == True).sum()
print(f"  corr OK: {n_corr_ok:,}  ({n_corr_ok/len(ki_full)*100:.1f}%)")


# ──────────────────────────────────────────────────────────────────────────────
# Excel
# ──────────────────────────────────────────────────────────────────────────────
print(f"\nЗберігаємо {OUT_EXCEL} ...")

# Pivot: billing profile shape vs modem (wide format)
bp_pivot = bill_profile_df.pivot_table(
    index=['appliance_group', 'consumer_type'],
    columns='month',
    values=['median_sp', 'ratio'],
)
bp_pivot.columns = [f'{v}_{MONTH_UA[int(m)]}' for v, m in bp_pivot.columns]
bp_pivot = bp_pivot.reset_index()

with pd.ExcelWriter(OUT_EXCEL, engine='openpyxl') as xw:
    # 1. Shape comparison
    bp_pivot.to_excel(xw, sheet_name='Форма_білінг_vs_модем', index=False)

    # 2. P_modem reference
    p_ref = pd.DataFrame([
        {'month': m, 'month_ua': MONTH_UA[m],
         'P_modem': P_MODEM[m], 'P_norm': P_NORM[m]}
        for m in HEATING_M
    ])
    p_ref.to_excel(xw, sheet_name='P_modem_reference', index=False)

    # 3. Group stats
    grp_stats_df.to_excel(xw, sheet_name='Статистика_груп', index=False)

    # 4. Ki by GRS
    ki_grs.sort_values(['grs', 'appliance_group']).to_excel(
        xw, sheet_name='Ki_по_ГРС', index=False)

    # 5. Global ki
    ki_global.to_excel(xw, sheet_name='Ki_global', index=False)

    # 6. Full ki table (top 10K by outlier status)
    ki_full.sort_values(['is_outlier', 'k_i'], ascending=[False, False]).head(
        10000).to_excel(xw, sheet_name='Ki_table_sample', index=False)

    # 7. Profile details per month per group
    bill_profile_df.to_excel(xw, sheet_name='Профілі_деталі', index=False)

print(f"[OK] {OUT_EXCEL}")

print("\n" + "=" * 65)
print("ПІДСУМОК: Формула Config K (зима)")
print("=" * 65)
print("  vol_i(m) = k_i × area_i × P(m)")
print(f"  P(m) = глобальний модемний профіль [м³/м²]")
print(f"  k_i  = billing_heat_sum / (area_i × {P_SUM:.2f} м³/м²/сезон)")
print(f"  Fallback: GRS-медіана k_i (де немає білінгу)")
print(f"  Outliers (k > {K_MAX_CLIP}): {ki_full['is_outlier'].sum():,} споживачів "
      f"→ clamp k = {K_MAX_CLIP}")
print("\n=== DONE ===")
