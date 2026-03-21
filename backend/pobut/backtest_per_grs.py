"""
backtest_per_grs.py

Тест Config E: per-GRS профілі для великих ГРС (≥50 OP-модемів).

Гіпотеза: С-1 і ГРС-2 мають власні теплові патерни.
Використовуємо лише їх власних модемних абонентів для:
  - grp_sb[grs, grp, ct]      — літній baseline по ГРС
  - heat_m2_sum[grs, grp, ct, y, m] — теплова інтенсивність по ГРС

Решта: fallback на глобальні профілі.
"""
import sys
import io
import os

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd

from pobut_predictor import (
    PobUtPredictor, CT_PRIV,
    HEATING_MONTHS, SUMMER_MONTHS,
    MIN_REF_STRAT,
)

MODEM_FILE = 'data/input/profile_pobut_daily_result.csv'
SUBS_FILE  = 'data/input/all_pobut_enriched.csv'
OUT_EXCEL  = 'data/backtest_per_grs.xlsx'

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
# Per-GRS predictor
# ══════════════════════════════════════════════════════════════════════════════

class PobUtPredictorPerGRS(PobUtPredictor):
    """
    PobUtPredictor з per-GRS профілями для великих ГРС.

    Для ГРС з ≥ MIN_GRS_OP_MODEMS OP-модемів:
      - grp_sb      — GRS-специфічний літній baseline
      - heat_m2_sum — GRS-специфічна теплова інтенсивність по (grp, ct, y, m)
    Решта ГРС → глобальні профілі (поточна логіка).
    """

    MIN_GRS_OP_MODEMS = 50

    # ── override _build_profiles to also build per-GRS profiles ───────────────
    def _build_profiles(self, long_df, monthly_df):
        super()._build_profiles(long_df, monthly_df)

        # Attach GRS to modem data (all_pobut is already loaded)
        grs_s = self._all_pobut.set_index('account_id')['grs']
        long_df['grs']    = long_df['account_id'].map(grs_s)
        monthly_df['grs'] = monthly_df['account_id'].map(grs_s)

        # Identify large GRS
        op_counts = (
            long_df[long_df['has_OP'] & long_df['grs'].notna()]
            .groupby('grs')['account_id'].nunique()
        )
        self._large_grs = set(op_counts[op_counts >= self.MIN_GRS_OP_MODEMS].index)

        print(f"\n  [PerGRS] Large GRS ({len(self._large_grs)}):")
        for g in sorted(self._large_grs):
            print(f"    {g}: {int(op_counts[g])} OP modems")

        # ── Per-GRS summer baselines ─────────────────────────────────────────
        self._grp_sb_grs     = {}  # (grs, grp, ct) → median
        self._grp_sb_grs_grp = {}  # (grs, grp)     → median (fallback)

        summer_op = monthly_df[monthly_df['month'].isin([6, 7, 8]) & monthly_df['has_OP']]
        for grs in self._large_grs:
            sub = summer_op[summer_op['grs'] == grs]
            if len(sub) < 5:
                continue
            for (grp, ct), v in (
                sub.groupby(['appliance_group', 'consumer_type'])['consumption']
                .median().items()
            ):
                self._grp_sb_grs[(grs, grp, ct)] = v
            for grp, v in sub.groupby('appliance_group')['consumption'].median().items():
                self._grp_sb_grs_grp[(grs, grp)] = v

        # Print comparison
        print(f"\n  Summer baseline (ОП,ПГ, Приватний) per GRS vs global:")
        glob_sb = self._grp_sb.get(('ОП,ПГ', CT_PRIV), self._grp_sb_grp.get('ОП,ПГ', np.nan))
        for grs in sorted(self._large_grs):
            v = self._grp_sb_grs.get((grs, 'ОП,ПГ', CT_PRIV), np.nan)
            ratio = v / glob_sb if (glob_sb and not np.isnan(v)) else np.nan
            print(f"    {grs[:38]}: {v:.2f}  vs global {glob_sb:.2f}  (ratio={ratio:.3f})")

        # ── Per-GRS heat_m2_sum ──────────────────────────────────────────────
        self._hms_ct_grs  = {}  # (grs, grp, ct, y, m) → heat_m2_sum
        self._hms_grp_grs = {}  # (grs, grp, y, m)     → heat_m2_sum

        ref_op = long_df[
            long_df['has_OP']
            & long_df['month'].isin(HEATING_MONTHS)
            & (long_df['heated_area'] > 0)
            & long_df['grs'].notna()
        ]

        def _hms(ref_df, groupby_cols):
            """Replicate PobUtPredictor._compute_hms logic."""
            if ref_df.empty:
                return pd.DataFrame()
            d = (
                ref_df.groupby(groupby_cols + ['date'])
                .apply(
                    lambda g: pd.Series({
                        'op_m2': g['heat_day'].sum() / g['heated_area'].sum(),
                        'n':     len(g),
                    }),
                    include_groups=False,
                )
                .reset_index()
            )
            if d.empty:
                return pd.DataFrame()
            d = d[d['n'] >= MIN_REF_STRAT].copy()
            d['op_m2_pos'] = d['op_m2'].clip(lower=0)
            d['year']  = d['date'].dt.year
            d['month'] = d['date'].dt.month
            return (
                d.groupby(groupby_cols + ['year', 'month'])['op_m2_pos']
                .sum().reset_index()
                .rename(columns={'op_m2_pos': 'heat_m2_sum'})
            )

        for grs in self._large_grs:
            ref_grs = ref_op[ref_op['grs'] == grs]
            if len(ref_grs) < 20:
                continue

            for cols, lkp in [
                (['appliance_group', 'consumer_type'], self._hms_ct_grs),
                (['appliance_group'],                  self._hms_grp_grs),
            ]:
                hms_df = _hms(ref_grs, cols)
                for _, r in hms_df.iterrows():
                    key = (grs,) + tuple(r[c] for c in cols) + (int(r['year']), int(r['month']))
                    lkp[key] = r['heat_m2_sum']

        # Print heat_m2 comparison for Jan
        print(f"\n  heat_m2_sum Jan-2025 (ОП,ПГ) per GRS vs global:")
        for grp in ['ОП,ПГ', 'ОП,ПГ,ВПГ']:
            glob_v = self._hms_grp_lkp.get((grp, 2025, 1), np.nan)
            for grs in sorted(self._large_grs):
                grs_v = self._hms_grp_grs.get((grs, grp, 2025, 1), np.nan)
                ratio = grs_v / glob_v if (glob_v and not np.isnan(grs_v)) else np.nan
                print(f"    {grs[:30]} | {grp:<12}: "
                      f"GRS={grs_v:.4f}  Global={glob_v:.4f}  ratio={ratio:.3f}")

    # ── Helpers: GRS-aware getters ─────────────────────────────────────────────
    def _pbl_grs(self, grs, grp, ct, pbl_fallback):
        """Return GRS-specific pbl if available, else fallback."""
        if grs not in self._large_grs:
            return pbl_fallback
        v = self._grp_sb_grs.get((grs, grp, ct))
        if v is not None:
            return v
        v = self._grp_sb_grs_grp.get((grs, grp))
        if v is not None:
            return v
        return pbl_fallback

    def _hms_grs(self, grs, grp, ct, is_alt, y, m):
        """Return GRS-specific heat_m2_sum, fallback to global chain."""
        if grs in self._large_grs:
            v = self._hms_ct_grs.get((grs, grp, ct, y, m))
            if v is not None:
                return v
            v = self._hms_ct_grs.get((grs, grp, CT_PRIV, y, m))
            if v is not None:
                return v
            v = self._hms_grp_grs.get((grs, grp, y, m))
            if v is not None:
                return v
        return self.get_hms(grp, ct, is_alt, y, m)

    # ── Override _predict_non_modem to use GRS-aware lookups ──────────────────
    def _predict_non_modem(self, periods):
        records = []
        non_modem = self._non_modem

        for (y, m) in periods:
            is_heat = m in HEATING_MONTHS

            for _, row in non_modem.iterrows():
                grp  = row['appliance_group']
                area = row['heated_area']
                pbl  = row['pbl']
                grs  = row['grs']
                aid  = row['account_id']
                ct   = row['consumer_type']
                ia   = row['is_alt']

                if row['is_dacha'] and m not in SUMMER_MONTHS:
                    continue

                pbl_eff = self._pbl_grs(grs, grp, ct, pbl)

                if row['has_OP'] and is_heat:
                    hms = self._hms_grs(grs, grp, ct, ia, y, m)
                    if np.isnan(hms):
                        continue
                    sr  = self.get_sr(grp, m)
                    vol = max(0.0, hms * area) + pbl_eff * sr
                else:
                    rs = self.get_rs(grp, y, m, ct=ct)
                    if np.isnan(rs):
                        continue
                    vol = pbl_eff * rs

                records.append({
                    'account_id': aid,
                    'appliance_group': grp,
                    'year': y, 'month': m,
                    'volume': vol,
                    'grs': grs,
                    'type': 'pred',
                })

        return pd.DataFrame(records)


# ══════════════════════════════════════════════════════════════════════════════
# Billing 2025
# ══════════════════════════════════════════════════════════════════════════════
print("=" * 65)
print("Завантаження білінгу 2025...")
ap = pd.read_csv(SUBS_FILE, low_memory=False)
ap['account_id'] = pd.to_numeric(ap['account_id'], errors='coerce')

month_cols = sorted(
    [c for c in ap.columns if c.endswith('_2025') and c.split('_')[0] in MONTH_ENG],
    key=lambda c: MONTH_ENG[c.split('_')[0]],
)
for c in month_cols:
    ap[c] = pd.to_numeric(ap[c], errors='coerce').fillna(0)

billing_rows = []
for c in month_cols:
    m = MONTH_ENG[c.split('_')[0]]
    g = ap[ap['grs'].notna()].groupby('grs')[c].sum().reset_index()
    g.columns = ['grs', 'billing']
    g['month'] = m
    g['year']  = 2025
    billing_rows.append(g)
billing_df = pd.concat(billing_rows, ignore_index=True)

print(f"  Загальний білінг 2025: {billing_df['billing'].sum():>14,.0f} м³")


# ══════════════════════════════════════════════════════════════════════════════
# Helper: run model & compute metrics
# ══════════════════════════════════════════════════════════════════════════════
def run_and_eval(cls, label, **kwargs):
    print(f"\n{'─' * 55}")
    print(f"  {label}")
    p = cls(MODEM_FILE, SUBS_FILE, mode='offline', summer_op_vpg=False, **kwargs)
    df = p.predict(years=[2025])

    agg = (
        df.groupby(['grs', 'year', 'month', 'type'])['volume']
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

    merged = pivot.merge(billing_df, on=['grs', 'year', 'month'], how='inner')
    merged = merged[merged['billing'] > 0].copy()
    merged['error']   = merged['total'] - merged['billing']
    merged['abs_err'] = merged['error'].abs()
    merged['ape']     = merged['abs_err'] / merged['billing']

    mae   = merged['abs_err'].mean()
    mape  = merged['ape'].mean() * 100
    bias  = merged['error'].sum() / merged['billing'].sum() * 100

    print(f"  MAE={mae:,.0f}  MAPE={mape:.2f}%  bias={bias:+.2f}%")
    return merged, mae, mape, bias


# ══════════════════════════════════════════════════════════════════════════════
# Run: Config A (baseline) and Config E (per-GRS)
# ══════════════════════════════════════════════════════════════════════════════
print("=" * 65)
print("Config A: global profiles (baseline)")
merged_a, mae_a, mape_a, bias_a = run_and_eval(PobUtPredictor,       'A: group_medians')

print("\n" + "=" * 65)
print("Config E: per-GRS profiles")
merged_e, mae_e, mape_e, bias_e = run_and_eval(PobUtPredictorPerGRS, 'E: per_grs')


# ══════════════════════════════════════════════════════════════════════════════
# Compare: delta MAE per GRS
# ══════════════════════════════════════════════════════════════════════════════
def grs_summary(merged, label):
    return (
        merged.groupby('grs')
        .agg(
            billing    = ('billing',  'sum'),
            total_pred = ('total',    'sum'),
            mae        = ('abs_err',  'mean'),
            mape       = ('ape',      'mean'),
            bias_sum   = ('error',    'sum'),
        )
        .assign(
            mape_pct  = lambda d: (d['mape'] * 100).round(2),
            bias_pct  = lambda d: (d['bias_sum'] / d['billing'] * 100).round(2),
            mae       = lambda d: d['mae'].round(0),
        )
        .drop(columns=['mape', 'bias_sum'])
        .rename(columns={'mae': f'mae_{label}', 'mape_pct': f'mape_{label}',
                         'bias_pct': f'bias_{label}'})
    )

grs_a = grs_summary(merged_a, 'A')
grs_e = grs_summary(merged_e, 'E')
compare = grs_a.merge(
    grs_e[[f'mae_E', f'mape_E', f'bias_E', 'total_pred']].rename(
        columns={'total_pred': 'pred_E'}
    ),
    on='grs', how='left',
)
compare['delta_mae']  = (compare['mae_E']  - compare[f'mae_A']).round(0)
compare['delta_bias'] = (compare['bias_E'] - compare[f'bias_A']).round(2)
compare = compare.sort_values('mae_A', ascending=False)

print("\n" + "=" * 65)
print("ПОРІВНЯННЯ A vs E по ГРС")
print("=" * 65)
print(f"{'ГРС':<42} {'MAE_A':>8} {'MAE_E':>8} {'ΔMAE':>8} {'Bias_A':>7} {'Bias_E':>7}")
print("-" * 82)
for _, r in compare.iterrows():
    marker = ' ◄' if abs(r['delta_mae']) > 1000 else ''
    print(f"{str(r['grs']):<42} {r['mae_A']:>8,.0f} {r['mae_E']:>8,.0f} "
          f"{r['delta_mae']:>+8,.0f} {r['bias_A']:>+6.1f}% {r['bias_E']:>+6.1f}%{marker}")
print("-" * 82)
print(f"{'WEIGHTED MAE':>42} {mae_a:>8,.0f} {mae_e:>8,.0f} {mae_e-mae_a:>+8,.0f}")

# Monthly comparison for high-error GRS
print("\n" + "=" * 65)
print("Місячний bias% для ГРС (2) м.Запоріжжя")
print("=" * 65)
grs2 = 'ГРС (2) м.Запоріжжя'
m_a  = merged_a[merged_a['grs'] == grs2].sort_values('month')
m_e  = merged_e[merged_e['grs'] == grs2].sort_values('month')
for m in range(1, 13):
    ra = m_a[m_a['month'] == m]
    re = m_e[m_e['month'] == m]
    if ra.empty:
        continue
    bias_a_m = float(ra['error'].iloc[0]) / float(ra['billing'].iloc[0]) * 100
    bias_e_m = float(re['error'].iloc[0]) / float(re['billing'].iloc[0]) * 100 if not re.empty else np.nan
    print(f"  {MONTH_UA[m]:>4}:  A={bias_a_m:>+6.1f}%  E={bias_e_m:>+6.1f}%  delta={bias_e_m-bias_a_m:>+6.1f}%")


# ══════════════════════════════════════════════════════════════════════════════
# Save
# ══════════════════════════════════════════════════════════════════════════════
print(f"\nЗберігаємо {OUT_EXCEL} ...")
with pd.ExcelWriter(OUT_EXCEL, engine='openpyxl') as xw:
    compare.to_excel(xw, sheet_name='GRS_порівняння', index=False)
    merged_a.rename(columns={'total': 'pred_A'}).to_excel(
        xw, sheet_name='Деталі_A', index=False)
    merged_e.rename(columns={'total': 'pred_E'}).to_excel(
        xw, sheet_name='Деталі_E', index=False)

print(f"[OK] {OUT_EXCEL}")
print(f"\n>>> A (global):  MAE={mae_a:,.0f}  MAPE={mape_a:.2f}%  bias={bias_a:+.2f}%")
print(f">>> E (per-GRS): MAE={mae_e:,.0f}  MAPE={mape_e:.2f}%  bias={bias_e:+.2f}%")
print(f">>> ΔMAE = {mae_e - mae_a:+,.0f} м³/ГРС/місяць")
print("\n=== DONE ===")
