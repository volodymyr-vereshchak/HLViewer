"""
Exploratory Data Analysis: Non-heating gas consumer groups (PG and PG,VPG).
Investigates why simple regression consumption ~ residents gives negative R2.
"""

import pandas as pd
import numpy as np
from scipy import stats

# ============================================================
# 1. LOAD DATA, MELT TO LONG FORMAT, FILTER
# ============================================================

print("=" * 80)
print("LOADING AND PREPARING DATA")
print("=" * 80)

df = pd.read_csv(
    r"D:\Projects\HLViewer\HLViewer\backend\data\profile_pobut_daily_result.csv",
    sep=";",
    encoding="utf-8",
)

# Rename columns for convenience
col_map = {
    df.columns[0]: "account_id",
    df.columns[1]: "appliance_group",
    df.columns[2]: "profile_no",
    df.columns[3]: "heated_area",
    df.columns[4]: "total_area",
    df.columns[5]: "residents",
    df.columns[6]: "residents_all",
    df.columns[7]: "device_serial",
}
df.rename(columns=col_map, inplace=True)

# Identify target groups by matching unique values
# (avoids encoding issues with hardcoded Cyrillic literals in script file)
all_groups = df["appliance_group"].unique()
print(f"All groups found: {list(all_groups)}")

# PG is the shortest group name (2 chars), PG,VPG has one comma and no ОП prefix
# Use length-based identification to avoid Cyrillic literal encoding issues
GROUP_PG = [g for g in all_groups if len(g) == 2][0]       # "ПГ"
GROUP_PG_VPG = [g for g in all_groups if len(g) == 6 and g.count(",") == 1][0]  # "ПГ,ВПГ"
TARGET_GROUPS = [GROUP_PG, GROUP_PG_VPG]
print(f"Target groups identified: {TARGET_GROUPS}")

df = df[df["appliance_group"].isin(TARGET_GROUPS)].copy()
print(f"Filtered to target groups. Shape: {df.shape}")
print(f"Group counts: {df['appliance_group'].value_counts().to_dict()}")

# Identify date columns (everything after column 7)
meta_cols = ["account_id", "appliance_group", "profile_no", "heated_area",
             "total_area", "residents", "residents_all", "device_serial"]
date_cols = [c for c in df.columns if c not in meta_cols]
print(f"Number of date columns (days): {len(date_cols)}")
print(f"Date range: {date_cols[0]} to {date_cols[-1]}")

# Melt to long format
long = df.melt(
    id_vars=meta_cols,
    value_vars=date_cols,
    var_name="date_str",
    value_name="daily_consumption",
)
long["date"] = pd.to_datetime(long["date_str"], format="%d.%m.%Y")
long["month"] = long["date"].dt.to_period("M")
long["year_month"] = long["date"].dt.to_period("M").astype(str)

# Convert consumption to numeric
long["daily_consumption"] = pd.to_numeric(long["daily_consumption"], errors="coerce")

# Drop rows with NaN consumption
before = len(long)
long = long.dropna(subset=["daily_consumption"])
print(f"Rows after dropping NaN consumption: {len(long)} (dropped {before - len(long)})")

# Also ensure residents is numeric
long["residents"] = pd.to_numeric(long["residents"], errors="coerce")
long["residents_all"] = pd.to_numeric(long["residents_all"], errors="coerce")

print(f"\nFinal long-format shape: {long.shape}")
print(f"Unique consumers: {long['account_id'].nunique()}")


# ============================================================
# HELPER: monthly aggregation per consumer
# ============================================================

monthly = (
    long.groupby(["account_id", "appliance_group", "residents", "residents_all", "month"])
    .agg(
        monthly_consumption=("daily_consumption", "sum"),
        days_in_month=("daily_consumption", "count"),
    )
    .reset_index()
)
# Only keep full-ish months (>=25 days)
monthly = monthly[monthly["days_in_month"] >= 25].copy()
monthly["per_capita"] = monthly["monthly_consumption"] / monthly["residents"].replace(0, np.nan)

print(f"Monthly records (>=25 days): {len(monthly)}")


for group in TARGET_GROUPS:
    grp_label = "PG (stove only)" if group == GROUP_PG else "PG,VPG (stove + water heater)"
    print("\n")
    print("=" * 80)
    print(f"GROUP: {grp_label}  [{group}]")
    print("=" * 80)

    gl = long[long["appliance_group"] == group]
    gm = monthly[monthly["appliance_group"] == group]

    n_consumers = gl["account_id"].nunique()
    print(f"Consumers: {n_consumers}")

    # ----------------------------------------------------------
    # 2a. Distribution of daily consumption
    # ----------------------------------------------------------
    print("\n--- 2a. DAILY CONSUMPTION DISTRIBUTION ---")
    dc = gl["daily_consumption"]
    print(f"  Total daily observations: {len(dc)}")
    print(f"  Mean:   {dc.mean():.4f}")
    print(f"  Median: {dc.median():.4f}")
    print(f"  Std:    {dc.std():.4f}")
    print(f"  Min:    {dc.min():.4f}")
    print(f"  Max:    {dc.max():.4f}")
    print(f"  Pct zero: {(dc == 0).mean() * 100:.1f}%")
    print(f"  Pct <= 0.01: {(dc <= 0.01).mean() * 100:.1f}%")
    print(f"  Skewness: {dc.skew():.2f}")
    print(f"  Kurtosis: {dc.kurtosis():.2f}")

    # Percentiles
    for p in [5, 10, 25, 50, 75, 90, 95, 99]:
        print(f"  P{p}: {dc.quantile(p/100):.4f}")

    # ----------------------------------------------------------
    # 2b. Distribution of monthly consumption per person
    # ----------------------------------------------------------
    print("\n--- 2b. MONTHLY CONSUMPTION PER PERSON ---")
    pc = gm["per_capita"].dropna()
    print(f"  Observations (consumer-months): {len(pc)}")
    print(f"  Mean:   {pc.mean():.3f} m3/person/month")
    print(f"  Median: {pc.median():.3f}")
    print(f"  Std:    {pc.std():.3f}")
    print(f"  CV (Std/Mean): {pc.std()/pc.mean():.2f}")
    for p in [5, 10, 25, 50, 75, 90, 95]:
        print(f"  P{p}: {pc.quantile(p/100):.3f}")

    # ----------------------------------------------------------
    # 2c. Correlation between consumption and residents
    # ----------------------------------------------------------
    print("\n--- 2c. CONSUMPTION vs RESIDENTS ---")

    # Per-consumer average monthly consumption
    consumer_avg = gm.groupby(["account_id", "residents"]).agg(
        avg_monthly=("monthly_consumption", "mean")
    ).reset_index()
    consumer_avg = consumer_avg.dropna(subset=["residents"])
    consumer_avg = consumer_avg[consumer_avg["residents"] > 0]

    r_pearson, p_pearson = stats.pearsonr(consumer_avg["residents"], consumer_avg["avg_monthly"])
    r_spearman, p_spearman = stats.spearmanr(consumer_avg["residents"], consumer_avg["avg_monthly"])
    print(f"  Pearson r:  {r_pearson:.4f}  (p={p_pearson:.4e})")
    print(f"  Spearman r: {r_spearman:.4f}  (p={p_spearman:.4e})")

    # Simple OLS: consumption = a + b*residents
    from numpy.polynomial.polynomial import polyfit
    slope, intercept = np.polyfit(consumer_avg["residents"], consumer_avg["avg_monthly"], 1)
    predicted = intercept + slope * consumer_avg["residents"]
    ss_res = ((consumer_avg["avg_monthly"] - predicted) ** 2).sum()
    ss_tot = ((consumer_avg["avg_monthly"] - consumer_avg["avg_monthly"].mean()) ** 2).sum()
    r2 = 1 - ss_res / ss_tot
    print(f"  OLS: avg_monthly = {intercept:.3f} + {slope:.3f} * residents")
    print(f"  R2 = {r2:.4f}")

    # Per-capita consumption by number of residents
    print("\n  Per-capita monthly consumption by # residents:")
    percap_by_res = consumer_avg.copy()
    percap_by_res["per_capita_avg"] = percap_by_res["avg_monthly"] / percap_by_res["residents"]
    res_table = percap_by_res.groupby("residents").agg(
        n_consumers=("account_id", "count"),
        mean_monthly=("avg_monthly", "mean"),
        median_monthly=("avg_monthly", "median"),
        mean_per_capita=("per_capita_avg", "mean"),
        median_per_capita=("per_capita_avg", "median"),
    )
    for res_val, row in res_table.iterrows():
        print(f"    residents={int(res_val)}: n={int(row['n_consumers']):3d}, "
              f"mean_monthly={row['mean_monthly']:.2f}, "
              f"mean_per_capita={row['mean_per_capita']:.2f}, "
              f"median_per_capita={row['median_per_capita']:.2f}")

    # ----------------------------------------------------------
    # 2d. Distinct sub-populations?
    # ----------------------------------------------------------
    print("\n--- 2d. SUB-POPULATIONS (per-capita monthly consumption) ---")

    # Consumer-level average per-capita
    consumer_pc = percap_by_res["per_capita_avg"]
    print(f"  Consumer-level avg per-capita monthly consumption:")
    print(f"    Mean: {consumer_pc.mean():.3f}")
    print(f"    Std:  {consumer_pc.std():.3f}")
    print(f"    CV:   {consumer_pc.std()/consumer_pc.mean():.2f}")

    # Histogram-like binning
    bins = [0, 0.5, 1, 2, 3, 5, 10, 20, 50, 200]
    labels = [f"{bins[i]}-{bins[i+1]}" for i in range(len(bins)-1)]
    consumer_pc_binned = pd.cut(consumer_pc, bins=bins, labels=labels, right=True)
    bin_counts = consumer_pc_binned.value_counts().sort_index()
    print(f"\n  Distribution of avg per-capita monthly consumption (m3/person/month):")
    for b, cnt in bin_counts.items():
        pct = cnt / len(consumer_pc) * 100
        bar = "#" * int(pct)
        print(f"    {b:>8s}: {cnt:4d} ({pct:5.1f}%) {bar}")

    # Check for zero/near-zero consumers
    near_zero = (consumer_pc < 0.5).sum()
    heavy = (consumer_pc > 10).sum()
    print(f"\n  Near-zero consumers (<0.5 m3/person/month): {near_zero} ({near_zero/len(consumer_pc)*100:.1f}%)")
    print(f"  Heavy consumers (>10 m3/person/month): {heavy} ({heavy/len(consumer_pc)*100:.1f}%)")

    # Top/bottom 10%
    q10 = consumer_pc.quantile(0.10)
    q90 = consumer_pc.quantile(0.90)
    print(f"  10th percentile: {q10:.3f}")
    print(f"  90th percentile: {q90:.3f}")
    print(f"  90/10 ratio: {q90/q10:.1f}x")

    # ----------------------------------------------------------
    # 2e. Seasonality patterns
    # ----------------------------------------------------------
    print("\n--- 2e. SEASONALITY (monthly medians) ---")
    gm_copy = gm.copy()
    gm_copy["cal_month"] = gm_copy["month"].apply(lambda x: x.month)
    season = gm_copy.groupby("cal_month").agg(
        median_consumption=("monthly_consumption", "median"),
        mean_consumption=("monthly_consumption", "mean"),
        median_per_capita=("per_capita", "median"),
        n_obs=("monthly_consumption", "count"),
    )
    month_names = {1:"Jan",2:"Feb",3:"Mar",4:"Apr",5:"May",6:"Jun",
                   7:"Jul",8:"Aug",9:"Sep",10:"Oct",11:"Nov",12:"Dec"}
    for m, row in season.iterrows():
        print(f"    {month_names.get(m, m):>3s}: median={row['median_consumption']:6.2f}, "
              f"mean={row['mean_consumption']:6.2f}, "
              f"median_per_cap={row['median_per_capita']:5.2f}, "
              f"n={int(row['n_obs'])}")

    # ----------------------------------------------------------
    # 2f. Variance decomposition: between-consumer vs within-consumer
    # ----------------------------------------------------------
    print("\n--- 2f. VARIANCE DECOMPOSITION ---")
    # Use monthly consumption
    overall_mean = gm["monthly_consumption"].mean()
    consumer_means = gm.groupby("account_id")["monthly_consumption"].mean()
    consumer_counts = gm.groupby("account_id")["monthly_consumption"].count()

    # Total variance
    total_var = gm["monthly_consumption"].var()

    # Between-consumer variance: variance of consumer means
    between_var = consumer_means.var()

    # Within-consumer variance: average of within-consumer variances
    within_vars = gm.groupby("account_id")["monthly_consumption"].var().dropna()
    within_var = within_vars.mean()

    print(f"  Total variance of monthly consumption: {total_var:.2f}")
    print(f"  Between-consumer variance (var of consumer means): {between_var:.2f}")
    print(f"  Within-consumer variance (mean of consumer vars):  {within_var:.2f}")
    print(f"  Between / Total: {between_var / total_var * 100:.1f}%")
    print(f"  Within / Total:  {within_var / total_var * 100:.1f}%")

    # ICC (intraclass correlation)
    icc = between_var / (between_var + within_var)
    print(f"  ICC (between / (between + within)): {icc:.3f}")

    # Also decompose per-capita
    pc_vals = gm["per_capita"].dropna()
    if len(pc_vals) > 0:
        total_var_pc = pc_vals.var()
        consumer_means_pc = gm.dropna(subset=["per_capita"]).groupby("account_id")["per_capita"].mean()
        between_var_pc = consumer_means_pc.var()
        within_var_pc = gm.dropna(subset=["per_capita"]).groupby("account_id")["per_capita"].var().dropna().mean()
        print(f"\n  Per-capita variance decomposition:")
        print(f"    Total:   {total_var_pc:.4f}")
        print(f"    Between: {between_var_pc:.4f} ({between_var_pc/total_var_pc*100:.1f}%)")
        print(f"    Within:  {within_var_pc:.4f} ({within_var_pc/total_var_pc*100:.1f}%)")

    # ----------------------------------------------------------
    # 2g. Summer vs Winter for PG,VPG
    # ----------------------------------------------------------
    if group == GROUP_PG_VPG:
        print("\n--- 2g. SUMMER vs WINTER (PG,VPG - water heater seasonality) ---")
        gm_copy2 = gm.copy()
        gm_copy2["cal_month"] = gm_copy2["month"].apply(lambda x: x.month)
        # Winter: Nov-Mar, Summer: May-Sep
        gm_copy2["season"] = gm_copy2["cal_month"].map(
            lambda m: "winter" if m in [11,12,1,2,3] else ("summer" if m in [5,6,7,8,9] else "transition")
        )
        for s in ["winter", "summer", "transition"]:
            subset = gm_copy2[gm_copy2["season"] == s]
            if len(subset) > 0:
                print(f"  {s:>10s}: n={len(subset):5d}, "
                      f"median={subset['monthly_consumption'].median():.2f}, "
                      f"mean={subset['monthly_consumption'].mean():.2f}, "
                      f"median_per_cap={subset['per_capita'].median():.2f}")

        # Ratio
        winter_med = gm_copy2[gm_copy2["season"]=="winter"]["monthly_consumption"].median()
        summer_med = gm_copy2[gm_copy2["season"]=="summer"]["monthly_consumption"].median()
        if summer_med > 0:
            print(f"\n  Winter/Summer median ratio: {winter_med/summer_med:.2f}")
        print(f"  Winter median: {winter_med:.2f}, Summer median: {summer_med:.2f}")
        print(f"  Difference (winter - summer): {winter_med - summer_med:.2f} m3/month")

        # Per-consumer summer vs winter
        consumer_season = gm_copy2[gm_copy2["season"].isin(["winter","summer"])].groupby(
            ["account_id", "season"]
        )["monthly_consumption"].mean().unstack(fill_value=np.nan)
        if "winter" in consumer_season.columns and "summer" in consumer_season.columns:
            both = consumer_season.dropna()
            print(f"\n  Consumers with both winter & summer data: {len(both)}")
            both["ratio"] = both["winter"] / both["summer"].replace(0, np.nan)
            both["diff"] = both["winter"] - both["summer"]
            print(f"  Mean winter/summer ratio: {both['ratio'].dropna().mean():.2f}")
            print(f"  Median winter/summer ratio: {both['ratio'].dropna().median():.2f}")
            print(f"  Mean winter-summer diff: {both['diff'].mean():.2f} m3/month")

    # Also do PG summer vs winter
    if group == GROUP_PG:
        print("\n--- 2g*. SUMMER vs WINTER (PG - stove only, should show NO seasonality) ---")
        gm_copy2 = gm.copy()
        gm_copy2["cal_month"] = gm_copy2["month"].apply(lambda x: x.month)
        gm_copy2["season"] = gm_copy2["cal_month"].map(
            lambda m: "winter" if m in [11,12,1,2,3] else ("summer" if m in [5,6,7,8,9] else "transition")
        )
        for s in ["winter", "summer", "transition"]:
            subset = gm_copy2[gm_copy2["season"] == s]
            if len(subset) > 0:
                print(f"  {s:>10s}: n={len(subset):5d}, "
                      f"median={subset['monthly_consumption'].median():.2f}, "
                      f"mean={subset['monthly_consumption'].mean():.2f}, "
                      f"median_per_cap={subset['per_capita'].median():.2f}")


# ============================================================
# 3. WHY IS R2 NEGATIVE? DIAGNOSIS
# ============================================================
print("\n\n")
print("=" * 80)
print("DIAGNOSIS: WHY IS R2 NEGATIVE FOR consumption ~ residents?")
print("=" * 80)

for group in TARGET_GROUPS:
    grp_label = "PG" if group == GROUP_PG else "PG,VPG"
    gm = monthly[monthly["appliance_group"] == group].copy()

    consumer_avg = gm.groupby(["account_id", "residents"]).agg(
        avg_monthly=("monthly_consumption", "mean")
    ).reset_index()
    consumer_avg = consumer_avg.dropna(subset=["residents"])
    consumer_avg = consumer_avg[consumer_avg["residents"] > 0]

    print(f"\n--- {grp_label} ---")

    # 1. Check how much variance residents explains
    slope, intercept = np.polyfit(consumer_avg["residents"], consumer_avg["avg_monthly"], 1)
    predicted = intercept + slope * consumer_avg["residents"]
    ss_res = ((consumer_avg["avg_monthly"] - predicted) ** 2).sum()
    ss_tot = ((consumer_avg["avg_monthly"] - consumer_avg["avg_monthly"].mean()) ** 2).sum()
    r2 = 1 - ss_res / ss_tot
    print(f"  R2 on consumer-level averages: {r2:.4f}")

    # 2. Check on raw monthly data
    raw = gm.dropna(subset=["residents"])
    raw = raw[raw["residents"] > 0]
    slope2, intercept2 = np.polyfit(raw["residents"], raw["monthly_consumption"], 1)
    pred2 = intercept2 + slope2 * raw["residents"]
    ss_res2 = ((raw["monthly_consumption"] - pred2) ** 2).sum()
    ss_tot2 = ((raw["monthly_consumption"] - raw["monthly_consumption"].mean()) ** 2).sum()
    r2_raw = 1 - ss_res2 / ss_tot2
    print(f"  R2 on raw monthly data: {r2_raw:.4f}")

    # 3. Signal-to-noise ratio
    consumer_means = gm.groupby("account_id")["monthly_consumption"].mean()
    between_std = consumer_means.std()
    within_std = gm.groupby("account_id")["monthly_consumption"].std().mean()
    print(f"  Between-consumer std: {between_std:.2f}")
    print(f"  Within-consumer std:  {within_std:.2f}")
    print(f"  Signal/Noise ratio:   {between_std/within_std:.2f}")

    # 4. Variance of consumption at each resident level
    print(f"\n  Within-group variability at each resident count:")
    for res_val in sorted(consumer_avg["residents"].unique()):
        subset = consumer_avg[consumer_avg["residents"] == res_val]["avg_monthly"]
        if len(subset) >= 2:
            print(f"    residents={int(res_val)}: n={len(subset)}, "
                  f"mean={subset.mean():.2f}, std={subset.std():.2f}, "
                  f"cv={subset.std()/subset.mean():.2f}")
        else:
            print(f"    residents={int(res_val)}: n={len(subset)}, mean={subset.mean():.2f}")

    # 5. How many distinct resident values?
    print(f"\n  Unique resident values: {sorted(consumer_avg['residents'].unique())}")
    print(f"  Residents distribution:")
    res_dist = consumer_avg["residents"].value_counts().sort_index()
    for r, c in res_dist.items():
        print(f"    {int(r)}: {c} consumers ({c/len(consumer_avg)*100:.1f}%)")


# ============================================================
# 4. SUGGESTIONS FOR BETTER PREDICTION
# ============================================================
print("\n\n")
print("=" * 80)
print("CONCRETE SUGGESTIONS FOR IMPROVING PREDICTION")
print("=" * 80)
print("""
Based on the analysis above, here are the key findings and recommendations:

KEY FINDINGS:
1. MASSIVE BETWEEN-CONSUMER HETEROGENEITY: The coefficient of variation of
   per-capita consumption is very high. Consumers with the same number of
   residents have wildly different consumption levels. This means 'residents'
   alone cannot explain consumption.

2. PER-CAPITA CONSUMPTION DECREASES WITH HOUSEHOLD SIZE: Economies of scale
   in cooking/water heating. A 1-person household uses more per-capita than
   a 4-person household. This makes a simple linear model inappropriate.

3. MANY ZERO/NEAR-ZERO CONSUMERS: A significant fraction of consumers barely
   use gas at all (possibly vacant apartments, seasonal absence, or electric
   alternatives). These create a spike at zero that ruins linear regression.

4. HEAVY RIGHT TAIL: Some consumers use far more than others, creating
   extreme outliers that dominate OLS regression.

5. FOR PG,VPG: Water heater creates seasonality - winter consumption is
   higher. A model ignoring month/temperature misses this.

RECOMMENDED APPROACHES:

A. TWO-STAGE MODEL:
   - Stage 1: Classify consumers into 'active' vs 'inactive' (logistic/threshold)
   - Stage 2: For active consumers only, predict consumption

B. LOG TRANSFORMATION:
   - Use log(consumption) as the target. The distribution is right-skewed,
     log transform will normalize it and make variance more homogeneous.

C. BETTER FEATURES:
   - Use log(residents) or sqrt(residents) instead of raw residents
     (captures diminishing per-capita effect)
   - Add month/season as a feature (especially for PG,VPG)
   - Consider residents^2 or categorical residents encoding
   - Profile_no may capture consumer typology

D. MIXED EFFECTS / HIERARCHICAL MODEL:
   - Random intercept per consumer captures individual baseline usage
   - Fixed effects for residents, season
   - This directly addresses the high between-consumer variance

E. QUANTILE REGRESSION:
   - Instead of predicting the mean (which is pulled by outliers),
     predict the median (50th percentile) - more robust

F. CONSUMER CLUSTERING:
   - Cluster consumers by usage pattern first (K-means on monthly profiles)
   - Build separate models per cluster
   - This handles the multi-modal distribution

G. ROBUST REGRESSION:
   - Use Huber loss or LAD instead of OLS to reduce outlier influence
""")
