# -*- coding: utf-8 -*-
import tempfile
from pathlib import Path
import unittest

from file_utils import create_temp_file, replace_text, write_text_with_backup


class FileUtilsTests(unittest.TestCase):
    def test_write_text_with_backup_creates_file_and_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "sample.txt"
            write_text_with_backup(target, "first\n")
            self.assertTrue(target.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "first\n")

            write_text_with_backup(target, "second\n")
            backups = list(Path(tmpdir).glob("sample.txt.*.bak"))
            self.assertEqual(target.read_text(encoding="utf-8"), "second\n")
            self.assertTrue(backups)

    def test_replace_text_uses_transform(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "config.ini"
            write_text_with_backup(target, "value=1\n")

            def transformer(original: str) -> str:
                return original.replace("1", "2")

            replace_text(target, transformer)
            self.assertEqual(target.read_text(encoding="utf-8"), "value=2\n")

    def test_create_temp_file_returns_existing_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            temp_path = create_temp_file(directory)
            self.assertTrue(temp_path.exists())


if __name__ == "__main__":
    unittest.main()
