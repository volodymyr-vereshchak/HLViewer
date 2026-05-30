"""
Script to create test Excel file with enterprise mappings.

Run this script to generate backend/data/enterprise_mappings.xlsx
with test data for development.
"""

import pandas as pd
import os

# Test data for enterprise mappings
test_data = [
    {
        "line_id": 1,
        "serNum": 100001,
        "mfDev": 16,
        "typeDev": 1,
        "chNum": 1,
        "enterprise_name": "Тестове підприємство 1",
        "active": True
    },
    {
        "line_id": 1,
        "serNum": 100002,
        "mfDev": 16,
        "typeDev": 1,
        "chNum": 1,
        "enterprise_name": "Тестове підприємство 2",
        "active": True
    },
    {
        "line_id": 6,
        "serNum": 200001,
        "mfDev": 16,
        "typeDev": 1,
        "chNum": 1,
        "enterprise_name": "Тестове підприємство 3",
        "active": True
    },
    {
        "line_id": 6,
        "serNum": 200002,
        "mfDev": 16,
        "typeDev": 1,
        "chNum": 1,
        "enterprise_name": "Тестове підприємство 4 (неактивне)",
        "active": False
    },
    {
        "line_id": 10,
        "serNum": 300001,
        "mfDev": 16,
        "typeDev": 1,
        "chNum": 1,
        "enterprise_name": "Тестове підприємство 5",
        "active": True
    }
]

# Create DataFrame
df = pd.DataFrame(test_data)

# Create directory if it doesn't exist
os.makedirs("backend/data", exist_ok=True)

# Save to Excel
output_path = "backend/data/enterprise_mappings.xlsx"
df.to_excel(output_path, index=False)

print(f"✅ Created test Excel file: {output_path}")
print(f"   Total mappings: {len(df)}")
print(f"   Active mappings: {len(df[df['active']])}")
print(f"   Lines covered: {sorted(df['line_id'].unique().tolist())}")
print()
print("Test data structure:")
print(df.to_string())
