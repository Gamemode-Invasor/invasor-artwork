"""Artwork for Invasor: browse community artwork from steamgriddb.com and apply it to
Steam games and non-Steam shortcuts. Inspired by decky-steamgriddb by the SteamGridDB
team (GPL-3.0); written from scratch.

Applying = writing Steam's own files in userdata/<account>/config/grid/ (grid.py), so
it doesn't depend on Steam's JS API. Then, only if Steam offers it, its
SetCustomArtworkForApp is called too so the library refreshes at once; without it
the artwork shows after restarting Steam.
"""
import base64

from . import grid, sgdb


ctx = None


def setup(context):
    global ctx
    ctx = context


def _client():
    return sgdb.Client(ctx.settings.get("api_key"))


def _grid_dir():
    user = grid.current_user()
    if user is None:
        raise ctx.Unavailable("can't tell which Steam account is in use (userdata/)")
    return grid.grid_dir(user)


def _checked(kind=None, appid=None):
    try:
        if kind is not None:
            grid.check_kind(kind)
        if appid is not None:
            grid.check_appid(appid)
    except ValueError as e:
        raise ctx.InvalidArgument(str(e)) from None


def _sgdb(call, *args, **kwargs):
    """Run a SteamGridDB call; its errors become clean API errors."""
    try:
        return call(*args, **kwargs)
    except sgdb.SGDBError as e:
        raise ctx.Unavailable(str(e)) from None


def status():
    user = grid.current_user()
    return {
        "has_key": bool(ctx.settings.get("api_key").strip()),
        "user": user,
        "grid_dir": str(grid.grid_dir(user)) if user else None,
    }


def match(appid, name=None, shortcut=False):
    """The SteamGridDB game for an app: the one the user chose, else Steam's own entry
    (Steam games), else the first search result for its name (shortcuts). None if none."""
    _checked(appid=appid)
    chosen = ctx.game_data(appid).load().get("sgdb")
    if chosen:
        return {**chosen, "source": "chosen"}
    client = _client()
    if not shortcut:
        try:
            return {**client.game_by_steam(appid), "source": "steam"}
        except sgdb.SGDBError as e:
            if e.status != 404:
                raise ctx.Unavailable(str(e)) from None
    if name:
        found = _sgdb(client.search, name)
        if found:
            return {**found[0], "source": "search"}
    return None


def search_games(term):
    if not isinstance(term, str) or not term.strip():
        raise ctx.InvalidArgument("type something to search for")
    return _sgdb(_client().search, term[:200])


def set_match(appid, game_id, name):
    """Remember which SteamGridDB game an app is (asked again only if cleared)."""
    _checked(appid=appid)
    if not isinstance(game_id, int) or isinstance(game_id, bool) or game_id <= 0:
        raise ctx.InvalidArgument("invalid SteamGridDB game id")
    ctx.game_data(appid).update(sgdb={"id": game_id, "name": str(name)[:200]})
    return True


def clear_match(appid):
    _checked(appid=appid)
    ctx.game_data(appid).update(sgdb=None)
    return True


def assets(kind, appid=None, game_id=None, page=0):
    """One page of images of a kind: by SteamGridDB game if known, else by Steam appid."""
    _checked(kind=kind)
    if game_id is None and appid is None:
        raise ctx.InvalidArgument("need a game")
    if not isinstance(page, int) or not 0 <= page <= 100:
        raise ctx.InvalidArgument("invalid page")
    filters = ctx.settings.get()
    try:
        return _client().images(kind, filters, page, steam_appid=appid, game_id=game_id)
    except sgdb.SGDBError as e:
        if e.status == 404:
            return []
        raise ctx.Unavailable(str(e)) from None


def _refresh(call, *args, timeout=15):
    """Ask Steam to show the change now. False if Steam can't (artwork still applied)."""
    try:
        ctx.steam_call(call, *args, timeout=timeout)
        return True
    except ctx.Unavailable as e:
        ctx.log.info("no live refresh (%s): the change shows after restarting Steam", e)
        return False


def apply(appid, kind, url, allow_big=False):
    """Download an image from SteamGridDB and make it the app's artwork of that kind.
    An unusually big image isn't downloaded without the user's OK: the answer is then
    {"needs_confirm": True, "size_mb": N}, and the UI asks and calls again with allow_big."""
    _checked(kind, appid)
    try:
        data = _client().download(url, allow_big=bool(allow_big))
    except sgdb.SGDBError as e:
        if e.status == 413:
            return {"needs_confirm": True, "size_mb": round(getattr(e, "size", 0) / (1 << 20))}
        raise ctx.Unavailable(str(e)) from None
    folder = _grid_dir()
    try:
        path = grid.write(folder, appid, kind, data)
    except ValueError as e:
        raise ctx.Unavailable(str(e)) from None
    except OSError as e:
        raise ctx.Unavailable(f"can't write {folder}: {e}") from None
    # The live refresh sends the whole image to Steam (as base64): give big ones time.
    refreshed = _refresh(
        "Apps.SetCustomArtworkForApp", int(appid), base64.b64encode(data).decode(), path.suffix[1:], grid.KINDS[kind][1],
        timeout=15 + len(data) // (2 << 20),
    )
    grid.keep_newest(folder, appid, kind)  # Steam may have written its own copy
    ctx.log.info("applied %s to %s (%s)", kind, appid, "live" if refreshed else "after restart")
    return {"refreshed": refreshed}


def clear(appid, kind):
    """Go back to Steam's default artwork of that kind."""
    _checked(kind, appid)
    removed = grid.remove(_grid_dir(), appid, kind)
    refreshed = _refresh("Apps.ClearCustomArtworkForApp", int(appid), grid.KINDS[kind][1])
    return {"removed": removed, "refreshed": refreshed}


def current(appid):
    """{kind: file} of the app's custom artwork, for previews via /customimages/<file>."""
    _checked(appid=appid)
    user = grid.current_user()
    return grid.current(grid.grid_dir(user), appid) if user else {}


METHODS = {
    "status": status,
    "match": match,
    "search_games": search_games,
    "set_match": set_match,
    "clear_match": clear_match,
    "assets": assets,
    "apply": apply,
    "clear": clear,
    "current": current,
}
