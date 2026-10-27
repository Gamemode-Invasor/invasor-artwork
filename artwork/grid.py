"""Steam's custom artwork folder: userdata/<account>/config/grid/ (backend.py finds it
with ctx.paths.grid_dir()).

The file names are Steam's own, stable for years and used by every SteamGridDB tool:
  <appid>p.<ext>      capsule (portrait grid)
  <appid>.<ext>       wide capsule
  <appid>_hero.<ext>  hero (library header)
  <appid>_logo.<ext>  logo, positioned by <appid>.json
Steam serves them to its UI as https://steamloopback.host/customimages/<file>.

Pure stdlib, no Invasor imports: tested on its own (tests/test_grid.py).
"""
import json
import os
import tempfile


# kind -> (file suffix, Steam's eAssetType)
KINDS = {
    "capsule": ("p", 0),
    "hero": ("_hero", 1),
    "logo": ("_logo", 2),
    "wide": ("", 3),
}
EXTS = (".png", ".jpg", ".jpeg", ".webp")  # .webp: only to clean up files an older version wrote
MAX_BYTES = 100 << 20  # same hard cap as sgdb.MAX_DOWNLOAD
# What Steam writes itself when a logo is set without a position (else shortcuts show none).
DEFAULT_LOGO_POSITION = {"nVersion": 1, "logoPosition": {"pinnedPosition": "BottomLeft", "nWidthPct": 50, "nHeightPct": 50}}


def check_kind(kind):
    if kind not in KINDS:
        raise ValueError(f"unknown artwork kind {kind!r} (one of {', '.join(KINDS)})")
    return kind


def check_appid(appid):
    appid = str(appid)
    if not (appid.isascii() and appid.isdigit()) or len(appid) > 10:
        raise ValueError(f"invalid appid {appid!r}")
    return appid


def sniff(data):
    """Image type from its first bytes: ".png", ".jpg", ".webp", or None."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def files(folder, appid, kind):
    """Existing files of one kind for one app, newest first."""
    base = f"{check_appid(appid)}{KINDS[check_kind(kind)][0]}"
    found = [folder / (base + ext) for ext in EXTS if (folder / (base + ext)).is_file()]
    return sorted(found, key=lambda p: p.stat().st_mtime_ns, reverse=True)


def write(folder, appid, kind, data):
    """Store an image as the app's artwork of that kind; returns its path. Other
    extensions of the same kind are removed, so Steam never shows a stale one."""
    appid, kind = check_appid(appid), check_kind(kind)
    ext = sniff(data)
    if ext is None:
        raise ValueError("the download isn't a PNG, JPEG or WebP image")
    if len(data) > MAX_BYTES:
        raise ValueError("image too large")
    folder.mkdir(parents=True, exist_ok=True)
    # Steam only looks for .png/.jpg and reads the content, not the name: a WebP (animated
    # art comes as one) is stored as .png, as Steam's own tools do.
    target = folder / f"{appid}{KINDS[kind][0]}{'.png' if ext == '.webp' else ext}"
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".invasor-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o644)  # as Steam's own files (mkstemp makes it 0600)
        os.replace(tmp, target)
    except BaseException:
        os.unlink(tmp)
        raise
    keep_newest(folder, appid, kind)
    if kind == "logo":
        position = folder / f"{appid}.json"
        if not position.exists():
            position.write_text(json.dumps(DEFAULT_LOGO_POSITION, separators=(",", ":")))
    return target


def keep_newest(folder, appid, kind):
    """Leave a single file for the kind (e.g. after Steam itself wrote another extension)."""
    for old in files(folder, appid, kind)[1:]:
        old.unlink(missing_ok=True)


def remove(folder, appid, kind):
    """Delete the app's artwork of that kind; True if there was any."""
    found = files(folder, appid, kind)
    for f in found:
        f.unlink(missing_ok=True)
    return bool(found)


def current(folder, appid):
    """{kind: "file?v=mtime"} of the app's custom artwork (the ?v busts Steam's cache)."""
    out = {}
    for kind in KINDS:
        found = files(folder, appid, kind)
        if found:
            out[kind] = f"{found[0].name}?v={found[0].stat().st_mtime_ns // 1_000_000}"
    return out
