import io
import json
import unittest
import urllib.error
import urllib.parse
import urllib.request

import sgdb


class FakeOpener:
    """Stands in for urllib.request.urlopen: records requests, returns canned answers."""

    def __init__(self):
        self.requests = []
        self.answer = {"success": True, "data": []}
        self.error = None

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if self.error:
            raise self.error
        raw = self.answer if isinstance(self.answer, bytes) else json.dumps(self.answer).encode()
        return io.BytesIO(raw)


class Client(unittest.TestCase):
    def setUp(self):
        self.net = FakeOpener()
        self.client = sgdb.Client(" secret ", opener=self.net)

    def query(self):
        return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.net.requests[-1].full_url).query))

    def test_key_is_sent_as_bearer_and_trimmed(self):
        self.client.search("Portal")
        self.assertEqual(self.net.requests[0].get_header("Authorization"), "Bearer secret")

    def test_no_key_never_calls_the_network(self):
        with self.assertRaises(sgdb.SGDBError):
            sgdb.Client("", opener=self.net).search("x")
        self.assertEqual(self.net.requests, [])

    def test_steam_and_game_targets(self):
        self.client.images("capsule", steam_appid=2100)
        self.assertIn("/grids/steam/2100?", self.net.requests[-1].full_url)
        self.client.images("hero", game_id=55)
        self.assertIn("/heroes/game/55?", self.net.requests[-1].full_url)
        self.client.images("logo", steam_appid=1)
        self.assertNotIn("dimensions", self.query())

    def test_filters_become_api_params(self):
        self.client.images("wide", {"nsfw": False, "humor": False, "epilepsy": True, "image_type": "animated"}, 2, steam_appid=1)
        q = self.query()
        self.assertEqual((q["nsfw"], q["humor"], q["epilepsy"], q["types"], q["page"]), ("false", "false", "any", "animated", "2"))
        self.assertEqual(q["dimensions"], "460x215,920x430")
        self.client.images("capsule", {"nsfw": True, "image_type": "any"}, steam_appid=1)
        self.assertEqual((self.query()["nsfw"], self.query()["types"]), ("any", "static,animated"))

    def test_images_are_normalized(self):
        self.net.answer = {"success": True, "data": [
            {"id": 1, "url": "https://cdn2.steamgriddb.com/grid/a.png", "thumb": "https://cdn2.steamgriddb.com/thumb/a.png",
             "width": 600, "height": 900, "style": "alternate", "mime": "image/png", "author": {"name": "Ana"}},
            {"id": 2, "url": "https://cdn2.steamgriddb.com/grid/b.png", "style": ["blurred", "material"]},
            {"id": 3},  # no url: dropped
        ]}
        imgs = self.client.images("capsule", steam_appid=1)
        self.assertEqual([i["id"] for i in imgs], [1, 2])
        self.assertEqual((imgs[0]["author"], imgs[1]["thumb"], imgs[1]["style"]), ("Ana", imgs[1]["url"], "blurred, material"))
        self.assertFalse(imgs[0]["animated"])

    def test_animated_art_has_a_video_thumbnail(self):
        self.net.answer = {"success": True, "data": [
            {"id": 9, "url": "https://cdn2.steamgriddb.com/grid/a.png", "thumb": "https://cdn2.steamgriddb.com/thumb/a.webm"}]}
        self.assertTrue(self.client.images("capsule", steam_appid=1)[0]["animated"])

    def test_errors_are_clear(self):
        cases = [
            (urllib.error.HTTPError("u", 401, "x", {}, None), 401, "API key"),
            (urllib.error.HTTPError("u", 404, "x", {}, None), 404, "not found"),
            (urllib.error.HTTPError("u", 500, "x", {}, None), 500, "HTTP 500"),
            (urllib.error.URLError("no route"), 0, "can't reach"),
        ]
        for error, status, text in cases:
            self.net.error = error
            with self.subTest(status=status), self.assertRaises(sgdb.SGDBError) as cm:
                self.client.search("x")
            self.assertEqual(cm.exception.status, status)
            self.assertIn(text, str(cm.exception))
        self.net.error = None
        self.net.answer = {"success": False, "errors": ["Game not found"]}
        with self.assertRaisesRegex(sgdb.SGDBError, "Game not found"):
            self.client.search("x")
        self.net.answer = b"<html>"
        with self.assertRaisesRegex(sgdb.SGDBError, "unreadable"):
            self.client.search("x")

    def test_redirects_never_take_the_key_away_from_the_api(self):
        handler = sgdb._ApiOnly()
        req = urllib.request.Request(sgdb.API + "/search/autocomplete/x", headers={"Authorization": "Bearer k"})
        for url in ("https://evil.example/api/v2/x", "http://www.steamgriddb.com/api/v2/x",
                    "https://www.steamgriddb.com.evil.example/x"):
            with self.subTest(url=url), self.assertRaises(urllib.error.URLError):
                handler.redirect_request(req, None, 302, "Found", {}, url)
        ok = handler.redirect_request(req, None, 301, "Moved", {}, sgdb.API + "/search/autocomplete/y")
        self.assertEqual(ok.full_url, sgdb.API + "/search/autocomplete/y")

    def test_search_term_is_encoded_twice(self):
        self.client.search("Half-Life: 2/Ep")
        self.assertIn("/search/autocomplete/Half-Life%253A%25202%252FEp", self.net.requests[-1].full_url)

    def test_downloads_only_from_steamgriddb(self):
        self.assertEqual(sgdb.check_download_url("https://cdn2.steamgriddb.com/grid/a.png"), "https://cdn2.steamgriddb.com/grid/a.png")
        for url in ("http://cdn2.steamgriddb.com/a.png", "https://evil.com/a.png", "https://steamgriddb.com.evil.com/a",
                    "file:///etc/passwd", "", None):
            with self.subTest(url=url), self.assertRaises(sgdb.SGDBError):
                sgdb.check_download_url(url)


if __name__ == "__main__":
    unittest.main()
