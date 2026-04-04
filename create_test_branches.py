"""
Create 20 test branches for performance testing.
Each branch gets:
- a copy of the ZP zip with EIS folders renamed to unique codes
- a LUMG with data path pointing to hostlibs/TEST_XX
- EIS codes matching the renamed folders
"""
import asyncio
import os
import zipfile
import io

# Must run from /app
os.chdir("/app")

DATABASE_URL = None  # loaded from settings

async def main():
    from backend.db.engine import async_session_factory
    from backend.db.models.grmu_branch_model import GrmuBranch
    from backend.db.models.lumg_model import Lumg, LumgDataPath, LumgEisCode
    from sqlmodel import select
    from datetime import datetime

    # --- Find latest ZP zip ---
    zp_dir = "hostlibs/ZP"
    zips = [os.path.join(zp_dir, f) for f in os.listdir(zp_dir) if f.endswith(".zip")]
    latest_zip = max(zips, key=os.path.getmtime)
    print(f"Source zip: {latest_zip}")

    # --- Read zip contents ---
    with zipfile.ZipFile(latest_zip) as zf:
        all_infos = zf.infolist()
        # Find top-level dir name (e.g. "Zaporizgaz")
        top_dir = all_infos[0].filename.split("/")[0]
        # Get unique EIS folder names (second level)
        eis_folders = sorted(set(
            info.filename.split("/")[1]
            for info in all_infos
            if len(info.filename.split("/")) >= 2 and info.filename.split("/")[1]
        ))
        print(f"Top dir: {top_dir}, EIS folders: {len(eis_folders)}: {eis_folders}")

    branch_names = [
        "Київська тест", "Харківська тест", "Одеська тест", "Дніпровська тест",
        "Львівська тест", "Полтавська тест", "Черкаська тест", "Херсонська тест",
        "Миколаївська тест", "Вінницька тест", "Житомирська тест", "Чернігівська тест",
        "Сумська тест", "Хмельницька тест", "Тернопільська тест", "Рівненська тест",
        "Волинська тест", "Івано-Франківська тест", "Чернівецька тест", "Закарпатська тест",
    ]

    ts = datetime.utcnow()

    async with async_session_factory() as session:
        for i, branch_name in enumerate(branch_names, start=1):
            # 1. Unique EIS codes for this branch: T{i:02d} + last 14 chars of original
            eis_map = {}  # old_name → new_name
            for original in eis_folders:
                new_code = f"T{i:02d}" + original[3:]  # replace first 3 chars
                eis_map[original] = new_code

            # 2. Create zip with renamed EIS folders
            test_dir = f"hostlibs/TEST_{i:02d}"
            os.makedirs(test_dir, exist_ok=True)
            zip_path = f"{test_dir}/testdata_{i:02d}.zip"

            with zipfile.ZipFile(latest_zip) as src_zf:
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as dst_zf:
                    for info in src_zf.infolist():
                        parts = info.filename.split("/")
                        if len(parts) >= 2 and parts[1] in eis_map:
                            parts[1] = eis_map[parts[1]]
                            info.filename = "/".join(parts)
                        data = src_zf.read(info.filename.replace(parts[1], list(eis_map.keys())[list(eis_map.values()).index(parts[1])]) if len(parts) >= 2 and parts[1] in eis_map.values() else info.filename) if not info.is_dir() else b""
                        if not info.is_dir():
                            dst_zf.writestr(info, data)
                with open(zip_path, "wb") as f:
                    f.write(buf.getvalue())

            print(f"[{i:02d}] Created {zip_path}")

            # 3. Create branch
            branch = GrmuBranch(name=branch_name, active=True, created_at=ts, updated_at=ts)
            session.add(branch)
            await session.flush()

            # 4. Create LUMG
            lumg = Lumg(name=f"ТЛВУМГ-{i:02d}", branch_id=branch.id, created_at=ts, updated_at=ts)
            session.add(lumg)
            await session.flush()

            # 5. Data path
            dp = LumgDataPath(lumg_id=lumg.id, path=test_dir, active=True, created_at=ts, updated_at=ts)
            session.add(dp)

            # 6. EIS codes
            for new_code in eis_map.values():
                ec = LumgEisCode(lumg_id=lumg.id, eis_code=new_code, created_at=ts, updated_at=ts)
                session.add(ec)

            await session.commit()
            print(f"[{i:02d}] Branch '{branch_name}' → LUMG {lumg.id}, path={test_dir}, {len(eis_map)} EIS codes")

    print("\nDone! Created 20 test branches.")

asyncio.run(main())
