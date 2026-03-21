import pandas as pd
import numpy as np
from datetime import datetime
from sklearn.metrics import r2_score, mean_squared_error
from sklearn.linear_model import LinearRegression
import warnings
warnings.filterwarnings('ignore')

# Load data
print("=" * 80)
print("SUMMER GAS CONSUMPTION ANALYSIS FOR OP CONSUMER GROUPS")
print("=" * 80)
print("\nLoading data...")

df = pd.read_csv(
    r'D:\Projects\HLViewer\HLViewer\backend\data\profile_pobut_daily_result.csv',
    sep=';',
    encoding='utf-8-sig'
)

print(f"Initial dataset size: {len(df)} consumers")

# Column names (Ukrainian)
account_col = 'Особ. рах.'
gas_off_col = 'газ відкл.'
alternative_col = 'альтернативне опалення'
dacha_col = 'дача'
appliance_col = 'Групи приладів'
area_col = 'Площа'
residents_col = 'Прож.'

# 1. DATA CLEANING
print("\n" + "=" * 80)
print("1. DATA CLEANING")
print("=" * 80)

# Filter out flagged consumers
initial_count = len(df)
df = df[
    (df[gas_off_col].isna() | (df[gas_off_col] == '')) &
    (df[alternative_col].isna() | (df[alternative_col] == '')) &
    (df[dacha_col].isna() | (df[dacha_col] == ''))
].copy()

print(f"Consumers after removing flagged (gas_off/alternative/dacha): {len(df)}")
print(f"Removed: {initial_count - len(df)} consumers")

# Filter for relevant appliance groups
valid_groups = ['ПГ', 'ПГ,ВПГ', 'ОП,ПГ', 'ОП,ПГ,ВПГ']
df = df[df[appliance_col].isin(valid_groups)].copy()
print(f"Consumers in target groups (ПГ, ПГ,ВПГ, ОП,ПГ, ОП,ПГ,ВПГ): {len(df)}")

# Clean residents and area
df[residents_col] = pd.to_numeric(df[residents_col], errors='coerce')
df[area_col] = pd.to_numeric(df[area_col], errors='coerce')

# Remove invalid residents/area
df = df[(df[residents_col] > 0) & (df[residents_col] <= 10)].copy()
print(f"Consumers with valid residents (1-10): {len(df)}")

# Get date columns (summer 2025: May-September)
all_cols = df.columns.tolist()
date_cols = [col for col in all_cols if '.' in col and col.count('.') == 2]

# Parse dates and filter for summer 2025
summer_months = []
for col in date_cols:
    try:
        dt = datetime.strptime(col, '%d.%m.%Y')
        if dt.year == 2025 and dt.month in [5, 6, 7, 8, 9]:
            summer_months.append(col)
    except:
        pass

print(f"\nSummer 2025 date columns found: {len(summer_months)}")
print(f"Period: {summer_months[0] if summer_months else 'N/A'} to {summer_months[-1] if summer_months else 'N/A'}")

# 2. AGGREGATE TO MONTHLY CONSUMPTION
print("\n" + "=" * 80)
print("2. MONTHLY SUMMER CONSUMPTION AGGREGATION")
print("=" * 80)

# Create monthly aggregation
records = []
for idx, row in df.iterrows():
    account = row[account_col]
    appliance_group = row[appliance_col]
    residents = row[residents_col]
    area = row[area_col]

    # Aggregate by month
    monthly_data = {}
    for date_col in summer_months:
        dt = datetime.strptime(date_col, '%d.%m.%Y')
        month = dt.month
        month_name = dt.strftime('%Y-%m')

        val = row[date_col]
        try:
            val = float(val)
            if np.isnan(val):
                continue
        except:
            continue

        if month_name not in monthly_data:
            monthly_data[month_name] = []
        monthly_data[month_name].append(val)

    # Calculate monthly sums
    for month_name, values in monthly_data.items():
        if len(values) >= 20:  # At least 20 days of data
            monthly_sum = sum(values)
            records.append({
                'account_id': account,
                'appliance_group': appliance_group,
                'residents': residents,
                'heated_area': area,
                'month': month_name,
                'consumption': monthly_sum,
                'days_count': len(values)
            })

monthly_df = pd.DataFrame(records)
print(f"\nTotal consumer-months: {len(monthly_df)}")
print(f"Consumers with at least one summer month: {monthly_df['account_id'].nunique()}")

# Remove outliers (consumption > 99th percentile per group)
for group in valid_groups:
    mask = monthly_df['appliance_group'] == group
    if mask.sum() > 0:
        p99 = monthly_df.loc[mask, 'consumption'].quantile(0.99)
        outliers = (mask) & (monthly_df['consumption'] > p99)
        monthly_df = monthly_df[~outliers].copy()

print(f"After removing outliers (>99th percentile): {len(monthly_df)} consumer-months")

# Group statistics
print("\nConsumer-months by appliance group:")
for group in valid_groups:
    count = len(monthly_df[monthly_df['appliance_group'] == group])
    unique_consumers = monthly_df[monthly_df['appliance_group'] == group]['account_id'].nunique()
    print(f"  {group:15s}: {count:5d} consumer-months ({unique_consumers:4d} consumers)")

# 3. COMPARE SUMMER CONSUMPTION BETWEEN GROUPS
print("\n" + "=" * 80)
print("3. SUMMER CONSUMPTION COMPARISON BY GROUP")
print("=" * 80)

print("\nMedian monthly consumption by (appliance_group, residents, month):")
print("-" * 80)

# Pivot table for visualization
pivot_residents_month = monthly_df.pivot_table(
    values='consumption',
    index=['appliance_group', 'residents'],
    columns='month',
    aggfunc='median'
)

print(pivot_residents_month.to_string())

print("\n\nMedian monthly consumption by (appliance_group, residents) - averaged across all summer months:")
print("-" * 80)

group_residents_stats = monthly_df.groupby(['appliance_group', 'residents']).agg({
    'consumption': ['median', 'mean', 'count']
}).round(2)
group_residents_stats.columns = ['median', 'mean', 'count']
print(group_residents_stats.to_string())

print("\n\nOverall median consumption by appliance_group:")
print("-" * 80)
for group in valid_groups:
    group_data = monthly_df[monthly_df['appliance_group'] == group]['consumption']
    if len(group_data) > 0:
        print(f"{group:15s}: median={group_data.median():.2f} m3, mean={group_data.mean():.2f} m3, n={len(group_data)}")

# Statistical comparison
print("\n\nAre OP groups systematically higher than pure groups?")
print("-" * 80)

pure_pg = monthly_df[monthly_df['appliance_group'] == 'ПГ']['consumption']
pure_pg_vpg = monthly_df[monthly_df['appliance_group'] == 'ПГ,ВПГ']['consumption']
op_pg = monthly_df[monthly_df['appliance_group'] == 'ОП,ПГ']['consumption']
op_pg_vpg = monthly_df[monthly_df['appliance_group'] == 'ОП,ПГ,ВПГ']['consumption']

print(f"Pure ПГ median: {pure_pg.median():.2f} m3")
print(f"ОП,ПГ median: {op_pg.median():.2f} m3")
print(f"Difference: {op_pg.median() - pure_pg.median():.2f} m3 ({(op_pg.median()/pure_pg.median() - 1)*100:.1f}% higher)")

print(f"\nPure ПГ,ВПГ median: {pure_pg_vpg.median():.2f} m3")
print(f"ОП,ПГ,ВПГ median: {op_pg_vpg.median():.2f} m3")
print(f"Difference: {op_pg_vpg.median() - pure_pg_vpg.median():.2f} m3 ({(op_pg_vpg.median()/pure_pg_vpg.median() - 1)*100:.1f}% higher)")

# 4. ANALYZE WHAT EXPLAINS THE DIFFERENCE
print("\n" + "=" * 80)
print("4. ANALYZING FACTORS EXPLAINING THE DIFFERENCE")
print("=" * 80)

# 4a. Heated area comparison
print("\n4a. Heated area distribution by group:")
print("-" * 80)

for group in valid_groups:
    group_data = monthly_df[monthly_df['appliance_group'] == group]
    if len(group_data) > 0:
        area_data = group_data['heated_area'].dropna()
        if len(area_data) > 0:
            print(f"{group:15s}: median_area={area_data.median():.1f} m2, mean_area={area_data.mean():.1f} m2, n={len(area_data)}")

# 4b. Correlation between area and summer consumption for OP groups
print("\n4b. Correlation between heated_area and summer consumption:")
print("-" * 80)

for group in ['ОП,ПГ', 'ОП,ПГ,ВПГ']:
    group_data = monthly_df[monthly_df['appliance_group'] == group].copy()
    group_data = group_data[group_data['heated_area'].notna() & (group_data['heated_area'] > 0)]

    if len(group_data) > 10:
        corr = group_data[['heated_area', 'consumption']].corr().iloc[0, 1]
        print(f"{group:15s}: correlation = {corr:.3f}, n={len(group_data)}")

        # Show consumption by area quartiles
        group_data['area_quartile'] = pd.qcut(group_data['heated_area'], q=4, labels=['Q1', 'Q2', 'Q3', 'Q4'], duplicates='drop')
        area_quartile_stats = group_data.groupby('area_quartile')['consumption'].median()
        print(f"  Median consumption by area quartile:")
        for q, val in area_quartile_stats.items():
            print(f"    {q}: {val:.2f} m3")

# 4c. Consumption vs residents relationship
print("\n4c. Consumption per resident by group:")
print("-" * 80)

for group in valid_groups:
    group_data = monthly_df[monthly_df['appliance_group'] == group].copy()
    if len(group_data) > 0:
        group_data['consumption_per_resident'] = group_data['consumption'] / group_data['residents']
        median_per_res = group_data['consumption_per_resident'].median()
        print(f"{group:15s}: {median_per_res:.2f} m3/resident/month")

print("\n\nConsumption by number of residents for each group:")
print("-" * 80)
residents_group_stats = monthly_df.groupby(['appliance_group', 'residents'])['consumption'].median().unstack(fill_value=0)
print(residents_group_stats.to_string())

# 5. PREDICTION MODELS
print("\n" + "=" * 80)
print("5. PREDICTION MODEL COMPARISON FOR SUMMER OP GROUPS")
print("=" * 80)

# Filter OP groups for testing
op_groups_df = monthly_df[monthly_df['appliance_group'].isin(['ОП,ПГ', 'ОП,ПГ,ВПГ'])].copy()
print(f"\nOP consumer-months for testing: {len(op_groups_df)}")

# Helper function to calculate metrics
def calculate_metrics(y_true, y_pred, model_name):
    # Remove any NaN predictions
    mask = ~(pd.isna(y_pred) | pd.isna(y_true))
    y_true_clean = y_true[mask]
    y_pred_clean = y_pred[mask]

    if len(y_true_clean) == 0:
        print(f"\n{model_name}:")
        print(f"  ERROR: No valid predictions")
        return

    r2 = r2_score(y_true_clean, y_pred_clean)
    rmse = np.sqrt(mean_squared_error(y_true_clean, y_pred_clean))
    mae = np.mean(np.abs(y_true_clean - y_pred_clean))
    mape = np.mean(np.abs((y_true_clean - y_pred_clean) / y_true_clean)) * 100

    print(f"\n{model_name}:")
    print(f"  R2 = {r2:.4f}")
    print(f"  RMSE = {rmse:.2f} m3")
    print(f"  MAE = {mae:.2f} m3")
    print(f"  MAPE = {mape:.1f}%")
    print(f"  Valid predictions: {len(y_true_clean)}/{len(y_true)}")

    return r2

# 5a. Current approach: use PG/VPG medians from pure groups
print("\n" + "-" * 80)
print("Model A: Use PG/VPG medians from PURE groups")
print("-" * 80)

# Calculate medians from pure groups
pure_pg_medians = monthly_df[monthly_df['appliance_group'] == 'ПГ'].groupby(['residents', 'month'])['consumption'].median()
pure_pg_vpg_medians = monthly_df[monthly_df['appliance_group'] == 'ПГ,ВПГ'].groupby(['residents', 'month'])['consumption'].median()

# Predict for OP groups
predictions_a = []
for idx, row in op_groups_df.iterrows():
    group = row['appliance_group']
    residents = row['residents']
    month = row['month']

    if group == 'ОП,ПГ':
        # Use pure ПГ median
        if (residents, month) in pure_pg_medians.index:
            pred = pure_pg_medians.loc[(residents, month)]
        elif residents in pure_pg_medians.index.get_level_values(0):
            # Use average across months for this residents count
            pred = pure_pg_medians.xs(residents, level=0).median()
        else:
            # Use overall median for ПГ
            pred = monthly_df[monthly_df['appliance_group'] == 'ПГ']['consumption'].median()
    else:  # ОП,ПГ,ВПГ
        # Use pure ПГ,ВПГ median
        if (residents, month) in pure_pg_vpg_medians.index:
            pred = pure_pg_vpg_medians.loc[(residents, month)]
        elif residents in pure_pg_vpg_medians.index.get_level_values(0):
            pred = pure_pg_vpg_medians.xs(residents, level=0).median()
        else:
            pred = monthly_df[monthly_df['appliance_group'] == 'ПГ,ВПГ']['consumption'].median()

    predictions_a.append(pred)

r2_a = calculate_metrics(op_groups_df['consumption'].values, np.array(predictions_a), "Model A")

# 5b. Use PG/VPG medians from OP groups themselves (summer data)
print("\n" + "-" * 80)
print("Model B: Use medians from OP groups' SUMMER data")
print("-" * 80)

op_pg_medians = monthly_df[monthly_df['appliance_group'] == 'ОП,ПГ'].groupby(['residents', 'month'])['consumption'].median()
op_pg_vpg_medians = monthly_df[monthly_df['appliance_group'] == 'ОП,ПГ,ВПГ'].groupby(['residents', 'month'])['consumption'].median()

predictions_b = []
for idx, row in op_groups_df.iterrows():
    group = row['appliance_group']
    residents = row['residents']
    month = row['month']

    if group == 'ОП,ПГ':
        if (residents, month) in op_pg_medians.index:
            pred = op_pg_medians.loc[(residents, month)]
        elif residents in op_pg_medians.index.get_level_values(0):
            pred = op_pg_medians.xs(residents, level=0).median()
        else:
            pred = monthly_df[monthly_df['appliance_group'] == 'ОП,ПГ']['consumption'].median()
    else:
        if (residents, month) in op_pg_vpg_medians.index:
            pred = op_pg_vpg_medians.loc[(residents, month)]
        elif residents in op_pg_vpg_medians.index.get_level_values(0):
            pred = op_pg_vpg_medians.xs(residents, level=0).median()
        else:
            pred = monthly_df[monthly_df['appliance_group'] == 'ОП,ПГ,ВПГ']['consumption'].median()

    predictions_b.append(pred)

r2_b = calculate_metrics(op_groups_df['consumption'].values, np.array(predictions_b), "Model B")

# 5c. Regression: consumption = b * residents
print("\n" + "-" * 80)
print("Model C: Linear regression with RESIDENTS only")
print("-" * 80)

# Train separate models for each OP group
predictions_c = []
for group in ['ОП,ПГ', 'ОП,ПГ,ВПГ']:
    group_data = op_groups_df[op_groups_df['appliance_group'] == group].copy()
    if len(group_data) > 10:
        X = group_data[['residents']].values
        y = group_data['consumption'].values

        model = LinearRegression()
        model.fit(X, y)

        print(f"\n  {group}: consumption = {model.coef_[0]:.2f} * residents + {model.intercept_:.2f}")

        # Predict
        preds = model.predict(X)
        predictions_c.extend(preds)

if len(predictions_c) > 0:
    r2_c = calculate_metrics(op_groups_df['consumption'].values, np.array(predictions_c), "Model C (combined)")

# 5d. Regression: consumption = b * residents + c * heated_area
print("\n" + "-" * 80)
print("Model D: Linear regression with RESIDENTS + HEATED_AREA")
print("-" * 80)

# Filter for valid area data
op_groups_with_area = op_groups_df[op_groups_df['heated_area'].notna() & (op_groups_df['heated_area'] > 0)].copy()
print(f"OP consumer-months with valid area data: {len(op_groups_with_area)}")

predictions_d = []
for group in ['ОП,ПГ', 'ОП,ПГ,ВПГ']:
    group_data = op_groups_with_area[op_groups_with_area['appliance_group'] == group].copy()
    if len(group_data) > 10:
        X = group_data[['residents', 'heated_area']].values
        y = group_data['consumption'].values

        model = LinearRegression()
        model.fit(X, y)

        print(f"\n  {group}: consumption = {model.coef_[0]:.2f} * residents + {model.coef_[1]:.4f} * area + {model.intercept_:.2f}")

        preds = model.predict(X)
        predictions_d.extend(preds)

if len(predictions_d) > 0:
    r2_d = calculate_metrics(op_groups_with_area['consumption'].values, np.array(predictions_d), "Model D (combined)")

# 5e. Medians by (appliance_group, residents, month) directly
print("\n" + "-" * 80)
print("Model E: Direct medians by (appliance_group, residents, month)")
print("-" * 80)

# This is essentially using the group's own median for each combination
group_medians = monthly_df.groupby(['appliance_group', 'residents', 'month'])['consumption'].median()
group_medians_no_month = monthly_df.groupby(['appliance_group', 'residents'])['consumption'].median()
overall_group_medians = monthly_df.groupby(['appliance_group'])['consumption'].median()

predictions_e = []
for idx, row in op_groups_df.iterrows():
    group = row['appliance_group']
    residents = row['residents']
    month = row['month']

    if (group, residents, month) in group_medians.index:
        pred = group_medians.loc[(group, residents, month)]
    elif (group, residents) in group_medians_no_month.index:
        pred = group_medians_no_month.loc[(group, residents)]
    else:
        pred = overall_group_medians.loc[group]

    predictions_e.append(pred)

r2_e = calculate_metrics(op_groups_df['consumption'].values, np.array(predictions_e), "Model E")

# SUMMARY
print("\n" + "=" * 80)
print("6. SUMMARY OF PREDICTION MODELS")
print("=" * 80)

results = [
    ("A: Pure group medians (current)", r2_a if 'r2_a' in locals() else None),
    ("B: OP summer medians", r2_b if 'r2_b' in locals() else None),
    ("C: Regression (residents)", r2_c if 'r2_c' in locals() else None),
    ("D: Regression (residents + area)", r2_d if 'r2_d' in locals() else None),
    ("E: Direct group medians", r2_e if 'r2_e' in locals() else None),
]

print("\nR2 Scores (higher is better):")
print("-" * 80)
for name, r2 in results:
    if r2 is not None:
        print(f"  {name:40s}: R2 = {r2:.4f}")

best_model = max(results, key=lambda x: x[1] if x[1] is not None else -999)
print(f"\nBEST MODEL: {best_model[0]} with R2 = {best_model[1]:.4f}")

print("\n" + "=" * 80)
print("ANALYSIS COMPLETE")
print("=" * 80)
