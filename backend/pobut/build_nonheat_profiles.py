"""
build_nonheat_profiles.py

Профілі та коефіцієнти k_i для НЕ-опалювального споживання.

Ключова ідея: профіль P(m) беремо з МОДЕМНИХ даних (реальні читання),
k_i = billing_annual_i / P_modem_annual  — масштаб від індивідуального білінгу.

Групи:
  1. ПГ МКД    — весь рік (541 модем)
  2. ПГ,ВПГ МКД — весь рік (653 модема)
  3. ОП,ПГ / ОП,ПГ,ВПГ — літо (Тра-Вер): P(m) з модемів, k_i з летнього білінгу

Виходи:
  data/profiles/ki_nonheat_consumers.csv     ← k_i per consumer (Config K input)
  data/profiles/p_modem_nonheat.csv          ← P_modem(m) по групах
  data/nonheat_profiles.xlsx                 ← деталі, статистики
"""
import sys, io, os, calendar
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd

from pobut_predictor import (
    PobUtPredictor, CT_PRIV, HEATING_MONTHS, GROUP_MERGE, SKIP_GROUPS,
)

MODEM_FILE  = 'data/input/profile_pobut_daily_result.csv'
SUBS_FILE   = 'data/input/all_pobut_enriched.csv'
KI_CSV      = 'data/profiles/ki_nonheat_consumers.csv'
P_MODEM_CSV = 'data/profiles/p_modem_nonheat.csv'
OUT_EXCEL   = 'data/nonheat_profiles.xlsx'

os.makedirs('data/profiles', exist_ok=True)

META_COLS = ['account_id','gas_off','alternative','dacha','appliance_group',
             'consumer_type','profile_no','heated_area','total_area',
             'residents','residents_all','serial_no']
MONTH_ENG = {
    'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,
    'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12,
}
MONTH_UA = {
    1:'Січ',2:'Лют',3:'Бер',4:'Кві',5:'Тра',6:'Чер',
    7:'Лип',8:'Сер',9:'Вер',10:'Жов',11:'Лис',12:'Гру',
}

HEATING_M   = [10, 11, 12, 1, 2, 3, 4]
SUMMER_M    = [5, 6, 7, 8, 9]    # ОП не-опалювальні місяці
ALL_M       = list(range(1, 13))

MIN_DAYS_MONTHLY = 20    # мін. денних читань для місячної агрегації
MIN_BILL_MONTHS  = 3     # мін. місяців з ненульовим білінгом для k_i
K_MAX_CLIP       = 5.0


# ══════════════════════════════════════════════════════════════════════════════
# 1. Завантаження та обробка МОДЕМНИХ даних
# ══════════════════════════════════════════════════════════════════════════════
print("=" * 65)
print("Завантаження модемних даних ...")

raw = pd.read_csv(MODEM_FILE, sep=';', low_memory=False, encoding='utf-8')
raw.columns = META_COLS + list(raw.columns[len(META_COLS):])
raw['appliance_group'] = raw['appliance_group'].astype(str).str.strip().replace(GROUP_MERGE)
raw['consumer_type']   = raw['consumer_type'].fillna(CT_PRIV)

# Фільтр: активні, не-дача
active = raw[
    raw['gas_off'].isna() &
    raw['alternative'].isna() &
    raw['dacha'].isna()
].copy()

modem_ids = set(pd.to_numeric(raw.iloc[:, 0], errors='coerce').dropna().astype(int))
print(f"  Всього модемів (активних): {len(active)}")
print(active.groupby(['appliance_group','consumer_type']).size().to_string())

# Melt → long format
date_cols = [c for c in active.columns if c not in META_COLS]
long = active.melt(
    id_vars=['account_id', 'appliance_group', 'consumer_type'],
    value_vars=date_cols,
    var_name='date_str', value_name='cons',
)
long['cons'] = pd.to_numeric(long['cons'], errors='coerce')
long['date'] = pd.to_datetime(long['date_str'], format='%d.%m.%Y', errors='coerce')
long = long[long['date'].notna() & long['cons'].notna() & (long['cons'] >= 0)].copy()
long['month'] = long['date'].dt.month
long['year']  = long['date'].dt.year

# Spike removal: > 5× rolling-14-day median → NaN
long = long.sort_values(['account_id', 'date'])
long['roll_med'] = (
    long.groupby('account_id')['cons']
    .transform(lambda s: s.rolling(14, min_periods=3, center=True).median())
)
long.loc[(long['roll_med'] > 0) & (long['cons'] > 5 * long['roll_med']), 'cons'] = np.nan
long = long[long['cons'].notna()].copy()

# Місячна агрегація (нормалізована до повного місяця)
monthly = (
    long.groupby(['account_id', 'appliance_group', 'consumer_type', 'year', 'month'])
    .agg(cons_sum=('cons', 'sum'), n_days=('cons', 'count'))
    .reset_index()
)
monthly = monthly[monthly['n_days'] >= MIN_DAYS_MONTHLY].copy()
monthly['dim'] = monthly.apply(
    lambda r: calendar.monthrange(int(r['year']), int(r['month']))[1], axis=1
)
monthly['cons_norm'] = monthly['cons_sum'] / monthly['n_days'] * monthly['dim']

print(f"\n  Місячних записів (≥{MIN_DAYS_MONTHLY} днів): {len(monthly):,}")


# ══════════════════════════════════════════════════════════════════════════════
# 2. P_modem(m) — медіанний профіль з модемів по кожній групі
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 65)
print("Будуємо P_modem(m) для кожної групи ...")

# Профілі з прямих модемних даних (достатньо модемів)
PROFILE_SPEC_DIRECT = {
    # (grp, ct): [місяці]
    ('ПГ',     'Багатоквартирний сектор'): ALL_M,    # 541 modems — надійно
    ('ПГ,ВПГ', 'Багатоквартирний сектор'): ALL_M,    # 653 modems — надійно
    ('ОП,ПГ,ВПГ', 'Приватний сектор'):      SUMMER_M, # 943 modems — надійно
    ('ОП,ПГ,ВПГ', 'Багатоквартирний сектор'): SUMMER_M, # 90 modems — достатньо
    ('ОП,ПГ',     'Приватний сектор'):      SUMMER_M,  # 259 modems — буде проксі-корекція
}

# ОП,ПГ отримує профіль через проксі (крос-ратіо до ПГ/ПГ,ВПГ)
# Аналіз показав: shape ОП,ПГ ближча до ПГ,ВПГ (corr > 0.8),
# крос-ратіо стабільне (std < 0.07)
OP_PG_PROXY = {
    # (grp, ct): (proxy_grp, proxy_ct, cross_ratio)
    # Прив: ОП,ПГ Прив ≈ ПГ,ВПГ МКД × 0.62 (cross-ratio Тра-Вер: std=0.059)
    ('ОП,ПГ', 'Приватний сектор'):        ('ПГ,ВПГ', 'Багатоквартирний сектор', 0.620),
    # МКД: ОП,ПГ МКД ≈ ПГ МКД × 1.00 (cross-ratio ≈ 1.0, only 20 modems direct)
    ('ОП,ПГ', 'Багатоквартирний сектор'): ('ПГ',     'Багатоквартирний сектор', 0.994),
}

PROFILE_SPEC = {**PROFILE_SPEC_DIRECT,
                ('ОП,ПГ', 'Приватний сектор'):        SUMMER_M,
                ('ОП,ПГ', 'Багатоквартирний сектор'): SUMMER_M}

p_modem = {}   # (grp, ct, m) → median_m³_per_month
p_modem_stats = []

# Крок 1: прямі профілі з модемів (де вибірка надійна)
for (grp, ct), months in PROFILE_SPEC_DIRECT.items():
    sub = monthly[
        (monthly['appliance_group'] == grp) &
        (monthly['consumer_type'] == ct) &
        (monthly['month'].isin(months))
    ]
    n_cons = sub['account_id'].nunique()
    print(f"\n  ({grp}, {ct[:22]})  n_модемів={n_cons}, місяці={months}")

    for m in months:
        vals = sub[sub['month'] == m]['cons_norm']
        if len(vals) < 5:
            continue
        med = float(vals.median())
        p_modem[(grp, ct, m)] = med
        p_modem_stats.append({
            'appliance_group': grp, 'consumer_type': ct, 'month': m,
            'month_ua': MONTH_UA[m],
            'P_modem_median': round(med, 3),
            'P_modem_mean':   round(float(vals.mean()), 3),
            'n_modem_months':  len(vals),
            'source': 'direct',
        })

    vals_str = '  '.join(
        f"{MONTH_UA[m]}:{p_modem.get((grp,ct,m), np.nan):.1f}" for m in months
    )
    p_sum = sum(p_modem.get((grp,ct,m), 0) for m in months)
    print(f"  {vals_str}")
    print(f"  Σ P(m) = {p_sum:.1f} м³/{len(months)}міс  mean_m = {p_sum/len(months):.2f}")

# Крок 2: проксі-профілі для ОП,ПГ (недостатня вибірка модемів)
print("\n  --- Проксі-профілі для ОП,ПГ ---")
for (grp, ct), (proxy_grp, proxy_ct, cross_ratio) in OP_PG_PROXY.items():
    months = PROFILE_SPEC[(grp, ct)]
    print(f"\n  ({grp}, {ct[:22]})  ← {proxy_grp} × {cross_ratio}")
    for m in months:
        p_base = p_modem.get((proxy_grp, proxy_ct, m), np.nan)
        if np.isnan(p_base):
            continue
        val = p_base * cross_ratio
        p_modem[(grp, ct, m)] = val
        p_modem_stats.append({
            'appliance_group': grp, 'consumer_type': ct, 'month': m,
            'month_ua': MONTH_UA[m],
            'P_modem_median': round(val, 3),
            'P_modem_mean':   round(val, 3),
            'n_modem_months':  0,
            'source': f'proxy_{proxy_grp}×{cross_ratio}',
        })
    vals_str = '  '.join(
        f"{MONTH_UA[m]}:{p_modem.get((grp,ct,m), np.nan):.1f}" for m in months
    )
    p_sum = sum(p_modem.get((grp,ct,m), 0) for m in months)
    print(f"  {vals_str}")
    print(f"  Σ P(m) = {p_sum:.1f} м³/{len(months)}міс  mean_m = {p_sum/len(months):.2f}")

p_modem_df = pd.DataFrame(p_modem_stats)

# Зберігаємо для Config K
p_modem_df.to_csv(P_MODEM_CSV, index=False, encoding='utf-8')
print(f"\n  Збережено: {P_MODEM_CSV}  ({len(p_modem_df)} рядків)")


# ══════════════════════════════════════════════════════════════════════════════
# 3. Завантаження БІЛІНГУ
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 65)
print("Завантаження all_pobut_enriched.csv ...")

ap = pd.read_csv(SUBS_FILE, low_memory=False)
ap['account_id']      = pd.to_numeric(ap['account_id'], errors='coerce')
ap['appliance_group'] = ap['appliance_group'].astype(str).str.strip().replace(GROUP_MERGE)
ap['consumer_type']   = ap['consumer_type'].fillna(CT_PRIV)
ap['gas_off']         = ap['gas_off'].notna()
ap['has_OP']          = ap['appliance_group'].str.contains('ОП', na=False)

bill_cols = {}
for name, m in MONTH_ENG.items():
    col = f'{name}_2025'
    if col in ap.columns:
        ap[col] = pd.to_numeric(ap[col], errors='coerce').fillna(0)
        bill_cols[m] = col

print(f"  Місячних колонок: {len(bill_cols)}")


def get_billing_matrix(subset_df, months):
    mat = np.zeros((len(subset_df), len(months)), dtype=np.float32)
    for i, m in enumerate(months):
        col = bill_cols.get(m)
        if col and col in subset_df.columns:
            mat[:, i] = subset_df[col].values.astype(np.float32)
    return mat


# ══════════════════════════════════════════════════════════════════════════════
# 4. k_i для ПГ та ПГ,ВПГ (весь рік)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 65)
print("k_i для ПГ та ПГ,ВПГ (k_i = billing_annual / P_modem_annual)")
print("=" * 65)

ki_records = []

for (grp, ct), months in PROFILE_SPEC.items():
    if 'ОП' in grp:
        continue   # ОП — обробляємо окремо

    # P_modem_annual = сума медіан по всіх місяцях
    p_annual = sum(p_modem.get((grp, ct, m), 0) for m in months)
    if p_annual <= 0:
        print(f"  ПРОПУСКАЄМО ({grp}, {ct}): немає P_modem")
        continue

    # Вибірка не-модемних активних споживачів
    subset = ap[
        (ap['appliance_group'] == grp) &
        (ap['consumer_type'] == ct) &
        ~ap['account_id'].isin(modem_ids) &
        ~ap['gas_off'] &
        ap['grs'].notna()
    ].copy().reset_index(drop=True)

    if len(subset) == 0:
        print(f"  ПРОПУСКАЄМО ({grp}, {ct}): 0 не-модемних")
        continue

    print(f"\n  ({grp}, {ct[:22]})  n={len(subset):,}  P_modem_annual={p_annual:.1f} м³/рік")

    bill_mat = get_billing_matrix(subset, months)

    # Кількість місяців з ненульовим білінгом
    n_nonzero = (bill_mat > 0.5).sum(axis=1)

    # billing_annual = сума по всіх місяцях (навіть нулі включено — норма)
    bill_annual = bill_mat.sum(axis=1)

    # k_i = billing_annual / P_modem_annual
    k_i_raw = (bill_annual / p_annual).astype(np.float32)

    valid = (n_nonzero >= MIN_BILL_MONTHS) & (bill_annual > 0)

    print(f"  valid (≥{MIN_BILL_MONTHS} міс. білінгу): {valid.sum():,} ({valid.mean()*100:.1f}%)")
    if valid.sum() > 0:
        k = pd.Series(k_i_raw[valid])
        print(f"  k_i: median={k.median():.3f}  mean={k.mean():.3f}  "
              f"IQR=[{k.quantile(0.25):.2f}, {k.quantile(0.75):.2f}]  "
              f"outlier(>{K_MAX_CLIP}): {(k>K_MAX_CLIP).sum()}")

    for i in range(len(subset)):
        ki_records.append({
            'account_id':      subset.at[i, 'account_id'],
            'grs':             subset.at[i, 'grs'],
            'appliance_group': grp,
            'consumer_type':   ct,
            'k_i':             float(k_i_raw[i]) if valid[i] else np.nan,
            'k_i_clipped':     float(min(k_i_raw[i], K_MAX_CLIP)) if valid[i] else np.nan,
            'is_outlier':      bool(k_i_raw[i] > K_MAX_CLIP) if valid[i] else False,
            'n_nonzero':       int(n_nonzero[i]),
            'bill_sum':        float(bill_annual[i]),
            'p_modem_annual':  p_annual,
            'valid':           bool(valid[i]),
            'source':          'nonop_billing',
        })


# ══════════════════════════════════════════════════════════════════════════════
# 5. k_i для ОП-груп — літній компонент (Тра-Вер)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 65)
print("k_i для ОП літо (k_i = billing_summer / P_modem_summer)")
print("=" * 65)

for (grp, ct), months in PROFILE_SPEC.items():
    if 'ОП' not in grp:
        continue

    p_summer = sum(p_modem.get((grp, ct, m), 0) for m in months)
    if p_summer <= 0:
        print(f"  ПРОПУСКАЄМО ({grp}, {ct}): немає P_modem_summer")
        continue

    subset = ap[
        (ap['appliance_group'] == grp) &
        (ap['consumer_type'] == ct) &
        ~ap['account_id'].isin(modem_ids) &
        ~ap['gas_off'] &
        ap['grs'].notna()
    ].copy().reset_index(drop=True)

    if len(subset) == 0:
        continue

    print(f"\n  ({grp}, {ct[:22]})  n={len(subset):,}  P_modem_summer={p_summer:.1f} м³/5міс")

    bill_mat = get_billing_matrix(subset, months)
    n_nonzero = (bill_mat > 0.5).sum(axis=1)
    bill_summer = bill_mat.sum(axis=1)

    k_i_raw = (bill_summer / p_summer).astype(np.float32)
    valid = (n_nonzero >= MIN_BILL_MONTHS) & (bill_summer > 0)

    print(f"  valid (≥{MIN_BILL_MONTHS} міс. білінгу): {valid.sum():,} ({valid.mean()*100:.1f}%)")
    if valid.sum() > 0:
        k = pd.Series(k_i_raw[valid])
        print(f"  k_i_summer: median={k.median():.3f}  mean={k.mean():.3f}  "
              f"IQR=[{k.quantile(0.25):.2f}, {k.quantile(0.75):.2f}]  "
              f"outlier(>{K_MAX_CLIP}): {(k>K_MAX_CLIP).sum()}")

    for i in range(len(subset)):
        ki_records.append({
            'account_id':      subset.at[i, 'account_id'],
            'grs':             subset.at[i, 'grs'],
            'appliance_group': grp,
            'consumer_type':   ct,
            'k_i':             float(k_i_raw[i]) if valid[i] else np.nan,
            'k_i_clipped':     float(min(k_i_raw[i], K_MAX_CLIP)) if valid[i] else np.nan,
            'is_outlier':      bool(k_i_raw[i] > K_MAX_CLIP) if valid[i] else False,
            'n_nonzero':       int(n_nonzero[i]),
            'bill_sum':        float(bill_summer[i]),
            'p_modem_annual':  p_summer,
            'valid':           bool(valid[i]),
            'source':          'op_summer_billing',
        })


# ══════════════════════════════════════════════════════════════════════════════
# 6. GRS-fallback медіани k_i
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 65)
print("GRS-рівні медіани k_i (fallback) ...")

ki_df = pd.DataFrame(ki_records)

valid_ki = ki_df[
    ki_df['valid'] &
    ki_df['k_i'].notna() &
    (ki_df['k_i'] > 0) &
    (ki_df['k_i'] <= K_MAX_CLIP)
].copy()

ki_grs = (
    valid_ki.groupby(['grs', 'appliance_group', 'consumer_type'])['k_i']
    .agg(
        n='count',
        median_k='median',
        mean_k='mean',
        p25_k=lambda x: x.quantile(0.25),
        p75_k=lambda x: x.quantile(0.75),
    )
    .reset_index()
)

ki_global = (
    valid_ki.groupby(['appliance_group', 'consumer_type'])['k_i']
    .agg(n='count', median_k='median', mean_k='mean')
    .reset_index()
    .rename(columns={'median_k': 'global_median_k', 'mean_k': 'global_mean_k'})
)

print(f"  GRS-комірок: {len(ki_grs):,}   ГРС: {ki_grs['grs'].nunique():,}")
print("\n  Глобальні медіани k_i:")
for _, r in ki_global.iterrows():
    print(f"    ({r['appliance_group']:12s}, {r['consumer_type'][:22]:22s}): "
          f"k_med={r['global_median_k']:.3f}  n={r['n']:,}")

# Заповнюємо fallback для споживачів без valid k_i
no_ki = ki_df[~ki_df['valid']].copy()
if len(no_ki) > 0:
    no_ki = no_ki.merge(
        ki_grs[['grs', 'appliance_group', 'consumer_type', 'median_k']],
        on=['grs', 'appliance_group', 'consumer_type'], how='left',
    )
    no_ki = no_ki.merge(
        ki_global[['appliance_group', 'consumer_type', 'global_median_k']],
        on=['appliance_group', 'consumer_type'], how='left',
    )
    no_ki['k_i_clipped'] = no_ki['median_k'].fillna(no_ki['global_median_k']).fillna(1.0)
    no_ki['source'] = no_ki['source'] + '_grs_fallback'
    no_ki = no_ki.drop(columns=['median_k', 'global_median_k'], errors='ignore')

    ki_df = pd.concat([ki_df[ki_df['valid']], no_ki], ignore_index=True)


# ══════════════════════════════════════════════════════════════════════════════
# 7. Зберігаємо ki_nonheat_consumers.csv
# ══════════════════════════════════════════════════════════════════════════════
save_cols = ['account_id', 'grs', 'appliance_group', 'consumer_type',
             'k_i', 'k_i_clipped', 'is_outlier', 'n_nonzero', 'bill_sum',
             'p_modem_annual', 'valid', 'source']
ki_out = ki_df[[c for c in save_cols if c in ki_df.columns]]
ki_out.to_csv(KI_CSV, index=False, encoding='utf-8')

print(f"\n  Збережено: {KI_CSV}  ({len(ki_out):,} рядків)")
n_bill = (~ki_out['source'].str.contains('fallback', na=False)).sum()
n_fb   = ki_out['source'].str.contains('fallback', na=False).sum()
n_outl = ki_out['is_outlier'].sum()
print(f"  billing k_i: {n_bill:,}   grs_fallback: {n_fb:,}   outliers(>{K_MAX_CLIP}): {n_outl:,}")


# ══════════════════════════════════════════════════════════════════════════════
# 8. Excel
# ══════════════════════════════════════════════════════════════════════════════
print(f"\nЗберігаємо {OUT_EXCEL} ...")

# Порівняльна таблиця modem vs billing (для кожної групи)
compare_rows = []
for (grp, ct), months in PROFILE_SPEC.items():
    # Білінгові медіани (по не-модемних споживачах)
    subset = ap[
        (ap['appliance_group'] == grp) &
        (ap['consumer_type'] == ct) &
        ~ap['account_id'].isin(modem_ids) &
        ~ap['gas_off']
    ]
    for m in months:
        col = bill_cols.get(m)
        bill_med = float(subset[col][subset[col] > 0.5].median()) if col and len(subset) > 0 else np.nan
        p_mod = p_modem.get((grp, ct, m), np.nan)
        compare_rows.append({
            'appliance_group': grp, 'consumer_type': ct,
            'month': m, 'month_ua': MONTH_UA[m],
            'P_modem': round(p_mod, 3) if not np.isnan(p_mod) else np.nan,
            'billing_median': round(bill_med, 3) if not np.isnan(bill_med) else np.nan,
            'ratio_bill_modem': round(bill_med / p_mod, 3)
                if not np.isnan(bill_med) and not np.isnan(p_mod) and p_mod > 0 else np.nan,
        })

compare_df = pd.DataFrame(compare_rows)

with pd.ExcelWriter(OUT_EXCEL, engine='openpyxl') as xw:
    p_modem_df.sort_values(['appliance_group','consumer_type','month']).to_excel(
        xw, sheet_name='P_modem_профілі', index=False)
    compare_df.sort_values(['appliance_group','consumer_type','month']).to_excel(
        xw, sheet_name='Modem_vs_Billing', index=False)
    ki_global.to_excel(xw, sheet_name='Ki_global', index=False)
    ki_grs.sort_values(['appliance_group','grs']).to_excel(
        xw, sheet_name='Ki_по_ГРС', index=False)
    ki_out.sort_values(['appliance_group','consumer_type','is_outlier'],
                       ascending=[True,True,False]).head(15000).to_excel(
        xw, sheet_name='Ki_full_sample', index=False)

print(f"[OK] {OUT_EXCEL}")


# ══════════════════════════════════════════════════════════════════════════════
# 9. ПІДСУМОК
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 65)
print("ПІДСУМОК: P_modem(m) [м³/місяць]")
print("=" * 65)
print(f"\n  {'Група':12s} {'CT':18s} " + "  ".join(f"{MONTH_UA[m]:4s}" for m in ALL_M))
print("-" * 130)

for (grp, ct), months in PROFILE_SPEC.items():
    vals = "  ".join(
        f"{p_modem.get((grp,ct,m),np.nan):5.1f}" if (grp,ct,m) in p_modem else "  ---"
        for m in ALL_M
    )
    p_sum = sum(p_modem.get((grp,ct,m),0) for m in months)
    print(f"  {grp:12s} {ct[:18]:18s} {vals}  Σ={p_sum:.0f}")

print()
print("  k_i (глобальні медіани):")
for _, r in ki_global.iterrows():
    k_col = 'global_median_k'
    print(f"    ({r['appliance_group']:12s}, {r['consumer_type'][:22]:22s}): "
          f"k_med={r[k_col]:.3f}  → mid consumer = {r[k_col]:.2f}×P_modem")

print()
print("  Формула Config K:")
print("    ПГ / ПГ,ВПГ:  vol_i(m) = k_i × P_modem_pg(m)       [весь рік]")
print("    ОП літо:      vol_i(m) = k_i × P_modem_op_summer(m) [Тра-Вер]")
print("    ОП зима:      vol_i(m) = k_i_heat × area_i × P_modem_heat(m)")
print("\n=== DONE ===")
