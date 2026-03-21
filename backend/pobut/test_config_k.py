"""Швидкий тест Config K / K_v2 / K_v3 (standalone)."""
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd

from pobut_predictor import (
    PobUtPredictor, CT_PRIV, HEATING_MONTHS, SUMMER_MONTHS, GROUP_MERGE,
)

MODEM_FILE = 'data/input/profile_pobut_daily_result.csv'
SUBS_FILE  = 'data/input/all_pobut_enriched.csv'
MONTH_ENG  = {'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,
               'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12}
MONTH_UA   = {1:'Січ',2:'Лют',3:'Бер',4:'Кві',5:'Тра',6:'Чер',
               7:'Лип',8:'Сер',9:'Вер',10:'Жов',11:'Лис',12:'Гру'}
LARGE_GRS_THRESH = 5_000_000


# ══════════════════════════════════════════════════════════════════════════════
class PobUtPredictorConfigK(PobUtPredictor):
    """
    Config K: vol_i(m) = k_i × P_modem(m)

    Зима ОП:   k_i_heat × area_i × P_heat(m)
    Літо ОП:   k_i_summer × P_op_summer(m)
    ПГ/ПГ,ВПГ: k_i_pg × P_pg(m)
    """
    KI_HEAT_CSV    = 'data/profiles/ki_heating_consumers.csv'
    KI_NONHEAT_CSV = 'data/profiles/ki_nonheat_consumers.csv'
    P_NONHEAT_CSV  = 'data/profiles/p_modem_nonheat.csv'

    P_HEAT = {1: 4.3167, 2: 4.3717, 3: 2.7025, 4: 1.3619,
              10: 1.1902, 11: 2.7532, 12: 3.9867}

    def _prepare_non_modem(self):
        super()._prepare_non_modem()
        self._load_k_profiles()

    def _load_k_profiles(self):
        # P(m) не-опалення
        p_df = pd.read_csv(self.P_NONHEAT_CSV)
        self._p_nonheat = {
            (r['appliance_group'], r['consumer_type'], int(r['month'])): float(r['P_modem_median'])
            for _, r in p_df.iterrows()
        }

        # k_i heating
        ki_h = pd.read_csv(self.KI_HEAT_CSV,
                           usecols=['account_id','grs','appliance_group','consumer_type','k_i_clipped'])
        ki_h['account_id'] = pd.to_numeric(ki_h['account_id'], errors='coerce')
        ki_h = ki_h[ki_h['k_i_clipped'].notna() & (ki_h['k_i_clipped'] > 0)]
        self._ki_heat_aid    = dict(zip(ki_h['account_id'].astype(int), ki_h['k_i_clipped'].astype(float)))
        self._ki_heat_grs    = ki_h.groupby(['grs','appliance_group','consumer_type'])['k_i_clipped'].median().to_dict()
        self._ki_heat_global = ki_h.groupby(['appliance_group','consumer_type'])['k_i_clipped'].median().to_dict()

        # k_i nonheat
        ki_nh = pd.read_csv(self.KI_NONHEAT_CSV,
                            usecols=['account_id','grs','appliance_group','consumer_type','k_i_clipped'])
        ki_nh['account_id'] = pd.to_numeric(ki_nh['account_id'], errors='coerce')
        ki_nh = ki_nh[ki_nh['k_i_clipped'].notna() & (ki_nh['k_i_clipped'] > 0)]
        self._ki_nh_aid    = dict(zip(ki_nh['account_id'].astype(int), ki_nh['k_i_clipped'].astype(float)))
        self._ki_nh_grs    = ki_nh.groupby(['grs','appliance_group','consumer_type'])['k_i_clipped'].median().to_dict()
        self._ki_nh_global = ki_nh.groupby(['appliance_group','consumer_type'])['k_i_clipped'].median().to_dict()

        print(f"  P_nonheat={len(self._p_nonheat)}, "
              f"ki_heat={len(self._ki_heat_aid):,}, ki_nonheat={len(self._ki_nh_aid):,}")
        self._build_nm_arrays()

    def _build_nm_arrays(self):
        nm   = self._non_modem
        n    = len(nm)
        aids = nm['account_id'].values.astype(int)
        grps = nm['appliance_group'].values
        cts  = nm['consumer_type'].values
        grs  = nm['grs'].values

        ki_h  = np.full(n, 0.85, dtype=np.float32)
        ki_nh = np.full(n, 1.0,  dtype=np.float32)

        for i in range(n):
            aid, grp, ct, g = int(aids[i]), grps[i], cts[i], grs[i]

            v = (self._ki_heat_aid.get(aid)
                 or self._ki_heat_grs.get((g, grp, ct))
                 or self._ki_heat_grs.get((g, grp, CT_PRIV))
                 or self._ki_heat_global.get((grp, ct))
                 or self._ki_heat_global.get((grp, CT_PRIV), 0.85))
            ki_h[i] = float(v or 0.85)

            v = (self._ki_nh_aid.get(aid)
                 or self._ki_nh_grs.get((g, grp, ct))
                 or self._ki_nh_grs.get((g, grp, CT_PRIV))
                 or self._ki_nh_global.get((grp, ct))
                 or self._ki_nh_global.get((grp, CT_PRIV), 1.0))
            ki_nh[i] = float(v or 1.0)

        nm['_ki_heat']    = ki_h
        nm['_ki_nonheat'] = ki_nh

        # P_nonheat matrix [n × 12]
        p_mat = np.zeros((n, 12), dtype=np.float32)
        for m in range(1, 13):
            for i in range(n):
                p_mat[i, m-1] = self._p_nonheat.get((grps[i], cts[i], m), 0.0)
        self._p_nonheat_matrix = p_mat

        op = nm['has_OP'].values
        print(f"  ki_heat  med(ОП)={float(np.median(ki_h[op])):.3f}")
        print(f"  ki_nonheat med   ={float(np.median(ki_nh)):.3f}")

    def _predict_non_modem(self, periods):
        nm      = self._non_modem
        aids    = nm['account_id'].values
        grps    = nm['appliance_group'].values
        grs_v   = nm['grs'].values
        areas   = nm['heated_area'].values.astype(np.float64)
        has_op  = nm['has_OP'].values
        is_dach = nm['is_dacha'].values
        ki_h    = nm['_ki_heat'].values.astype(np.float64)
        ki_nh   = nm['_ki_nonheat'].values.astype(np.float64)
        p_mat   = self._p_nonheat_matrix.astype(np.float64)

        records = []
        for (y, m) in periods:
            is_heat = m in HEATING_MONTHS
            active  = ~is_dach | np.isin(m, list(SUMMER_MONTHS))
            p_col   = p_mat[:, m - 1]
            vols    = np.zeros(len(nm))

            if is_heat:
                p_h = self.P_HEAT.get(m, 0.0)
                op  = active & has_op
                vols[op] = ki_h[op] * areas[op] * p_h
            else:
                op = active & has_op
                vols[op] = ki_nh[op] * p_col[op]

            non_op = active & ~has_op
            vols[non_op] = ki_nh[non_op] * p_col[non_op]

            vols = np.maximum(vols, 0.0)
            mask = (vols > 0) & active
            if not mask.any():
                continue

            records.append(pd.DataFrame({
                'account_id':      aids[mask],
                'appliance_group': grps[mask],
                'year': y, 'month': m,
                'volume':          vols[mask],
                'grs':             grs_v[mask],
                'type':            'pred',
            }))

        return pd.concat(records, ignore_index=True) if records else pd.DataFrame()


# ══════════════════════════════════════════════════════════════════════════════
# K_v2: P_HEAT = фактичний modem за конкретний рік (2025)
# Логіка: модемні споживачі = датчики поточної погоди
# ══════════════════════════════════════════════════════════════════════════════
class PobUtPredictorConfigK_v2(PobUtPredictorConfigK):
    """
    Як Config K, але P_HEAT(m) береться з реального моdemного
    споживання за той самий рік (не з mid-year average).
    2025 actual modem medians (м³/м²/month).
    """
    P_HEAT = {1: 3.8374, 2: 4.3598, 3: 2.7025, 4: 1.3619,
              10: 1.4048, 11: 2.3105, 12: 3.7630}


# ══════════════════════════════════════════════════════════════════════════════
# K_v3: P_HEAT = P_typical × (HDH_actual / HDH_typical)
# Логіка: типовий профіль + поправка на фактичну погоду
# Для майбутніх прогнозів — не потрібні modемні дані, тільки погода
# ══════════════════════════════════════════════════════════════════════════════
def _compute_pheat_v3():
    """Обчислює HDH-нормалізований P_HEAT для 2025."""
    w = pd.read_csv('../data/weather_2025_daily_contractual.csv', parse_dates=['date'])
    w['month'] = w['date'].dt.month
    w['year']  = w['date'].dt.year
    wm = w.groupby(['year','month'])['hdh_18c'].sum().reset_index()
    wm.columns = ['year','month','hdh18']

    # Типове HDH = середнє по доступних роках
    hdh_typical = wm.groupby('month')['hdh18'].mean()

    # P_typical = 2-річне середнє по modемних даних
    P_typical = {1:4.3167, 2:4.3717, 3:2.7025, 4:1.3619,
                 10:1.1902, 11:2.7532, 12:3.9867}

    p_v3 = {}
    for m, pt in P_typical.items():
        hdh_act = wm[(wm['year']==2025) & (wm['month']==m)]['hdh18'].values
        hdh_typ = hdh_typical.get(m, None)
        if len(hdh_act) and hdh_typ and hdh_typ > 0:
            ratio = float(hdh_act[0]) / float(hdh_typ)
            p_v3[m] = round(pt * ratio, 4)
        else:
            p_v3[m] = pt
    return p_v3

_P_HEAT_V3 = _compute_pheat_v3()
print("P_HEAT порівняння:")
P_avg = {1:4.3167, 2:4.3717, 3:2.7025, 4:1.3619, 10:1.1902, 11:2.7532, 12:3.9867}
P_v2  = PobUtPredictorConfigK_v2.P_HEAT
m_ua  = {1:'Січ',2:'Лют',3:'Бер',4:'Кві',10:'Жов',11:'Лис',12:'Гру'}
print(f"  {'Міс':5s}  {'P_avg(K)':>9s}  {'P_v2(modem25)':>14s}  {'P_v3(HDH)':>10s}")
for m in [10,11,12,1,2,3,4]:
    print(f"  {m_ua[m]:5s}  {P_avg[m]:>9.4f}  {P_v2[m]:>14.4f}  {_P_HEAT_V3[m]:>10.4f}")

class PobUtPredictorConfigK_v3(PobUtPredictorConfigK):
    """P_HEAT нормалізований на HDH фактичної погоди 2025."""
    P_HEAT = _P_HEAT_V3


# ══════════════════════════════════════════════════════════════════════════════
# K_v4: Без binary switch. ОП — один профіль на весь рік з modемних даних 2025.
# Логіка: modem consumers = sensor для кожного місяця (включно травень/вересень).
# k_i = billing_annual / (area × P_annual_sum)  — калібрується по річному білінгу.
# ══════════════════════════════════════════════════════════════════════════════

# P_modem 2025 full-year (all 12 months, median spec consumption м³/м²/month)
_P_MODEM_FULL_25 = {
    1: 3.8374, 2: 4.3598, 3: 2.7025, 4: 1.3619,
    5: 0.3534, 6: 0.2102, 7: 0.1983, 8: 0.2060, 9: 0.2587,
    10: 1.4048, 11: 2.3105, 12: 3.7630,
}
_P_SUM_FULL_25 = sum(_P_MODEM_FULL_25.values())   # 20.9665
_P_SUM_HEAT    = sum(PobUtPredictorConfigK_v2.P_HEAT.values())  # 19.7398 (2025 heating only)

print(f"P_SUM full-year 2025 = {_P_SUM_FULL_25:.4f}  (heat-only = {_P_SUM_HEAT:.4f})")


class PobUtPredictorConfigK_v4(PobUtPredictorConfigK_v2):
    """
    K_v4: ОП без перемикань, ki_heat × area × P_modem_full.
    НЕ масштабуємо k_i — використовуємо ki_heat як є для всіх місяців.
    Логіка: щоденний modem aggregate = природний індикатор опалення.
    Немодемні (ПГ/ПГ,ВПГ): без змін (P_nonheat).
    """
    # P_HEAT_FULL = full 12-month modem profile (м³/м²/month)
    P_HEAT_FULL = _P_MODEM_FULL_25

    def _predict_non_modem(self, periods):
        nm      = self._non_modem
        aids    = nm['account_id'].values
        grps    = nm['appliance_group'].values
        grs_v   = nm['grs'].values
        areas   = nm['heated_area'].values.astype(np.float64)
        has_op  = nm['has_OP'].values
        is_dach = nm['is_dacha'].values
        # Без масштабування — ki_heat як є
        ki_h    = nm['_ki_heat'].values.astype(np.float64)
        ki_nh   = nm['_ki_nonheat'].values.astype(np.float64)
        p_mat   = self._p_nonheat_matrix.astype(np.float64)

        records = []
        for (y, m) in periods:
            active  = ~is_dach | np.isin(m, list(SUMMER_MONTHS))
            vols    = np.zeros(len(nm))

            # ОП: ki_heat × area × P_modem(m) для ВСІХ 12 місяців
            p_h  = self.P_HEAT_FULL.get(m, 0.0)
            op   = active & has_op
            vols[op] = ki_h[op] * areas[op] * p_h

            # Не-ОП: P_nonheat як раніше
            p_col  = p_mat[:, m - 1]
            non_op = active & ~has_op
            vols[non_op] = ki_nh[non_op] * p_col[non_op]

            vols = np.maximum(vols, 0.0)
            mask = (vols > 0) & active
            if not mask.any():
                continue

            records.append(pd.DataFrame({
                'account_id':      aids[mask],
                'appliance_group': grps[mask],
                'year': y, 'month': m,
                'volume':          vols[mask],
                'grs':             grs_v[mask],
                'type':            'pred',
            }))

        return pd.concat(records, ignore_index=True) if records else pd.DataFrame()


# ══════════════════════════════════════════════════════════════════════════════
# K_v5: Щоденна класифікація опалення / літа за граничним питомим споживанням
# vol_OP(m) = ki_heat×area×heat_factor(m) + ki_nh×P_nonheat(m)×summer_frac(m)
# Де heat_factor(m) = Σ p_day(d) для d: median_spec(d) > THRESHOLD   [м³/м²]
#    summer_frac(m) = n_summer_days / n_days_in_month
# ══════════════════════════════════════════════════════════════════════════════

def _load_daily_op_specs(modem_file):
    """Медіана питомого споживання ОП-модемних (м³/м²/день) по кожному дню."""
    md = pd.read_csv(modem_file, sep=';', low_memory=False)
    if 'appliance_group' in md.columns:
        md['appliance_group'] = md['appliance_group'].replace(GROUP_MERGE)
    md['heated_area'] = pd.to_numeric(md['heated_area'], errors='coerce')
    op_mask = md['appliance_group'].str.contains('ОП', na=False) & (md['heated_area'] > 0)
    for col in ['gas_off', 'alternative', 'dacha']:
        if col in md.columns:
            op_mask = op_mask & md[col].isna()
    md_op  = md[op_mask].copy()
    areas  = md_op['heated_area'].values.astype(np.float64)
    date_cols = [c for c in md.columns
                 if isinstance(c, str) and c.count('.') == 2 and len(c) == 10]
    daily_spec = {}
    for col in date_cols:
        try:
            d_s, m_s, y_s = col.split('.')
            date = pd.Timestamp(f'{y_s}-{m_s}-{d_s}')
        except Exception:
            continue
        vals  = pd.to_numeric(md_op[col], errors='coerce').values.astype(np.float64)
        valid = (areas > 0) & np.isfinite(vals)
        if valid.sum() < 5:
            continue
        daily_spec[date] = float(np.median(vals[valid] / areas[valid]))
    return daily_spec


def _monthly_factors(daily_spec, threshold, year=2025):
    """
    heat_factor[m] = Σ p_day для днів де median_spec > threshold, year==year (м³/м²)
    summer_frac[m] = частка днів де median_spec <= threshold

    Фільтрація по року необхідна: файл містить 2024, 2025 і 2026 дані.
    """
    by_month = {}
    for d, spec in daily_spec.items():
        if d.year == year:
            by_month.setdefault(d.month, []).append(spec)
    hf, sf = {}, {}
    for m, specs in by_month.items():
        arr   = np.array(specs)
        hf[m] = float(arr[arr > threshold].sum())
        sf[m] = float((arr <= threshold).mean())
    return hf, sf


class PobUtPredictorConfigK_v5(PobUtPredictorConfigK_v2):
    """
    K_v5: щоденна класифікація опалення/літа за граничним питомим споживанням.
    Для ОП:    vol(m) = ki_heat×area×heat_factor(m) + ki_nh×P_nonheat(m)×summer_frac(m)
    Для ПГ/ВПГ: без змін (ki_nh × P_nonheat(m)).
    """
    THRESHOLD = 0.013  # м³/м²/день

    def _load_k_profiles(self):
        super()._load_k_profiles()
        self._heat_factor, self._summer_frac = _monthly_factors(
            _DAILY_OP_SPECS, self.THRESHOLD
        )
        print(f"  K_v5 T={self.THRESHOLD:.3f}  "
              f"(Σheat_f={sum(self._heat_factor.values()):.4f} м³/м²):")
        for _m in sorted(self._heat_factor):
            _hf = self._heat_factor[_m]
            _sf = self._summer_frac[_m]
            if _hf > 0 or _sf < 1.0:
                print(f"    {MONTH_UA.get(_m,'?'):5s}: "
                      f"heat_f={_hf:.4f}  summer_frac={_sf:.3f}")

    def _predict_non_modem(self, periods):
        nm      = self._non_modem
        aids    = nm['account_id'].values
        grps    = nm['appliance_group'].values
        grs_v   = nm['grs'].values
        areas   = nm['heated_area'].values.astype(np.float64)
        has_op  = nm['has_OP'].values
        is_dach = nm['is_dacha'].values
        ki_h    = nm['_ki_heat'].values.astype(np.float64)
        ki_nh   = nm['_ki_nonheat'].values.astype(np.float64)
        p_mat   = self._p_nonheat_matrix.astype(np.float64)

        records = []
        for (y, m) in periods:
            active = ~is_dach | np.isin(m, list(SUMMER_MONTHS))
            vols   = np.zeros(len(nm))
            p_col  = p_mat[:, m - 1]
            heat_f = self._heat_factor.get(m, 0.0)
            sum_fr = self._summer_frac.get(m, 1.0)

            # OP: heating days component + summer days component
            op = active & has_op
            vols[op] = ki_h[op] * areas[op] * heat_f + ki_nh[op] * p_col[op] * sum_fr

            # non-OP: P_nonheat unchanged
            non_op = active & ~has_op
            vols[non_op] = ki_nh[non_op] * p_col[non_op]

            vols = np.maximum(vols, 0.0)
            mask = (vols > 0) & active
            if not mask.any():
                continue

            records.append(pd.DataFrame({
                'account_id':      aids[mask],
                'appliance_group': grps[mask],
                'year': y, 'month': m,
                'volume':          vols[mask],
                'grs':             grs_v[mask],
                'type':            'pred',
            }))

        return pd.concat(records, ignore_index=True) if records else pd.DataFrame()


# ══════════════════════════════════════════════════════════════════════════════
# Білінг-еталон
# ══════════════════════════════════════════════════════════════════════════════
print("\nЗавантаження щоденних modem spec для K_v5 ...")
_DAILY_OP_SPECS = _load_daily_op_specs(MODEM_FILE)
_THRESHOLDS_V5  = [0.010, 0.013, 0.015, 0.017, 0.020, 0.025]
print(f"  Завантажено {len(_DAILY_OP_SPECS)} дні/ів. Threshold sweep (2025, опал. днів / всього):")
print(f"  {'Міс':5s}  " + "  ".join(f"{t:.3f}" for t in _THRESHOLDS_V5))
for _m in [1,2,3,4,5,6,7,8,9,10,11,12]:
    _days_m = [(d, s) for d, s in _DAILY_OP_SPECS.items() if d.month == _m and d.year == 2025]
    if not _days_m:
        continue
    _row = [f"{MONTH_UA.get(_m,'?'):5s}"]
    for _t in _THRESHOLDS_V5:
        _hd = sum(1 for _, s in _days_m if s > _t)
        _row.append(f"{_hd:>4d}/{len(_days_m)}")
    print("  " + "  ".join(_row))

print("Завантаження білінгу...")
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
billing_df['billing'] = billing_df['billing'].clip(lower=0)

grs_annual    = billing_df.groupby('grs')['billing'].sum()
large_grs_set = set(grs_annual[grs_annual >= LARGE_GRS_THRESH].index)
print(f"Великих ГРС: {len(large_grs_set)}  Всього: {billing_df['grs'].nunique()}")

# ══════════════════════════════════════════════════════════════════════════════
# Запуск всіх трьох варіантів
# ══════════════════════════════════════════════════════════════════════════════
def run_variant(cls, label):
    print(f"\n{'='*60}")
    print(f"Запуск {label} ...")
    p = cls(MODEM_FILE, SUBS_FILE, mode='offline', summer_op_vpg=False)
    df = p.predict(years=[2025])

    agg   = df.groupby(['grs','year','month','type'])['volume'].sum().reset_index()
    pivot = agg.pivot_table(index=['grs','year','month'], columns='type',
                            values='volume', aggfunc='sum', fill_value=0).reset_index()
    pivot.columns.name = None
    pivot['fact']  = pivot.get('fact', 0)
    pivot['pred']  = pivot.get('pred', 0)
    pivot['total'] = pivot['fact'] + pivot['pred']

    m = pivot.merge(billing_df, on=['grs','year','month'], how='inner')
    m = m[m['billing'] > 0].copy()
    m['error']   = m['total'] - m['billing']
    m['abs_err'] = m['error'].abs()
    m['bias_pct']= m['error'] / m['billing'] * 100
    m['ape']     = m['abs_err'] / m['billing']
    return m

merged_k   = run_variant(PobUtPredictorConfigK,       'K   (P_avg 2-year)')
merged_v2  = run_variant(PobUtPredictorConfigK_v2,    'K_v2 (P_modem 2025)')
merged_v3  = run_variant(PobUtPredictorConfigK_v3,    'K_v3 (P_HDH-norm)')
merged_v4  = run_variant(PobUtPredictorConfigK_v4,    'K_v4 (full-year, no switch)')

# K_v5: threshold sweep
def make_k_v5(threshold):
    """Фабрика підкласів K_v5 з різними THRESHOLD."""
    class _K(PobUtPredictorConfigK_v5):
        THRESHOLD = threshold
    _K.__name__ = f'K_v5_T{threshold:.3f}'
    return _K

v5_merged = {}
for _thr in _THRESHOLDS_V5:
    v5_merged[_thr] = run_variant(make_k_v5(_thr), f'K_v5(T={_thr:.3f})')

# ── Зведена таблиця по варіантах ─────────────────────────────────────────────
def summary(merged, label):
    mae  = merged['abs_err'].mean()
    mape = merged['ape'].mean() * 100
    bias = merged['error'].sum() / merged['billing'].sum() * 100
    lm   = merged['grs'].isin(large_grs_set)
    wl   = (merged[lm]['bias_pct'].abs()  <= 3).mean() * 100
    ws   = (merged[~lm]['bias_pct'].abs() <= 6).mean() * 100
    return dict(label=label, mae=mae, mape=mape, bias=bias, wl=wl, ws=ws)

results = [
    summary(merged_k,  'K   (P_avg)'),
    summary(merged_v2, 'K_v2(modem25)'),
    summary(merged_v3, 'K_v3(HDH)'),
    summary(merged_v4, 'K_v4(full,no-sw)'),
] + [summary(v5_merged[_thr], f'K_v5(T={_thr:.3f})') for _thr in _THRESHOLDS_V5]

print(f"\n{'='*72}")
print("ЗВЕДЕНІ РЕЗУЛЬТАТИ")
print(f"{'='*72}")
print(f"  {'Варіант':16s}  {'MAE':>8s}  {'MAPE':>6s}  {'bias':>7s}  {'Вел≤3%':>7s}  {'Мал≤6%':>7s}")
print(f"  {'-'*67}")
for r in results:
    print(f"  {r['label']:16s}  {r['mae']:>8,.0f}  {r['mape']:>5.2f}%  {r['bias']:>+6.2f}%"
          f"  {r['wl']:>6.1f}%  {r['ws']:>6.1f}%")

# ── Помісячно: bias для кожного варіанту ─────────────────────────────────────
print(f"\n{'='*72}")
print("ПОМІСЯЧНИЙ BIAS")
print(f"{'='*72}")
print(f"  {'Міс':5s}  {'K(avg)':>8s}  {'K_v2(25)':>10s}  {'K_v3(HDH)':>11s}  {'K_v4(full)':>12s}  {'billing М³':>12s}")
print(f"  {'-'*68}")
for m_i in range(1, 13):
    sk  = merged_k[merged_k['month']==m_i]
    sv2 = merged_v2[merged_v2['month']==m_i]
    sv3 = merged_v3[merged_v3['month']==m_i]
    sv4 = merged_v4[merged_v4['month']==m_i]
    if sk.empty: continue
    bk  = sk['error'].sum()  / sk['billing'].sum()  * 100
    bv2 = sv2['error'].sum() / sv2['billing'].sum() * 100
    bv3 = sv3['error'].sum() / sv3['billing'].sum() * 100
    bv4 = sv4['error'].sum() / sv4['billing'].sum() * 100
    bil = sk['billing'].sum()
    print(f"  {MONTH_UA[m_i]:5s}  {bk:>+7.2f}%  {bv2:>+9.2f}%  {bv3:>+10.2f}%  {bv4:>+11.2f}%  {bil:>12,.0f}")

# ── Детально по ГРС для найкращого варіанту ──────────────────────────────────
best_bias = min(results, key=lambda r: abs(r['bias']))
best_merg = {'K   (P_avg)': merged_k,
             'K_v2(modem25)': merged_v2,
             'K_v3(HDH)': merged_v3,
             'K_v4(full,no-sw)': merged_v4}[best_bias['label']]
print(f"\n=== Деталі по ГРС ({best_bias['label']}) ===")
print(f"  {'ГРС':45s}  {'bias%':>7s}  {'MAE':>8s}")
print("  " + "-"*65)
for grs, sub in best_merg.groupby('grs'):
    b  = sub['error'].sum() / sub['billing'].sum() * 100
    e  = sub['abs_err'].mean()
    lg = '  [L]' if grs in large_grs_set else ''
    fl = ' !' if abs(b) > (3 if grs in large_grs_set else 6) else '  '
    print(f"  {fl}{grs[:43]:43s}  {b:>+7.2f}%  {e:>8,.0f}{lg}")

# ── K_v5: помісячний bias по threshold ───────────────────────────────────────
print(f"\n{'='*90}")
print("K_v5: ПОМІСЯЧНИЙ BIAS ПО ПОРОГУ (порівняння з K_v2)")
print(f"{'='*90}")
print(f"  {'Міс':5s}  {'K_v2':>9s}  " + "  ".join(f"T={t:.3f}" for t in _THRESHOLDS_V5))
print("  " + "-"*80)
for m_i in range(1, 13):
    sv2 = merged_v2[merged_v2['month'] == m_i]
    if sv2.empty:
        continue
    bv2 = sv2['error'].sum() / sv2['billing'].sum() * 100
    _row = [f"  {MONTH_UA[m_i]:5s}  {bv2:>+8.2f}%"]
    for _thr in _THRESHOLDS_V5:
        sv5 = v5_merged[_thr][v5_merged[_thr]['month'] == m_i]
        bv5 = sv5['error'].sum() / sv5['billing'].sum() * 100 if not sv5.empty else 0.0
        _row.append(f"{bv5:>+9.2f}%")
    print("  ".join(_row))

print("\n=== DONE ===")
