"""Minimal SteamGridDB API v2 client (stdlib urllib only).

Each user brings their own API key (steamgriddb.com -> Preferences -> API): the key
built into the Decky plugin is reserved for it. The key is sent only to
www.steamgriddb.com and never logged.

The images themselves are downloaded by backend.py with ctx.http (check_download_url first).
Pure stdlib, no Invasor imports: tested on its own with a fake opener (tests/test_sgdb.py).
"""
import json
import urllib.error
import urllib.parse
import urllib.request

API_ORIGIN = "https://www.steamgriddb.com"
API = API_ORIGIN + "/api/v2"
USER_AGENT = "invasor-artwork"
TIMEOUT = 15
# Usual artwork is well under SOFT_LIMIT; bigger (animated art can be 20+ MB) needs the
# user's OK; nothing over MAX_DOWNLOAD (ctx.http's limit for a file the user chose) is downloaded.
SOFT_LIMIT = 20 << 20
MAX_DOWNLOAD = 100 << 20
# Downloads are only ever fetched from SteamGridDB's own hosts.
DOWNLOAD_HOSTS = ("steamgriddb.com",)

# Our artwork kinds -> API resource.
RESOURCE = {"capsule": "grids", "wide": "grids", "hero": "heroes", "logo": "logos"}

# Defaults of the original plugin (all styles; each kind's usual dimensions and formats).
_GRID_STYLES = ["alternate", "white_logo", "no_logo", "blurred", "material"]
STYLES = {
    "capsule": _GRID_STYLES,
    "wide": _GRID_STYLES,
    "hero": ["alternate", "blurred", "material"],
    "logo": ["official", "white", "black", "custom"],
}
DIMENSIONS = {
    "capsule": ["600x900", "342x482", "660x930"],
    "wide": ["460x215", "920x430"],
    "hero": ["1920x620", "3840x1240", "1600x650"],
    "logo": [],
}
MIMES = {
    "capsule": ["image/png", "image/jpeg", "image/webp"],
    "wide": ["image/png", "image/jpeg", "image/webp"],
    "hero": ["image/png", "image/jpeg", "image/webp"],
    "logo": ["image/png", "image/webp"],
}
IMAGE_TYPES = {"static": "static", "animated": "animated", "any": "static,animated"}


class SGDBError(Exception):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status  # HTTP status, 0 = no answer (network)


def params(kind, filters, page=0):
    """Query string values for an image search, from the module's filter settings."""
    filters = filters or {}
    out = {
        "page": str(int(page)),
        "styles": ",".join(STYLES[kind]),
        "mimes": ",".join(MIMES[kind]),
        "types": IMAGE_TYPES.get(filters.get("image_type"), IMAGE_TYPES["any"]),
        "nsfw": "any" if filters.get("nsfw") else "false",
        "humor": "any" if filters.get("humor", True) else "false",
        "epilepsy": "any" if filters.get("epilepsy", True) else "false",
    }
    if DIMENSIONS[kind]:
        out["dimensions"] = ",".join(DIMENSIONS[kind])
    return out


def _image(raw):
    style = raw.get("style")
    thumb = raw.get("thumb") or raw.get("url")
    return {
        "id": raw.get("id"),
        "url": raw.get("url"),
        "thumb": thumb,
        # Animated art has a video thumbnail (.webm); the image applied is still the url.
        "animated": urllib.parse.urlparse(thumb or "").path.lower().endswith((".webm", ".mp4")),
        "width": raw.get("width"),
        "height": raw.get("height"),
        "style": ", ".join(style) if isinstance(style, list) else (style or ""),
        "mime": raw.get("mime"),
        "nsfw": bool(raw.get("nsfw")),
        "humor": bool(raw.get("humor")),
        "author": (raw.get("author") or {}).get("name"),
    }


def _game(raw):
    return {"id": raw.get("id"), "name": raw.get("name"), "verified": bool(raw.get("verified"))}


class _ApiOnly(urllib.request.HTTPRedirectHandler):
    """urllib copies every header to a redirect, Authorization too: the key stays on the API's host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith(API_ORIGIN + "/"):
            raise urllib.error.URLError(f"redirected away from SteamGridDB's API: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Client:
    def __init__(self, key, opener=urllib.request.build_opener(_ApiOnly).open):
        self.key = (key or "").strip()
        self._open = opener

    def _get(self, path, query=None):
        if not self.key:
            raise SGDBError("no SteamGridDB API key: add yours in Settings", 401)
        url = API + path + ("?" + urllib.parse.urlencode(query) if query else "")
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.key}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        })
        try:
            with self._open(req, timeout=TIMEOUT) as res:
                body = json.loads(res.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise SGDBError("SteamGridDB rejected the API key: check it in Settings", e.code) from None
            if e.code == 404:
                raise SGDBError("not found on SteamGridDB", 404) from None
            raise SGDBError(f"SteamGridDB answered HTTP {e.code}", e.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise SGDBError(f"can't reach SteamGridDB ({getattr(e, 'reason', e)})") from None
        except ValueError:
            raise SGDBError("SteamGridDB sent an unreadable answer") from None
        if not isinstance(body, dict) or not body.get("success"):
            errors = body.get("errors") if isinstance(body, dict) else None
            raise SGDBError("SteamGridDB: " + (", ".join(map(str, errors)) if errors else "request failed"))
        return body.get("data")

    def search(self, term):
        # Encoded twice, as the API expects for terms with symbols (same as the original plugin).
        term = urllib.parse.quote(urllib.parse.quote(term.strip(), safe=""), safe="")
        return [_game(g) for g in self._get(f"/search/autocomplete/{term}") or []]

    def game_by_steam(self, appid):
        return _game(self._get(f"/games/steam/{int(appid)}") or {})

    def images(self, kind, filters=None, page=0, steam_appid=None, game_id=None):
        """One page of images for a Steam app (steam_appid) or a SteamGridDB game (game_id)."""
        target = f"game/{int(game_id)}" if game_id is not None else f"steam/{int(steam_appid)}"
        data = self._get(f"/{RESOURCE[kind]}/{target}", params(kind, filters, page))
        return [_image(i) for i in data or [] if isinstance(i, dict) and i.get("url")]


def check_download_url(url):
    """Images are only downloaded from SteamGridDB's own hosts, over https; else SGDBError."""
    parts = urllib.parse.urlparse(url or "")
    host = parts.hostname or ""
    if parts.scheme != "https" or not any(host == h or host.endswith("." + h) for h in DOWNLOAD_HOSTS):
        raise SGDBError(f"refusing to download from {host or url!r}: not SteamGridDB")
    return url
