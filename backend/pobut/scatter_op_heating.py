"""
Scatter plot: питоме споживання (м³/м²/рік) vs площа (м²)
для всіх ОП-абонентів. Точки підфарбовані за рівнем підозрілості (ratio_to_ref).
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

from pobut_predictor import GROUP_MERGE

SUBS_FILE = "data/input/all_pobut_enriched.csv"
OUT_PNG   = "data/scatter_op_heating.png"

MONTH_COLS = ['jan_2025','feb_2025','mar_2025','apr_2025','may_2025','jun_2025',
              'jul_2025','aug_2025','sep_2025','oct_2025','nov_2025','dec_2025']

ABS_LOW   = 5.0   # критично низьке питоме споживання (критерій D)
Y_CAP_PCT = 0.99  # обрізаємо вісь Y на цьому перцентилі (відкидаємо хвіст)

# ── 1. Завантаження та фільтрація ──────────────────────────────────────────────
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
    ap['has_OP'] &
    ap['gas_off'].isna() &
    ap['alternative'].isna() &
    ap['dacha'].isna() &
    ap['grs'].notna() &
    (ap['annual'] > 0)
].copy().reset_index(drop=True)

print(f"  ОП-абонентів (активних, з білінгом): {len(op):,}")

# ── 2. Питомі метрики ──────────────────────────────────────────────────────────
print("\n=== 2. Метрики ===")
op['specific'] = op['annual'] / op['heated_area']

ref = (op.groupby(['grs', 'consumer_type'])['specific']
         .median().rename('grs_ct_median').reset_index())
op = op.merge(ref, on=['grs', 'consumer_type'], how='left')

grs_ref = op.groupby('grs')['specific'].median().rename('grs_median').reset_index()
op = op.merge(grs_ref, on='grs', how='left')
op['ref_median'] = op['grs_ct_median'].fillna(op['grs_median'])
op['ratio_to_ref'] = op['specific'] / np.maximum(op['ref_median'], 1)

# ── 3. Рівень підозрілості ─────────────────────────────────────────────────────
def classify(r):
    if r < 0.30:
        return 'critical'
    elif r < 0.40:
        return 'high'
    elif r < 0.50:
        return 'low'
    else:
        return 'normal'

op['level'] = op['ratio_to_ref'].apply(classify)

COLORS = {
    'normal':   ('#999999', 'Норма (ratio ≥ 0.50)'),
    'low':      ('#FFD700', 'Низький (0.40–0.50)'),
    'high':     ('#FF8C00', 'Середній/Високий (0.30–0.40)'),
    'critical': ('#FF2222', 'Критичний (< 0.30)'),
}
ZORDER = {'normal': 1, 'low': 2, 'high': 3, 'critical': 4}

# ── 4. Статистика ──────────────────────────────────────────────────────────────
print("\n=== 3. Статистика по рівнях ===")
for lvl in ['normal', 'low', 'high', 'critical']:
    sub = op[op['level'] == lvl]
    if sub.empty:
        continue
    s = sub['specific']
    print(f"  {COLORS[lvl][1]}: N={len(sub):,}  "
          f"median={s.median():.1f}  p10={s.quantile(0.10):.1f}  "
          f"p25={s.quantile(0.25):.1f}  p75={s.quantile(0.75):.1f}")

print(f"\n  Загальна медіана specific: {op['specific'].median():.1f} м³/м²/рік")
print(f"  Нижче {ABS_LOW} м³/м²/рік (критерій D): {(op['specific'] < ABS_LOW).sum():,}")

global_median = op['specific'].median()
y_cap = op['specific'].quantile(Y_CAP_PCT)
print(f"  Верхня межа осі Y (p{Y_CAP_PCT*100:.0f}): {y_cap:.1f} м³/м²/рік  "
      f"(відсічено {(op['specific'] > y_cap).sum()} точок з {len(op):,})")

# ── 5. Scatter plot — два subplot'и по типу застройки ─────────────────────────
print("\n=== 4. Побудова графіку ===")

SECTORS = [
    ('Приватний сектор',         'Приватний сектор'),
    ('Багатоквартирний сектор',  'Багатоквартирний сектор'),
]

fig, axes = plt.subplots(1, 2, figsize=(16, 7), sharey=False)

for ax, (ct_name, ct_label) in zip(axes, SECTORS):
    ct_data = op[op['consumer_type'] == ct_name]
    ct_median = ct_data['specific'].median()
    ct_cap = ct_data['specific'].quantile(Y_CAP_PCT)

    for lvl in ['normal', 'low', 'high', 'critical']:
        sub = ct_data[ct_data['level'] == lvl]
        if sub.empty:
            continue
        color, label = COLORS[lvl]
        ax.scatter(
            sub['heated_area'], sub['specific'],
            c=color, alpha=0.3, s=8,
            label=f"{label}  (N={len(sub):,})",
            zorder=ZORDER[lvl],
            linewidths=0,
        )

    ax.axhline(ct_median, color='royalblue', linewidth=1.5, linestyle='--',
               label=f'Медіана = {ct_median:.1f} м³/м²/рік', zorder=5)
    ax.axhline(ABS_LOW, color='crimson', linewidth=1.5, linestyle=':',
               label=f'Критерій D = {ABS_LOW} м³/м²/рік', zorder=5)

    ax.set_xscale('log')
    ax.set_ylim(0, ct_cap)
    ax.set_xlabel('Опалювана площа, м² (лог. шкала)', fontsize=11)
    ax.set_ylabel('Питоме споживання, м³/м²/рік', fontsize=11)
    ax.set_title(
        f'{ct_label}  (N={len(ct_data):,})\n'
        f'Підфарбування за рівнем підозрілості',
        fontsize=12, fontweight='bold'
    )
    ax.legend(fontsize=8, loc='upper right', framealpha=0.85)
    ax.grid(True, alpha=0.25)

    ct_cap_clipped = (ct_data['specific'] > ct_cap).sum()
    print(f"  {ct_label}: N={len(ct_data):,}, медіана={ct_median:.1f}, "
          f"p99={ct_cap:.1f}, відсічено={ct_cap_clipped}")

fig.suptitle('Питоме споживання vs площа — ОП-абоненти',
             fontsize=14, fontweight='bold', y=1.01)
plt.tight_layout()
fig.savefig(OUT_PNG, dpi=150, bbox_inches='tight')
plt.close(fig)
print(f"\nЗбережено: {OUT_PNG}")
