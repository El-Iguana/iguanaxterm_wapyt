"""
Local names for folder downloads.

A remote Linux host allows names the downloading machine may not. On Windows
`a:b.txt` cannot be created at all, and on any case-insensitive disk (Windows,
macOS by default) `report.txt` would silently overwrite `Report.txt` -- the
browser's File System Access API gives no warning of either.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "appcode"))

from services.paths import LocalNames, windows_safe_name  # noqa: E402


@pytest.mark.parametrize(
    "name, expected",
    [
        ("plain.txt", "plain.txt"),
        ("backup 17:30.tar", "backup 17_30.tar"),
        ('what?<>|*"\\.txt', "what_______.txt"),
        ("tab\there", "tab_here"),
        ("trailing dot.", "trailing dot"),
        ("trailing space ", "trailing space"),
        ("...", "_"),
        ("CON", "CON_"),
        ("con.log", "con_.log"),
        ("NUL.tar.gz", "NUL_.tar.gz"),
        ("LPT9", "LPT9_"),
        ("COM10.txt", "COM10.txt"),  # only COM1-9 are reserved
        ("CONSOLE.txt", "CONSOLE.txt"),
        ("Ámbér 🐱.jpg", "Ámbér 🐱.jpg"),
    ],
)
def test_windows_safe_name(name, expected):
    assert windows_safe_name(name) == expected


def test_linux_changes_nothing():
    names = LocalNames()
    paths = ["Photos/Report.txt", "Photos/report.txt", "a:b/c?d.txt", "x/CON.log"]
    assert [names.map(p) for p in paths] == paths
    assert names.renamed == 0


def test_case_collision_is_numbered_not_overwritten():
    names = LocalNames(case_insensitive=True)
    assert names.map("docs/Report.txt") == "docs/Report.txt"
    assert names.map("docs/report.txt") == "docs/report (2).txt"
    assert names.map("docs/REPORT.txt") == "docs/REPORT (3).txt"
    assert names.renamed == 2


def test_a_renamed_directory_is_renamed_for_every_file_in_it():
    names = LocalNames(case_insensitive=True)
    assert names.map("Photos/a.jpg") == "Photos/a.jpg"
    assert names.map("photos/b.jpg") == "photos (2)/b.jpg"
    assert names.map("photos/c.jpg") == "photos (2)/c.jpg"
    assert names.map("Photos/d.jpg") == "Photos/d.jpg"
    assert names.renamed == 1  # the directory, once


def test_files_and_directories_share_a_namespace():
    names = LocalNames(case_insensitive=True)
    assert names.map("notes") == "notes"
    assert names.map("Notes/today.txt") == "Notes (2)/today.txt"


def test_two_names_that_clean_to_the_same_are_both_kept():
    names = LocalNames(windows=True, case_insensitive=True)
    assert names.map("logs/a:b.txt") == "logs/a_b.txt"
    assert names.map("logs/a?b.txt") == "logs/a_b (2).txt"
    assert names.map("logs/a_b.txt") == "logs/a_b (3).txt"


def test_the_same_remote_file_maps_the_same_way_twice():
    names = LocalNames(windows=True, case_insensitive=True)
    first = names.map("x/a:b")
    assert names.map("x/a:b") == first
    assert names.map("/x//a:b") == first  # stray slashes are not a new name
    assert names.renamed == 1


def test_numbering_keeps_extensions_and_dotfiles_readable():
    names = LocalNames(case_insensitive=True)
    names.map(".bashrc")
    assert names.map(".BASHRC") == ".BASHRC (2)"
    names.map("archive.tar.gz")
    assert names.map("ARCHIVE.tar.gz") == "ARCHIVE (2).tar.gz"
    names.map("my.photo.jpg")
    assert names.map("MY.photo.jpg") == "MY.photo (2).jpg"


def test_same_name_in_different_directories_is_not_a_collision():
    names = LocalNames(case_insensitive=True)
    assert names.map("a/readme.md") == "a/readme.md"
    assert names.map("b/readme.md") == "b/readme.md"
    assert names.renamed == 0
