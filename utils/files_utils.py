import asyncio
import glob
import logging
import os
import re
import shutil
import struct
import uuid
import zipfile
import zlib
from dataclasses import asdict

logger = logging.getLogger(__name__)


# ── Extraction safety limits (zip-slip / zip-bomb guards) ─────────────────────
# Sized far above the real hostlib profile (~1260 files per archive, biggest
# entry ~3.85 MB, ~19 MB uncompressed total, ~3x compression) so legitimate
# data never trips them, while a decompression bomb or a corrupt/runaway
# archive is rejected instead of silently filling the disk.
MAX_ZIP_ENTRIES = 50_000
MAX_FILE_BYTES = 64 * 1024 * 1024        # 64 MB per extracted file
MAX_TOTAL_BYTES = 1024 * 1024 * 1024     # 1 GB per archive
_EXTRACT_CHUNK = 1024 * 1024             # 1 MB streaming chunks


def all_zips(path: str) -> list[str]:
    """Every archive under `path`, the most recently written first.

    Nothing is read out of the file name. Which archive holds what used to be
    guessed from it — "Dnipropetr_2026_09_11_23.zip" was taken apart into a
    source and a snapshot time, and only the newest snapshot of each source
    was read — and a source that renamed its files would have gone unread
    without a word. What has been read is remembered instead (see
    hostlib_archive_log), so a name is only a name.

    The order decides nothing about correctness — everything here is read
    sooner or later — it only puts today's data before last month's when a
    run takes in a backlog.
    """
    found = []
    for root, _dirs, files in os.walk(path):
        for f in files:
            if f.lower().endswith(".zip"):
                found.append(os.path.join(root, f))

    def written_at(p: str) -> float:
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0.0

    return sorted(found, key=written_at, reverse=True)


def zip_is_complete(path: str) -> bool:
    """Whether the archive can be opened — its central directory is written
    last, so a zip still being copied has none and fails here."""
    try:
        with zipfile.ZipFile(path) as archive:
            archive.namelist()
        return True
    except (zipfile.BadZipFile, OSError, EOFError):
        return False


class UnzipUtils:

    def __init__(self, path: str, archives: list[str] | None = None):
        self.path = path
        #: Exactly which archives to extract. The poller passes the ones it has
        #: not read yet; None means everything under `path`, which is what a
        #: person asking for an update by hand wants — they are asking because
        #: something has to be read again.
        self.archives = archives
        # Unique per-instance temp dir to avoid collisions when multiple LUMGs
        # are processed concurrently (each gets its own isolated temp directory).
        self.temp_path = os.path.join(
            os.getcwd(), "hostlibs", f"__temp_{uuid.uuid4().hex}__"
        )
        #: Snapshots that did not open, absolute paths — reported by the
        #: caller so a corrupt archive is named once and not met again.
        self.broken: list[str] = []

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

    def _readable(self) -> list[str]:
        """The archives to extract, minus the ones that do not open."""
        chosen = []
        for zip_path in (self.archives if self.archives is not None
                         else all_zips(self.path)):
            try:
                with zipfile.ZipFile(zip_path, "r"):
                    pass
            except (zipfile.BadZipFile, OSError) as error:
                self.broken.append(zip_path)
                logger.warning("Архів %r не читається (%s) — пропускаю",
                               zip_path, error)
                continue
            chosen.append(zip_path)
        return chosen

    def unzip_files(self):
        """Extract the archives this instance was given, into one temp dir.

        An archive that does not open is skipped rather than fatal: a
        truncated Dnipropetr_2026_01_18_23.zip, eight months old and never
        re-copied, aborted the extraction of the whole share on every run, and
        the branch took in nothing for forty hours (241 runs, 241 failures,
        21–23.09.2026). What could not be read is left in `self.broken` for
        the caller to write down, so it is passed over quietly from then on.

        The size and byte caps below still stop everything, because a bomb is
        somebody trying, not a copy that went wrong.
        """
        os.makedirs(self.temp_path, exist_ok=True)
        temp_root = os.path.realpath(self.temp_path)

        for zip_path in self._readable():
            try:
                self._extract(zip_path, temp_root)
            except (zipfile.BadZipFile, OSError, EOFError, zlib.error) as error:
                # It opened and then came apart mid-read: half its files are
                # in the temp dir, which is fine — the engines read what is
                # there, and the rest arrives with the next snapshot.
                self.broken.append(zip_path)
                logger.warning("Архів %r обірвався під час розпакування (%s) "
                               "— беру, що встиг", zip_path, error)

    def _extract(self, zip_path: str, temp_root: str) -> None:
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
