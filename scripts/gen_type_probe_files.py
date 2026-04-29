"""
Generate probe hostlib files to enumerate all sys/edit type descriptions.

Each file contains 3000 records with type_id 1..3000, fixed date 2024-01-01 00:00:00.
Load the generated files into the original metering software, export to Excel,
then use parse_type_excel.py to create seed JSONs.

Output files:
  out/S001R1A.001  — sys archive  (mask S*R*A.*)
  out/S001R1U.001  — edit archive (mask S*R*U.*)
"""

import os
import struct

OUT_DIR = os.path.join(os.path.dirname(__file__), "probe_hostlib")
os.makedirs(OUT_DIR, exist_ok=True)

# Fixed date: 2024-01-01 00:00:00  ->  month=1, day=1, year=24, hour=0, min=0, sec=0
DATE = (1, 1, 24, 0, 0, 0)
N = 3000

# ── SysStruct: "=6BHBf"  (13 bytes per record) ───────────────────────────────
#   month, day, year, hour, min, sec, sys_type_id(H), unknown(B), volume(f)
sys_fmt = "=6BHBf"

sys_path = os.path.join(OUT_DIR, "S001R1A.001")
with open(sys_path, "wb") as f:
    for type_id in range(0, N):
        f.write(struct.pack(sys_fmt, *DATE, type_id, 0, 0.0))

print(f"Written {N} sys records  -> {sys_path}")

# ── EditStruct: "=6B1H2i"  (16 bytes per record) ─────────────────────────────
#   month, day, year, hour, min, sec, edit_type_id(H), old_value(i), new_value(i)
edit_fmt = "=6B1H2i"

edit_path = os.path.join(OUT_DIR, "S001R1U.001")
with open(edit_path, "wb") as f:
    for type_id in range(0, N):
        f.write(struct.pack(edit_fmt, *DATE, type_id, 0x11111111, 0x11111111))

print(f"Written {N} edit records -> {edit_path}")
print(f"\nFiles are in: {OUT_DIR}")
print("Load them into the original metering software and export to Excel.")
