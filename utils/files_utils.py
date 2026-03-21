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

    def unzip_files(self):
        os.makedirs(self.temp_path, exist_ok=True)

        for root, _, files in os.walk(self.path):
            for file in files:
                if file.endswith(".zip"):
                    zip_path = os.path.join(root, file)
                    with zipfile.ZipFile(zip_path, "r") as zip_file:
                        for file_info in zip_file.infolist():
                            if file_info.is_dir():
                                continue
                            extracted_path = os.path.join(
                                self.temp_path, file_info.filename
                            )

                            if os.path.exists(extracted_path):
                                existing_size = os.stat(extracted_path).st_size
                                new_size = file_info.file_size

                                if new_size > existing_size:
                                    zip_file.extract(file_info.filename, self.temp_path)
                            else:
                                zip_file.extract(file_info.filename, self.temp_path)

    def delete_unzip_folder(self):
        if os.path.exists(self.temp_path):
            shutil.rmtree(self.temp_path)


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
