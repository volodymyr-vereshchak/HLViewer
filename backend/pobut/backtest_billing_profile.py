"""
backtest_billing_profile.py

Config F / G / H: Billing-Profile моделі.

ЛОГІКА Config F (billing_profile):
  Для не-ОП (ПГ, ПГ,ВПГ): немає модемних даних (1 ПГ модем!) →
      vol_i_m = sb_i × SR_bill[grp, ct, m]
  Для ОП-нагрів: модемний heat_m2_sum + pbl × pg_seasonal_ratio
  Для ОП-літо:   pbl × modem_rate_sum  ← ПРОБЛЕМА: rate_sum≈1.0, SR_bill<1.0

ЛОГІКА Config G (proxy_group):
  Розширення Config F: для груп < MIN_MODEM_COUNT шукаємо proxy-групу
  + billing cross-ratio. На практиці єдина low-modem група — ПГ Приватний
  (не-ОП) → billing SR вже її покриває → G = F.

ЛОГІКА Config H (billing_sr_everywhere):
  Виправляє проблему rate_sum≈1.0 в ОП-літо:
    ОП-нагрів:  area × heat_m2_sum_modem + pbl × billing_sr[m]
    ОП-літо:    pbl × billing_sr[m]          ← billing SR замість rate_sum
    Не-ОП:      pbl × billing_sr[m]          (без змін)
  Всюди: pbl = individual summer_baseline, billing_sr = global з 10K+ споживачів.

ПОРІВНЯННЯ: Config A vs F vs G vs H
МЕТРИКА:   bias% per (GRS, month)   ← ціль: великі ГРС ≤3%, інші ≤6%
"""
import sys
import io
import os

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from pobut_predictor import (
    PobUtPredictor, CT_PRIV,
    HEATING_MONTHS, SUMMER_MONTHS,
)

MODEM_FILE = 'data/input/profile_pobut_daily_result.csv'
SUBS_FILE  = 'data/input/all_pobut_enriched.csv'
OUT_EXCEL  = 'data/backtest_billing_profile.xlsx'

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
LARGE_GRS_THRESH = 5_000_000   # річний білінг > 5M → "великий ГРС"
MIN_MODEM_COUNT  = 20          # мінімум модемів для "хорошого" покриття групи


# ══════════════════════════════════════════════════════════════════════════════
# Model F: Billing-Profile Predictor
# ══════════════════════════════════════════════════════════════════════════════

class PobUtPredictorBillingProfile(PobUtPredictor):
    """
    Замінює noisy modem rate_sum для не-ОП груп на стабільний
    білінговий сезонний коефіцієнт, обчислений з тисяч споживачів.
    Для ОП-опалення залишає модемний heat_m2_sum.
    """

    def _prepare_non_modem(self):
        # 1. Обчислюємо білінгові сезонні коефіцієнти
        self._build_billing_sr()
        # 2. Стандартна підготовка не-модемних
        super()._prepare_non_modem()
        # 3. Перезаписуємо pbl = individual summer_baseline (де є)
        sb_map = (
            self._all_pobut
            .assign(_sb=lambda d: pd.to_numeric(d['summer_baseline'], errors='coerce'))
            .set_index('account_id')['_sb']
            .to_dict()
        )
        nm = self._non_modem
        nm['_sb'] = nm['account_id'].map(sb_map)
        valid = nm['_sb'].notna() & (nm['_sb'] > 0)
        nm.loc[valid, 'pbl'] = nm.loc[valid, '_sb']
        nm.drop(columns=['_sb'], inplace=True)
        print(f"  pbl: individual_sb={valid.sum():,}  "
              f"group_fallback={( ~valid).sum():,}")

    def _build_billing_sr(self):
        """
        Обчислює SR_bill[grp, ct, month] = median_billing_m / median_billing_summer.
        Використовує ВСІ (~222K) споживачів з all_pobut_enriched.
        """
        ap = self._all_pobut.copy()
        ap['account_id'] = pd.to_numeric(ap['account_id'], errors='coerce')

        bill_cols = {}
        for name, m in MONTH_ENG.items():
            col = f'{name}_2025'
            if col in ap.columns:
                ap[col] = pd.to_numeric(ap[col], errors='coerce').fillna(0)
                bill_cols[m] = col

        GROUP_MERGE_L = {'ОП,ВПГ': 'ОП,ПГ,ВПГ', 'ОП': 'ОП,ПГ'}
        ap['appliance_group'] = (
            ap['appliance_group'].astype(str).str.strip().replace(GROUP_MERGE_L)
        )
        ap['consumer_type'] = ap['consumer_type'].fillna(CT_PRIV)

        summer_cols = [bill_cols[m] for m in [6, 7, 8] if m in bill_cols]
        ap['_summer_avg'] = ap[summer_cols].mean(axis=1)

        # Використовуємо тільки активних не-нульових (є літнє споживання)
        valid = ap[ap['_summer_avg'] > 0.5].copy()

        self._billing_sr = {}         # (grp, ct, m) → SR
        self._billing_grp_sb = {}     # (grp, ct)    → median summer (pbl fallback)
        self._billing_med_m = {}      # (grp, ct, m) → median billing для cross-ratio

        for (grp, ct), gdf in valid.groupby(['appliance_group', 'consumer_type']):
            summer_med = gdf['_summer_avg'].median()
            if summer_med <= 0:
                continue
            self._billing_grp_sb[(grp, ct)] = float(summer_med)
            for m, col in bill_cols.items():
                month_med = gdf[col].median()
                self._billing_sr[(grp, ct, m)] = float(month_med) / float(summer_med)
                self._billing_med_m[(grp, ct, m)] = float(month_med)

        print(f"  Billing SR: {len(self._billing_sr)} entries "
              f"({len(self._billing_grp_sb)} groups)")

        # Діагностика
        print("  SR приклад (Зима/Літо порівняння):")
        for grp, ct_sample in [('ПГ', 'Багатоквартирний сектор'),
                                ('ПГ,ВПГ', 'Багатоквартирний сектор'),
                                ('ОП,ПГ', CT_PRIV)]:
            vals = {m: self._billing_sr.get((grp, ct_sample, m), np.nan)
                    for m in [1, 7, 12]}
            sb = self._billing_grp_sb.get((grp, ct_sample), np.nan)
            print(f"    {grp:<15} {ct_sample[:20]:<20}: "
                  f"SR[Січ]={vals[1]:.3f}  SR[Лип]={vals[7]:.3f}  "
                  f"SR[Гру]={vals[12]:.3f}  sb_med={sb:.2f}")

    def _predict_non_modem(self, periods):
        records = []
        nm = self._non_modem

        for (y, m) in periods:
            is_heat = m in HEATING_MONTHS

            for _, row in nm.iterrows():
                grp  = row['appliance_group']
                area = row['heated_area']
                pbl  = row['pbl']
                grs  = row['grs']
                aid  = row['account_id']
                ct   = row['consumer_type']
                ia   = row['is_alt']

                if row['is_dacha'] and m not in SUMMER_MONTHS:
                    continue

                if row['has_OP'] and is_heat:
                    # ОП опалення: модемний heat_m2_sum + individual pbl
                    hms = self.get_hms(grp, ct, ia, y, m)
                    if np.isnan(hms):
                        continue
                    sr  = self.get_sr(grp, m)
                    vol = max(0.0, hms * area) + pbl * sr

                elif row['has_OP']:
                    # ОП літо: модемний rate_sum + individual pbl
                    rs = self.get_rs(grp, y, m, ct=ct)
                    if np.isnan(rs):
                        continue
                    vol = pbl * rs

                else:
                    # Не-ОП (ПГ, ПГ,ВПГ): білінговий SR + individual pbl
                    sr_b = self._billing_sr.get((grp, ct, m))
                    if sr_b is not None:
                        vol = pbl * sr_b
                    else:
                        # Fallback: modem rate_sum (для груп без білінгових SR)
                        rs = self.get_rs(grp, y, m, ct=ct)
                        if np.isnan(rs):
                            continue
                        vol = pbl * rs

                records.append({
                    'account_id': aid, 'appliance_group': grp,
                    'year': y, 'month': m, 'volume': vol,
                    'grs': grs, 'type': 'pred',
                })

        return pd.DataFrame(records)


# ══════════════════════════════════════════════════════════════════════════════
# Model G: Proxy-Group Predictor
# ══════════════════════════════════════════════════════════════════════════════

class PobUtPredictorProxyGroup(PobUtPredictorBillingProfile):
    """
    Config G: розширення Config F — для груп з < MIN_MODEM_COUNT модемів
    знаходить proxy-групу та обчислює міжгруповий коефіцієнт з білінгових медіан.

    Ключова ідея:
      hms_adj[low_grp, m] = hms[proxy_grp, m] × k_heat[m]
      k_heat[m]           = bill_heat(low_grp, m) / bill_heat(proxy_grp, m)

    де bill_heat(grp, m) = max(0, billing_med_m - billing_summer_avg)
    — обчислена з тисяч споживачів, тому стабільна.

    Proxy-вибір: той самий consumer_type якщо можливо, інакше будь-який;
    з максимальною кількістю модемів серед кандидатів.
    """

    def _prepare_non_modem(self):
        # Config F: будує _billing_sr + _billing_grp_sb + individual pbl
        super()._prepare_non_modem()
        # Тепер _billing_sr доступний — будуємо proxy-маппінг
        self._build_proxy_mapping()

    def _build_proxy_mapping(self):
        """
        Для кожної (grp, ct) з < MIN_MODEM_COUNT модемів:
          1. Знаходимо proxy (той самий CT > більше CT; найбільше модемів)
          2. cross_ratio_heat[m]   = bill_heat(tgt,m) / bill_heat(proxy,m)
          3. cross_ratio_summer[m] = bill_med_m(tgt)  / bill_med_m(proxy)
        """
        if self._modem_monthly is None:
            self._proxy_assign = {}
            return

        # Кількість унікальних модемів на (grp, ct)
        modem_counts = (
            self._modem_monthly
            .groupby(['appliance_group', 'consumer_type'])['account_id']
            .nunique()
        )

        good_pairs = {gc for gc, n in modem_counts.items() if n >= MIN_MODEM_COUNT}

        # Білінгові місячні медіани: (grp, ct, m) → abs median м³
        def bill_m(g, c, m):
            return self._billing_med_m.get((g, c, m), 0.0)

        self._proxy_assign = {}  # (grp, ct) → (prx_grp, prx_ct, heat_cr, summer_cr)

        print(f"\n  Proxy mapping (< {MIN_MODEM_COUNT} модемів):")
        for (grp, ct), n in sorted(modem_counts.items()):
            if n >= MIN_MODEM_COUNT:
                continue

            # Вибір proxy
            same_ct  = [(g, c) for (g, c) in good_pairs if c == ct]
            any_ct   = list(good_pairs)
            candidates = same_ct if same_ct else any_ct
            if not candidates:
                continue

            proxy_grp, proxy_ct = max(candidates, key=lambda gc: modem_counts[gc])

            # Базовий рівень літнього споживання для обох груп
            summer_tgt   = self._billing_grp_sb.get((grp, ct), 0.0)
            summer_proxy = self._billing_grp_sb.get((proxy_grp, proxy_ct), 0.0)

            heat_cr   = {}
            summer_cr = {}
            for month in range(1, 13):
                bt = bill_m(grp,       ct,       month)
                bp = bill_m(proxy_grp, proxy_ct, month)

                # Загальний місячний cross-ratio (для rate_sum + не-ОП fallback)
                summer_cr[month] = (bt / bp) if bp > 0 else 1.0

                # Тепловий cross-ratio: тільки heating-компонент
                # bill_heat = max(0, bill_m - summer_avg)
                if month in HEATING_MONTHS:
                    ht = max(0.0, bt - summer_tgt)
                    hp = max(0.0, bp - summer_proxy)
                    heat_cr[month] = (ht / hp) if hp > 1e-3 else summer_cr[month]
                else:
                    heat_cr[month] = summer_cr[month]

            self._proxy_assign[(grp, ct)] = (proxy_grp, proxy_ct, heat_cr, summer_cr)
            print(f"    [{n:3d}→{modem_counts[(proxy_grp, proxy_ct)]:4d}] "
                  f"({grp:12s}, {ct[:18]:18s}) "
                  f"→ ({proxy_grp:12s}, {proxy_ct[:18]:18s})  "
                  f"heat_CR[Січ]={heat_cr.get(1, 1):.3f}  "
                  f"sum_CR[Лип]={summer_cr.get(7, 1):.3f}")

        print(f"  Proxy-груп: {len(self._proxy_assign)}")

    # ── Proxy-aware lookup helpers ────────────────────────────────────────────

    def _proxy_hms(self, grp, ct, is_alt, y, m):
        """heat_m2_sum з proxy-корекцією для low-coverage груп."""
        assign = self._proxy_assign.get((grp, ct))
        if assign is None:
            return self.get_hms(grp, ct, is_alt, y, m)

        proxy_grp, proxy_ct, heat_cr, _ = assign
        hms = self.get_hms(proxy_grp, proxy_ct, is_alt, y, m)
        if np.isnan(hms):
            # proxy не дав результат — fallback до оригінальної групи
            return self.get_hms(grp, ct, is_alt, y, m)
        return hms * heat_cr.get(m, 1.0)

    def _proxy_rs(self, grp, ct, y, m):
        """rate_sum з proxy-корекцією для low-coverage груп."""
        assign = self._proxy_assign.get((grp, ct))
        if assign is None:
            return self.get_rs(grp, y, m, ct=ct)

        proxy_grp, proxy_ct, _, summer_cr = assign
        rs = self.get_rs(proxy_grp, y, m, ct=proxy_ct)
        if np.isnan(rs):
            return self.get_rs(grp, y, m, ct=ct)
        return rs * summer_cr.get(m, 1.0)

    def _predict_non_modem(self, periods):
        records = []
        nm = self._non_modem

        for (y, m) in periods:
            is_heat = m in HEATING_MONTHS

            for _, row in nm.iterrows():
                grp  = row['appliance_group']
                area = row['heated_area']
                pbl  = row['pbl']
                grs  = row['grs']
                aid  = row['account_id']
                ct   = row['consumer_type']
                ia   = row['is_alt']

                if row['is_dacha'] and m not in SUMMER_MONTHS:
                    continue

                if row['has_OP'] and is_heat:
                    # ОП опалення: proxy-скоригований heat_m2_sum
                    hms = self._proxy_hms(grp, ct, ia, y, m)
                    if np.isnan(hms):
                        continue
                    sr  = self.get_sr(grp, m)
                    vol = max(0.0, hms * area) + pbl * sr

                elif row['has_OP']:
                    # ОП літо: proxy-скоригований rate_sum
                    rs = self._proxy_rs(grp, ct, y, m)
                    if np.isnan(rs):
                        continue
                    vol = pbl * rs

                else:
                    # Не-ОП: білінговий SR (стабільний, незалежно від proxy)
                    sr_b = self._billing_sr.get((grp, ct, m))
                    if sr_b is not None:
                        vol = pbl * sr_b
                    else:
                        # Fallback через proxy rate_sum
                        rs = self._proxy_rs(grp, ct, y, m)
                        if np.isnan(rs):
                            continue
                        vol = pbl * rs

                records.append({
                    'account_id': aid, 'appliance_group': grp,
                    'year': y, 'month': m, 'volume': vol,
                    'grs': grs, 'type': 'pred',
                })

        return pd.DataFrame(records)


# ══════════════════════════════════════════════════════════════════════════════
# Model I: Calibrated-PBL Predictor
# ══════════════════════════════════════════════════════════════════════════════

class PobUtPredictorCalibratedPBL(PobUtPredictorBillingProfile):
    """
    Config I: нормалізація individual summer_baseline до modem-calibrated рівня.

    ПРОБЛЕМА Config F: mean(sb_i for GRS) ≠ grp_med_modem →
      С-1 ГРС-1,3: mean(sb_i) < grp_med → F занижує влітку
      С-2 Вільнянськ: mean(sb_i) >> grp_med → F завищує влітку

    РІШЕННЯ:
      k[grp, ct] = median_modem_summer[grp,ct] / median_billing_sb[grp,ct]
      pbl_calibrated_i = sb_i × k

    Ефект:
      - mean(pbl_cal for group) = grp_med  ← калібрований до modem рівня
      - pbl_cal_i / grp_med = sb_i / median_billing_sb  ← відносна варіація збережена
      - Для споживачів з high/low sb_i → відповідно вище/нижче прогноз

    Застосовується тільки для ОП-груп (де grp_sb з modemів значущий).
    Для не-ОП (ПГ, ПГ,ВПГ) — billing SR вже достатньо (без змін).
    """

    def _prepare_non_modem(self):
        # Config F: _billing_sr + individual pbl
        super()._prepare_non_modem()
        # Тепер нормалізуємо pbl для ОП-груп
        self._calibrate_op_pbl()

    def _calibrate_op_pbl(self):
        """
        Для ОП-груп: pbl_cal = sb_i × (grp_med_modem / median_billing_sb[grp,ct]).
        Для не-ОП: залишаємо як є (billing SR вже використовується).
        """
        # Median billing summer_baseline per (grp, ct) — з білінгу для НЕ-модемних
        ap = self._all_pobut.copy()
        ap['appliance_group'] = (
            ap['appliance_group'].astype(str).str.strip()
            .replace({'ОП,ВПГ': 'ОП,ПГ,ВПГ', 'ОП': 'ОП,ПГ'})
        )
        ap['consumer_type'] = ap['consumer_type'].fillna(CT_PRIV)
        ap['_sb'] = pd.to_numeric(ap['summer_baseline'], errors='coerce')
        ap = ap[ap['_sb'] > 0]

        # Медіана sb за (grp, ct) — тільки ОП-групи
        med_billing_sb = (
            ap[ap['appliance_group'].str.contains('ОП', na=False)]
            .groupby(['appliance_group', 'consumer_type'])['_sb']
            .median()
            .to_dict()
        )

        # Modem grp_sb: median summer з модемів (вже обчислено в _build_profiles)
        # self._grp_sb = {(grp, ct): median_modem_summer}

        # Calibration factor
        k_cal = {}
        print("  Calibration factors k = modem_median / billing_sb_median:")
        for (grp, ct), modem_med in sorted(self._grp_sb.items()):
            bill_med = med_billing_sb.get((grp, ct))
            if bill_med and bill_med > 0:
                k = modem_med / bill_med
                k_cal[(grp, ct)] = k
                print(f"    ({grp:12s}, {ct[:20]:20s}): "
                      f"modem={modem_med:.2f}  billing={bill_med:.2f}  k={k:.3f}")

        # Застосовуємо до pbl для ОП-споживачів
        nm = self._non_modem
        op_mask = nm['has_OP']
        n_calibrated = 0
        for idx in nm[op_mask].index:
            grp = nm.at[idx, 'appliance_group']
            ct  = nm.at[idx, 'consumer_type']
            k   = k_cal.get((grp, ct), 1.0)
            if k != 1.0:
                nm.at[idx, 'pbl'] *= k
                n_calibrated += 1

        print(f"  Calibrated pbl for {n_calibrated:,} OP consumers "
              f"(non-OP unchanged: {(~op_mask).sum():,})")


# ══════════════════════════════════════════════════════════════════════════════
# Model H: Billing-SR-Everywhere Predictor
# ══════════════════════════════════════════════════════════════════════════════

class PobUtPredictorBillingSR(PobUtPredictorBillingProfile):
    """
    Config H: виправляє проблему rate_sum≈1.0 в ОП-літо.

    billing_sr[ОП,ПГ, Лип] ≈ 0.87 (з тисяч споживачів)
    rate_sum[ОП,ПГ, Лип]   ≈ 1.0  (з модемів, тільки ratio)

    В Config F: vol_OP_summer = pbl × rate_sum ≈ pbl × 1.0 = pbl
    В Config H: vol_OP_summer = pbl × billing_sr ≈ pbl × 0.87  ← правильніше

    ВАЖЛИВО: billing_sr[ОП,ПГ, Січ] ≈ 22.6 (включає heating!).
    НЕ використовуємо billing_sr для ОП-нагрів — залишаємо hms + pg_sr,
    щоб уникнути подвійного рахунку.

    Формула:
      ОП-нагрів: vol = area × hms_modem + pbl × pg_sr[m]  (без змін vs F)
      ОП-літо:   vol = pbl × billing_sr[grp, ct, m]        ← ВИПРАВЛЕНО
      Не-ОП:     vol = pbl × billing_sr[grp, ct, m]        (без змін vs F)
    """

    def _predict_non_modem(self, periods):
        records = []
        nm = self._non_modem

        for (y, m) in periods:
            is_heat = m in HEATING_MONTHS

            for _, row in nm.iterrows():
                grp  = row['appliance_group']
                area = row['heated_area']
                pbl  = row['pbl']
                grs  = row['grs']
                aid  = row['account_id']
                ct   = row['consumer_type']
                ia   = row['is_alt']

                if row['is_dacha'] and m not in SUMMER_MONTHS:
                    continue

                if row['has_OP'] and is_heat:
                    # ОП опалення: modem hms + pg_sr baseline (без змін)
                    hms = self.get_hms(grp, ct, ia, y, m)
                    if np.isnan(hms):
                        continue
                    vol = max(0.0, hms * area) + pbl * self.get_sr(grp, m)

                elif row['has_OP']:
                    # ОП літо: billing_sr замість rate_sum
                    # billing_sr_summer ≈ 0.87 < rate_sum ≈ 1.0 → зменшує завищення
                    sr_b = self._billing_sr.get((grp, ct, m))
                    if sr_b is not None:
                        vol = pbl * sr_b
                    else:
                        rs = self.get_rs(grp, y, m, ct=ct)
                        if np.isnan(rs):
                            continue
                        vol = pbl * rs

                else:
                    # Не-ОП: billing SR (без змін vs F)
                    sr_b = self._billing_sr.get((grp, ct, m))
                    if sr_b is not None:
                        vol = pbl * sr_b
                    else:
                        rs = self.get_rs(grp, y, m, ct=ct)
                        if np.isnan(rs):
                            continue
                        vol = pbl * rs

                records.append({
                    'account_id': aid, 'appliance_group': grp,
                    'year': y, 'month': m, 'volume': vol,
                    'grs': grs, 'type': 'pred',
                })

        return pd.DataFrame(records)


# ══════════════════════════════════════════════════════════════════════════════
# Model J: GRS-Level Calibration
# ══════════════════════════════════════════════════════════════════════════════

class PobUtPredictorGRSCalib(PobUtPredictor):
    """
    Config J: GRS-рівнева калібровка.

    Замінює глобальний modem grp_sb/grp_med на ГРС-специфічну медіану
    billing summer_baseline для кожного (grs, appliance_group, consumer_type).

      grs_sb[grs, grp, ct] = median(summer_baseline  за споживачами в цій комірці)

    Цей grs_sb використовується як pbl; rate_sum, hms — без змін від моделі A.

    Ключова різниця від Config I:
      Config I: pbl_cal = sb_i × k  де k = modem_med / billing_med  (global)
      Config J: pbl      = median(billing_sb for grs×grp×ct)         (local GRS)

    Fallback: (grs, grp, ct) → (grs, grp, CT_PRIV) → global grp_sb / grp_med
    """
    MIN_GRS_CONS = 5  # мінімум споживачів для надійної GRS-медіани

    def _prepare_non_modem(self):
        # 1. GRS-специфічні медіани з білінгу (потребує _grp_sb/_grp_med вже готових)
        self._build_grs_sb()
        # 2. Стандартна підготовка (глобальний pbl = grp_sb / grp_med)
        super()._prepare_non_modem()
        # 3. Замінюємо pbl на GRS-рівні де є достатньо даних
        nm = self._non_modem

        keys_ct   = list(zip(nm['grs'], nm['appliance_group'], nm['consumer_type']))
        keys_priv = list(zip(nm['grs'], nm['appliance_group'], [CT_PRIV] * len(nm)))

        nm['_grs_pbl'] = pd.Series(keys_ct, index=nm.index).map(self._grs_sb_map)
        # CT_PRIV fallback для МКД тощо де немає GRS-клітинки
        priv_fb = nm['_grs_pbl'].isna() & (nm['consumer_type'] != CT_PRIV)
        nm.loc[priv_fb, '_grs_pbl'] = (
            pd.Series(keys_priv, index=nm.index)[priv_fb].map(self._grs_sb_map)
        )

        override_mask = nm['_grs_pbl'].notna()
        nm.loc[override_mask, 'pbl'] = nm.loc[override_mask, '_grs_pbl']
        nm.drop(columns=['_grs_pbl'], inplace=True)

        print(f"  GRS calibration: {override_mask.sum():,} → GRS-specific pbl  |  "
              f"{(~override_mask).sum():,} → global fallback")

    def _build_grs_sb(self):
        """Median(summer_baseline) per (grs, grp, ct) — з всіх білінгових споживачів."""
        ap = self._all_pobut.copy()
        ap['account_id']      = pd.to_numeric(ap['account_id'], errors='coerce')
        ap['appliance_group'] = (
            ap['appliance_group'].astype(str).str.strip()
            .replace({'ОП,ВПГ': 'ОП,ПГ,ВПГ', 'ОП': 'ОП,ПГ'})
        )
        ap['consumer_type']   = ap['consumer_type'].fillna(CT_PRIV)
        ap['summer_baseline'] = pd.to_numeric(ap['summer_baseline'], errors='coerce')

        valid = ap[(ap['summer_baseline'] > 0) & ap['grs'].notna()]
        agg = (
            valid.groupby(['grs', 'appliance_group', 'consumer_type'])
            .agg(n=('summer_baseline', 'count'), median_sb=('summer_baseline', 'median'))
            .reset_index()
        )
        agg = agg[agg['n'] >= self.MIN_GRS_CONS]

        self._grs_sb_map = {
            (r['grs'], r['appliance_group'], r['consumer_type']): r['median_sb']
            for _, r in agg.iterrows()
        }

        grs_count = agg['grs'].nunique()
        print(f"  GRS calibration map: {len(self._grs_sb_map)} cells "
              f"({grs_count} GRS, ≥{self.MIN_GRS_CONS} consumers each)")

        # Діагностика по проблемних ГРС
        FOCUS_GRS = [
            'С-1(Запоріж) ГРС-1,3 кільце',
            'ГРС (2) м.Запоріжжя',
            'С-2 (Вільнянс) ГРС Вільнянськ',
        ]
        print("  grs_sb vs global modem anchor (k = grs_sb / global):")
        for grs in FOCUS_GRS:
            sub = agg[agg['grs'] == grs].sort_values('n', ascending=False).head(4)
            if sub.empty:
                continue
            print(f"    {grs}:")
            for _, r in sub.iterrows():
                grp, ct = r['appliance_group'], r['consumer_type']
                g_anchor = (
                    self._grp_sb.get((grp, ct),
                    self._grp_sb.get((grp, CT_PRIV),
                    self._grp_med.get(grp, np.nan)))
                )
                k = r['median_sb'] / g_anchor if g_anchor and g_anchor > 0 else np.nan
                print(f"      ({grp:12s}, {ct[:15]:15s}): "
                      f"grs_sb={r['median_sb']:.2f}  global={g_anchor:.2f}  k={k:.3f}")


# ══════════════════════════════════════════════════════════════════════════════
# Model K: Profile-based Predictor (modem P(m) + individual k_i)
# ══════════════════════════════════════════════════════════════════════════════

class PobUtPredictorConfigK(PobUtPredictor):
    """
    Config K: Профільний предиктор на основі модемних профілів P(m) + k_i.

    Зима ОП (жов-кві):  vol_i(m) = k_i_heat × area_i × P_heat(m)
      P_heat(m)  = модемний удільний профіль [м³/м²] (з build_ki_table.py)
      k_i_heat   = billing_heating_sum / (area × Σ P_heat)   (ki_heating_consumers.csv)

    Літо ОП (тра-вер):  vol_i(m) = k_i_summer × P_op_summer(m)
      P_op_summer(m) = модемний профіль [м³/міс] (з build_nonheat_profiles.py)
      k_i_summer     = billing_summer / Σ P_op_summer         (ki_nonheat_consumers.csv)

    ПГ/ПГ,ВПГ (весь рік): vol_i(m) = k_i_pg × P_pg(m)
      P_pg(m)  = модемний профіль 541/653 модемів
      k_i_pg   = billing_annual / Σ P_pg                      (ki_nonheat_consumers.csv)
    """

    KI_HEAT_CSV    = 'data/profiles/ki_heating_consumers.csv'
    KI_NONHEAT_CSV = 'data/profiles/ki_nonheat_consumers.csv'
    P_NONHEAT_CSV  = 'data/profiles/p_modem_nonheat.csv'

    # Зимовий ОП профіль [м³/м²/місяць] — з build_ki_table.py
    P_HEAT = {1: 4.3167, 2: 4.3717, 3: 2.7025, 4: 1.3619,
              10: 1.1902, 11: 2.7532, 12: 3.9867}

    def _prepare_non_modem(self):
        super()._prepare_non_modem()
        self._load_k_profiles()

    # ── завантаження таблиць ──────────────────────────────────────────────────

    def _load_k_profiles(self):
        # P(m) для не-опалення
        p_df = pd.read_csv(self.P_NONHEAT_CSV)
        self._p_nonheat = {}
        for _, r in p_df.iterrows():
            self._p_nonheat[
                (r['appliance_group'], r['consumer_type'], int(r['month']))
            ] = float(r['P_modem_median'])

        # k_i heating
        ki_h = pd.read_csv(self.KI_HEAT_CSV, usecols=['account_id','grs',
                            'appliance_group','consumer_type','k_i_clipped'])
        ki_h['account_id'] = pd.to_numeric(ki_h['account_id'], errors='coerce')
        ki_h = ki_h[ki_h['k_i_clipped'].notna() & (ki_h['k_i_clipped'] > 0)]
        self._ki_heat_aid = dict(zip(ki_h['account_id'].astype(int),
                                     ki_h['k_i_clipped'].astype(float)))
        self._ki_heat_grs    = self._median_by(ki_h, ['grs','appliance_group','consumer_type'])
        self._ki_heat_global = self._median_by(ki_h, ['appliance_group','consumer_type'])

        # k_i nonheat
        ki_nh = pd.read_csv(self.KI_NONHEAT_CSV, usecols=['account_id','grs',
                             'appliance_group','consumer_type','k_i_clipped'])
        ki_nh['account_id'] = pd.to_numeric(ki_nh['account_id'], errors='coerce')
        ki_nh = ki_nh[ki_nh['k_i_clipped'].notna() & (ki_nh['k_i_clipped'] > 0)]
        self._ki_nh_aid    = dict(zip(ki_nh['account_id'].astype(int),
                                      ki_nh['k_i_clipped'].astype(float)))
        self._ki_nh_grs    = self._median_by(ki_nh, ['grs','appliance_group','consumer_type'])
        self._ki_nh_global = self._median_by(ki_nh, ['appliance_group','consumer_type'])

        print(f"  Config K: P_nonheat={len(self._p_nonheat)}, "
              f"ki_heat={len(self._ki_heat_aid):,}, ki_nonheat={len(self._ki_nh_aid):,}")

        # Попередньо будуємо матриці k_i та P для nm — vectorized predict
        self._build_nm_arrays()

    @staticmethod
    def _median_by(df, cols):
        return df.groupby(cols)['k_i_clipped'].median().to_dict()

    def _build_nm_arrays(self):
        """Будує numpy-масиви ki та P для всіх не-модемних споживачів."""
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

            # heating k_i
            v = (self._ki_heat_aid.get(aid)
                 or self._ki_heat_grs.get((g, grp, ct))
                 or self._ki_heat_grs.get((g, grp, CT_PRIV))
                 or self._ki_heat_global.get((grp, ct))
                 or self._ki_heat_global.get((grp, CT_PRIV), 0.85))
            ki_h[i] = float(v or 0.85)

            # nonheat k_i
            v = (self._ki_nh_aid.get(aid)
                 or self._ki_nh_grs.get((g, grp, ct))
                 or self._ki_nh_grs.get((g, grp, CT_PRIV))
                 or self._ki_nh_global.get((grp, ct))
                 or self._ki_nh_global.get((grp, CT_PRIV), 1.0))
            ki_nh[i] = float(v or 1.0)

        nm['_ki_heat']   = ki_h
        nm['_ki_nonheat'] = ki_nh

        # P_nonheat matrix: [n × 12] → p_nonheat_matrix[i, m-1]
        p_mat = np.zeros((n, 12), dtype=np.float32)
        for m in range(1, 13):
            col = m - 1
            for i in range(n):
                p_mat[i, col] = self._p_nonheat.get((grps[i], cts[i], m), 0.0)
        self._p_nonheat_matrix = p_mat

        print(f"  ki_heat  : med={float(np.median(ki_h[nm['has_OP'].values])):.3f}  "
              f"(ОП споживачів: {nm['has_OP'].sum():,})")
        print(f"  ki_nonheat: med={float(np.median(ki_nh)):.3f}  (всіх: {n:,})")

    # ── predict ───────────────────────────────────────────────────────────────

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

        all_records = []

        for (y, m) in periods:
            is_heat = m in HEATING_MONTHS

            # Маска активних (дачі тільки влітку)
            active = ~is_dach | np.isin(m, list(SUMMER_MONTHS))

            p_col = p_mat[:, m - 1]   # P(m) per consumer

            vols = np.zeros(len(nm))

            if is_heat:
                # Зима ОП: k_i_heat × area × P_heat(m)
                p_h = self.P_HEAT.get(m, 0.0)
                op  = active & has_op
                vols[op] = ki_h[op] * areas[op] * p_h
            else:
                # Літо ОП: k_i_nonheat × P_op_summer(m)
                op = active & has_op
                vols[op] = ki_nh[op] * p_col[op]

            # ПГ/ПГ,ВПГ: k_i_nonheat × P_pg(m)
            non_op = active & ~has_op
            vols[non_op] = ki_nh[non_op] * p_col[non_op]

            vols = np.maximum(vols, 0.0)
            mask = (vols > 0) & active

            if mask.sum() == 0:
                continue

            all_records.append(pd.DataFrame({
                'account_id':      aids[mask],
                'appliance_group': grps[mask],
                'year':            y,
                'month':           m,
                'volume':          vols[mask],
                'grs':             grs_v[mask],
                'type':            'pred',
            }))

        return pd.concat(all_records, ignore_index=True) if all_records else pd.DataFrame()


# ══════════════════════════════════════════════════════════════════════════════
# Завантаження білінгу (еталон)
# ══════════════════════════════════════════════════════════════════════════════
print("=" * 65)
print("Завантаження білінгу 2025...")
ap = pd.read_csv(SUBS_FILE, low_memory=False)
ap['account_id'] = pd.to_numeric(ap['account_id'], errors='coerce')

month_cols_sorted = sorted(
    [c for c in ap.columns if c.endswith('_2025') and c.split('_')[0] in MONTH_ENG],
    key=lambda c: MONTH_ENG[c.split('_')[0]],
)
for c in month_cols_sorted:
    ap[c] = pd.to_numeric(ap[c], errors='coerce').fillna(0)

billing_rows = []
for c in month_cols_sorted:
    m = MONTH_ENG[c.split('_')[0]]
    g = ap[ap['grs'].notna()].groupby('grs')[c].sum().reset_index()
    g.columns = ['grs', 'billing']
    g['month'] = m
    g['year']  = 2025
    billing_rows.append(g)
billing_df = pd.concat(billing_rows, ignore_index=True)
billing_df['billing'] = billing_df['billing'].clip(lower=0)

total_billing = billing_df['billing'].sum()
print(f"  Загальний білінг 2025: {total_billing:>14,.0f} м³")

# Класифікація ГРС: великий / малий
grs_annual = billing_df.groupby('grs')['billing'].sum()
large_grs_set = set(grs_annual[grs_annual >= LARGE_GRS_THRESH].index)
print(f"  Великих ГРС (>{LARGE_GRS_THRESH/1e6:.0f}M м³/рік): {len(large_grs_set)}")
for g in sorted(large_grs_set):
    print(f"    {g}: {grs_annual[g]:,.0f}")


# ══════════════════════════════════════════════════════════════════════════════
# Запуск моделей
# ══════════════════════════════════════════════════════════════════════════════
def run_model(cls, label, **kwargs):
    print(f"\n{'─'*55}")
    print(f"Запуск: {label}")
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
    pivot['fact']  = pivot.get('fact', 0)
    pivot['pred']  = pivot.get('pred', 0)
    pivot['total'] = pivot['fact'] + pivot['pred']
    return pivot[['grs', 'year', 'month', 'fact', 'pred', 'total']]


pred_a = run_model(PobUtPredictor,               'A: group_medians (baseline)')
pred_f = run_model(PobUtPredictorBillingProfile, 'F: billing_profile')
pred_g = run_model(PobUtPredictorProxyGroup,     'G: proxy_group')
pred_h = run_model(PobUtPredictorBillingSR,      'H: billing_sr_op_summer')
pred_i = run_model(PobUtPredictorCalibratedPBL,  'I: calibrated_pbl')
pred_j = run_model(PobUtPredictorGRSCalib,       'J: grs_calibration')
pred_k = run_model(PobUtPredictorConfigK,        'K: profile_ki (modem P + individual k_i)')


# ══════════════════════════════════════════════════════════════════════════════
# Метрики: bias% per (GRS, month)
# ══════════════════════════════════════════════════════════════════════════════
def compute_detail(pred_df, bill_df):
    merged = pred_df.merge(bill_df, on=['grs', 'year', 'month'], how='inner')
    merged = merged[merged['billing'] > 0].copy()
    merged['error']    = merged['total'] - merged['billing']
    merged['abs_err']  = merged['error'].abs()
    merged['bias_pct'] = merged['error'] / merged['billing'] * 100
    merged['ape']      = merged['abs_err'] / merged['billing']
    return merged


det_a = compute_detail(pred_a, billing_df)
det_f = compute_detail(pred_f, billing_df)
det_g = compute_detail(pred_g, billing_df)
det_h = compute_detail(pred_h, billing_df)
det_i = compute_detail(pred_i, billing_df)
det_j = compute_detail(pred_j, billing_df)
det_k = compute_detail(pred_k, billing_df)


def model_summary(det, label):
    mae  = det['abs_err'].mean()
    mape = det['ape'].mean() * 100
    bias = det['error'].sum() / det['billing'].sum() * 100
    large_mask = det['grs'].isin(large_grs_set)
    within_large = (det[large_mask]['bias_pct'].abs() <= 3).mean() * 100
    within_small = (det[~large_mask]['bias_pct'].abs() <= 6).mean() * 100
    print(f"  {label}")
    print(f"    MAE={mae:,.0f}  MAPE={mape:.2f}%  bias={bias:+.2f}%")
    print(f"    Великих ГРС ≤3%: {within_large:.1f}% пар | "
          f"Малих ГРС ≤6%: {within_small:.1f}% пар")
    return mae, mape, bias, within_large, within_small


print("\n" + "=" * 65)
print("ПОРІВНЯННЯ РЕЗУЛЬТАТІВ")
print("=" * 65)
mae_a, mape_a, bias_a, wl_a, ws_a = model_summary(det_a, 'A: group_medians')
mae_f, mape_f, bias_f, wl_f, ws_f = model_summary(det_f, 'F: billing_profile')
mae_g, mape_g, bias_g, wl_g, ws_g = model_summary(det_g, 'G: proxy_group')
mae_h, mape_h, bias_h, wl_h, ws_h = model_summary(det_h, 'H: billing_sr_op_summer')
mae_i, mape_i, bias_i, wl_i, ws_i = model_summary(det_i, 'I: calibrated_pbl')
mae_j, mape_j, bias_j, wl_j, ws_j = model_summary(det_j, 'J: grs_calibration')
mae_k, mape_k, bias_k, wl_k, ws_k = model_summary(det_k, 'K: profile_ki')
print(f"\n  F vs A: ΔMAE={mae_f-mae_a:+,.0f}  "
      f"Δ(великих≤3%)={wl_f-wl_a:+.1f}pp  Δ(малих≤6%)={ws_f-ws_a:+.1f}pp")
print(f"  H vs A: ΔMAE={mae_h-mae_a:+,.0f}  "
      f"Δ(великих≤3%)={wl_h-wl_a:+.1f}pp  Δ(малих≤6%)={ws_h-ws_a:+.1f}pp")
print(f"  J vs A: ΔMAE={mae_j-mae_a:+,.0f}  "
      f"Δ(великих≤3%)={wl_j-wl_a:+.1f}pp  Δ(малих≤6%)={ws_j-ws_a:+.1f}pp")
print(f"  K vs A: ΔMAE={mae_k-mae_a:+,.0f}  "
      f"Δ(великих≤3%)={wl_k-wl_a:+.1f}pp  Δ(малих≤6%)={ws_k-ws_a:+.1f}pp")
print(f"  K vs H: ΔMAE={mae_k-mae_h:+,.0f}  "
      f"Δ(великих≤3%)={wl_k-wl_h:+.1f}pp  Δ(малих≤6%)={ws_k-ws_h:+.1f}pp")
print(f"  K vs J: ΔMAE={mae_k-mae_j:+,.0f}  "
      f"Δ(великих≤3%)={wl_k-wl_j:+.1f}pp  Δ(малих≤6%)={ws_k-ws_j:+.1f}pp")


# ══════════════════════════════════════════════════════════════════════════════
# Детальна таблиця: GRS × month, bias% A vs F vs G
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 65)
print("bias% по ГРС × місяць  (A → F → H → J)")
print("=" * 65)

def make_bias_pivot(det, col_prefix):
    p = det.pivot_table(
        index='grs', columns='month', values='bias_pct',
        aggfunc='mean',
    )
    p.columns = [f'{col_prefix}_{MONTH_UA[int(c)]}' for c in p.columns]
    return p

piv_a = make_bias_pivot(det_a, 'A')
piv_f = make_bias_pivot(det_f, 'F')
piv_h = make_bias_pivot(det_h, 'H')
piv_j = make_bias_pivot(det_j, 'J')
piv_k = make_bias_pivot(det_k, 'K')

month_labels_ua = [MONTH_UA[m] for m in range(1, 13)]

comparison = (piv_a.join(piv_f, how='outer').join(piv_h, how='outer')
              .join(piv_j, how='outer').join(piv_k, how='outer'))
comparison['MAE_A'] = det_a.groupby('grs')['abs_err'].mean().round(0)
comparison['MAE_F'] = det_f.groupby('grs')['abs_err'].mean().round(0)
comparison['MAE_H'] = det_h.groupby('grs')['abs_err'].mean().round(0)
comparison['MAE_J'] = det_j.groupby('grs')['abs_err'].mean().round(0)
comparison['MAE_K'] = det_k.groupby('grs')['abs_err'].mean().round(0)
comparison['Large']  = comparison.index.isin(large_grs_set)
comparison = comparison.sort_values('MAE_A', ascending=False)

print("\n=== ВЕЛИКІ ГРС (bias% по місяцях: A / H / K) ===")
for grs_name in [g for g in comparison.index if g in large_grs_set]:
    row = comparison.loc[grs_name]
    print(f"\n{grs_name}  [MAE: A={row.get('MAE_A',0):,.0f}  "
          f"H={row.get('MAE_H',0):,.0f}  K={row.get('MAE_K',0):,.0f}]")
    a_vals = [f"{row.get(f'A_{ua}', np.nan):>+6.1f}%" for ua in month_labels_ua]
    h_vals = [f"{row.get(f'H_{ua}', np.nan):>+6.1f}%" for ua in month_labels_ua]
    k_vals = [f"{row.get(f'K_{ua}', np.nan):>+6.1f}%" for ua in month_labels_ua]
    months = [f"  {ua:<4}" for ua in month_labels_ua]
    print("  " + " ".join(months))
    print("A:" + " ".join(a_vals))
    print("H:" + " ".join(h_vals))
    print("K:" + " ".join(k_vals))


# ══════════════════════════════════════════════════════════════════════════════
# Excel output
# ══════════════════════════════════════════════════════════════════════════════
print(f"\nЗберігаємо {OUT_EXCEL} ...")

def full_bias_pivot(det, label):
    p = det.pivot_table(
        index='grs', columns='month', values='bias_pct', aggfunc='mean',
    ).round(2)
    p.columns = [MONTH_UA[int(c)] for c in p.columns]
    p.insert(0, 'ГРС', p.index)
    p['MAE_avg']    = det.groupby('grs')['abs_err'].mean().round(0)
    p['MAPE_avg%']  = (det.groupby('grs')['ape'].mean() * 100).round(2)
    p['Bias_рік%']  = (det.groupby('grs').apply(
        lambda d: d['error'].sum() / d['billing'].sum() * 100
    )).round(2)
    p['Large_GRS']  = p['ГРС'].isin(large_grs_set)
    return p.reset_index(drop=True)


def month_agg(det):
    bm = det.groupby('month').agg(
        billing=('billing', 'sum'),
        total=('total', 'sum'),
        abs_err=('abs_err', 'mean'),
    ).reset_index()
    bm['Місяць']  = bm['month'].map(MONTH_UA)
    bm['bias%']   = ((bm['total'] - bm['billing']) / bm['billing'] * 100).round(2)
    bm['MAE_avg'] = bm['abs_err'].round(0)
    return bm[['Місяць', 'billing', 'total', 'MAE_avg', 'bias%']]


def grs_agg(det):
    return det.groupby('grs').agg(
        Billing   = ('billing',  'sum'),
        Pred      = ('total',    'sum'),
        MAE       = ('abs_err',  'mean'),
        MAPE      = ('ape',      'mean'),
        Bias_sum  = ('error',    'sum'),
    ).assign(
        MAPE_pct  = lambda d: (d['MAPE'] * 100).round(2),
        Bias_pct  = lambda d: (d['Bias_sum'] / d['Billing'] * 100).round(2),
        MAE       = lambda d: d['MAE'].round(0),
        Large_GRS = lambda d: d.index.isin(large_grs_set),
    ).drop(columns=['MAPE', 'Bias_sum']).sort_values('MAE', ascending=False)


def within_target(det):
    """% пар (GRS, month) в межах допуску."""
    rows = []
    for grs in sorted(det['grs'].unique()):
        is_large = grs in large_grs_set
        tgt = 3.0 if is_large else 6.0
        sub = det[det['grs'] == grs]
        n_ok    = (sub['bias_pct'].abs() <= tgt).sum()
        n_total = len(sub)
        rows.append({
            'ГРС': grs, 'Large_GRS': is_large, 'Допуск%': tgt,
            'Місяців_у_цілі': int(n_ok), 'Всього_місяців': int(n_total),
            'Покриття%': round(n_ok / n_total * 100, 1),
        })
    return pd.DataFrame(rows).sort_values(['Large_GRS', 'Покриття%'], ascending=[False, True])


target_a = within_target(det_a)
target_f = within_target(det_f)
target_h = within_target(det_h)
target_i = within_target(det_i)
target_j = within_target(det_j)

print("\nПокриття цілі (модель J):")
print(target_j[target_j['Large_GRS']].to_string(index=False))
print()
print(target_j[~target_j['Large_GRS']].head(10).to_string(index=False))

# ── Зведена таблиця по місяцях A / F / G ─────────────────────────────────────
monthly_compare = (
    month_agg(det_a).rename(columns={'bias%': 'bias%_A', 'MAE_avg': 'MAE_A',
                                      'billing': 'billing', 'total': 'total_A'})
    .merge(
        month_agg(det_f)[['Місяць', 'bias%', 'MAE_avg', 'total']].rename(
            columns={'bias%': 'bias%_F', 'MAE_avg': 'MAE_F', 'total': 'total_F'}),
        on='Місяць',
    )
    .merge(
        month_agg(det_g)[['Місяць', 'bias%', 'MAE_avg', 'total']].rename(
            columns={'bias%': 'bias%_G', 'MAE_avg': 'MAE_G', 'total': 'total_G'}),
        on='Місяць',
    )
    .merge(
        month_agg(det_h)[['Місяць', 'bias%', 'MAE_avg', 'total']].rename(
            columns={'bias%': 'bias%_H', 'MAE_avg': 'MAE_H', 'total': 'total_H'}),
        on='Місяць',
    )
    .merge(
        month_agg(det_i)[['Місяць', 'bias%', 'MAE_avg', 'total']].rename(
            columns={'bias%': 'bias%_I', 'MAE_avg': 'MAE_I', 'total': 'total_I'}),
        on='Місяць',
    )
    .merge(
        month_agg(det_j)[['Місяць', 'bias%', 'MAE_avg', 'total']].rename(
            columns={'bias%': 'bias%_J', 'MAE_avg': 'MAE_J', 'total': 'total_J'}),
        on='Місяць',
    )
)

bias_a_xl = full_bias_pivot(det_a, 'A')
bias_f_xl = full_bias_pivot(det_f, 'F')
bias_h_xl = full_bias_pivot(det_h, 'H')
bias_i_xl = full_bias_pivot(det_i, 'I')
bias_j_xl = full_bias_pivot(det_j, 'J')

with pd.ExcelWriter(OUT_EXCEL, engine='openpyxl') as xw:
    # Sheet 1: головний результат — Bias% J (grs_calibration)
    bias_j_xl.to_excel(xw, sheet_name='J_bias_GRS×міс', index=False)
    # Sheet 2-5: Bias% I, H, F, A
    bias_i_xl.to_excel(xw, sheet_name='I_bias_GRS×міс', index=False)
    bias_h_xl.to_excel(xw, sheet_name='H_bias_GRS×міс', index=False)
    bias_f_xl.to_excel(xw, sheet_name='F_bias_GRS×міс', index=False)
    bias_a_xl.to_excel(xw, sheet_name='A_bias_GRS×міс', index=False)
    # Coverage (% пар у цілі)
    target_j.to_excel(xw, sheet_name='J_покриття_цілі', index=False)
    target_i.to_excel(xw, sheet_name='I_покриття_цілі', index=False)
    target_h.to_excel(xw, sheet_name='H_покриття_цілі', index=False)
    target_f.to_excel(xw, sheet_name='F_покриття_цілі', index=False)
    target_a.to_excel(xw, sheet_name='A_покриття_цілі', index=False)
    # GRS summary per model
    grs_agg(det_j).reset_index().to_excel(xw, sheet_name='J_GRS_підсумок', index=False)
    grs_agg(det_i).reset_index().to_excel(xw, sheet_name='I_GRS_підсумок', index=False)
    grs_agg(det_h).reset_index().to_excel(xw, sheet_name='H_GRS_підсумок', index=False)
    grs_agg(det_f).reset_index().to_excel(xw, sheet_name='F_GRS_підсумок', index=False)
    grs_agg(det_a).reset_index().to_excel(xw, sheet_name='A_GRS_підсумок', index=False)
    # Monthly comparison A vs F vs H vs I vs J
    monthly_compare.to_excel(xw, sheet_name='Місяць_A_F_H_I_J', index=False)
    # Raw detail J (grs × month)
    det_j[['grs', 'year', 'month', 'fact', 'pred', 'total', 'billing',
           'error', 'bias_pct', 'abs_err']].rename(columns={
        'fact': 'Факт_модем', 'pred': 'Предикт_RT',
        'total': 'Разом_RT', 'billing': 'Білінг',
        'error': 'Похибка', 'bias_pct': 'Bias%', 'abs_err': 'AbsErr',
    }).sort_values(['grs', 'month']).to_excel(xw, sheet_name='J_деталі', index=False)

print(f"\n[OK] {OUT_EXCEL}")

# ══════════════════════════════════════════════════════════════════════════════
# Фінальний підсумок
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 65)
print("ФІНАЛЬНИЙ ПІДСУМОК")
print("=" * 65)
print(f"{'Метрика':<40} {'A:group_med':>11} {'F:bill_prof':>11} {'H:sr_summer':>11} {'I:cal_pbl':>11} {'J:grs_cal':>11}")
print("-" * 87)
print(f"{'MAE (м³/ГРС/місяць)':<40} {mae_a:>11,.0f} {mae_f:>11,.0f} {mae_h:>11,.0f} {mae_i:>11,.0f} {mae_j:>11,.0f}")
print(f"{'MAPE%':<40} {mape_a:>10.2f}% {mape_f:>10.2f}% {mape_h:>10.2f}% {mape_i:>10.2f}% {mape_j:>10.2f}%")
print(f"{'Загальний bias%':<40} {bias_a:>+10.2f}% {bias_f:>+10.2f}% {bias_h:>+10.2f}% {bias_i:>+10.2f}% {bias_j:>+10.2f}%")
print(f"{'Великих ГРС: % пар ≤3% bias':<40} {wl_a:>10.1f}% {wl_f:>10.1f}% {wl_h:>10.1f}% {wl_i:>10.1f}% {wl_j:>10.1f}%")
print(f"{'Малих ГРС: % пар ≤6% bias':<40} {ws_a:>10.1f}% {ws_f:>10.1f}% {ws_h:>10.1f}% {ws_i:>10.1f}% {ws_j:>10.1f}%")
print("\n=== DONE ===")
