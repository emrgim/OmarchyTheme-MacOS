#!/usr/bin/env python3
"""OmarchyMac - the TV side of the Omarchy Link protocol (docs/PROTOCOL.md),
running on macOS so that SuperOMI's `omarchy-link` can push its theme here
exactly as it does to an OmarchyTV.

This serves the Link endpoints a theme sync needs (info, pairing via SPAKE2,
status, wallpaper, theme) with the same OTV1 request/response signing, then
applies the received palette/accent/background to macOS appearance.

Stdlib only for the server; macOS is driven through `defaults`, `osascript`
and `dns-sd`. Importable by the rumps menu-bar app, or run headless for tests.
"""
from __future__ import annotations

import base64
import colorsys
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import otv_crypto as crypto  # noqa: E402

JSON_MAX = 64 * 1024
JPEG_MAX = 8 * 1024 * 1024
SKEW = 120
PORT = 47800
# Tailscale's own ranges, accepted like the LAN so a paired PC can push over the
# tailnet (same policy OmarchyTV uses when its Tailscale switch is on).
TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
# Capabilities we honour. We deliberately do NOT advertise "enc-a256gcm", so the
# PC stays on signed plaintext (no python-cryptography needed on macOS stock py3).
CAPS = ["theme", "wallpaper"]

STATE_DIR = Path(os.environ.get("OMARCHYMAC_HOME") or (Path.home() / ".config" / "omarchy-mac-link"))
LOG_FILE = STATE_DIR / "omarchymac.log"
HEX6 = re.compile(r"^#[0-9a-fA-F]{6}$")

# All colours.toml keys the TV receives (theme.py PALETTE_KEYS), kept for app-level theming.
PALETTE_KEYS = [
    "background", "dark_background", "darker_background", "lighter_background",
    "foreground", "dark_foreground", "light_foreground", "bright_foreground",
    "accent", "selection", "muted",
    "red", "yellow", "orange", "green", "cyan", "blue", "magenta",
    "bright_red", "bright_yellow", "bright_green", "bright_cyan", "bright_blue", "bright_magenta",
]


def log(msg: str) -> None:
    line = f"{time.strftime('%F %T%z')} {msg}"
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


# ----------------------------------------------------------- macOS apply layer

def _hex_rgb(h: str):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


# macOS AppleAccentColor enum. Graphite handled separately (-1 + AquaVariant 6).
ACCENT_ENUM = {"red": 0, "orange": 1, "yellow": 2, "green": 3, "blue": 4, "purple": 5, "pink": 6}


def accent_bucket(hex_color: str):
    """Map an arbitrary accent hex to the nearest macOS system-accent swatch.

    macOS exposes only 8 fixed accents (the AppleAccentColor enum + graphite),
    so this snaps by hue, with a graphite fallback for near-greys. Returns
    (name, AppleAccentColor int, AppleAquaColorVariant int).
    """
    r, g, b = (c / 255.0 for c in _hex_rgb(hex_color))
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    deg = h * 360.0
    if s < 0.14 or v < 0.08:
        return "graphite", -1, 6
    if deg < 15 or deg >= 345:
        name = "red"
    elif deg < 45:
        name = "orange"
    elif deg < 70:
        name = "yellow"
    elif deg < 170:
        name = "green"
    elif deg < 255:
        name = "blue"
    elif deg < 310:
        name = "purple"
    else:
        name = "pink"
    return name, ACCENT_ENUM[name], 1


def _run(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            log(f"cmd failed ({p.returncode}): {' '.join(cmd)} :: {p.stderr.strip()[:200]}")
        return p.returncode == 0
    except (OSError, subprocess.SubprocessError) as e:
        log(f"cmd error: {' '.join(cmd)} :: {e}")
        return False


def _osa(script, timeout=15):
    return _run(["osascript", "-e", script], timeout=timeout)


def apply_mode(mode: str) -> None:
    dark = "true" if mode == "dark" else "false"
    _osa(f'tell application "System Events" to tell appearance preferences to set dark mode to {dark}')


def apply_accent(accent_hex: str) -> dict:
    """Highlight colour gets the EXACT accent (arbitrary RGB is allowed there);
    the system accent snaps to the nearest of macOS's fixed swatches."""
    r, g, b = _hex_rgb(accent_hex)
    name, enum, aqua = accent_bucket(accent_hex)
    # exact highlight (text selection) colour
    hi = f"{r/255.0:.6f} {g/255.0:.6f} {b/255.0:.6f} Other"
    _run(["defaults", "write", "-g", "AppleHighlightColor", "-string", hi])
    # nearest system accent swatch
    if enum < 0:
        _run(["defaults", "write", "-g", "AppleAccentColor", "-int", "-1"])
    else:
        _run(["defaults", "write", "-g", "AppleAccentColor", "-int", str(enum)])
    _run(["defaults", "write", "-g", "AppleAquaColorVariant", "-int", str(aqua)])
    # nudge apps to re-read colour prefs (not all honour it until relaunch/login)
    try:
        from Foundation import NSDistributedNotificationCenter  # type: ignore
        c = NSDistributedNotificationCenter.defaultCenter()
        for n in ("AppleColorPreferencesChangedNotification", "AppleAquaColorVariantChanged"):
            c.postNotificationName_object_(n, None)
    except Exception:
        pass
    return {"accent_name": name, "accent_enum": enum, "aqua": aqua, "highlight": hi}


def apply_wallpaper(path: Path) -> None:
    p = str(path)
    _osa(f'tell application "System Events" to tell every desktop to set picture to "{p}"')


# Standard macOS home folders to recolour (the "default" folders). Only those
# that exist are touched. ~/Desktop, Documents and Downloads are TCC-guarded and
# may raise a one-time macOS privacy prompt.
DEFAULT_FOLDERS = ["Desktop", "Documents", "Downloads", "Movies", "Music", "Pictures", "Public"]
FOLDER_BASE_HUE_DEG = 212.0  # the hue of the stock blue macOS folder


def tint_folders(accent_hex: str, folders=None) -> dict:
    """Recolour the default folder icons to the theme accent by hue-rotating the
    stock folder icon (CoreImage), then setting it via NSWorkspace. Best effort."""
    import math
    result = {"tinted": [], "failed": []}
    try:
        from AppKit import NSWorkspace, NSImage, NSBitmapImageRep, NSMakeSize
        from Quartz import CIImage, CIFilter, CIContext
    except Exception as e:  # not in a PyObjC process
        log(f"tint_folders: AppKit/Quartz unavailable: {e}")
        return result
    r, g, b = (c / 255.0 for c in _hex_rgb(accent_hex))
    import colorsys as _cs
    h, s, v = _cs.rgb_to_hsv(r, g, b)
    accent_deg = h * 360.0
    angle = math.radians(accent_deg - FOLDER_BASE_HUE_DEG)
    try:
        ws = NSWorkspace.sharedWorkspace()
        base = ws.iconForFileType_("public.folder")
        base.setSize_(NSMakeSize(512, 512))
        tiff = base.TIFFRepresentation()
        rep = NSBitmapImageRep.imageRepWithData_(tiff)
        ci = CIImage.alloc().initWithBitmapImageRep_(rep)
        hue = CIFilter.filterWithName_("CIHueAdjust")
        hue.setValue_forKey_(ci, "inputImage")
        hue.setValue_forKey_(angle, "inputAngle")
        out = hue.valueForKey_("outputImage")
        if s < 0.18:  # near-grey accent: desaturate the folder instead of rotating
            ctl = CIFilter.filterWithName_("CIColorControls")
            ctl.setValue_forKey_(out, "inputImage")
            ctl.setValue_forKey_(0.0, "inputSaturation")
            out = ctl.valueForKey_("outputImage")
        ctx = CIContext.contextWithOptions_(None)
        ext = out.extent()
        cg = ctx.createCGImage_fromRect_(out, ext)
        img = NSImage.alloc().initWithCGImage_size_(cg, ext.size)
    except Exception as e:
        log(f"tint_folders: could not build icon: {e}")
        return result
    home = Path.home()
    for name in (folders or DEFAULT_FOLDERS):
        p = home / name
        if not p.is_dir():
            continue
        try:
            ok = ws.setIcon_forFile_options_(img, str(p), 0)
            (result["tinted"] if ok else result["failed"]).append(name)
        except Exception as e:
            log(f"tint_folders: {name}: {e}")
            result["failed"].append(name)
    log(f"tint_folders accent={accent_hex} -> tinted {result['tinted']} failed {result['failed']}")
    return result


def apply_theme(payload: dict, wallpaper_path: Path | None = None) -> dict:
    """Apply mode + accent + folder tint. Wallpaper is handled separately by
    MacTV.apply_effective_wallpaper() so the wallpaper offset can shift it."""
    mode = payload.get("mode", "dark")
    accent = payload.get("accent", "#7aa2f7")
    applied = {"mode": mode, "accent": accent}
    try:
        apply_mode(mode)
    except Exception as e:
        log(f"apply_mode error: {e}")
    try:
        applied.update(apply_accent(accent))
    except Exception as e:
        log(f"apply_accent error: {e}")
    if wallpaper_path and wallpaper_path.is_file():
        try:
            apply_wallpaper(wallpaper_path)
            applied["wallpaper"] = str(wallpaper_path)
        except Exception as e:
            log(f"apply_wallpaper error: {e}")
    try:
        applied["folders"] = tint_folders(accent)  # noqa
    except Exception as e:
        log(f"tint_folders error: {e}")
    log(f"applied theme '{payload.get('name')}' mode={mode} accent={accent} "
        f"-> system accent {applied.get('accent_name')} (enum {applied.get('accent_enum')})")
    return applied


# ----------------------------------------------------------- Link TV server

class MacTV:
    def __init__(self, state_dir: Path = STATE_DIR, name: str = "MacLiber", on_update=None):
        self.dir = Path(state_dir)
        (self.dir / "internal").mkdir(parents=True, exist_ok=True, mode=0o700)
        self.secrets_file = self.dir / "internal" / "link.json"
        self.state_file = self.dir / "state.json"
        self.name = name
        self.on_update = on_update  # callback(dict) for the menu-bar UI
        self.lock = threading.RLock()
        self.nonces: dict = {}
        self.pending: dict = {}
        self.failures = 0
        sec = self._read(self.secrets_file, {})
        self.tv_id = sec.get("tvId") or secrets.token_hex(16)
        self.pcs: dict = sec.get("pcs", {})
        st = self._read(self.state_file, {})
        self.code = st.get("code") or f"{secrets.randbelow(10**6):06d}"
        # pairing opens automatically until at least one PC is paired
        self.pairing_open = st.get("pairingOpen", not self.pcs)
        self.wallpaper_sha = st.get("wallpaperSha")
        self.last_theme = st.get("lastTheme")
        self.last_push_at = st.get("lastPushAt")
        self.offset = int(st.get("wallpaperOffset", 0) or 0)   # Mac-side wallpaper shift
        self.wallset = st.get("wallset")                       # {theme,count,current,names}
        self._save_secrets()
        self._save_state()

    # -------- storage
    @staticmethod
    def _read(path: Path, default):
        try:
            return json.loads(Path(path).read_text())
        except (OSError, ValueError):
            return default

    def _save_secrets(self):
        tmp = self.secrets_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"tvId": self.tv_id, "pcs": self.pcs}, indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.secrets_file)

    def _save_state(self):
        st = {
            "code": self.code, "pairingOpen": self.pairing_open,
            "wallpaperSha": self.wallpaper_sha, "lastTheme": self.last_theme,
            "lastPushAt": self.last_push_at, "tvId": self.tv_id,
            "wallpaperOffset": self.offset, "wallset": self.wallset,
            "paired": [{"name": p.get("name"), "address": p.get("address"), "lastSeen": p.get("lastSeen")}
                       for p in self.pcs.values()],
        }
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2))
        os.replace(tmp, self.state_file)
        if self.on_update:
            try:
                self.on_update(st)
            except Exception:
                pass

    def open_pairing(self):
        with self.lock:
            self.code = f"{secrets.randbelow(10**6):06d}"
            self.pairing_open = True
            self.failures = 0
            self._save_state()
        return self.code

    def status(self) -> dict:
        return {"tvId": self.tv_id, "name": self.name, "launcherVersion": "omarchymac-1.0",
                "protocol": 1, "capabilities": CAPS, "homeVisible": True, "screenOn": True,
                "foreground": {"package": "im.mrg.omarchymac", "label": "OmarchyMac"},
                "screensaver": False, "time": int(time.time())}

    def wallpaper_path(self) -> Path | None:
        if not self.wallpaper_sha:
            return None
        p = self.dir / f"wallpaper-{self.wallpaper_sha[:12]}.jpg"
        return p if p.is_file() else None

    def store_wallpaper(self, jpeg: bytes) -> str:
        sha = hashlib.sha256(jpeg).hexdigest()
        for old in self.dir.glob("wallpaper-*.jpg"):
            try:
                old.unlink()
            except OSError:
                pass
        (self.dir / f"wallpaper-{sha[:12]}.jpg").write_bytes(jpeg)
        with self.lock:
            self.wallpaper_sha = sha
            self._save_state()
        return sha

    # -------- wallpaper set + offset (the Mac can show a shifted image of the set)
    def wallset_dir(self) -> Path:
        d = self.dir / "wallset"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def store_wallset_image(self, index: int, jpeg: bytes) -> None:
        (self.wallset_dir() / f"{int(index)}.jpg").write_bytes(jpeg)

    def finalize_wallset(self, meta: dict):
        names = [str(n) for n in (meta.get("names") or [])]
        count = int(meta.get("count") or len(names))
        current = int(meta.get("current") or 0)
        with self.lock:
            self.wallset = {"theme": str(meta.get("theme", ""))[:64],
                            "count": max(0, count), "current": current % count if count else 0,
                            "names": names, "at": int(time.time())}
            self._save_state()
        self.apply_effective_wallpaper()
        return self.wallset

    def effective_index(self):
        ws = self.wallset
        if not ws or not ws.get("count"):
            return None
        return (int(ws["current"]) + int(self.offset)) % int(ws["count"])

    def effective_wallpaper_path(self) -> Path | None:
        idx = self.effective_index()
        if idx is not None:
            p = self.wallset_dir() / f"{idx}.jpg"
            if p.is_file():
                return p
        return self.wallpaper_path()

    def apply_effective_wallpaper(self):
        p = self.effective_wallpaper_path()
        if not p:
            return None
        try:
            apply_wallpaper(p)
        except Exception as e:
            log(f"apply_effective_wallpaper error: {e}")
            return None
        idx = self.effective_index()
        with self.lock:
            if isinstance(self.last_theme, dict):
                self.last_theme.setdefault("applied", {})
                self.last_theme["applied"]["wallpaper"] = str(p)
                self.last_theme["applied"]["wallpaper_index"] = idx
                self.last_theme["applied"]["offset"] = self.offset
            self._save_state()
        log(f"wallpaper -> {p.name} (offset {self.offset}, index {idx})")
        return p

    def set_offset(self, n: int):
        with self.lock:
            self.offset = int(n)   # raw; effective_index() wraps it modulo the set size
            self._save_state()
        self.apply_effective_wallpaper()
        return self.offset

    def receive_theme(self, payload: dict):
        colors = {k: v for k, v in (payload.get("colors") or {}).items() if HEX6.match(str(v))}
        theme = {"name": str(payload.get("name", ""))[:64], "mode": payload.get("mode", "dark"),
                 "accent": payload.get("accent"), "accent2": payload.get("accent2"),
                 "colors": colors, "at": int(time.time())}
        applied = apply_theme(theme)
        theme["applied"] = applied
        with self.lock:
            self.last_theme = theme
            self.last_push_at = int(time.time())
            self._save_state()
        self.apply_effective_wallpaper()
        return theme


class Handler(BaseHTTPRequestHandler):
    tv: MacTV = None
    protocol_version = "HTTP/1.1"
    server_version = "OmarchyMac"

    def log_message(self, fmt, *args):
        pass

    def _lan_only(self) -> bool:
        try:
            ip = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            ip = None
        if ip is not None:
            if ip.is_loopback or ip.is_private or ip.is_link_local:
                return True
            if ip.version == 4 and ip in TAILNET_V4:
                return True
            if ip.version == 6 and ip in TAILNET_V6:
                return True
        self._json(403, {"error": "not_lan", "message": "LAN or tailnet clients only"})
        return False

    def _json(self, status, obj, sign=None, extra=None):
        body = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-OTV-Time", str(int(time.time())))
        if sign:
            token, nonce = sign
            self.send_header("X-OTV-Sig", crypto.sign_response(token, nonce, status, body))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self, limit):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            self._json(413, {"error": "too_large"})
            self.close_connection = True
            return None
        return self.rfile.read(n) if n else b""

    def _auth(self, body):
        tv = self.tv
        try:
            f = crypto.parse_authorization(self.headers.get("Authorization", ""))
        except ValueError:
            self._json(401, {"error": "unauthorized", "message": "missing or bad Authorization"})
            return None
        pc = tv.pcs.get(f["pc"])
        if not pc:
            self._json(401, {"error": "unauthorized", "message": "unknown PC"})
            return None
        if not f["ts"].lstrip("-").isdigit() or abs(int(f["ts"]) - time.time()) > SKEW:
            self._json(401, {"error": "clock_skew", "message": "timestamp outside +/-120 s"})
            return None
        token = base64.b64decode(pc["token"])
        expected = crypto.hmac256(token, crypto.request_string(self.command, self.path, f["ts"], f["nonce"], body))
        try:
            ok = hmac.compare_digest(crypto.unb64u(f["sig"]), expected)
        except ValueError:
            ok = False
        if not ok:
            self._json(401, {"error": "unauthorized", "message": "bad signature"})
            return None
        with tv.lock:
            now = time.time()
            for k in [k for k, t in tv.nonces.items() if now - t > 2 * SKEW]:
                del tv.nonces[k]
            if (f["pc"], f["nonce"]) in tv.nonces:
                self._json(401, {"error": "replay", "message": "nonce already used"})
                return None
            tv.nonces[(f["pc"], f["nonce"])] = now
            pc["lastSeen"] = int(now)
            pc["address"] = self.client_address[0]
            tv._save_secrets()
        return f["pc"], token, f["nonce"]

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def _route(self, method):
        if not self._lan_only():
            return
        tv, path = self.tv, self.path
        if method == "GET" and path == "/v1/info":
            return self._json(200, {"service": "omarchytv-link", "protocol": 1, "tvId": tv.tv_id,
                                    "name": tv.name, "pairingOpen": tv.pairing_open})
        if method == "POST" and path in ("/v1/pair/start", "/v1/pair/confirm"):
            body = self._read_body(JSON_MAX)
            return None if body is None else self._pair(path, body)
        limit = (JPEG_MAX if (method == "PUT" and (path == "/v1/wallpaper" or path.startswith("/v1/wallset/")))
                 else JSON_MAX)
        body = self._read_body(limit)
        if body is None:
            return
        who = self._auth(body)
        if not who:
            return
        pc_id, token, nonce = who
        sign = (token, nonce)
        is_image = path == "/v1/wallpaper" or path.startswith("/v1/wallset/")
        try:
            payload = json.loads(body) if body and not is_image else {}
        except ValueError:
            return self._json(400, {"error": "bad_json"}, sign)
        try:
            status, obj = self._dispatch(method, path, payload, body)
        except Exception as e:
            log(f"handler error {method} {path}: {e}")
            status, obj = 500, {"error": "internal"}
        self._json(status, obj, sign)

    def _dispatch(self, method, path, payload, body):
        tv = self.tv
        if (method, path) == ("GET", "/v1/status"):
            return 200, tv.status()
        if (method, path) == ("GET", "/v1/wallpaper"):
            return 200, {"sha256": tv.wallpaper_sha}
        if (method, path) == ("PUT", "/v1/wallpaper"):
            if self.headers.get("Content-Type") != "image/jpeg" or body[:3] != b"\xff\xd8\xff":
                return 415, {"error": "not_jpeg"}
            return 200, {"sha256": tv.store_wallpaper(body)}
        if (method, path) == ("PUT", "/v1/theme"):
            if not all(HEX6.match(str(payload.get(k, ""))) for k in ("accent", "accent2")):
                return 400, {"error": "bad_theme", "message": "accent/accent2 must be #rrggbb"}
            wp = (payload.get("wallpaper") or {}).get("sha256")
            if wp and wp != tv.wallpaper_sha:
                return 409, {"error": "unknown_wallpaper", "message": "PUT /v1/wallpaper first"}
            tv.receive_theme(payload)
            return 200, {"ok": True}
        if method == "PUT" and path.startswith("/v1/wallset/"):
            try:
                idx = int(path.rsplit("/", 1)[1])
            except ValueError:
                return 400, {"error": "bad_index"}
            if self.headers.get("Content-Type") != "image/jpeg" or body[:3] != b"\xff\xd8\xff":
                return 415, {"error": "not_jpeg"}
            tv.store_wallset_image(idx, body)
            return 200, {"ok": True, "index": idx}
        if (method, path) == ("PUT", "/v1/wallset"):
            tv.finalize_wallset(payload)
            return 200, {"ok": True, "effective_index": tv.effective_index(), "offset": tv.offset}
        if (method, path) == ("GET", "/v1/wallset"):
            return 200, {"wallset": tv.wallset, "offset": tv.offset,
                         "effective_index": tv.effective_index()}
        if (method, path) == ("POST", "/v1/offset"):
            tv.set_offset(int(payload.get("offset", 0)))
            return 200, {"ok": True, "offset": tv.offset, "effective_index": tv.effective_index()}
        if (method, path) == ("POST", "/v1/unpair"):
            with tv.lock:
                tv.pcs.pop(self._pc_id, None) if hasattr(self, "_pc_id") else None
                tv._save_secrets()
                tv._save_state()
            return 200, {"ok": True}
        return 404, {"error": "not_found"}

    def _pair(self, path, body):
        tv = self.tv
        if not tv.pairing_open:
            return self._json(403, {"error": "pairing_closed",
                                    "message": "open pairing from the OmarchyMac menu bar"})
        try:
            req = json.loads(body)
        except ValueError:
            return self._json(400, {"error": "bad_json"})
        with tv.lock:
            if path == "/v1/pair/start":
                pc_id = str(req.get("pcId", ""))
                if not (8 <= len(pc_id) <= 64) or not pc_id.isalnum():
                    return self._json(400, {"error": "bad_request", "message": "pcId"})
                try:
                    b = crypto.TvPairing(tv.code, tv.tv_id, pc_id, crypto.unb64u(req.get("t", "")))
                except ValueError as e:
                    return self._json(400, {"error": "bad_request", "message": str(e)})
                pid = secrets.token_hex(8)
                tv.pending = {pid: {"b": b, "pcId": pc_id,
                                    "pcName": str(req.get("pcName", "omarchy"))[:40],
                                    "expires": time.time() + 60}}
                return self._json(200, {"pairingId": pid, "tvId": tv.tv_id, "tvName": tv.name,
                                        "s": crypto.b64u(b.message()), "expiresIn": 60})
            p = tv.pending.pop(str(req.get("pairingId", "")), None)
            if not p or p["expires"] < time.time():
                return self._json(400, {"error": "no_pairing", "message": "start again"})
            try:
                ok = hmac.compare_digest(crypto.unb64u(req.get("confirm", "")), p["b"].keys.confirm_pc)
            except ValueError:
                ok = False
            if not ok:
                tv.failures += 1
                left = max(0, 5 - tv.failures)
                if left == 0:
                    tv.code = f"{secrets.randbelow(10**6):06d}"
                    tv.failures = 0
                    tv._save_state()
                return self._json(401, {"error": "bad_code", "message": "wrong code", "attemptsLeft": left})
            tv.failures = 0
            tv.pcs[p["pcId"]] = {"name": p["pcName"],
                                 "token": base64.b64encode(p["b"].keys.token).decode(),
                                 "pairedAt": int(time.time()), "address": self.client_address[0],
                                 "lastSeen": int(time.time())}
            tv._save_secrets()
            tv.code = f"{secrets.randbelow(10**6):06d}"   # a code pairs one PC
            tv.pairing_open = False                       # close after a successful pair, like the TV
            tv._save_state()
            log(f"paired PC '{p['pcName']}' ({p['pcId']}) from {self.client_address[0]}")
            return self._json(200, {"confirm": crypto.b64u(p["b"].keys.confirm_tv), "tvId": tv.tv_id})


# ----------------------------------------------------------- mDNS advertise

def advertise_mdns(tv: MacTV, port: int = PORT):
    """Register _omarchytv._tcp via macOS dns-sd so omarchy-link can discover/relocate us."""
    txt = [f"v=1", f"id={tv.tv_id}", f"name={tv.name}", f"pair={'1' if tv.pairing_open else '0'}"]
    cmd = ["dns-sd", "-R", tv.name, "_omarchytv._tcp", "local", str(port)] + txt
    try:
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL)
    except OSError as e:
        log(f"dns-sd advertise failed: {e}")
        return None


def serve(tv: MacTV, host: str = "", port: int = PORT):
    handler = type("H", (Handler,), {"tv": tv})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    log(f"OmarchyMac link server on :{port}  tvId {tv.tv_id}  pairing={'open' if tv.pairing_open else 'closed'} "
        f"code={tv.code if tv.pairing_open else '----'}")
    return httpd


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--name", default="MacLiber")
    ap.add_argument("--open", action="store_true", help="force pairing open and print the code")
    ap.add_argument("--mdns", action="store_true")
    a = ap.parse_args()
    tv = MacTV(name=a.name)
    if a.open:
        tv.open_pairing()
    httpd = serve(tv, port=a.port)
    responder = advertise_mdns(tv, a.port) if a.mdns else None
    if tv.pairing_open:
        print(f"PAIRING CODE: {tv.code}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        if responder:
            responder.terminate()


if __name__ == "__main__":
    main()
