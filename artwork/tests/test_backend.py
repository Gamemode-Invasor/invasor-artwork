import importlib.util
import logging
import sys
import tempfile
import unittest
from pathlib import Path

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


class FakeCtx:
    InvalidArgument = InvalidArgument
    Unavailable = Unavailable

    def __init__(self):
        self.values = {"api_key": "k", "nsfw": False, "humor": True, "epilepsy": True, "image_type": "any"}
        self.stores = {}
        self.steam_calls = []
        self.steam_ok = True
        self.log = logging.getLogger("test.sgdb")
        self.settings = self

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
    downloads = {}

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

    def download(self, url, allow_big=False):
        data = self.downloads.get(url, PNG)
        if len(data) > self.sgdb.SOFT_LIMIT and not allow_big:
            err = self.sgdb.SGDBError("big", 413)
            err.size = len(data)
            raise err
        return data


class Backend(unittest.TestCase):
    def setUp(self):
        self.b = load_backend()
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.folder = Path(d.name) / "grid"
        self.b.grid.current_user = lambda root=None: "1000"
        self.b.grid.grid_dir = lambda user, root=None: self.folder
        FakeClient.sgdb = self.b.sgdb
        self.b.sgdb.Client = FakeClient
        self.ctx = FakeCtx()
        self.b.setup(self.ctx)

    def test_apply_writes_steams_file_and_refreshes(self):
        self.assertEqual(self.b.apply("2100", "capsule", "https://cdn2.steamgriddb.com/a.png"), {"refreshed": True})
        self.assertTrue((self.folder / "2100p.png").exists())
        path, args = self.ctx.steam_calls[0]
        self.assertEqual((path, args[0], args[2], args[3]), ("Apps.SetCustomArtworkForApp", 2100, "png", 0))

    def test_apply_without_steam_still_applies(self):
        self.ctx.steam_ok = False
        with self.assertLogs("test.sgdb", "INFO"):
            self.assertEqual(self.b.apply("2100", "hero", "https://cdn2.steamgriddb.com/a.png"), {"refreshed": False})
        self.assertTrue((self.folder / "2100_hero.png").exists())

    def test_apply_rejects_non_images_and_bad_input(self):
        FakeClient.downloads = {"https://cdn2.steamgriddb.com/page.html": b"<html>"}
        with self.assertRaises(Unavailable):
            self.b.apply("2100", "capsule", "https://cdn2.steamgriddb.com/page.html")
        for appid, kind in (("../1", "capsule"), ("1", "icon")):
            with self.subTest(appid=appid, kind=kind), self.assertRaises(InvalidArgument):
                self.b.apply(appid, kind, "https://cdn2.steamgriddb.com/a.png")
        self.assertEqual(self.ctx.steam_calls, [])

    def test_big_images_need_the_users_ok(self):
        big = "https://cdn2.steamgriddb.com/big.png"
        FakeClient.downloads = {big: PNG + b"\0" * (self.b.sgdb.SOFT_LIMIT + (4 << 20))}
        self.assertEqual(self.b.apply("2100", "hero", big), {"needs_confirm": True, "size_mb": 24})
        self.assertFalse((self.folder / "2100_hero.png").exists())
        self.assertEqual(self.b.apply("2100", "hero", big, allow_big=True), {"refreshed": True})
        self.assertTrue((self.folder / "2100_hero.png").exists())
        self.assertGreater(self.ctx.last_timeout, 15)  # more time to send a big image to Steam
        FakeClient.downloads = {}

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

    def test_assets_by_game_or_steam_appid(self):
        self.assertEqual(self.b.assets("wide", appid="2100")[0]["steam"], "2100")
        self.assertEqual(self.b.assets("wide", game_id=22, page=3)[0]["game"], 22)
        for bad in ({"kind": "icon", "appid": "1"}, {"kind": "hero"}, {"kind": "hero", "appid": "1", "page": -1}):
            with self.subTest(args=bad), self.assertRaises(InvalidArgument):
                self.b.assets(**bad)

    def test_status(self):
        self.assertEqual(self.b.status(), {"has_key": True, "user": "1000", "grid_dir": str(self.folder)})
        self.ctx.values["api_key"] = "  "
        self.assertFalse(self.b.status()["has_key"])


if __name__ == "__main__":
    unittest.main()
