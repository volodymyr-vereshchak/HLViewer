# research/

Standalone analysis, EDA and prediction scripts. **Not part of the FastAPI
server** — nothing here is imported by `backend/api`, `backend/hl_engine`,
`backend/db` or `backend/services`. These are run manually from the repo root.

| File | Purpose |
|------|---------|
| `analysis_population_gas.py` / `.ipynb` | Population gas-consumption analysis |
| `analyze_alter.py`, `analyze_non_op.py` | Ad-hoc data analyses |
| `anomaly_analysis.py` | Anomaly detection over archive data |
| `eda_gas_consumption.py` | Exploratory data analysis |
| `data_cleaning.py` | Data-cleaning helpers |
| `check_db_structure.py` | DB structure inspection |
| `summer_consumption_analysis.py` | Summer consumption study |
| `prepare_weather_data.py` | Weather data preparation for prediction |
| `hourly_prediction.py`, `hourly_tri_regime_cv.py` | Hourly consumption prediction |
| `PREDICTION_METHODOLOGY.md` | Methodology notes for the prediction work |

Generated outputs (`*.xlsx`, `*.csv`, `*.png`, `*_output/`) are git-ignored.
