import asyncio
import glob
import os
import shutil
import struct
import uuid
import zipfile
from dataclasses import asdict


class UnzipUtils:

    def __init__(self, path: str):
        self.path = path
        # Unique per-instance temp dir to avoid collisions when multiple LUMGs
        # are processed concurrently (each gets its own isolated temp directory).
        self.temp_path = os.path.join(
            os.getcwd(), "hostlibs", f"__temp_{uuid.uuid4().hex}__"
        )

    def __enter__(self):
        self.unzip_files()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.delete_unzip_folder()
        return False

    async def __aenter__(self):
        await asyncio.to_thread(self.unzip_files)
        return self

    async def __aexit__(self, *args):
        await asyncio.to_thread(self.delete_unzip_folder)
        return False

    @staticmethod
    def _latest_zip_per_dir(path: str) -> list[str]:
        """For each directory under path, return only the most recently modified zip.

        When a data source uploads multiple hourly snapshots (e.g.
        Zaporizgaz_2026_04_04_0.zip … Zaporizgaz_2026_04_04_19.zip) each
        containing a full copy of all device files, we only need the newest one.
        Extracting all 20 would take 20× as long for zero extra data.
        """
        result = []
        for root, dirs, files in os.walk(path):
            zips = [os.path.join(root, f) for f in files if f.endswith(".zip")]
            if not zips:
                continue
            # Pick the single newest zip in this directory
            latest = max(zips, key=os.path.getmtime)
            result.append(latest)
        return result

    def unzip_files(self):
        os.makedirs(self.temp_path, exist_ok=True)

        for zip_path in self._latest_zip_per_dir(self.path):
            with zipfile.ZipFile(zip_path, "r") as zip_file:
                for file_info in zip_file.infolist():
                    if file_info.is_dir():
                        continue
                    extracted_path = os.path.join(
                        self.temp_path, file_info.filename
                    )
                    if os.path.isfile(extracted_path):
                        try:
                            existing_size = os.path.getsize(extracted_path)
                        except OSError:
                            existing_size = -1
                        if file_info.file_size > existing_size:
                            zip_file.extract(file_info.filename, self.temp_path)
                    else:
                        zip_file.extract(file_info.filename, self.temp_path)

    def delete_unzip_folder(self):
        # Tolerate races during teardown: on overlayfs (Docker) rmtree can hit a
        # transient FileNotFoundError when an entry it just enumerated is already
        # gone. Cleanup is best-effort — any leftover temp dir is swept by
        # _cleanup_orphan_temp_dirs() on the next run, so a cleanup hiccup must
        # never fail an update whose data was already written.
        if os.path.exists(self.temp_path):
            shutil.rmtree(self.temp_path, ignore_errors=True)


def newest_zip_signature(path: str) -> tuple[frozenset, float]:
    """Build a change-detection signature for the zips under `path`.

    Mirrors UnzipUtils._latest_zip_per_dir: for each directory we only care about
    the single newest zip (data sources upload full hourly snapshots, so only the
    latest matters). The signature is a frozenset of (relpath, mtime, size) for
    those newest-per-dir zips, plus the maximum mtime across them.

    The poller compares signatures between ticks: a changed signature means a new
    file arrived. `max_mtime` feeds the settle-guard (don't act on a file that may
    still be mid-upload). Missing/empty path → (frozenset(), 0.0).
    """
    entries: list[tuple[str, float, int]] = []
    max_mtime = 0.0
    if not os.path.isdir(path):
        return frozenset(), 0.0
    for root, dirs, files in os.walk(path):
        zips = [os.path.join(root, f) for f in files if f.endswith(".zip")]
        if not zips:
            continue
        try:
            latest = max(zips, key=os.path.getmtime)
            mtime = os.path.getmtime(latest)
            size = os.path.getsize(latest)
        except OSError:
            # File vanished between listing and stat (e.g. mid-upload churn).
            continue
        rel = os.path.relpath(latest, path)
        entries.append((rel, mtime, size))
        if mtime > max_mtime:
            max_mtime = mtime
    return frozenset(entries), max_mtime


def find_files_by_mask(path: str, mask: str) -> list[str]:
    file_path = os.path.join(path, "**", mask)
    unpacked_files = glob.glob(file_path, recursive=True)

    return unpacked_files


def read_archive_file(file, file_struct):
    with open(file, "rb") as archive_file:
        while True:
            data = archive_file.read(file_struct.size)
            if not data:
                break
            try:
                file_dict = asdict(file_struct.unpack(data))
                yield file_dict
            except (struct.error, ValueError) as e:
                # Skip invalid records and continue
                continue
