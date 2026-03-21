"""
backtest_rt.py

Бектест RT моделі: порівняння прогнозу з реальним білінгом 2025 по ГРС.
Мета: мінімізація MAE.

Тестуємо кілька конфігурацій:
  A — group medians (оригінальний PobUtPredictor)
  B — individual summer_baseline (PobUtPredictorSB)
  C — individual summer_baseline + виключення нульових абонентів (billing=0 за рік)
  D — individual summer_baseline + zero-excl + summer_op_vpg=True

Метрики: MAE / MAPE / bias% — по ГРС × місяць (2025)
"""
import sys
import io
import os

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
from pobut_predictor import PobUtPredictor, CT_PRIV

MODEM_FILE = 'data/input/profile_pobut_daily_result.csv'
SUBS_FILE  = 'data/input/all_pobut_enriched.csv'
OUT_EXCEL  = 'data/backtest_rt.xlsx'

MONTH_ENG = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4,
    'may': 5, 'jun': 6, 'jul': 7, 'aug': 8,
    'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}
MONTH_UA = {
    1: 'Січ', 2: 'Лют',  3: 'Бер', 4: 'Кві',
    5: 'Тра', 6: 'Чер',  7: 'Лип', 8: 'Сер',
    9: 'Вер', 10: 'Жов', 11: 'Лис', 12: 'Гру',
}


# ══════════════════════════════════════════════════════════════════════════════
# 0. Білінг 2025 по ГРС × місяць (еталон)
# ══════════════════════════════════════════════════════════════════════════════
print("=" * 65)
print("Завантаження білінгу 2025...")
print("=" * 65)

ap = pd.read_csv(SUBS_FILE, low_memory=False)
ap['account_id'] = pd.to_numeric(ap['account_id'], errors='coerce')

month_cols = sorted(
    [c for c in ap.columns if c.endswith('_2025') and c.split('_')[0] in MONTH_ENG],
    key=lambda c: MONTH_ENG[c.split('_')[0]],
)
for c in month_cols:
    ap[c] = pd.to_numeric(ap[c], errors='coerce').fillna(0)

# Нульові активні абоненти (нема білінгу та нема gas_off) — виключаємо в моделі C/D
zero_active_ids = set(
    ap[ap['gas_off'].isna() & ap[month_cols].eq(0).all(axis=1)]
    ['account_id'].dropna().astype(int)
)
print(f"  Нульових активних (excl в C/D): {len(zero_active_ids):,}")

# Білінг по ГРС × місяць
billing_rows = []
for c in month_cols:
    m = MONTH_ENG[c.split('_')[0]]
    grp_bill = (
        ap[ap['grs'].notna()][['grs', c]]
        .groupby('grs')[c].sum()
        .reset_index()
    )
    grp_bill.columns = ['grs', 'billing']
    grp_bill['month'] = m
    grp_bill['year']  = 2025
    billing_rows.append(grp_bill)

billing_df = pd.concat(billing_rows, ignore_index=True)
billing_df['billing'] = billing_df['billing'].round(0)

total_billing = billing_df['billing'].sum()
print(f"  Загальний білінг 2025: {total_billing:>14,.0f} м³")
print(f"  ГРС: {billing_df['grs'].nunique()}, місяців: {billing_df['month'].nunique()}")


# ══════════════════════════════════════════════════════════════════════════════
# 1. Моделі для тестування
# ══════════════════════════════════════════════════════════════════════════════

class PobUtPredictorSB(PobUtPredictor):
    """Використовує individual summer_baseline як pbl для не-модемних."""
    def _prepare_non_modem(self):
        super()._prepare_non_modem()
        sb_map = (
            self._all_pobut
            .assign(summer_baseline=lambda d: pd.to_numeric(
                d['summer_baseline'], errors='coerce'))
            .set_index('account_id')['summer_baseline']
            .to_dict()
        )
        nm = self._non_modem
        nm['_sb'] = nm['account_id'].map(sb_map)
        valid = nm['_sb'].notna() & (nm['_sb'] > 0)
        nm.loc[valid, 'pbl'] = nm.loc[valid, '_sb']
        nm.drop(columns=['_sb'], inplace=True)


CONFIGS = [
    # (label, class, kwargs, zero_excl)
    ('A: group_medians',          PobUtPredictor,    {'summer_op_vpg': False}, False),
    ('B: indiv_sb',               PobUtPredictorSB,  {'summer_op_vpg': False}, False),
    ('C: indiv_sb + zero_excl',   PobUtPredictorSB,  {'summer_op_vpg': False}, True),
    ('D: indiv_sb + zero_excl + vpg', PobUtPredictorSB, {'summer_op_vpg': True}, True),
]


# ══════════════════════════════════════════════════════════════════════════════
# 2. Запуск та агрегація
# ══════════════════════════════════════════════════════════════════════════════

def run_model(cls, kwargs, zero_excl):
    p = cls(MODEM_FILE, SUBS_FILE, mode='offline', **kwargs)
    df_all = p.predict(years=[2025])
    if zero_excl:
        df_all = df_all[~df_all['account_id'].isin(zero_active_ids)].copy()
    agg = (
        df_all
        .groupby(['grs', 'year', 'month', 'type'])['volume']
        .sum().reset_index()
    )
    pivot = agg.pivot_table(
        index=['grs', 'year', 'month'], columns='type',
        values='volume', aggfunc='sum', fill_value=0,
    ).reset_index()
    pivot.columns.name = None
    pivot['fact']  = pivot.get('fact',  0)
    pivot['pred']  = pivot.get('pred',  0)
    pivot['total'] = pivot['fact'] + pivot['pred']
    return pivot[['grs', 'year', 'month', 'fact', 'pred', 'total']]


def compute_metrics(pred_df, bill_df, label):
    """Compute MAE, MAPE, bias per (GRS, month) and totals."""
    merged = pred_df.merge(
        bill_df[['grs', 'year', 'month', 'billing']],
        on=['grs', 'year', 'month'], how='inner',
    )
    merged = merged[merged['billing'] > 0].copy()
    merged['error']   = merged['total'] - merged['billing']
    merged['abs_err'] = merged['error'].abs()
    merged['ape']     = merged['abs_err'] / merged['billing']

    mae   = merged['abs_err'].mean()
    mape  = merged['ape'].mean() * 100
    bias  = merged['error'].sum() / merged['billing'].sum() * 100
    total_pred = merged['total'].sum()
    total_bill = merged['billing'].sum()

    return {
        'label':      label,
        'mae':        round(mae, 0),
        'mape':       round(mape, 2),
        'bias%':      round(bias, 2),
        'total_pred': round(total_pred, 0),
        'total_bill': round(total_bill, 0),
        'n_pairs':    len(merged),
        '_merged':    merged,
    }


results = {}
for label, cls, kwargs, zero_excl in CONFIGS:
    print(f"\n{'─' * 50}")
    print(f"  Запуск: {label}")
    pred = run_model(cls, kwargs, zero_excl)
    m    = compute_metrics(pred, billing_df, label)
    results[label] = m
    print(f"  MAE={m['mae']:,.0f}  MAPE={m['mape']:.2f}%  "
          f"bias={m['bias%']:+.2f}%  "
          f"total_pred={m['total_pred']:,.0f}  total_bill={m['total_bill']:,.0f}")


# ══════════════════════════════════════════════════════════════════════════════
# 3. Порівняльна таблиця конфігурацій
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 65)
print("ПОРІВНЯННЯ КОНФІГУРАЦІЙ")
print("=" * 65)
print(f"{'Конфігурація':<35} {'MAE':>10} {'MAPE%':>8} {'Bias%':>8}")
print("-" * 65)
for lbl, m in results.items():
    print(f"{lbl:<35} {m['mae']:>10,.0f} {m['mape']:>7.2f}% {m['bias%']:>+7.2f}%")


# ══════════════════════════════════════════════════════════════════════════════
# 4. Детальний аналіз найкращої моделі
# ══════════════════════════════════════════════════════════════════════════════
best_label = min(results, key=lambda k: results[k]['mae'])
best = results[best_label]
print(f"\n>>> Найкраща конфігурація за MAE: {best_label}")

merged_best = best['_merged'].copy()
merged_best['period'] = merged_best.apply(
    lambda r: f"{MONTH_UA[int(r['month'])]}-{str(int(r['year']))[2:]}", axis=1
)

# Per-GRS metrics
by_grs = (
    merged_best
    .groupby('grs')
    .agg(
        billing      = ('billing',  'sum'),
        total_pred   = ('total',    'sum'),
        mae          = ('abs_err',  'mean'),
        mape         = ('ape',      'mean'),
        bias_sum     = ('error',    'sum'),
        n_months     = ('month',    'count'),
    )
    .reset_index()
)
by_grs['mape']  = (by_grs['mape'] * 100).round(2)
by_grs['bias%'] = (by_grs['bias_sum'] / by_grs['billing'] * 100).round(2)
by_grs['mae']   = by_grs['mae'].round(0)
by_grs = by_grs.sort_values('mae', ascending=False)

print(f"\n{'ГРС':<42} {'Білінг':>11} {'Прогноз':>11} {'MAE':>8} {'MAPE%':>7} {'Bias%':>8}")
print("-" * 90)
for _, r in by_grs.iterrows():
    print(f"{str(r['grs']):<42} {r['billing']:>11,.0f} {r['total_pred']:>11,.0f} "
          f"{r['mae']:>8,.0f} {r['mape']:>6.1f}% {r['bias%']:>+7.1f}%")
print("-" * 90)
print(f"{'MAE середній':<42} {' ':>11} {' ':>11} "
      f"{by_grs['mae'].mean():>8,.0f}  (weighted MAE = {best['mae']:,.0f})")

# Per-month metrics
by_month = (
    merged_best
    .groupby('month')
    .agg(
        billing    = ('billing',  'sum'),
        total_pred = ('total',    'sum'),
        mae        = ('abs_err',  'mean'),
        mape       = ('ape',      'mean'),
    )
    .reset_index()
)
by_month['mape']  = (by_month['mape'] * 100).round(2)
by_month['bias%'] = ((by_month['total_pred'] - by_month['billing'])
                     / by_month['billing'] * 100).round(2)
by_month['mae']   = by_month['mae'].round(0)
by_month = by_month.sort_values('month')

print(f"\n{'Місяць':>8} {'Білінг':>12} {'Прогноз':>12} {'MAE':>8} {'MAPE%':>7} {'Bias%':>8}")
print("-" * 65)
for _, r in by_month.iterrows():
    print(f"{MONTH_UA[int(r['month'])]:>8} {r['billing']:>12,.0f} {r['total_pred']:>12,.0f} "
          f"{r['mae']:>8,.0f} {r['mape']:>6.1f}% {r['bias%']:>+7.1f}%")


# ══════════════════════════════════════════════════════════════════════════════
# 5. Excel: детальний бектест + порівняння конфігурацій
# ══════════════════════════════════════════════════════════════════════════════
print(f"\nЗберігаємо {OUT_EXCEL} ...")

# Config comparison table
config_df = pd.DataFrame([
    {
        'Конфігурація': lbl,
        'MAE':          m['mae'],
        'MAPE%':        m['mape'],
        'Bias%':        m['bias%'],
        'Прогноз_всього': m['total_pred'],
        'Білінг_всього':  m['total_bill'],
    }
    for lbl, m in results.items()
])

# Pivot: best model GRS × month
period_order = [
    f"{MONTH_UA[m]}-25"
    for m in sorted(merged_best['month'].unique())
]
pivot_best = merged_best.pivot_table(
    index='grs', columns='period', values='total', aggfunc='sum', fill_value=0,
).reindex(columns=period_order, fill_value=0)
pivot_best.insert(0, 'Прогноз_рік', pivot_best.sum(axis=1))

pivot_bill = merged_best.pivot_table(
    index='grs', columns='period', values='billing', aggfunc='sum', fill_value=0,
).reindex(columns=period_order, fill_value=0)
pivot_bill.insert(0, 'Білінг_рік', pivot_bill.sum(axis=1))

pivot_err = (pivot_best.iloc[:, 1:] - pivot_bill.iloc[:, 1:]).abs()
pivot_err.insert(0, 'MAE_рік', pivot_err.mean(axis=1).round(0))

with pd.ExcelWriter(OUT_EXCEL, engine='openpyxl') as xw:
    # Sheet 1: config comparison
    config_df.to_excel(xw, sheet_name='Config_порівняння', index=False)

    # Sheet 2: best model detail by GRS
    by_grs_excel = by_grs[['grs', 'billing', 'total_pred', 'mae', 'mape', 'bias%', 'n_months']]
    by_grs_excel.columns = ['ГРС', 'Білінг', 'Прогноз_RT', 'MAE', 'MAPE%', 'Bias%', 'N_місяців']
    by_grs_excel.to_excel(xw, sheet_name='GRS_метрики', index=False)

    # Sheet 3: best model by month
    by_month_excel = by_month.copy()
    by_month_excel.insert(0, 'Місяць', by_month_excel['month'].map(MONTH_UA))
    by_month_excel.drop(columns=['month']).to_excel(
        xw, sheet_name='Місяць_метрики', index=False
    )

    # Sheet 4: pred pivot GRS x month (best model)
    pivot_best.reset_index().to_excel(xw, sheet_name='Прогноз_pivot', index=False)

    # Sheet 5: billing pivot GRS x month
    pivot_bill.reset_index().to_excel(xw, sheet_name='Білінг_pivot', index=False)

    # Sheet 6: absolute error pivot GRS x month
    pivot_err.reset_index().to_excel(xw, sheet_name='Похибка_pivot', index=False)

    # Sheet 7: raw detail (grs x month, pred vs billing)
    detail_cols = ['grs', 'year', 'month', 'period',
                   'total', 'billing', 'error', 'abs_err', 'ape']
    merged_best[detail_cols].rename(columns={
        'total': 'прогноз', 'billing': 'білінг',
        'error': 'похибка', 'abs_err': 'abs_похибка', 'ape': 'відн_похибка',
    }).sort_values(['grs', 'month']).to_excel(
        xw, sheet_name='Деталі_GRS_місяць', index=False
    )

print(f"[OK] {OUT_EXCEL}")
print(f"\n>>> Найкраща модель: {best_label}")
print(f"    MAE  = {best['mae']:,.0f} м³ / (ГРС × місяць)")
print(f"    MAPE = {best['mape']:.2f}%")
print(f"    Bias = {best['bias%']:+.2f}%")
print("\n=== DONE ===")
