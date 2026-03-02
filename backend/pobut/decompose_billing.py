"""
decompose_billing.py

Декомпозиція білінгу кожного абонента на компоненти:
  - non_heat_annual: річне споживання на готування/ГВП (summer_baseline * 12)
  - heat_annual:     річне споживання на опалення (annual_billing - non_heat_annual)

Для модемних абонентів: дані з фактичних добових вимірів.
Для не-модемних: оцінка через summer_baseline і heated_area.

Також будує модель очікуваного споживання:
  expected_annual = non_heat_annual + alpha * heated_area
де alpha калібрується з модемних ОП-абонентів.

Результат: data/billing_decomposition.xlsx
"""
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
from pobut_predictor import GROUP_MERGE, CT_PRIV, META_COLS

# ── Конфігурація ─────────────────────────────────────────────────────────────
SUBS_FILE   = 'data/input/all_pobut_enriched.csv'
MODEM_FILE  = 'data/input/profile_pobut_daily_result.csv'
OUT_XLSX    = 'data/billing_decomposition.xlsx'

MONTH_ENG = {'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,
             'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12}
SUMMER_MONTHS = [5, 6, 7, 8, 9]   # травень–вересень
MIN_HEAT_HA = 10                    # мінімальна площа ОП (м²) для розрахунку α
SB_RATIO_MAX = 2.0                  # фільтр для калібрування: summer_modem/baseline < 2.0

# ── 1. Завантаження модемних даних ────────────────────────────────────────────
print("=== 1. Модемні дані (профіль добового споживання 2025) ===")
md_raw = pd.read_csv(MODEM_FILE, sep=';', low_memory=False)
md_raw.columns = META_COLS + list(md_raw.columns[len(META_COLS):])
md_raw['appliance_group'] = md_raw['appliance_group'].astype(str).str.strip().replace(GROUP_MERGE)
md_raw['consumer_type']   = md_raw['consumer_type'].fillna(CT_PRIV)
md_raw['account_id']      = pd.to_numeric(md_raw['account_id'], errors='coerce')
md_raw['heated_area']     = pd.to_numeric(md_raw['heated_area'], errors='coerce').fillna(55)
md_raw['has_OP']          = md_raw['appliance_group'].str.contains('\u041e\u041f', na=False)

# Активні модемні абоненти
md_raw = md_raw[
    md_raw['gas_off'].isna() &
    md_raw['alternative'].isna() &
    md_raw['dacha'].isna()
].copy()

# Денні колонки 2025 (формат DD.MM.YYYY)
date_cols_2025 = [c for c in md_raw.columns
                  if isinstance(c, str) and c.count('.') == 2
                  and len(c) == 10 and c.endswith('.2025')]
date_cols_2025.sort(key=lambda c: (int(c[6:]), int(c[3:5]), int(c[:2])))  # YYYY,MM,DD

for col in date_cols_2025:
    md_raw[col] = pd.to_numeric(md_raw[col], errors='coerce').fillna(0)

# Місячні суми для модемів
monthly_sums = {}
for m in range(1, 13):
    cols_m = [c for c in date_cols_2025 if int(c[3:5]) == m]
    monthly_sums[m] = md_raw[cols_m].sum(axis=1).values

md = md_raw[['account_id', 'appliance_group', 'consumer_type', 'heated_area', 'has_OP']].copy()
for m in range(1, 13):
    md[f'm{m}'] = monthly_sums[m]

md['annual_modem'] = sum(md[f'm{m}'] for m in range(1, 13))
md['summer_modem_avg'] = sum(md[f'm{m}'] for m in SUMMER_MONTHS) / len(SUMMER_MONTHS)

modem_ids = set(md_raw['account_id'].dropna().astype(int))
print(f"  Всього модемних записів: {len(md_raw):,}")
print(f"  Активних модемних IDs:   {len(modem_ids):,}")
print(f"    з яких ОП: {md['has_OP'].sum():,}")
print(f"  Денних колонок 2025: {len(date_cols_2025)}")

# ── 2. Завантаження білінгу ───────────────────────────────────────────────────
print("\n=== 2. Білінг абонентів ===")
ap = pd.read_csv(SUBS_FILE, low_memory=False)
ap['account_id']      = pd.to_numeric(ap['account_id'], errors='coerce')
ap['appliance_group'] = ap['appliance_group'].astype(str).str.strip().replace(GROUP_MERGE)
ap['consumer_type']   = ap['consumer_type'].fillna(CT_PRIV)
ap['heated_area']     = pd.to_numeric(ap['heated_area'], errors='coerce')
ap['summer_baseline'] = pd.to_numeric(ap['summer_baseline'], errors='coerce')
ap['has_OP']          = ap['appliance_group'].str.contains('\u041e\u041f', na=False)

bill_cols = {}
for eng, m in MONTH_ENG.items():
    col = f'{eng}_2025'
    if col in ap.columns:
        ap[col] = pd.to_numeric(ap[col], errors='coerce').fillna(0)
        bill_cols[m] = col

mc = [bill_cols[m] for m in range(1, 13) if m in bill_cols]
ap['annual_billing'] = ap[mc].sum(axis=1)
ap['n_months_nonzero'] = (ap[mc].abs() > 0).sum(axis=1)

# Виключаємо рядки без ГРС (aggregate/totals rows в CSV, наприклад account_id=222046)
n_before = len(ap)
ap = ap[ap['grs'].notna()].copy()
print(f"  Всього абонентів (після фільтру grs.notna()): {len(ap):,} "
      f"(видалено {n_before - len(ap)} рядків без ГРС)")
print(f"  З ненульовим річним білінгом: {(ap['annual_billing'].abs() > 0).sum():,}")
print(f"  ОП: {ap['has_OP'].sum():,}")
print(f"  summer_baseline populated: {ap['summer_baseline'].notna().sum():,} "
      f"({ap['summer_baseline'].notna().mean()*100:.1f}%)")

# ── 3. Калібрування α з модемних ОП-абонентів ────────────────────────────────
print("\n=== 3. Калібрування α (питоме теплопостачання) ===")
md_ap = md.merge(
    ap[['account_id', 'summer_baseline', 'heated_area', 'grs']].rename(
        columns={'heated_area': 'ha_billing'}),
    on='account_id', how='left'
)
md_ap['ha_final'] = md_ap['ha_billing'].fillna(md_ap['heated_area'])

# Фільтр для калібрування α:
# - ОП модемний
# - є дані за 2025
# - summer_baseline є і адекватний (відношення actual/baseline < SB_RATIO_MAX)
calib_mask = (
    (md_ap['has_OP']) &
    (md_ap['annual_modem'] > 0) &
    (md_ap['summer_baseline'].notna()) &
    (md_ap['summer_baseline'] > 0) &
    (md_ap['ha_final'] >= MIN_HEAT_HA) &
    (md_ap['summer_modem_avg'] / md_ap['summer_baseline'] < SB_RATIO_MAX)
)
calib = md_ap[calib_mask].copy()
print(f"  Модемних ОП для калібрування: {len(calib):,}")

calib['non_heat_annual'] = calib['summer_baseline'] * 12
calib['heat_annual_md']  = (calib['annual_modem'] - calib['non_heat_annual']).clip(lower=0)
has_heat_c = calib[calib['heat_annual_md'] > 0]
alpha_vals = has_heat_c['heat_annual_md'] / has_heat_c['ha_final']

# Видаляємо явних аутлаєрів (alpha > P95)
p5, p95 = alpha_vals.quantile([0.05, 0.95])
alpha_clean = alpha_vals[(alpha_vals >= p5) & (alpha_vals <= p95)]
ALPHA = alpha_clean.median()

print(f"  Alpha (heat_annual/heated_area) з модемів:")
print(f"    P5={p5:.1f}, Median={alpha_clean.median():.2f}, "
      f"Mean={alpha_clean.mean():.2f}, P95={p95:.1f} м³/м²/рік")
print(f"  → Використовуємо α = {ALPHA:.2f} м³/м²/рік")

# Також оцінюємо summer_baseline відносно фактичного літнього споживання
sb_ratio_median = (calib['summer_modem_avg'] / calib['summer_baseline']).median()
print(f"\n  Медіана summer_modem_avg / summer_baseline = {sb_ratio_median:.3f}")
print(f"  (1.0 = ідеально, {'недооцінка' if sb_ratio_median > 1 else 'переоцінка'} на "
      f"{abs(sb_ratio_median - 1)*100:.1f}%)")

# ── 4. Медіанний summer_baseline для імпутації ───────────────────────────────
print("\n=== 4. Медіанний baseline для імпутації (за типом абонента) ===")
sb_medians = {}
for ct in ap['consumer_type'].unique():
    for has_op in [True, False]:
        mask = (ap['consumer_type'] == ct) & (ap['has_OP'] == has_op) & ap['summer_baseline'].notna()
        sub = ap.loc[mask, 'summer_baseline']
        if len(sub) > 0:
            sb_medians[(ct, has_op)] = sub.median()

print("  Медіанний summer_baseline (м³/міс) за категоріями:")
for (ct, has_op), val in sorted(sb_medians.items()):
    cat = '\u041e\u041f' if has_op else 'non-ОП'
    print(f"    [{cat}, {ct[:20]}]: {val:.2f}")

def get_sb_median(row):
    key = (row['consumer_type'], row['has_OP'])
    return sb_medians.get(key, 5.0)  # fallback = 5.0 м³/міс

# ── 5. Декомпозиція білінгу ───────────────────────────────────────────────────
print("\n=== 5. Декомпозиція білінгу (всі абоненти) ===")

result = ap.copy()
result['is_modem'] = result['account_id'].isin(modem_ids)

# Для модемних: підтягуємо фактичні місячні дані
md_monthly = md[['account_id'] + [f'm{m}' for m in range(1, 13)] +
                ['annual_modem', 'summer_modem_avg']].copy()
result = result.merge(md_monthly.rename(columns={f'm{m}': f'md_m{m}' for m in range(1, 13)} |
                                        {'annual_modem': 'annual_modem',
                                         'summer_modem_avg': 'summer_modem_avg'}),
                      on='account_id', how='left')

# Імпутація summer_baseline
result['sb_imputed'] = result['summer_baseline'].isna()
result['summer_baseline_used'] = result['summer_baseline'].fillna(
    result.apply(get_sb_median, axis=1)
)

# non_heat_annual
result['non_heat_annual'] = result['summer_baseline_used'] * 12

# Для НЕ-ОП: весь білінг = non_heat (немає опалення)
non_op_mask = ~result['has_OP']
result.loc[non_op_mask, 'heat_annual']     = 0.0
result.loc[non_op_mask, 'non_heat_annual'] = result.loc[non_op_mask, 'annual_billing']

# Для ОП: heat = max(0, annual - non_heat)
op_mask = result['has_OP']
heat_raw = result.loc[op_mask, 'annual_billing'] - result.loc[op_mask, 'non_heat_annual']
result.loc[op_mask, 'heat_annual'] = heat_raw.clip(lower=0)

# Коригуємо non_heat якщо non_heat > annual (сезонні абоненти, нові, часткові)
overshoot = op_mask & (result['non_heat_annual'] > result['annual_billing'])
result.loc[overshoot, 'non_heat_annual'] = result.loc[overshoot, 'annual_billing'].clip(lower=0)
result.loc[overshoot, 'heat_annual'] = 0.0

# Для модемних: використовуємо фактичне споживання (замінюємо білінг)
modem_op_mask = result['is_modem'] & result['has_OP'] & result['annual_modem'].notna()
result.loc[modem_op_mask, 'heat_annual'] = (
    result.loc[modem_op_mask, 'annual_modem'] -
    result.loc[modem_op_mask, 'non_heat_annual']
).clip(lower=0)

# heat_share
result['heat_share'] = np.where(
    result['annual_billing'].abs() > 0,
    result['heat_annual'] / result['annual_billing'].abs(),
    np.nan
)

# ── 6. Модель очікуваного споживання ─────────────────────────────────────────
print("\n=== 6. Модель очікуваного споживання ===")
result['heated_area_used'] = result['heated_area'].fillna(55).clip(lower=MIN_HEAT_HA)

# expected = non_heat + alpha * area (тільки для ОП)
result['expected_annual'] = np.where(
    result['has_OP'],
    result['non_heat_annual'] + ALPHA * result['heated_area_used'],
    result['non_heat_annual']
)

# Відхилення: > 0 = billing < expected (підозра на крадіжку/витоки)
result['deviation_pct'] = np.where(
    result['expected_annual'] > 0,
    (result['expected_annual'] - result['annual_billing']) / result['expected_annual'] * 100,
    np.nan
)

# Alpha implied з білінгу (для ОП з ненульовим heat)
op_heat_mask = result['has_OP'] & (result['heat_annual'] > 0) & (result['heated_area_used'] >= MIN_HEAT_HA)
result['alpha_implied'] = np.where(
    op_heat_mask,
    result['heat_annual'] / result['heated_area_used'],
    np.nan
)

print(f"  Абонентів з ОП і тепловою складовою > 0: {op_heat_mask.sum():,}")
ai = result.loc[op_heat_mask, 'alpha_implied']
print(f"  Alpha implied з білінгу: median={ai.median():.2f}, mean={ai.mean():.2f}")
print(f"  Alpha з модемів: {ALPHA:.2f} м³/м²/рік")

# ── 7. Статистика декомпозиції ────────────────────────────────────────────────
print("\n=== 7. Статистика декомпозиції ===")
active = result[result['annual_billing'].abs() > 0].copy()

total_billing    = active['annual_billing'].sum() / 1e6
total_heat       = active['heat_annual'].sum() / 1e6
total_non_heat   = active['non_heat_annual'].sum() / 1e6
total_expected   = active['expected_annual'].sum() / 1e6

print(f"  Всього абонентів (ненульовий білінг): {len(active):,}")
print(f"  Загальний річний білінг: {total_billing:.2f} млн м³")
print(f"    → опалення:    {total_heat:.2f} млн м³ ({total_heat/total_billing*100:.1f}%)")
print(f"    → готування/ГВП: {total_non_heat:.2f} млн м³ ({total_non_heat/total_billing*100:.1f}%)")
print(f"  Очікуване (модель):      {total_expected:.2f} млн м³")
print(f"  Відхилення (очік. - білінг): {total_expected - total_billing:.2f} млн м³ "
      f"({(total_expected/total_billing - 1)*100:+.1f}%)")

# Розбивка за типом
print()
for has_op, label in [(True, 'ОП (опалення)'), (False, 'Не-ОП (ПГ/ВПГ)')]:
    sub = active[active['has_OP'] == has_op]
    bill = sub['annual_billing'].sum() / 1e6
    heat = sub['heat_annual'].sum() / 1e6
    print(f"  {label}: {len(sub):,} абон., білінг={bill:.2f} млн м³, "
          f"heat={heat:.2f} млн м³ ({heat/max(bill,0.001)*100:.1f}%)")

# Deviation distribution
print()
print("  Розподіл відхилення (очікуване - білінг) / очікуване:")
for p in [5, 25, 50, 75, 95]:
    print(f"    P{p}: {np.nanpercentile(active['deviation_pct'], p):.1f}%")

# Підозрілі: billing < 50% від expected
suspect = active[(active['deviation_pct'] > 50) & active['has_OP']]
print(f"\n  Підозрілих ОП-абонентів (billing < 50% очікуваного): {len(suspect):,}")
print(f"    Втрачений обсяг: {(suspect['expected_annual'] - suspect['annual_billing']).sum()/1e6:.2f} млн м³")

# ── 8. Збереження результатів ─────────────────────────────────────────────────
print(f"\n=== 8. Збереження {OUT_XLSX} ===")

# Вибираємо ключові колонки для виводу
out_cols = [
    'account_id', 'grs', 'consumer_type', 'appliance_group', 'has_OP',
    'heated_area', 'summer_baseline', 'sb_imputed',
    'annual_billing', 'n_months_nonzero',
    'non_heat_annual', 'heat_annual', 'heat_share',
    'expected_annual', 'deviation_pct', 'alpha_implied',
    'is_modem', 'annual_modem',
]
out_cols = [c for c in out_cols if c in result.columns]
df_out = result[out_cols].copy()

# Округлення
for col in ['annual_billing', 'non_heat_annual', 'heat_annual', 'expected_annual', 'annual_modem']:
    if col in df_out.columns:
        df_out[col] = df_out[col].round(2)
df_out['heat_share']     = df_out['heat_share'].round(3)
df_out['deviation_pct']  = df_out['deviation_pct'].round(1)
df_out['alpha_implied']  = df_out['alpha_implied'].round(2)

# Лист 1: всі абоненти (ненульовий білінг)
df_main = df_out[df_out['annual_billing'].abs() > 0].sort_values(
    'deviation_pct', ascending=False)

# Лист 2: підозрілі (відхилення > 30%, ОП)
df_suspect = df_main[
    (df_main['deviation_pct'] > 30) &
    df_main['has_OP'] &
    ~df_main['is_modem']
].copy()

# Лист 3: GRS summary
grs_summary = active.groupby('grs').agg(
    n_subs=('account_id', 'count'),
    n_op=('has_OP', 'sum'),
    n_modem=('is_modem', 'sum'),
    billing_mln=('annual_billing', lambda x: x.sum() / 1e6),
    heat_mln=('heat_annual', lambda x: x.sum() / 1e6),
    non_heat_mln=('non_heat_annual', lambda x: x.sum() / 1e6),
    expected_mln=('expected_annual', lambda x: x.sum() / 1e6),
    avg_deviation=('deviation_pct', 'median'),
    suspect_count=('deviation_pct', lambda x: (x > 30).sum()),
).reset_index()
grs_summary['heat_share'] = (grs_summary['heat_mln'] / grs_summary['billing_mln'] * 100).round(1)
grs_summary['gap_mln']    = (grs_summary['expected_mln'] - grs_summary['billing_mln']).round(3)
grs_summary['gap_pct']    = (grs_summary['gap_mln'] / grs_summary['billing_mln'] * 100).round(1)
for col in ['billing_mln', 'heat_mln', 'non_heat_mln', 'expected_mln']:
    grs_summary[col] = grs_summary[col].round(3)
grs_summary['avg_deviation'] = grs_summary['avg_deviation'].round(1)
grs_summary = grs_summary.sort_values('billing_mln', ascending=False)

# Лист 4: calibration summary
calib_out = calib[['account_id', 'summer_baseline', 'summer_modem_avg',
                   'ha_final', 'heat_annual_md', 'grs', 'consumer_type']].copy()
calib_out['alpha_actual'] = (calib_out['heat_annual_md'] / calib_out['ha_final']).round(2)
calib_out['sb_ratio']     = (calib_out['summer_modem_avg'] / calib_out['summer_baseline']).round(3)
calib_out = calib_out.sort_values('account_id')

with pd.ExcelWriter(OUT_XLSX, engine='openpyxl') as w:
    df_main.to_excel(w, sheet_name='All_Subscribers', index=False)
    df_suspect.to_excel(w, sheet_name='Suspects_OP', index=False)
    grs_summary.to_excel(w, sheet_name='GRS_Summary', index=False)
    calib_out.to_excel(w, sheet_name='Calibration', index=False)

print(f"  Записано {len(df_main):,} абонентів → лист All_Subscribers")
print(f"  Підозрілих (>30% відхилення, ОП): {len(df_suspect):,} → лист Suspects_OP")
print(f"  ГРС: {len(grs_summary)} → лист GRS_Summary")
print(f"  Калібрування: {len(calib_out):,} → лист Calibration")
print()
print("=== Готово ===")
print(f"  α = {ALPHA:.2f} м³/м²/рік")
print(f"  Файл: {OUT_XLSX}")
