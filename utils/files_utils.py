import asyncio
import glob
import os
import shutil
import struct
import uuid
import zipfile
from dataclasses import asdict


# ── Extraction safety limits (zip-slip / zip-bomb guards) ─────────────────────
# Sized far above the real hostlib profile (~1260 files per archive, biggest
# entry ~3.85 MB, ~19 MB uncompressed total, ~3x compression) so legitimate
# data never trips them, while a decompression bomb or a corrupt/runaway
# archive is rejected instead of silently filling the disk.
MAX_ZIP_ENTRIES = 50_000
MAX_FILE_BYTES = 64 * 1024 * 1024        # 64 MB per extracted file
MAX_TOTAL_BYTES = 1024 * 1024 * 1024     # 1 GB per archive
_EXTRACT_CHUNK = 1024 * 1024             # 1 MB streaming chunks


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
        temp_root = os.path.realpath(self.temp_path)

        for zip_path in self._latest_zip_per_dir(self.path):
            with zipfile.ZipFile(zip_path, "r") as zip_file:
                infos = [i for i in zip_file.infolist() if not i.is_dir()]
                if len(infos) > MAX_ZIP_ENTRIES:
                    raise ValueError(
                        f"Refusing to extract {zip_path}: {len(infos)} entries "
                        f"exceed limit {MAX_ZIP_ENTRIES}"
                    )

                total_written = 0
                for file_info in infos:
                    # Zip Slip: resolve the destination and require it to stay
                    # inside temp_path. We block traversal explicitly (with a log
                    # trail) instead of relying on extract()'s silent sanitizing.
                    dest = os.path.realpath(
                        os.path.join(self.temp_path, file_info.filename)
                    )
                    if dest != temp_root and not dest.startswith(temp_root + os.sep):
                        raise ValueError(
                            f"Refusing path traversal in {zip_path}: "
                            f"{file_info.filename!r}"
                        )

                    # Zip Bomb: cheap pre-check on the declared uncompressed size.
                    if file_info.file_size > MAX_FILE_BYTES:
                        raise ValueError(
                            f"Refusing oversized entry in {zip_path}: "
                            f"{file_info.filename} declares {file_info.file_size} bytes"
                        )

                    # Preserve the original "only re-extract if larger" behaviour.
                    if os.path.isfile(dest):
                        try:
                            existing_size = os.path.getsize(dest)
                        except OSError:
                            existing_size = -1
                        if file_info.file_size <= existing_size:
                            continue

                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    written = 0
                    # Stream-extract with a hard byte cap so a lying header
                    # (declares small, decompresses large) can't fill the disk.
                    with zip_file.open(file_info) as src, open(dest, "wb") as out:
                        while True:
                            chunk = src.read(_EXTRACT_CHUNK)
                            if not chunk:
                                break
                            written += len(chunk)
                            total_written += len(chunk)
                            if written > MAX_FILE_BYTES or total_written > MAX_TOTAL_BYTES:
                                out.close()
                                try:
                                    os.remove(dest)
                                except OSError:
                                    pass
                                raise ValueError(
                                    f"Refusing decompression bomb in {zip_path}: "
                                    f"size limit exceeded extracting {file_info.filename}"
                                )
                            out.write(chunk)

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
