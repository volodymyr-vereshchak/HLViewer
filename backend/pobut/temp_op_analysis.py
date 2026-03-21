import pandas as pd
import numpy as np
import calendar

raw = pd.read_csv("data/input/profile_pobut_daily_result.csv", sep=';', low_memory=False, encoding='utf-8')
META = ['account_id','gas_off','alternative','dacha','appliance_group',
        'profile_no','heated_area','total_area','residents','residents_all','serial_no']
raw.columns = META + list(raw.columns[11:])
date_cols = raw.columns[11:].tolist()
raw['account_id'] = pd.to_numeric(raw['account_id'], errors='coerce')
raw['appliance_group'] = raw['appliance_group'].astype(str).str.strip()
raw['heated_area'] = pd.to_numeric(raw['heated_area'].astype(str).str.strip(), errors='coerce').fillna(55.0)
raw['residents'] = pd.to_numeric(raw['residents'], errors='coerce').fillna(2.0)
op = raw[raw['appliance_group'].str.contains('ОП', na=False)].copy()
print(f"ОП consumers: {len(op)}")
print(f"Groups: {op['appliance_group'].value_counts().to_dict()}")
print(f"heated_area: median={op['heated_area'].median():.1f}, mean={op['heated_area'].mean():.1f}")
print(f"residents: median={op['residents'].median():.1f}, mean={op['residents'].mean():.1f}")

# Melt and aggregate monthly
long = op.melt(id_vars=['account_id','appliance_group','heated_area','residents'],
               value_vars=date_cols, var_name='date_str', value_name='consumption')
long['date'] = pd.to_datetime(long['date_str'], format='%d.%m.%Y', errors='coerce')
long['consumption'] = pd.to_numeric(long['consumption'], errors='coerce')
long = long.dropna(subset=['date'])
long['year'] = long['date'].dt.year
long['month'] = long['date'].dt.month

monthly = (long.groupby(['account_id','appliance_group','heated_area','residents','year','month'])
           .agg(cons_sum=('consumption','sum'), n_days=('date','count')).reset_index())
monthly = monthly[monthly['n_days'] >= 20].copy()
monthly['days_in_m'] = monthly.apply(lambda r: calendar.monthrange(int(r['year']),int(r['month']))[1], axis=1)
monthly['consumption'] = monthly['cons_sum'] / monthly['n_days'] * monthly['days_in_m']
monthly = monthly[monthly['consumption'] > 0].copy()

print("\nMonthly records by month (all ОП consumers):")
print(monthly.groupby('month').agg(n=('account_id','count'), median_cons=('consumption','median')).round(1).to_string())

print("\nRecords with summer data (months 6,7,8) per group:")
summer = monthly[monthly['month'].isin([6,7,8])]
print(summer.groupby('appliance_group').agg(
    n_records=('account_id','count'),
    n_consumers=('account_id','nunique'),
    median_cons=('consumption','median'),
    mean_cons=('consumption','mean')
).round(2).to_string())

print("\nFor consumers WITH summer data — what is their winter consumption pattern?")
consumers_with_summer = set(summer['account_id'].unique())
winter = monthly[monthly['month'].isin([11,12,1,2]) & monthly['account_id'].isin(consumers_with_summer)].copy()
print(f"Consumers with both summer AND winter data: {winter['account_id'].nunique()}")

# Compute per-consumer summer baseline (months 6,7,8)
summer_baseline = (summer.groupby(['account_id','appliance_group'])
                   ['consumption'].median().reset_index()
                   .rename(columns={'consumption':'summer_baseline'}))
print("\nSummer baseline stats by group:")
print(summer_baseline.groupby('appliance_group')['summer_baseline']
      .describe().round(2).to_string())

# Join baseline with winter data
winter2 = winter.merge(summer_baseline[['account_id','summer_baseline']], on='account_id', how='inner')
winter2['op_heating'] = np.maximum(0, winter2['consumption'] - winter2['summer_baseline'])
winter2['op_per_m2'] = winter2['op_heating'] / winter2['heated_area'].clip(lower=1)

print("\nop_per_m2 statistics by group and month:")
print(winter2.groupby(['appliance_group','month'])['op_per_m2']
      .describe()[['count','mean','std','50%']].round(3).to_string())

print("\nCorrelation of op_per_m2 with heated_area, residents:")
for grp, gdf in winter2.groupby('appliance_group'):
    corr_area = gdf['op_per_m2'].corr(gdf['heated_area'])
    corr_res  = gdf['op_per_m2'].corr(gdf['residents'])
    cv = gdf['op_per_m2'].std() / gdf['op_per_m2'].mean()
    print(f"  {grp}: corr_area={corr_area:.3f}, corr_residents={corr_res:.3f}, CV={cv:.3f}")
