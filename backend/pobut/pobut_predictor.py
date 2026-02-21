"""
PobUtPredictor — Production Prediction Class for Household Gas Meters.

Implements RT (Real-Time) model for gas consumption prediction:
  - Modem consumers (with daily readings): use actual data as fact
  - Non-modem consumers (~219K): RT model using group coefficients:
      OP + heating → area × heat_m2_sum(grp, m) + grp_sb[grp] × sr
      OP + summer  → grp_sb[grp] × rate_sum(grp, m)
      non-OP       → grp_med[grp] × rate_sum(grp, m)

Modes:
  - 'offline': load existing modem file → build profiles → predict
  - 'online':  query DPD API for missing dates → append to modem file → predict

Usage:
    from backend.pobut.pobut_predictor import PobUtPredictor, aggregate_by_grs_month

    # Offline mode
    p = PobUtPredictor(
        modem_file='data/input/profile_pobut_daily_result.csv',
        subscribers_file='data/input/all_pobut_enriched.csv',
    )
    df = p.predict(years=[2025])
    agg = aggregate_by_grs_month(df)

    # Online mode (fetches missing dates from DPD API)
    from datetime import datetime
    p = PobUtPredictor(
        modem_file='data/input/profile_pobut_daily_result.csv',
        subscribers_file='data/input/all_pobut_enriched.csv',
        mode='online',
        date_from=datetime(2025, 2, 1),
        date_to=datetime(2025, 2, 28),
    )
    df = p.predict()
"""
import asyncio
import calendar
import logging
import os
import re
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SR_FILE = os.path.join(_MODULE_DIR, 'data', 'profiles', 'pg_seasonal_ratios.csv')

# Regex for DD.MM.YYYY date column names
_DATE_PAT = re.compile(r'^\d{2}\.\d{2}\.\d{4}$')

# ── Constants ──────────────────────────────────────────────────────────────────
HEATING_MONTHS = {1, 2, 3, 4, 10, 11, 12}
SUMMER_MONTHS  = {5, 6, 7, 8, 9}
SKIP_GROUPS    = {'---', '222046', 'ВПГ', 'nan'}
GROUP_MERGE    = {'ОП,ВПГ': 'ОП,ПГ,ВПГ', 'ОП': 'ОП,ПГ'}
OP_FB          = {'ОП,ПГ': 'ОП,ПГ', 'ОП,ПГ,ВПГ': 'ОП,ПГ,ВПГ'}
OP_TO_SR       = {'ОП,ПГ': 'ПГ', 'ОП,ПГ,ВПГ': 'ПГ,ВПГ'}
MIN_REF        = 5
MIN_REF_STRAT  = 3
CT_PRIV        = 'Приватний сектор'

META_COLS = [
    'account_id', 'gas_off', 'alternative', 'dacha', 'appliance_group',
    'consumer_type', 'profile_no', 'heated_area', 'total_area',
    'residents', 'residents_all', 'serial_no',
]

# Optional DPD device columns needed for online mode
DPD_COLS = ['mf_dev', 'type_dev', 'ch_num']


class PobUtPredictor:
    """
    Production predictor for household gas meters using RT model.

    Parameters
    ----------
    modem_file : str
        Path to the modem CSV (semicolon-delimited, UTF-8).
        First 12 columns must be META_COLS; remaining columns are date columns
        in DD.MM.YYYY format. Online mode additionally requires mf_dev,
        type_dev, ch_num columns for DPD device identification.
    subscribers_file : str
        Path to all_pobut_enriched.csv with full consumer metadata.
    mode : str
        'offline' (default) — use only existing modem file data.
        'online' — query DPD API for missing dates; requires date_from/date_to.
    date_from : datetime, optional
        Start date for DPD API queries (required for online mode).
    date_to : datetime, optional
        End date for DPD API queries (required for online mode).
    seasonal_ratios_file : str, optional
        Path to pg_seasonal_ratios.csv. Defaults to
        data/profiles/pg_seasonal_ratios.csv relative to this module.
    """

    def __init__(
        self,
        modem_file: str,
        subscribers_file: str,
        mode: str = 'offline',
        date_from: datetime = None,
        date_to: datetime = None,
        seasonal_ratios_file: str = None,
    ):
        self.modem_file = modem_file
        self.subscribers_file = subscribers_file
        self.mode = mode
        self.date_from = date_from
        self.date_to = date_to
        self.seasonal_ratios_file = seasonal_ratios_file or DEFAULT_SR_FILE

        if mode not in ('offline', 'online'):
            raise ValueError(f"mode must be 'offline' or 'online', got: {mode!r}")
        if mode == 'online' and (date_from is None or date_to is None):
            raise ValueError("date_from and date_to are required for online mode")

        # Internal state — populated by predict()
        self._grp_sb: dict = {}
        self._grp_med: dict = {}
        self._hms_strat_lkp: dict = {}
        self._hms_ct_lkp: dict = {}
        self._hms_grp_lkp: dict = {}
        self._rs_lkp: dict = {}
        self._pg_sr: dict = {}
        self._modem_ids: set = set()
        self._non_modem: pd.DataFrame = None
        self._modem_monthly: pd.DataFrame = None
        self._grs_map: dict = {}
        self._all_pobut: pd.DataFrame = None

    # ── Data Loading ───────────────────────────────────────────────────────────

    def _load_modem_raw(self) -> pd.DataFrame:
        """
        Load modem CSV in wide format.

        First 12 columns are renamed to META_COLS; remaining columns keep
        their original names (date columns DD.MM.YYYY, optional DPD cols).
        Sets self._modem_ids as a side-effect.
        """
        raw = pd.read_csv(self.modem_file, sep=';', low_memory=False, encoding='utf-8')
        raw.columns = META_COLS + list(raw.columns[12:])

        raw['account_id']      = pd.to_numeric(raw['account_id'], errors='coerce')
        raw['appliance_group'] = (
            raw['appliance_group'].astype(str).str.strip().replace(GROUP_MERGE)
        )
        raw['heated_area'] = pd.to_numeric(
            raw['heated_area'].astype(str).str.strip(), errors='coerce'
        ).fillna(55.0)
        raw['heated_area'] = raw['heated_area'].where(raw['heated_area'] > 0, 55.0)

        self._modem_ids = set(raw['account_id'].dropna().astype(int))
        logger.info(f"Loaded {len(self._modem_ids):,} modem consumers from {self.modem_file}")
        return raw

    def _query_and_update_modem(self, raw_df: pd.DataFrame) -> pd.DataFrame:
        """
        Online mode: fetch missing date columns from DPD API and append to modem file.

        Returns updated raw_df with new date columns added.
        If all expected dates are already present (cache hit), returns raw_df unchanged.

        Raises
        ------
        ValueError
            If DPD device columns (mf_dev, type_dev, ch_num) are missing from the file.
        """
        missing_dpd = [c for c in DPD_COLS if c not in raw_df.columns]
        if missing_dpd:
            raise ValueError(
                f"Online mode requires DPD device columns in the modem file, "
                f"but these are missing: {missing_dpd}. "
                f"Add columns mf_dev, type_dev, ch_num alongside serial_no."
            )

        # Build device list from rows that have a serial_no
        device_rows = raw_df[raw_df['serial_no'].notna()].copy()
        devices = [
            {
                'serNum':  str(row['serial_no']),
                'mfDev':   str(row['mf_dev']),
                'typeDev': str(row['type_dev']),
                'chNum':   str(row['ch_num']),
            }
            for _, row in device_rows.iterrows()
        ]
        serial_to_aid = device_rows.set_index('serial_no')['account_id'].to_dict()

        # Existing date columns in file
        existing_date_cols = {c for c in raw_df.columns if _DATE_PAT.match(str(c))}

        # All expected dates in the requested range
        all_expected: list[str] = []
        cur = self.date_from
        while cur <= self.date_to:
            all_expected.append(cur.strftime('%d.%m.%Y'))
            cur += timedelta(days=1)

        missing_dates = [d for d in all_expected if d not in existing_date_cols]
        if not missing_dates:
            logger.info("All dates already cached in modem file — skipping DPD fetch")
            return raw_df

        logger.info(
            f"Fetching {len(missing_dates)} missing date(s) from DPD API "
            f"({missing_dates[0]} … {missing_dates[-1]})"
        )

        # Fetch from DPD API
        records = asyncio.run(self._async_fetch(devices))
        if not records:
            logger.warning("DPD API returned no records")
            return raw_df

        # Convert API records to DataFrame
        api_df = pd.DataFrame(records)
        api_df = api_df[api_df['dvstAlwrk'].notna()].copy()
        api_df['date_str'] = (
            pd.to_datetime(api_df['date'], format='%Y-%m-%d').dt.strftime('%d.%m.%Y')
        )
        api_df['account_id'] = pd.to_numeric(
            api_df['serNum'].map(serial_to_aid), errors='coerce'
        )
        api_df = api_df[
            api_df['account_id'].notna() & api_df['date_str'].isin(missing_dates)
        ]

        if api_df.empty:
            logger.warning("No new data from DPD API for the missing date range")
            return raw_df

        # Pivot: account_id × date_str → dvstAlwrk
        pivoted = api_df.pivot_table(
            index='account_id', columns='date_str',
            values='dvstAlwrk', aggfunc='sum',
        ).reset_index()

        new_cols = [c for c in pivoted.columns if c != 'account_id']
        raw_updated = raw_df.merge(pivoted[['account_id'] + new_cols], on='account_id', how='left')

        # Persist updated file (overwrites in-place, preserving all existing columns)
        raw_updated.to_csv(self.modem_file, sep=';', index=False, encoding='utf-8')
        logger.info(
            f"Modem file updated with {len(new_cols)} new date column(s): {self.modem_file}"
        )
        return raw_updated

    async def _async_fetch(self, devices: list) -> list:
        """Async helper that calls DPDClient.get_volumes."""
        from backend.services.dpd_client import DPDClient
        client = DPDClient()
        return await client.get_volumes(devices, self.date_from, self.date_to)

    # ── Profile Building ───────────────────────────────────────────────────────

    def _build_modem_long(self, raw_df: pd.DataFrame):
        """
        Convert wide modem DataFrame to long format and compute monthly aggregates.

        Parameters
        ----------
        raw_df : pd.DataFrame
            Must already have 'has_OP' column (computed before this call).

        Returns
        -------
        long_df : pd.DataFrame
            One row per (account_id, date) with consumption and derived columns.
        monthly_df : pd.DataFrame
            Monthly aggregated consumption filtered to n_days >= 20.
            Also stored as self._modem_monthly.
        """
        date_cols = [c for c in raw_df.columns if _DATE_PAT.match(str(c))]

        long_df = raw_df.melt(
            id_vars=['account_id', 'appliance_group', 'heated_area', 'has_OP'],
            value_vars=date_cols,
            var_name='date_str',
            value_name='consumption',
        )
        long_df['date']        = pd.to_datetime(long_df['date_str'], format='%d.%m.%Y', errors='coerce')
        long_df['consumption'] = pd.to_numeric(long_df['consumption'], errors='coerce')
        long_df = long_df.dropna(subset=['date', 'consumption']).copy()
        long_df['year']  = long_df['date'].dt.year
        long_df['month'] = long_df['date'].dt.month
        long_df['days_in_m'] = long_df.apply(
            lambda r: calendar.monthrange(int(r['year']), int(r['month']))[1], axis=1
        )

        # Monthly aggregation: sum over all days with reading, then scale to full month
        monthly_df = (
            long_df
            .groupby(['account_id', 'appliance_group', 'has_OP', 'year', 'month'])
            .agg(
                cons_sum=('consumption', 'sum'),
                n_days=('date', 'count'),
                days_in_m=('days_in_m', 'first'),
            )
            .reset_index()
        )
        monthly_df = monthly_df[monthly_df['n_days'] >= 20].copy()
        monthly_df['consumption'] = (
            monthly_df['cons_sum'] / monthly_df['n_days'] * monthly_df['days_in_m']
        )

        self._modem_monthly = monthly_df
        return long_df, monthly_df

    def _build_profiles(self, long_df: pd.DataFrame, monthly_df: pd.DataFrame):
        """
        Build all RT lookup tables from modem data.

        Populates:
          self._grp_sb, self._grp_med, self._pg_sr
          self._hms_strat_lkp, self._hms_ct_lkp, self._hms_grp_lkp
          self._rs_lkp
        """
        # grp_sb: median summer (Jun/Jul/Aug) monthly consumption for OP groups
        summer_op = monthly_df[monthly_df['month'].isin([6, 7, 8]) & monthly_df['has_OP']]
        self._grp_sb = summer_op.groupby('appliance_group')['consumption'].median().to_dict()

        # grp_med: median monthly consumption for non-OP groups
        self._grp_med = (
            monthly_df[~monthly_df['has_OP']]
            .groupby('appliance_group')['consumption'].median()
            .to_dict()
        )

        # Seasonal ratios: group × month → ratio
        sr_df = pd.read_csv(self.seasonal_ratios_file)
        self._pg_sr = {
            (r['group'], int(r['month'])): r['seasonal_ratio']
            for _, r in sr_df.iterrows()
        }

        # Enrich long_df with consumer_type and is_alt from subscribers file
        _ap_meta = self._all_pobut[['account_id', 'consumer_type', 'alternative']].copy()
        _ap_meta['consumer_type'] = _ap_meta['consumer_type'].fillna(CT_PRIV)
        _ap_meta['is_alt'] = _ap_meta['alternative'].notna()
        ct_map  = _ap_meta.set_index('account_id')['consumer_type'].to_dict()
        alt_map = _ap_meta.set_index('account_id')['is_alt'].to_dict()

        long_df['consumer_type'] = long_df['account_id'].map(ct_map).fillna(CT_PRIV)
        long_df['is_alt']        = long_df['account_id'].map(lambda x: alt_map.get(x, False))

        # pbl (group baseline), sr (seasonal ratio), sb_day, heat_day
        long_df['pbl'] = long_df.apply(
            lambda r: self._grp_sb.get(r['appliance_group'], 1.0) if r['has_OP']
                      else self._grp_med.get(r['appliance_group'], 1.0),
            axis=1,
        )
        long_df['sr'] = long_df.apply(
            lambda r: self.get_sr(r['appliance_group'], int(r['month'])), axis=1
        )
        long_df['sb_day'] = long_df.apply(
            lambda r: r['pbl'] * r['sr'] / r['days_in_m'] if r['has_OP']
                      else r['pbl'] / r['days_in_m'],
            axis=1,
        )
        long_df['heat_day'] = long_df['consumption'] - long_df['sb_day']

        # heat_m2_sum strata (OP groups in heating months only)
        ref_op = long_df[
            long_df['has_OP']
            & long_df['month'].isin(HEATING_MONTHS)
            & (long_df['heated_area'] > 0)
        ]

        def _compute_hms(ref: pd.DataFrame, groupby_cols: list) -> pd.DataFrame:
            d = (
                ref.groupby(groupby_cols + ['date'])
                .apply(
                    lambda g: pd.Series({
                        'op_m2': g['heat_day'].sum() / g['heated_area'].sum(),
                        'n': len(g),
                    }),
                    include_groups=False,
                )
                .reset_index()
            )
            d = d[d['n'] >= MIN_REF_STRAT].copy()
            d['op_m2_pos'] = d['op_m2'].clip(lower=0)
            d['year']  = d['date'].dt.year
            d['month'] = d['date'].dt.month
            return (
                d.groupby(groupby_cols + ['year', 'month'])['op_m2_pos']
                .sum()
                .reset_index()
                .rename(columns={'op_m2_pos': 'heat_m2_sum'})
            )

        hms_strat = _compute_hms(ref_op, ['appliance_group', 'consumer_type', 'is_alt'])
        hms_ct    = _compute_hms(ref_op, ['appliance_group', 'consumer_type'])
        hms_grp   = _compute_hms(ref_op, ['appliance_group'])

        self._hms_strat_lkp = hms_strat.set_index(
            ['appliance_group', 'consumer_type', 'is_alt', 'year', 'month']
        )['heat_m2_sum'].to_dict()
        self._hms_ct_lkp = hms_ct.set_index(
            ['appliance_group', 'consumer_type', 'year', 'month']
        )['heat_m2_sum'].to_dict()
        self._hms_grp_lkp = hms_grp.set_index(
            ['appliance_group', 'year', 'month']
        )['heat_m2_sum'].to_dict()

        # rate_sum for non-heating / summer OP periods
        ref_nonheat = long_df[
            ~(long_df['has_OP'] & long_df['month'].isin(HEATING_MONTHS))
            & (long_df['pbl'] > 0)
        ]
        rate_daily = (
            ref_nonheat.groupby(['appliance_group', 'date'])
            .apply(
                lambda g: pd.Series({
                    'rate': g['consumption'].sum() / g['pbl'].sum(),
                    'n': len(g),
                }),
                include_groups=False,
            )
            .reset_index()
        )
        rate_daily = rate_daily[rate_daily['n'] >= MIN_REF].copy()
        rate_daily['year']  = rate_daily['date'].dt.year
        rate_daily['month'] = rate_daily['date'].dt.month
        rate_sum = (
            rate_daily.groupby(['appliance_group', 'year', 'month'])['rate']
            .sum()
            .reset_index()
            .rename(columns={'rate': 'rate_sum'})
        )
        self._rs_lkp = rate_sum.set_index(
            ['appliance_group', 'year', 'month']
        )['rate_sum'].to_dict()

        logger.info(
            f"Profiles built: hms strat={len(hms_strat)} / ct={len(hms_ct)} / grp={len(hms_grp)}, "
            f"rate_sum={len(rate_sum)}"
        )

    def _prepare_non_modem(self):
        """
        Filter non-modem consumers from subscribers file and prepare for prediction.

        Populates self._non_modem and self._grs_map.
        """
        all_pobut = self._all_pobut

        # GRS mapping covers all consumers (modem + non-modem)
        self._grs_map = all_pobut.set_index('account_id')['grs'].to_dict()

        # Filter: exclude modem (have daily readings) and gas-off consumers
        non_modem = all_pobut[
            ~all_pobut['account_id'].isin(self._modem_ids)
            & all_pobut['gas_off'].isna()
        ].copy()

        non_modem['appliance_group'] = (
            non_modem['appliance_group'].astype(str).str.strip().replace(GROUP_MERGE)
        )
        non_modem = non_modem[~non_modem['appliance_group'].isin(SKIP_GROUPS)].copy()

        non_modem['has_OP']        = non_modem['appliance_group'].str.contains('ОП', na=False)
        non_modem['is_dacha']      = non_modem['dacha'].notna()
        non_modem['is_alt']        = non_modem['alternative'].notna()
        non_modem['consumer_type'] = non_modem['consumer_type'].fillna(CT_PRIV)
        non_modem['heated_area']   = pd.to_numeric(
            non_modem['heated_area'].astype(str).str.strip(), errors='coerce'
        ).fillna(55.0)
        non_modem['heated_area'] = non_modem['heated_area'].where(
            non_modem['heated_area'] > 0, 55.0
        )

        # Group baseline — same as modem group medians, with fallbacks
        non_modem['pbl'] = non_modem.apply(
            lambda r: (
                self._grp_sb.get(r['appliance_group'], self._grp_sb.get('ОП,ПГ', 10.0))
                if r['has_OP']
                else self._grp_med.get(r['appliance_group'], self._grp_med.get('ПГ', 5.0))
            ),
            axis=1,
        )

        self._non_modem = non_modem
        logger.info(
            f"Non-modem consumers: {len(non_modem):,} "
            f"(dacha: {non_modem['is_dacha'].sum():,}, "
            f"alternative: {non_modem['is_alt'].sum():,})"
        )

    def _predict_non_modem(self, periods: list) -> pd.DataFrame:
        """
        Compute predicted monthly consumption for all non-modem consumers.

        Parameters
        ----------
        periods : list of (year, month) tuples
            Periods to predict.

        Returns
        -------
        pd.DataFrame with columns: account_id, appliance_group, year, month, volume, grs, type
        """
        records = []
        non_modem = self._non_modem

        for (y, m) in periods:
            is_heat = m in HEATING_MONTHS

            for _, row in non_modem.iterrows():
                grp = row['appliance_group']
                area = row['heated_area']
                pbl  = row['pbl']
                grs  = row['grs']
                aid  = row['account_id']
                ct   = row['consumer_type']
                ia   = row['is_alt']

                # Dacha consumers only in summer
                if row['is_dacha'] and m not in SUMMER_MONTHS:
                    continue

                if row['has_OP'] and is_heat:
                    hms = self.get_hms(grp, ct, ia, y, m)
                    if np.isnan(hms):
                        continue
                    sr  = self.get_sr(grp, m)
                    vol = max(0.0, hms * area) + pbl * sr
                else:
                    rs = self.get_rs(grp, y, m)
                    if np.isnan(rs):
                        continue
                    vol = pbl * rs

                records.append({
                    'account_id': aid,
                    'appliance_group': grp,
                    'year': y,
                    'month': m,
                    'volume': vol,
                    'grs': grs,
                    'type': 'pred',
                })

        return pd.DataFrame(records)

    # ── Lookup Helpers ─────────────────────────────────────────────────────────

    def get_hms(self, grp: str, ct: str, is_alt: bool, y: int, m: int) -> float:
        """
        Heat-per-m² monthly sum for OP groups in heating months.

        Falls back through strata: (grp, ct, is_alt) → (grp, ct) →
        (grp, CT_PRIV) → (grp) → fallback group.
        """
        v = self._hms_strat_lkp.get((grp, ct, is_alt, y, m))
        if v is not None:
            return v
        v = self._hms_ct_lkp.get((grp, ct, y, m))
        if v is not None:
            return v
        v = self._hms_ct_lkp.get((grp, CT_PRIV, y, m))
        if v is not None:
            return v
        v = self._hms_grp_lkp.get((grp, y, m))
        if v is not None:
            return v
        fb = OP_FB.get(grp)
        return self._hms_grp_lkp.get((fb, y, m), np.nan) if fb else np.nan

    def get_rs(self, grp: str, y: int, m: int) -> float:
        """Monthly rate_sum for non-heating / summer OP periods."""
        v = self._rs_lkp.get((grp, y, m))
        if v is not None:
            return v
        fb = OP_FB.get(grp)
        return self._rs_lkp.get((fb, y, m), np.nan) if fb else np.nan

    def get_sr(self, grp: str, m: int) -> float:
        """Seasonal ratio for a group and month."""
        key = OP_TO_SR.get(grp, 'ПГ')
        return self._pg_sr.get((key, m), 1.0)

    # ── Main Entry Point ───────────────────────────────────────────────────────

    def predict(self, years: list = None, months: list = None) -> pd.DataFrame:
        """
        Run the full RT prediction pipeline.

        Parameters
        ----------
        years : list of int, optional
            Restrict output to these years.
        months : list of int, optional
            Restrict output to these months.

        Returns
        -------
        pd.DataFrame
            Combined fact + prediction rows with columns:
            account_id, appliance_group, year, month, volume, grs, type
            where type is 'fact' (modem daily readings) or 'pred' (RT model).
        """
        # 1. Load modem data
        raw = self._load_modem_raw()

        # 2. Online: fetch missing dates and update modem file
        if self.mode == 'online':
            raw = self._query_and_update_modem(raw)

        # Derive has_OP after GROUP_MERGE but before melting
        raw['has_OP'] = raw['appliance_group'].str.contains('ОП', na=False)

        # 3. Load subscribers data once (shared between _build_profiles + _prepare_non_modem)
        self._all_pobut = pd.read_csv(
            self.subscribers_file, low_memory=False, encoding='utf-8'
        )
        self._all_pobut['account_id'] = pd.to_numeric(
            self._all_pobut['account_id'], errors='coerce'
        )

        # 4. Build modem long + monthly
        long_df, monthly_df = self._build_modem_long(raw)

        # 5. Build RT profiles from modem reference data
        self._build_profiles(long_df, monthly_df)

        # 6. Prepare non-modem consumers
        self._prepare_non_modem()

        # 7. Determine periods
        periods = sorted(set(zip(monthly_df['year'], monthly_df['month'])))
        if years:
            periods = [(y, m) for y, m in periods if y in years]
        if months:
            periods = [(y, m) for y, m in periods if m in months]

        # 8. Modem fact (monthly aggregated readings)
        modem_fact = self._modem_monthly[
            ['account_id', 'appliance_group', 'year', 'month', 'consumption']
        ].copy()
        modem_fact['grs']  = modem_fact['account_id'].map(self._grs_map)
        modem_fact['type'] = 'fact'
        modem_fact = modem_fact.rename(columns={'consumption': 'volume'})

        # Filter modem_fact to requested periods
        if years or months:
            period_set = set(periods)
            modem_fact = modem_fact[
                modem_fact.apply(lambda r: (r['year'], r['month']) in period_set, axis=1)
            ]

        # 9. Non-modem RT prediction
        pred = self._predict_non_modem(periods)

        # 10. Combine and filter rows that have a known GRS
        combined = pd.concat([modem_fact, pred], ignore_index=True)
        combined = combined[combined['grs'].notna()].copy()
        return combined

    # ── Aggregation Helper ─────────────────────────────────────────────────────

    @staticmethod
    def aggregate_by_grs_month(combined_df: pd.DataFrame) -> pd.DataFrame:
        """
        Aggregate predict() output by GRS × year × month.

        Returns a pivot table with columns: grs, year, month, fact, pred, total, fact_share.
        """
        agg = (
            combined_df
            .groupby(['grs', 'year', 'month', 'type'])['volume']
            .sum()
            .reset_index()
        )
        pivot = agg.pivot_table(
            index=['grs', 'year', 'month'],
            columns='type',
            values='volume',
            aggfunc='sum',
            fill_value=0,
        ).reset_index()
        pivot.columns.name = None

        pivot['total'] = pivot.get('fact', 0) + pivot.get('pred', 0)
        pivot['fact_share'] = (
            pivot.get('fact', 0) / pivot['total'].replace(0, np.nan) * 100
        ).round(1)
        return pivot


# Module-level convenience alias
def aggregate_by_grs_month(combined_df: pd.DataFrame) -> pd.DataFrame:
    """Module-level alias for PobUtPredictor.aggregate_by_grs_month."""
    return PobUtPredictor.aggregate_by_grs_month(combined_df)
