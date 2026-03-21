"""Аналіз модемів у сільських ГРС та побудова scatter по місяцях."""
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.chdir(r"D:\Projects\HLViewer\HLViewer\backend\pobut")

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

MONTH_NAME = {'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,
              'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12}
MONTH_UA = {1:'Січ',2:'Лют',3:'Бер',4:'Кві',5:'Тра',6:'Чер',
            7:'Лип',8:'Сер',9:'Вер',10:'Жов',11:'Лис',12:'Гру'}

# ── Завантаження ──────────────────────────────────────────────────────────────
ap = pd.read_csv('data/input/all_pobut_enriched.csv', low_memory=False)
ap['account_id'] = pd.to_numeric(ap['account_id'], errors='coerce')
month_cols = [c for c in ap.columns if c.endswith('_2025') and c.split('_')[0] in MONTH_NAME]
for c in month_cols:
    ap[c] = pd.to_numeric(ap[c], errors='coerce').fillna(0)

raw = pd.read_csv('../data/profile_pobut_daily_result.csv', sep=';', low_memory=False)
META = ['account_id','gas_off','alternative','dacha','appliance_group','consumer_type',
        'profile_no','heated_area','total_area','residents','residents_all','serial_no']
raw.columns = META + list(raw.columns[12:])
raw['account_id'] = pd.to_numeric(raw['account_id'], errors='coerce')
modem_ids = set(raw['account_id'].dropna().astype(int))

ap['is_modem'] = ap['account_id'].isin(modem_ids)
ap['billing_total'] = ap[month_cols].sum(axis=1)

# Без агрегованого рядка 222046
ap = ap[ap['account_id'] != 222046].copy()

# Сільські ГРС (без Запоріжжя та Вільнянськ)
city_pat = 'Запоріж|Вільнянськ'
rural = ap[ap['grs'].notna() & ~ap['grs'].str.contains(city_pat, na=False)].copy()

# ── 1. Зведена таблиця: модеми по сільських ГРС ──────────────────────────────
print("=== Модеми по сільських ГРС (білінг 2025) ===")
grp = rural.groupby(['grs','is_modem']).agg(
    count=('account_id','count'),
    billing=('billing_total','sum')
).reset_index()
pivot = grp.pivot_table(index='grs', columns='is_modem',
    values=['count','billing'], aggfunc='sum', fill_value=0)
pivot.columns = ['bill_nonmodem','bill_modem','cnt_nonmodem','cnt_modem']
pivot['modem%'] = (pivot['cnt_modem'] / (pivot['cnt_modem'] + pivot['cnt_nonmodem']) * 100).round(1)
pivot['bill_m%'] = (pivot['bill_modem'] / (pivot['bill_modem'] + pivot['bill_nonmodem']).replace(0, np.nan) * 100).round(1)
pivot = pivot.sort_values('cnt_modem', ascending=False)
print(pivot[['cnt_modem','cnt_nonmodem','modem%','bill_modem','bill_nonmodem','bill_m%']].to_string())
print()

# ── 2. Scatter: модемний avg/споживач vs загальний avg/споживач по місяцях ────
# Для кожної сільської ГРС і кожного місяця:
# X = середнє модемне споживання (м3/споживача)
# Y = середнє по біллінгу (м3/споживача) — всі без gas_off
# Точка = ГРС × місяць

rural_active = rural[rural['gas_off'].fillna('').eq('')].copy()

records = []
for grs_name, g in rural_active.groupby('grs'):
    modem_g = g[g['is_modem']]
    all_g   = g
    cnt_modem = len(modem_g)
    cnt_all   = len(all_g)
    if cnt_modem == 0:
        continue
    for mc, m in [(c, MONTH_NAME[c.split('_')[0]]) for c in month_cols]:
        avg_modem  = modem_g[mc].sum() / cnt_modem if cnt_modem > 0 else np.nan
        avg_all    = all_g[mc].sum() / cnt_all   if cnt_all > 0 else np.nan
        if avg_modem > 0 and avg_all > 0:
            records.append({
                'grs': grs_name,
                'month': m,
                'avg_modem': avg_modem,
                'avg_all': avg_all,
                'cnt_modem': cnt_modem,
                'cnt_all': cnt_all,
                'coef': avg_all / avg_modem,
            })

sc = pd.DataFrame(records)
print(f"Scatter точок (ГРС × місяць): {len(sc)}")
print()

# Коефіцієнти по місяцях
print("=== Коефіцієнт avg_all / avg_modem по місяцях (median по всіх ГРС) ===")
coef_m = sc.groupby('month')['coef'].agg(['median','std','count']).round(3)
coef_m.index = [f"{m} ({MONTH_UA[m]})" for m in coef_m.index]
print(coef_m.to_string())
print()

print("=== Коефіцієнт по ГРС (median по місяцях) ===")
coef_grs = sc.groupby('grs')['coef'].agg(['median','std','count']).round(3)
coef_grs = coef_grs.sort_values('median', ascending=False)
print(coef_grs.to_string())

# ── 3. Scatter plot ───────────────────────────────────────────────────────────
fig, axes = plt.subplots(3, 4, figsize=(16, 12))
axes = axes.flatten()

for idx, m in enumerate(range(1, 13)):
    ax = axes[idx]
    dm = sc[sc['month'] == m]
    if dm.empty:
        ax.set_visible(False)
        continue

    scatter = ax.scatter(dm['avg_modem'], dm['avg_all'],
                         s=dm['cnt_modem'] * 3 + 20,
                         c=dm['coef'], cmap='RdYlGn_r',
                         vmin=0.5, vmax=2.0, alpha=0.8, edgecolors='gray', lw=0.5)

    # Лінія y=x (ідеальний предикт)
    lim = max(dm['avg_modem'].max(), dm['avg_all'].max()) * 1.05
    ax.plot([0, lim], [0, lim], 'k--', lw=0.8, alpha=0.5, label='y=x')

    # Тренд
    if len(dm) >= 3:
        z = np.polyfit(dm['avg_modem'], dm['avg_all'], 1)
        xs = np.linspace(0, dm['avg_modem'].max(), 50)
        ax.plot(xs, np.polyval(z, xs), 'r-', lw=1.2, alpha=0.7,
                label=f'trend k={z[0]:.2f}')

    # Підписи ГРС
    for _, row in dm.iterrows():
        short = row['grs'].split('ГРС ')[-1][:12]
        ax.annotate(short, (row['avg_modem'], row['avg_all']),
                    fontsize=5.5, alpha=0.7,
                    xytext=(2, 2), textcoords='offset points')

    median_coef = dm['coef'].median()
    ax.set_title(f"{MONTH_UA[m]} (медіана k={median_coef:.2f})", fontsize=9)
    ax.set_xlabel('Avg modem (м³/спож)', fontsize=7)
    ax.set_ylabel('Avg billing (м³/спож)', fontsize=7)
    ax.tick_params(labelsize=7)
    ax.legend(fontsize=6)

plt.colorbar(scatter, ax=axes[:], label='coef = avg_all / avg_modem', shrink=0.4)
plt.suptitle('Scatter: середнє модем vs середнє білінг по сільських ГРС × місяць (2025)',
             fontsize=11, y=1.01)
plt.tight_layout()
out_png = 'data/village_scatter.png'
plt.savefig(out_png, dpi=140, bbox_inches='tight')
print(f"\nЗбережено: {out_png}")

# Зберегти таблицю
sc.to_excel('data/village_scatter_data.xlsx', index=False)
print("Збережено: data/village_scatter_data.xlsx")
