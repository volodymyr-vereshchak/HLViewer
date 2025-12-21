"""
Script to create enterprise_mappings.xlsx from DPD CSV files.

This script reads CSV files from D:/PO/dpd/ and creates an Excel file
with enterprise-to-line mappings. You need to manually fill in line_id column.
"""

import pandas as pd
import os
import sys

# Manufacturer mapping from loader.py
MF_DEV_MAPPING = {
    "РадмирТех ТОВ СП, м. Харків": 1,
    "Укргазтех ДП, м.Київ": 5,
    "ГРЕМПІС ТОВ НВП, м. Вінниця": 3,
    "Тандем ПП НВФ, м.Вінниця": 4,
}

# Device type mapping from loader.py (manufacturer -> device_type -> code)
TYPE_DEV_MAPPING = {
    1: {  # РадмирТех
        "ВЕГА-1.01": 5,
        "ВЕГА-1.01Н": 15,
        "КПЛГ-2.01Р": 4,
        "КПЛГ-1.02Р": 2,
        "КПЛГ-1.01Р": 2,
        "КПЛГ-1.02РВ": 3,
        "ВЕГА-2.01": 8,
        "ВЕГА-2.01Н": 16,
        "ВЕГА-1.02": 7,
        "КВР-1.01": 11,
        "КВР-1.02": 12,
    },
    5: {  # Укргазтех
        "ФЛОУТЕК-ТМ-2-3-4-Т": 12,
        "ФЛОУТЕК-ТМ-2-3-4": 12,
        "ФЛОУТЕК-ТМ-2-3-6": 12,
        "ФЛОУТЕК-ТМ": 15,
        "ФЛОУТЕК-ТМ-2": 15,
        "ФЛОУТЕК-ТМ-1-3-1": 15,
        "ФЛОУТЕК-ТМ-1": 9,
        "ФЛОУТЕК-ТМ-3": 2
    },
    3: {  # ГРЕМПІС
        "Універсал-02": 2,
        "Універсал-01": 1,
    },
    4: {  # Тандем
        "ТАНДЕМ-ТР": 1,
    },
}


def process_dpd_csv(csv_path):
    """Process DPD CSV file and extract enterprise data."""

    if not os.path.exists(csv_path):
        print(f"[WARNING] CSV file not found: {csv_path}")
        return None

    try:
        # Read CSV (with separator ; and skip first 3 rows as in loader.py)
        df = pd.read_csv(csv_path, sep=";", skiprows=3, header=0)

        print(f"[OK] Loaded {len(df)} records from {csv_path}")

        # Extract needed columns
        result = []

        for idx, row in df.iterrows():
            # Get manufacturer
            manufacturer = row.get("Прилад обліку.Тип приладу обліку.Виробник", "")
            mf_dev = MF_DEV_MAPPING.get(manufacturer)

            if mf_dev is None:
                continue  # Skip unknown manufacturers

            # Get device type
            device_type_name = row.get("Прилад обліку.Тип приладу обліку", "")
            type_dev = TYPE_DEV_MAPPING.get(mf_dev, {}).get(device_type_name)

            if type_dev is None:
                continue  # Skip unknown device types

            # Get serial number
            ser_num_raw = row.get("Прилад обліку.Номер приладу", "")
            if pd.isna(ser_num_raw) or str(ser_num_raw).strip() == "":
                continue

            # Clean serial number (remove dashes and leading zeros)
            ser_num = str(ser_num_raw).replace("-", "").lstrip("0")
            if not ser_num or not ser_num.isdigit():
                continue

            # Get channel number (convert to 0-indexed)
            ch_num_raw = row.get("Прилад обліку.Номер каналу (нумерація з 1)", 1)
            try:
                ch_num = int(ch_num_raw) - 1
            except:
                ch_num = 0

            # Get enterprise name
            enterprise_name = row.get("Контрагент", "")
            if pd.isna(enterprise_name):
                enterprise_name = row.get("Точка обліку", "")

            result.append({
                "line_id": "",  # To be filled manually
                "serNum": int(ser_num),
                "mfDev": mf_dev,
                "typeDev": type_dev,
                "chNum": ch_num,
                "enterprise_name": str(enterprise_name),
                "active": True
            })

        return pd.DataFrame(result)

    except Exception as e:
        print(f"[ERROR] Error processing {csv_path}: {e}")
        return None


def main():
    """Main function to create enterprise mappings Excel file."""

    print("=" * 60)
    print("Creating Enterprise Mappings Excel File")
    print("=" * 60)
    print()

    # Paths to CSV files (from dpd project)
    dpd_csv_paths = [
        "D:/PO/dpd/samples/dpd.csv",
        "../dpd/samples/dpd.csv",
        "../../dpd/samples/dpd.csv",
    ]

    # Try to find CSV file
    csv_path = None
    for path in dpd_csv_paths:
        if os.path.exists(path):
            csv_path = path
            break

    if not csv_path:
        print("[ERROR] Could not find dpd.csv file")
        print("   Tried:")
        for path in dpd_csv_paths:
            print(f"   - {path}")
        print()
        print("Creating minimal example file instead...")

        # Create minimal example
        example_data = [
            {
                "line_id": "",
                "serNum": 8189,
                "mfDev": 1,
                "typeDev": 5,
                "chNum": 0,
                "enterprise_name": "Автосистем Дп ТОВ",
                "active": True
            },
            {
                "line_id": "",
                "serNum": 21390,
                "mfDev": 1,
                "typeDev": 15,
                "chNum": 0,
                "enterprise_name": "Агрокомплекс-2012 ТОВ",
                "active": True
            },
            {
                "line_id": "",
                "serNum": 8480,
                "mfDev": 1,
                "typeDev": 5,
                "chNum": 0,
                "enterprise_name": "Агрошляхбуд ТОВ",
                "active": True
            },
        ]

        df = pd.DataFrame(example_data)
    else:
        # Process CSV file
        print(f"[INFO] Processing: {csv_path}")
        df = process_dpd_csv(csv_path)

        if df is None or df.empty:
            print("[ERROR] No data extracted from CSV")
            return

    # Create output directory
    os.makedirs("backend/data", exist_ok=True)

    # Save to CSV first (can be opened in Excel and saved as .xlsx)
    output_path_csv = "backend/data/enterprise_mappings.csv"
    df.to_csv(output_path_csv, index=False, encoding='utf-8-sig')

    # Try to save as Excel if openpyxl is available
    output_path = "backend/data/enterprise_mappings.xlsx"
    try:
        df.to_excel(output_path, index=False)
        print(f"[SUCCESS] Created Excel file: {output_path}")
    except:
        print(f"[INFO] openpyxl not available, created CSV instead: {output_path_csv}")
        print("   You can open this CSV in Excel and save as .xlsx")
        output_path = output_path_csv

    print()
    print("=" * 60)
    print(f"   Total records: {len(df)}")
    print(f"   Active records: {len(df[df['active']])}")
    print()
    print("[IMPORTANT] You need to manually fill in 'line_id' column!")
    print("   - Open the file in Excel")
    print("   - For each enterprise, add corresponding line_id from HLViewer DB")
    print("   - Save the file as .xlsx")
    print("=" * 60)
    print()
    print("Sample of generated data:")
    print(df.head(10).to_string())


if __name__ == "__main__":
    main()
