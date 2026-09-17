import asyncio
import glob
import os
import re
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


#: "Dnipropetr_2026_09_11_23.zip" -> "Dnipropetr": the snapshot's date and
#: hour number are dropped, what is left names the source.
_SNAPSHOT_SUFFIX = re.compile(r"_(\d{4})_(\d{2})_(\d{2})(?:_(\d+))?\.zip$", re.IGNORECASE)


def snapshot_order(path: str) -> tuple:
    """Newest-first ordering key for a snapshot: the date and hour in its name,
    then the file's time.

    The name comes first because it is what the source wrote on purpose; the
    file time is whatever the copy left — a copy that keeps the original time
    would otherwise make a newer snapshot look older. The file time only
    decides among names that carry no date.
    """
    match = _SNAPSHOT_SUFFIX.search(os.path.basename(path))
    stamp = tuple(int(g or 0) for g in match.groups()) if match else (0, 0, 0, 0)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    return stamp + (mtime,)


def zip_is_complete(path: str) -> bool:
    """Whether the archive can be opened — its central directory is written
    last, so a zip still being copied has none and fails here."""
    try:
        with zipfile.ZipFile(path) as archive:
            archive.namelist()
        return True
    except (zipfile.BadZipFile, OSError, EOFError):
        return False


def _zip_source(filename: str) -> str:
    """The data source a snapshot belongs to, from its file name.

    A name without the date pattern is its own source, so an archive named
    some other way is still read rather than dropped behind another one.
    """
    stripped = _SNAPSHOT_SUFFIX.sub("", filename)
    return stripped if stripped != filename else filename


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
        """The most recently modified zip of each data source under path.

        When a data source uploads multiple hourly snapshots (e.g.
        Zaporizgaz_2026_04_04_0.zip … Zaporizgaz_2026_04_04_19.zip) each
        containing a full copy of all device files, we only need the newest one.
        Extracting all 20 would take 20× as long for zero extra data.

        Newest per SOURCE, not per directory. One folder can hold several
        sources side by side — the Дніпро test folder has Dnipropetr_* (every
        ГРС of the branch) and UGV_DNP_* (seven ГПУ devices) — and picking one
        zip for the whole folder took whichever was written last: UGV_DNP, by
        fourteen minutes, so the whole Dnipropetr archive was never read and
        126 lines of two ЛВУМГ got nothing while their EIC codes matched.
        """
        result = []
        for root, dirs, files in os.walk(path):
            by_source: dict[str, list[str]] = {}
            for f in files:
                if f.lower().endswith(".zip"):
                    by_source.setdefault(_zip_source(f), []).append(os.path.join(root, f))
            for zips in by_source.values():
                result.append(max(zips, key=snapshot_order))
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

    Mirrors UnzipUtils._latest_zip_per_dir exactly — the newest zip of each
    data source, not of each directory. The two must agree: if the signature
    looked at one zip per folder while the unzip read one per source, a new
    Dnipropetr snapshot in a folder where a UGV_DNP one is newer would change
    nothing the poller sees, and no update would ever be triggered for it.

    The signature is a frozenset of (relpath, mtime, size) for those zips, plus
    the maximum mtime across them. The poller compares signatures between
    ticks: a changed signature means a new file arrived. `max_mtime` feeds the
    settle-guard (don't act on a file that may still be mid-upload).
    Missing/empty path → (frozenset(), 0.0).
    """
    entries: list[tuple[str, float, int]] = []
    max_mtime = 0.0
    if not os.path.isdir(path):
        return frozenset(), 0.0
    try:
        newest = UnzipUtils._latest_zip_per_dir(path)
    except OSError:
        return frozenset(), 0.0
    for latest in newest:
        try:
            mtime = os.path.getmtime(latest)
            size = os.path.getsize(latest)
        except OSError:
            # File vanished between listing and stat (e.g. mid-upload churn).
            continue
        entries.append((os.path.relpath(latest, path), mtime, size))
        max_mtime = max(max_mtime, mtime)
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
