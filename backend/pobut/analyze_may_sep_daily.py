"""
Аналіз денного питомого споживання (м³/м²) модемних ОП-споживачів
у квітні-травні та вересні-жовтні 2025.

Мета: знайти точку переходу між опалювальним та літнім споживанням.
"""
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
from pobut_predictor import GROUP_MERGE

MODEM_FILE = 'data/input/profile_pobut_daily_result.csv'

# ── 1. Завантаження ─────────────────────────────────────────────────────────
print("Завантаження модемних даних...")
md = pd.read_csv(MODEM_FILE, sep=';', low_memory=False)
if 'appliance_group' in md.columns:
    md['appliance_group'] = md['appliance_group'].replace(GROUP_MERGE)

md['heated_area'] = pd.to_numeric(md['heated_area'], errors='coerce')

# Фільтр: ОП-споживачі з площею > 0, не дача, не альтернативні
op_mask = (
    md['appliance_group'].str.contains('ОП', na=False)
    & (md['heated_area'] > 0)
)
for col in ['gas_off', 'alternative', 'dacha']:
    if col in md.columns:
        op_mask = op_mask & md[col].isna()

md_op = md[op_mask].copy()
areas = md_op['heated_area'].values.astype(np.float64)
print(f"  ОП-модемів: {len(md_op):,}")

# ── 2. Зібрати всі денні колонки 2025 квітень-жовтень ───────────────────────
date_cols = []
for c in md.columns:
    if isinstance(c, str) and c.count('.') == 2 and len(c) == 10:
        try:
            d_s, m_s, y_s = c.split('.')
            date = pd.Timestamp(f'{y_s}-{m_s}-{d_s}')
            if date.year == 2025 and date.month in (4, 5, 6, 9, 10):
                date_cols.append((date, c))
        except Exception:
            continue

date_cols.sort(key=lambda x: x[0])
print(f"  Денних колонок (квіт/трав/черв/вер/жов 2025): {len(date_cols)}")

# ── 3. Порахувати медіанний питомий обсяг по кожному дню ────────────────────
records = []
for date, col in date_cols:
    vals = pd.to_numeric(md_op[col], errors='coerce').values.astype(np.float64)
    valid = (areas > 0) & np.isfinite(vals) & (vals >= 0)
    if valid.sum() < 10:
        continue
    specs = vals[valid] / areas[valid]
    # Видалити аномальні викиди (> 99-й перцентиль)
    p99 = np.percentile(specs, 99)
    specs_clean = specs[specs <= p99]

    records.append({
        'date':    date,
        'month':   date.month,
        'day':     date.day,
        'spec_med':   float(np.median(specs_clean)),
        'spec_mean':  float(np.mean(specs_clean)),
        'spec_p25':   float(np.percentile(specs_clean, 25)),
        'spec_p75':   float(np.percentile(specs_clean, 75)),
        'n_active':   int((vals[valid] > 0).sum()),
        'n_valid':    int(valid.sum()),
    })

df = pd.DataFrame(records)
df['pct_active'] = df['n_active'] / df['n_valid'] * 100

# ── 4. Вивід таблиці з ASCII-графіком ───────────────────────────────────────
MONTH_UA = {4:'Квіт', 5:'Трав', 6:'Черв', 9:'Вер', 10:'Жов'}

def ascii_bar(val, min_val, max_val, width=25):
    if max_val <= min_val:
        return '░' * width
    frac = (val - min_val) / (max_val - min_val)
    filled = int(frac * width)
    return '█' * filled + '░' * (width - filled)

# Секції: квітень-червень та вересень-жовтень
for months, title in [([4, 5, 6], "КВІТЕНЬ → ЧЕРВЕНЬ (кінець опалення)"),
                       ([9, 10],    "ВЕРЕСЕНЬ → ЖОВТЕНЬ (початок опалення)")]:
    sub = df[df['month'].isin(months)].copy()
    if sub.empty:
        continue

    vmin = sub['spec_med'].min()
    vmax = sub['spec_med'].max()
    baseline = df[df['month'] == 6]['spec_med'].median() if 6 in df['month'].values else sub['spec_med'].min()

    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"  Базовий літній рівень (червень): {baseline:.5f} м³/м²/день")
    print(f"{'='*70}")
    print(f"  {'Дата':12s}  {'Медіана':>8s}  {'Середнє':>8s}  {'Акт%':>5s}  Графік (відносно діапазону)")
    print(f"  {'-'*65}")

    prev_month = None
    for _, r in sub.iterrows():
        if prev_month is not None and r['month'] != prev_month:
            print(f"  {'─'*65}")
        prev_month = r['month']

        bar   = ascii_bar(r['spec_med'], vmin, vmax)
        ratio = r['spec_med'] / baseline if baseline > 0 else 1.0
        flag  = ''
        if ratio > 2.0:
            flag = ' ◄ ОПАЛЕННЯ'
        elif ratio > 1.3:
            flag = ' ◄ перехід↑'
        elif ratio < 0.9:
            flag = ''

        date_str = r['date'].strftime('%d.%m.%Y')
        ua_m = MONTH_UA.get(r['month'], str(r['month']))
        print(f"  {date_str:12s}  {r['spec_med']:8.5f}  {r['spec_mean']:8.5f}  "
              f"{r['pct_active']:5.1f}%  {bar}{flag}")

# ── 5. Знайти точку переходу (rolling change) ───────────────────────────────
print(f"\n{'='*70}")
print("  ЗМІНА МЕДІАНИ ВІД ПОПЕРЕДНЬОГО ДНЯ (|Δ| > поріг)")
print(f"{'='*70}")
df_sorted = df.sort_values('date').reset_index(drop=True)
df_sorted['delta'] = df_sorted['spec_med'].diff()
df_sorted['delta_pct'] = df_sorted['delta'] / df_sorted['spec_med'].shift(1) * 100

# Великі стрибки
thresh = 0.002  # м³/м²/день
big = df_sorted[df_sorted['delta'].abs() > thresh].copy()
if not big.empty:
    print(f"  {'Дата':12s}  {'Δ м³/м²':>10s}  {'Δ%':>8s}  Напрямок")
    print(f"  {'-'*50}")
    for _, r in big.iterrows():
        direction = '↑ РІСТ (початок опалення?)' if r['delta'] > 0 else '↓ ПАДІННЯ (кінець опалення?)'
        print(f"  {r['date'].strftime('%d.%m.%Y'):12s}  {r['delta']:>+10.5f}  "
              f"{r['delta_pct']:>+8.1f}%  {direction}")
else:
    print("  Великих стрибків не знайдено (поріг 0.002 м³/м²/день)")

# ── 6. Порівняння місячних середніх ─────────────────────────────────────────
print(f"\n{'='*70}")
print("  МІСЯЧНЕ СЕРЕДНЄ ПИТОМОГО СПОЖИВАННЯ")
print(f"{'='*70}")
monthly = df.groupby('month').agg(
    spec_med=('spec_med', 'median'),
    spec_mean=('spec_mean', 'mean'),
    n_days=('date', 'count'),
).reset_index()

ref_jun = monthly.loc[monthly['month'] == 6, 'spec_med'].values
ref_val = float(ref_jun[0]) if len(ref_jun) else None

print(f"  {'Місяць':8s}  {'Медіана':>9s}  {'Ratio/Чер':>10s}  {'Днів':>6s}")
print(f"  {'-'*45}")
for _, r in monthly.iterrows():
    ua_m = MONTH_UA.get(r['month'], str(r['month']))
    ratio_str = f"{r['spec_med']/ref_val:.2f}x" if ref_val else "—"
    print(f"  {ua_m:8s}  {r['spec_med']:>9.5f}  {ratio_str:>10s}  {r['n_days']:>6.0f}")

print("\nДОНЕ")
