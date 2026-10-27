import json
import os
import tempfile
import unittest
from pathlib import Path

import grid

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16
JPG = b"\xff\xd8\xff\xe0" + b"\0" * 16
WEBP = b"RIFF\0\0\0\0WEBPVP8 " + b"\0" * 8


class Files(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.folder = Path(d.name) / "grid"

    def test_names_follow_steam(self):
        self.assertEqual(grid.write(self.folder, "2100", "capsule", PNG).name, "2100p.png")
        self.assertEqual(grid.write(self.folder, "2100", "wide", JPG).name, "2100.jpg")
        self.assertEqual(grid.write(self.folder, "2100", "hero", WEBP).name, "2100_hero.png")
        self.assertEqual(grid.write(self.folder, "2100", "logo", PNG).name, "2100_logo.png")
        self.assertEqual(set(grid.current(self.folder, "2100")), {"capsule", "wide", "hero", "logo"})

    def test_written_like_steams_own_files(self):
        path = grid.write(self.folder, "5", "capsule", PNG)
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        self.assertEqual([p.name for p in self.folder.iterdir()], ["5p.png"])  # no temporary leftovers

    def test_one_file_per_kind(self):
        grid.write(self.folder, "7", "capsule", PNG)
        grid.write(self.folder, "7", "capsule", JPG)
        self.assertEqual([p.name for p in grid.files(self.folder, "7", "capsule")], ["7p.jpg"])
        self.assertTrue(grid.current(self.folder, "7")["capsule"].startswith("7p.jpg?v="))

    def test_keep_newest_after_steam_writes_its_own_copy(self):
        grid.write(self.folder, "7", "hero", JPG)
        steam_copy = self.folder / "7_hero.png"
        steam_copy.write_bytes(PNG)
        os.utime(steam_copy, ns=(10**19, 10**19))
        grid.keep_newest(self.folder, "7", "hero")
        self.assertEqual([p.name for p in grid.files(self.folder, "7", "hero")], ["7_hero.png"])

    def test_logo_gets_a_default_position_once(self):
        grid.write(self.folder, "9", "logo", PNG)
        pos = self.folder / "9.json"
        self.assertEqual(json.loads(pos.read_text())["logoPosition"]["pinnedPosition"], "BottomLeft")
        pos.write_text('{"custom": true}')
        grid.write(self.folder, "9", "logo", PNG)
        self.assertEqual(pos.read_text(), '{"custom": true}')  # the user's position is kept

    def test_remove(self):
        grid.write(self.folder, "5", "wide", PNG)
        self.assertTrue(grid.remove(self.folder, "5", "wide"))
        self.assertFalse(grid.remove(self.folder, "5", "wide"))
        self.assertEqual(grid.current(self.folder, "5"), {})

    def test_rejects_non_images_bad_kinds_and_appids(self):
        with self.assertRaises(ValueError):
            grid.write(self.folder, "5", "capsule", b"<html>nope</html>")
        for appid in ("../5", "5a", "", "12345678901", "²"):
            with self.subTest(appid=appid), self.assertRaises(ValueError):
                grid.write(self.folder, appid, "capsule", PNG)
        with self.assertRaises(ValueError):
            grid.write(self.folder, "5", "icon", PNG)
        self.assertFalse(any(self.folder.glob("*")) if self.folder.exists() else False)


if __name__ == "__main__":
    unittest.main()
