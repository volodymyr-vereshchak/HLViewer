import os
import tempfile
import zipfile
import pytest
from unittest.mock import patch, mock_open

from utils.files_utils import (UnzipUtils, all_zips, find_files_by_mask,
                               read_archive_file)
from backend.hl_engine.data_classes.hour_dataclass import HourStruct


@pytest.fixture
def temp_dir(tmp_path) -> str:
    """String path to a per-test temporary directory (the code under test
    works with plain string paths)."""
    return str(tmp_path)


class TestUnzipUtils:
    """Test UnzipUtils functionality."""
    
    def test_unzip_utils_initialization(self, temp_dir):
        """Test UnzipUtils initialization."""
        unzip_utils = UnzipUtils(temp_dir)
        assert unzip_utils.path == temp_dir
        # temp dir is unique per instance: hostlibs/__temp_<uuid>__
        assert "hostlibs" in unzip_utils.temp_path
        assert "__temp_" in unzip_utils.temp_path
        # two instances must not collide
        assert UnzipUtils(temp_dir).temp_path != unzip_utils.temp_path
    
    def test_context_manager_behavior(self, temp_dir):
        """Test UnzipUtils as context manager."""
        with UnzipUtils(temp_dir) as utils:
            assert os.path.exists(utils.temp_path)
        
        # After context exit, temp directory should be cleaned up
        assert not os.path.exists(utils.temp_path)
    
    def test_unzip_files_creates_temp_directory(self, temp_dir):
        """Test that unzip_files creates temporary directory."""
        unzip_utils = UnzipUtils(temp_dir)
        unzip_utils.unzip_files()
        
        assert os.path.exists(unzip_utils.temp_path)
        assert os.path.isdir(unzip_utils.temp_path)
    
    def test_delete_unzip_folder(self, temp_dir):
        """Test deletion of unzipped folder."""
        unzip_utils = UnzipUtils(temp_dir)
        unzip_utils.unzip_files()
        
        # Verify folder exists
        assert os.path.exists(unzip_utils.temp_path)
        
        # Delete folder
        unzip_utils.delete_unzip_folder()
        
        # Verify folder is deleted
        assert not os.path.exists(unzip_utils.temp_path)
    
    def test_unzip_files_with_zip_file(self, temp_dir):
        """Test unzipping actual zip files."""
        # Create a test zip file
        zip_path = os.path.join(temp_dir, "test.zip")
        test_content = b"test file content"
        
        with zipfile.ZipFile(zip_path, 'w') as zip_file:
            zip_file.writestr("test_file.txt", test_content)
        
        # Test unzipping
        with UnzipUtils(temp_dir) as utils:
            extracted_file_path = os.path.join(utils.temp_path, "test_file.txt")
            assert os.path.exists(extracted_file_path)
            
            # Verify content
            with open(extracted_file_path, 'rb') as f:
                content = f.read()
                assert content == test_content
    
    def test_unzip_files_handles_existing_files(self, temp_dir):
        """Test handling of existing files during unzip."""
        # Create initial zip with small file
        zip_path = os.path.join(temp_dir, "test.zip")
        small_content = b"small content"
        
        with zipfile.ZipFile(zip_path, 'w') as zip_file:
            zip_file.writestr("test_file.txt", small_content)
        
        # Unzip first time
        with UnzipUtils(temp_dir) as utils:
            extracted_file_path = os.path.join(utils.temp_path, "test_file.txt")
            assert os.path.exists(extracted_file_path)
        
        # Create new zip with larger file
        large_content = b"larger content" * 10
        with zipfile.ZipFile(zip_path, 'w') as zip_file:
            zip_file.writestr("test_file.txt", large_content)
        
        # Unzip second time - should overwrite with larger file
        with UnzipUtils(temp_dir) as utils:
            extracted_file_path = os.path.join(utils.temp_path, "test_file.txt")
            assert os.path.exists(extracted_file_path)
            
            with open(extracted_file_path, 'rb') as f:
                content = f.read()
                assert content == large_content


class TestFindFilesByMask:
    """Test find_files_by_mask functionality."""
    
    def test_find_files_by_mask_simple(self, temp_dir):
        """Test finding files with simple mask."""
        # Create test files
        test_files = ["file1.txt", "file2.txt", "file3.dat"]
        for filename in test_files:
            file_path = os.path.join(temp_dir, filename)
            with open(file_path, 'w') as f:
                f.write("test content")
        
        # Find .txt files
        result = find_files_by_mask(temp_dir, "*.txt")
        
        assert len(result) == 2
        assert any("file1.txt" in path for path in result)
        assert any("file2.txt" in path for path in result)
    
    def test_find_files_by_mask_recursive(self, temp_dir):
        """Test finding files recursively."""
        # Create nested directory structure
        subdir = os.path.join(temp_dir, "subdir")
        os.makedirs(subdir, exist_ok=True)
        
        # Create files in different locations
        files = [
            os.path.join(temp_dir, "file1.txt"),
            os.path.join(subdir, "file2.txt"),
            os.path.join(temp_dir, "file3.dat")
        ]
        
        for file_path in files:
            with open(file_path, 'w') as f:
                f.write("test content")
        
        # Find all .txt files recursively
        result = find_files_by_mask(temp_dir, "**/*.txt")
        
        # Convert to set to remove duplicates and normalize paths
        result_set = {os.path.normpath(path) for path in result}
        expected_files = {
            os.path.normpath(os.path.join(temp_dir, "file1.txt")),
            os.path.normpath(os.path.join(subdir, "file2.txt"))
        }
        
        assert result_set == expected_files
    
    def test_find_files_by_mask_no_matches(self, temp_dir):
        """Test finding files when no matches exist."""
        # Create only .dat files
        test_files = ["file1.dat", "file2.dat"]
        for filename in test_files:
            file_path = os.path.join(temp_dir, filename)
            with open(file_path, 'w') as f:
                f.write("test content")
        
        # Try to find .txt files
        result = find_files_by_mask(temp_dir, "*.txt")
        
        assert len(result) == 0
    
    def test_find_files_by_mask_empty_directory(self, temp_dir):
        """Test finding files in empty directory."""
        result = find_files_by_mask(temp_dir, "*.txt")
        assert len(result) == 0


class TestReadArchiveFile:
    """Test read_archive_file functionality."""
    
    def test_read_archive_file_single_record(self, sample_archive_file):
        """Test reading single record from archive file."""
        records = list(read_archive_file(sample_archive_file, HourStruct))
        
        assert len(records) == 10  # 10 records in sample file
        
        # Check first record
        first_record = records[0]
        assert first_record['month'] == 12
        assert first_record['day'] == 25
        assert first_record['year'] == 24
        assert first_record['hour'] == 14
        assert first_record['minutes'] == 30
        assert abs(first_record['volume'] - 1000.5) < 1e-6
        assert abs(first_record['pressure'] - 5.2) < 1e-6
        assert abs(first_record['temperature'] - 20.5) < 1e-6
        assert abs(first_record['density'] - 0.7) < 1e-6
    
    def test_read_archive_file_empty_file(self, temp_dir):
        """Test reading from empty file."""
        empty_file = os.path.join(temp_dir, "empty.bin")
        with open(empty_file, 'wb') as f:
            pass  # Create empty file
        
        records = list(read_archive_file(empty_file, HourStruct))
        assert len(records) == 0
    
    def test_read_archive_file_invalid_data(self, temp_dir):
        """Test reading file with invalid data."""
        invalid_file = os.path.join(temp_dir, "invalid.bin")
        with open(invalid_file, 'wb') as f:
            f.write(b"invalid binary data")
        
        # Should skip invalid records and continue
        records = list(read_archive_file(invalid_file, HourStruct))
        assert len(records) == 0
    
    def test_read_archive_file_partial_record(self, temp_dir):
        """Test reading file with partial record at end."""
        partial_file = os.path.join(temp_dir, "partial.bin")
        
        # Create one complete record and one partial
        import struct
        complete_record = struct.pack("=5B6f", 12, 25, 24, 14, 30, 1000.5, 0.0, 0.1, 5.2, 20.5, 0.7)
        partial_record = b"partial"
        
        with open(partial_file, 'wb') as f:
            f.write(complete_record)
            f.write(partial_record)
        
        records = list(read_archive_file(partial_file, HourStruct))
        assert len(records) == 1  # Only complete record should be read
    
    def test_read_archive_file_multiple_records(self, temp_dir):
        """Test reading multiple records from archive file."""
        multi_file = os.path.join(temp_dir, "multi.bin")
        
        import struct
        records_data = []
        for i in range(5):
            record = struct.pack("=5B6f", 12, 25, 24, i, 30, 1000.0 + i, 0.0, 0.1, 5.2, 20.5, 0.7)
            records_data.append(record)
        
        with open(multi_file, 'wb') as f:
            for record in records_data:
                f.write(record)
        
        records = list(read_archive_file(multi_file, HourStruct))
        assert len(records) == 5
        
        # Check that each record has different hour and volume
        for i, record in enumerate(records):
            assert record['hour'] == i
            assert record['volume'] == 1000.0 + i 

class TestEveryArchiveIsFound:
    """What to read is no longer guessed from the file name.

    It used to be: "Dnipropetr_2026_09_11_23.zip" was taken apart into a
    source and a snapshot time, and only the newest snapshot of each source
    was read. A source that renamed its files would have gone unread without a
    word — and every run re-read the whole history of the share to be safe.
    """

    def _touch(self, folder, name, mtime):
        path = os.path.join(folder, name)
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("x.txt", name)
        os.utime(path, (mtime, mtime))
        return path

    def test_every_zip_is_there_whatever_it_is_called(self, tmp_path):
        folder = str(tmp_path)
        wanted = {
            self._touch(folder, "Dnipropetr_2026_09_11_22.zip", 1000),
            self._touch(folder, "Dnipropetr_2026_09_11_23.zip", 2000),
            self._touch(folder, "UGV_DNP_2026_09_11_23.zip", 1500),
            self._touch(folder, "manual-export.zip", 500),
            self._touch(folder, "хай_буде_кирилиця.zip", 900),
        }
        assert set(all_zips(folder)) == wanted

    def test_the_newest_comes_first(self, tmp_path):
        """Order decides nothing about what is read — everything is, sooner or
        later — it only puts today's data before last month's."""
        folder = str(tmp_path)
        self._touch(folder, "old.zip", 1000)
        self._touch(folder, "new.zip", 3000)
        self._touch(folder, "middle.zip", 2000)
        assert [os.path.basename(z) for z in all_zips(folder)] == [
            "new.zip", "middle.zip", "old.zip"]

    def test_subfolders_count_too(self, tmp_path):
        folder = str(tmp_path)
        os.makedirs(os.path.join(folder, "Arhiv", "18.01.2026"))
        deep = self._touch(os.path.join(folder, "Arhiv", "18.01.2026"),
                           "Dnipropetr_2026_01_18_23.zip", 1000)
        assert deep in all_zips(folder)


class TestOnlyWhatWasAskedFor:
    """The poller hands over the archives it has not read; nothing else is
    touched, however much else lies in the folder."""

    def _touch(self, folder, name):
        path = os.path.join(folder, name)
        with zipfile.ZipFile(path, "w") as z:
            z.writestr(name + ".txt", name)
        return path

    def test_the_listed_archives_and_no_others(self, tmp_path):
        folder = str(tmp_path)
        asked = self._touch(folder, "new.zip")
        self._touch(folder, "read-last-week.zip")
        with UnzipUtils(folder, [asked]) as unzip:
            assert os.listdir(unzip.temp_path) == ["new.zip.txt"]

    def test_without_a_list_everything_is_read(self, tmp_path):
        """What an update asked for by hand means: read it again."""
        folder = str(tmp_path)
        self._touch(folder, "a.zip")
        self._touch(folder, "b.zip")
        with UnzipUtils(folder) as unzip:
            assert sorted(os.listdir(unzip.temp_path)) == ["a.zip.txt", "b.zip.txt"]


class TestABrokenArchiveIsSkipped:
    """One file that does not open must not cost the whole share.

    A truncated Dnipropetr_2026_01_18_23.zip, eight months old and never
    re-copied, aborted the extraction of /mnt/as4 on every run: 241 runs, 241
    failures, and the branch took in nothing for forty hours (21–23.09.2026).
    """

    def _zip(self, folder, name, mtime=1000):
        path = os.path.join(folder, name)
        with zipfile.ZipFile(path, "w") as z:
            z.writestr(name + ".txt", name)
        os.utime(path, (mtime, mtime))
        return path

    def _rubbish(self, folder, name, mtime=1000):
        path = os.path.join(folder, name)
        with open(path, "wb") as f:
            f.write(b"PK half an archive")
        os.utime(path, (mtime, mtime))
        return path

    def test_the_other_sources_are_still_read(self, tmp_path):
        folder = str(tmp_path)
        os.makedirs(os.path.join(folder, "Arhiv"))
        self._rubbish(os.path.join(folder, "Arhiv"), "Dnipropetr_2026_01_18_23.zip")
        self._zip(folder, "UGV_DNP_2026_09_23_9.zip", 3000)

        with UnzipUtils(folder) as unzip:
            files = os.listdir(unzip.temp_path)
            assert "UGV_DNP_2026_09_23_9.zip.txt" in files
            assert [os.path.basename(b) for b in unzip.broken] == [
                "Dnipropetr_2026_01_18_23.zip"]

    def test_the_rest_of_the_batch_still_arrives(self, tmp_path):
        folder = str(tmp_path)
        good = self._zip(folder, "UGV_DNP_2026_09_23_9.zip", 3000)
        bad = self._rubbish(folder, "UGV_DNP_2026_09_23_10.zip", 4000)

        with UnzipUtils(folder, [bad, good]) as unzip:
            assert os.listdir(unzip.temp_path) == ["UGV_DNP_2026_09_23_9.zip.txt"]
            assert unzip.broken == [bad]

    def test_a_folder_of_nothing_but_broken_archives_is_survived(self, tmp_path):
        folder = str(tmp_path)
        self._rubbish(folder, "Dnipropetr_2026_09_23_8.zip", 2000)
        self._rubbish(folder, "Dnipropetr_2026_09_23_9.zip", 3000)

        with UnzipUtils(folder) as unzip:
            assert os.listdir(unzip.temp_path) == []
            assert len(unzip.broken) == 2


