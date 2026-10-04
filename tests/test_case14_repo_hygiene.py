"""Case 14: the repository ships no real network's identifiers.

Org, site and topology IDs and device MACs from a real lab must never be
committed. Sample data uses placeholders only: UUIDs that start
00000000-0000-0000-0000-000000, Mist device IDs whose MAC part is a
placeholder, and MACs that start 02:00:00 (a locally administered prefix that
no vendor ships).
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", "__pycache__", ".ruff_cache", ".pytest_cache", ".venv", "state", "dist", "build"}

UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
MAC = re.compile(r"(?<![0-9a-f:-])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f:-])", re.IGNORECASE)
#: A bare MAC (12 hex digits, no separators), anywhere: JSON values, URLs, docstrings. UUID tails are
#: left to the UUID check, and a run of decimal digits (a date stamp) is not a MAC.
BARE_MAC = re.compile(r"(?<![0-9a-z_-])((?=[0-9]*[a-f])[0-9a-f]{12})(?![0-9a-z_-])", re.IGNORECASE)


def shipped_text_files():
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT)
        if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in relative.parts) or not path.is_file():
            continue
        try:
            yield relative, path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue


def is_placeholder_mac(mac: str) -> bool:
    return re.sub(r"[^0-9a-f]", "", mac.lower()).startswith("020000")


def is_placeholder_uuid(value: str) -> bool:
    value = value.lower()
    if value.startswith("00000000-0000-0000-1000-"):
        return is_placeholder_mac(value[-12:])
    return value.startswith("00000000-0000-0000-0000-000000")


class TestRepoHygiene(unittest.TestCase):
    def test_the_scan_sees_the_whole_repository(self):
        names = {str(relative) for relative, _ in shipped_text_files()}
        self.assertIn("README.md", names)
        self.assertIn("simrack/config.py", names)
        self.assertTrue(any(name.startswith("tests/fixtures/") for name in names), "the sample fabric was not scanned")

    def test_only_placeholder_uuids_ship(self):
        found = [
            f"{relative}: {value}"
            for relative, text in shipped_text_files()
            for value in UUID.findall(text)
            if not is_placeholder_uuid(value)
        ]
        self.assertEqual(found, [], "real-looking UUIDs: use 00000000-0000-0000-0000-000000xxxxxx")

    def test_only_placeholder_macs_ship(self):
        found = [
            f"{relative}: {value}"
            for relative, text in shipped_text_files()
            for value in MAC.findall(text) + BARE_MAC.findall(text)
            if not is_placeholder_mac(value)
        ]
        self.assertEqual(found, [], "real-looking MACs: use the 02:00:00 placeholder prefix")


if __name__ == "__main__":
    unittest.main()
