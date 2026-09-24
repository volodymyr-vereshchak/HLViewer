"""Finding the EIC-coded folders a data path holds.

The scan opens every archive on the path and looks at the folder names inside
them. It is the one screen that tells an administrator which metering points a
share actually carries, so what it must not do is miss one — which is why it
reads everything rather than sampling the newest files. On the local set of
141 archives the oldest one still held a code no newer archive had.
"""
import os
import zipfile

from backend.api.endpoints.lumg_ep import _folders_in, _scan_for_eis


def _archive(folder, name, entries):
    path = os.path.join(folder, name)
    with zipfile.ZipFile(path, "w") as archive:
        for entry in entries:
            archive.writestr(entry, b"x")
    return path


class TestWhatCountsAsACode:
    def test_a_coded_folder_is_found_whatever_it_is_nested_in(self, tmp_path):
        folder = str(tmp_path)
        _archive(folder, "Dnipropetr_2026_09_11_23.zip",
                 ["Dnipropetr/56ZOPDNP40031011/S086R1A.426",
                  "Dnipropetr/56ZOPDNP40031029/S086R1A.426"])

        assert _scan_for_eis(folder) == ["56ZOPDNP40031011", "56ZOPDNP40031029"]

    def test_ordinary_folder_names_are_not_codes(self, tmp_path):
        """A code is six or more capitals and digits; the source's own folder
        and anything lower-case is not one."""
        folder = str(tmp_path)
        _archive(folder, "a.zip", ["Dnipropetr/hourly/x.426",
                                   "ZP/2026/56ZOPDNP40031011/x.426"])

        assert _scan_for_eis(folder) == ["56ZOPDNP40031011"]

    def test_a_folder_entry_of_its_own_counts(self, tmp_path):
        """An archive may hold the directory as an entry ending in "/" and
        nothing under it — the point is still on the share."""
        folder = str(tmp_path)
        _archive(folder, "a.zip", ["Dnipropetr/56ZOPDNP40031011/"])

        assert _scan_for_eis(folder) == ["56ZOPDNP40031011"]


class TestWhatItReads:
    def test_every_archive_on_the_path_including_old_ones(self, tmp_path):
        """Not only the newest: a point that stopped reporting in January is
        still a point the administrator is looking for."""
        folder = str(tmp_path)
        os.makedirs(os.path.join(folder, "Arhiv", "18.01.2026"))
        _archive(folder, "today.zip", ["D/56ZOPDNP40031011/x.426"])
        _archive(os.path.join(folder, "Arhiv", "18.01.2026"), "old.zip",
                 ["D/56ZOPDNP40031029/x.426"])

        assert _scan_for_eis(folder) == ["56ZOPDNP40031011", "56ZOPDNP40031029"]

    def test_the_same_code_in_every_snapshot_is_reported_once(self, tmp_path):
        folder = str(tmp_path)
        for hour in range(5):
            _archive(folder, f"Dnipropetr_2026_09_11_{hour}.zip",
                     ["Dnipropetr/56ZOPDNP40031011/x.426"])

        assert _scan_for_eis(folder) == ["56ZOPDNP40031011"]

    def test_an_empty_path_gives_nothing_rather_than_failing(self, tmp_path):
        assert _scan_for_eis(str(tmp_path)) == []


def test_one_archive_gives_its_folders_and_not_its_files(tmp_path):
    path = _archive(str(tmp_path), "a.zip",
                    ["Dnipropetr/56ZOPDNP40031011/S086R1A.426"])
    assert _folders_in(path) == {"Dnipropetr", "56ZOPDNP40031011"}
