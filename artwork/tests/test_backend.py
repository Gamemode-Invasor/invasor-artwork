import importlib.util
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16


def load_backend():
    """Load backend.py as a package, as Invasor does (it uses relative imports)."""
    name = "sgdb_backend_under_test"
    for n in [n for n in sys.modules if n == name or n.startswith(name + ".")]:
        del sys.modules[n]
    spec = importlib.util.spec_from_file_location(name, HERE / "backend.py", submodule_search_locations=[str(HERE)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class InvalidArgument(ValueError):
    pass


class Unavailable(RuntimeError):
    pass


class Store:
    def __init__(self):
        self.data = {}

    def load(self):
        return dict(self.data)

    def update(self, **kw):
        self.data.update(kw)
        return self.data


class FakePaths:
    """ctx.paths: the account's grid folder, or Unavailable (no Steam account)."""

    def __init__(self, folder):
        self.folder, self.ok = folder, True

    def grid_dir(self):
        if not self.ok:
            raise Unavailable("couldn't tell which Steam account is in use")
        return self.folder


class FakeHttp:
    """ctx.http.download: writes the canned bytes for url at dest, within max_bytes."""

    def __init__(self):
        self.downloads, self.calls = {}, []

    def download(self, url, dest, *, user_chose=False, max_bytes=None, **_):
        self.calls.append({"url": url, "user_chose": user_chose, "max_bytes": max_bytes})
        data = self.downloads.get(url, PNG)
        if max_bytes is not None and len(data) > max_bytes:
            raise InvalidArgument("download too large")  # as the core says it
        Path(dest).write_bytes(data)
        return {"path": str(dest), "size": len(data)}


class FakeCtx:
    InvalidArgument = InvalidArgument
    Unavailable = Unavailable

    def __init__(self, folder):
        self.values = {"api_key": "k", "nsfw": False, "humor": True, "epilepsy": True, "image_type": "any"}
        self.stores = {}
        self.steam_calls = []
        self.steam_ok = True
        self.log = logging.getLogger("test.sgdb")
        self.settings = self
        self.paths = FakePaths(folder)
        self.http = FakeHttp()

    def get(self, key=None):
        return dict(self.values) if key is None else self.values[key]

    def game_data(self, appid):
        return self.stores.setdefault(str(appid), Store())

    def steam_call(self, path, *args, timeout=15):
        self.steam_calls.append((path, args))
        self.last_timeout = timeout
        if not self.steam_ok:
            raise Unavailable("SteamClient.x isn't available")


class FakeClient:
    games = {"2100": {"id": 11, "name": "Dark Messiah", "verified": True}}
    found = [{"id": 22, "name": "Some Shortcut", "verified": False}]

    def __init__(self, key):
        self.key = key

    def game_by_steam(self, appid):
        if str(appid) not in self.games:
            raise self.sgdb.SGDBError("not found on SteamGridDB", 404)
        return self.games[str(appid)]

    def search(self, term):
        return self.found

    def images(self, kind, filters, page, steam_appid=None, game_id=None):
        return [{"id": 1, "kind": kind, "steam": steam_appid, "game": game_id, "page": page}]


class Backend(unittest.TestCase):
    def setUp(self):
        self.b = load_backend()
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.folder = Path(d.name) / "userdata" / "1000" / "config" / "grid"
        FakeClient.sgdb = self.b.sgdb
        self.b.sgdb.Client = FakeClient
        self.ctx = FakeCtx(self.folder)
        self.b.setup(self.ctx)

    def test_apply_writes_steams_file_and_refreshes(self):
        self.assertEqual(self.b.apply("2100", "capsule", "https://cdn2.steamgriddb.com/a.png"), {"refreshed": True})
        self.assertTrue((self.folder / "2100p.png").exists())
        path, args = self.ctx.steam_calls[0]
        self.assertEqual((path, args[0], args[2], args[3]), ("Apps.SetCustomArtworkForApp", 2100, "png", 0))
        call = self.ctx.http.calls[0]
        self.assertEqual((call["user_chose"], call["max_bytes"]), (True, self.b.sgdb.SOFT_LIMIT))
        self.assertEqual([p.name for p in self.folder.iterdir()], ["2100p.png"])  # no temporary leftovers

    def test_apply_without_steam_still_applies(self):
        self.ctx.steam_ok = False
        with self.assertLogs("test.sgdb", "INFO"):
            self.assertEqual(self.b.apply("2100", "hero", "https://cdn2.steamgriddb.com/a.png"), {"refreshed": False})
        self.assertTrue((self.folder / "2100_hero.png").exists())

    def test_apply_rejects_non_images_and_bad_input(self):
        self.ctx.http.downloads = {"https://cdn2.steamgriddb.com/page.html": b"<html>"}
        with self.assertRaises(Unavailable):
            self.b.apply("2100", "capsule", "https://cdn2.steamgriddb.com/page.html")
        for appid, kind in (("../1", "capsule"), ("1", "icon")):
            with self.subTest(appid=appid, kind=kind), self.assertRaises(InvalidArgument):
                self.b.apply(appid, kind, "https://cdn2.steamgriddb.com/a.png")
        self.assertEqual(self.ctx.steam_calls, [])

    def test_big_images_need_the_users_ok(self):
        big = "https://cdn2.steamgriddb.com/big.png"
        self.ctx.http.downloads = {big: PNG + b"\0" * (self.b.sgdb.SOFT_LIMIT + (4 << 20))}
        self.assertEqual(self.b.apply("2100", "hero", big), {"needs_confirm": True, "over_mb": 20})
        self.assertFalse((self.folder / "2100_hero.png").exists())
        self.assertEqual(self.b.apply("2100", "hero", big, allow_big=True), {"refreshed": True})
        self.assertEqual(self.ctx.http.calls[-1]["max_bytes"], self.b.sgdb.MAX_DOWNLOAD)
        self.assertTrue((self.folder / "2100_hero.png").exists())
        self.assertGreater(self.ctx.last_timeout, 15)  # more time to send a big image to Steam
        self.ctx.http.downloads[big] = PNG + b"\0" * self.b.sgdb.MAX_DOWNLOAD  # over the hard cap: never
        with self.assertRaises(InvalidArgument):
            self.b.apply("2100", "hero", big, allow_big=True)

    def test_only_steamgriddb_images_are_downloaded(self):
        for url in ("https://evil.com/a.png", "http://cdn2.steamgriddb.com/a.png"):
            with self.subTest(url=url), self.assertRaises(InvalidArgument):
                self.b.apply("2100", "capsule", url)
        self.assertEqual(self.ctx.http.calls, [])

    def test_no_steam_account(self):
        self.ctx.paths.ok = False
        self.assertEqual(self.b.status()["user"], None)
        self.assertEqual(self.b.current("2100"), {})
        for call in (lambda: self.b.apply("2100", "capsule", "https://cdn2.steamgriddb.com/a.png"),
                     lambda: self.b.clear("2100", "capsule")):
            with self.assertRaises(Unavailable):
                call()
        self.assertEqual(self.ctx.http.calls, [])

    def test_clear(self):
        self.b.apply("2100", "logo", "https://cdn2.steamgriddb.com/a.png")
        self.assertEqual(self.b.clear("2100", "logo"), {"removed": True, "refreshed": True})
        self.assertEqual(self.b.current("2100"), {})
        self.assertEqual(self.ctx.steam_calls[-1], ("Apps.ClearCustomArtworkForApp", (2100, 2)))

    def test_match_steam_shortcut_and_chosen(self):
        self.assertEqual(self.b.match("2100")["source"], "steam")
        self.assertEqual(self.b.match("3000000001", "Some Shortcut", True), {**FakeClient.found[0], "source": "search"})
        self.assertEqual(self.b.match("999", "Unknown Steam Game")["source"], "search")  # not on SGDB as Steam: by name
        self.b.set_match("3000000001", 33, "The Right One")
        self.assertEqual(self.b.match("3000000001", "Some Shortcut", True), {"id": 33, "name": "The Right One", "source": "chosen"})
        self.b.clear_match("3000000001")
        self.assertEqual(self.b.match("3000000001", "Some Shortcut", True)["source"], "search")

    def test_not_on_steamgriddb_and_other_errors(self):
        def fail(status):
            def call(*args, **kwargs):
                raise self.b.sgdb.SGDBError("boom", status)
            return call

        with mock.patch.object(FakeClient, "images", fail(404)):
            self.assertEqual(self.b.assets("hero", appid="2100"), [])
        with mock.patch.object(FakeClient, "images", fail(500)), self.assertRaises(Unavailable):
            self.b.assets("hero", appid="2100")
        with mock.patch.object(FakeClient, "game_by_steam", fail(502)), self.assertRaises(Unavailable):
            self.b.match("2100", "Dark Messiah")

    def test_assets_by_game_or_steam_appid(self):
        self.assertEqual(self.b.assets("wide", appid="2100")[0]["steam"], "2100")
        self.assertEqual(self.b.assets("wide", game_id=22, page=3)[0]["game"], 22)
        for bad in ({"kind": "icon", "appid": "1"}, {"kind": "hero"}, {"kind": "hero", "appid": "1", "page": -1}):
            with self.subTest(args=bad), self.assertRaises(InvalidArgument):
                self.b.assets(**bad)

    def test_status(self):
        self.assertEqual(self.b.status(), {"has_key": True, "user": "1000"})
        self.ctx.values["api_key"] = "  "
        self.assertFalse(self.b.status()["has_key"])


if __name__ == "__main__":
    unittest.main()
