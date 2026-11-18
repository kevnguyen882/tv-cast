#!/usr/bin/env python3
"""
TV Cast — cast or download web videos, and cast files from your Mac, to a DLNA smart TV.

Run:   python3 tv_cast.py
Then a control page opens in your browser at http://127.0.0.1:8765

One-time setup for the extras:
  python3 -m pip install playwright yt-dlp   # page scanner + fallback extractor
  python3 -m playwright install chromium     # browser for the page scanner
  brew install ffmpeg                         # needed for .m3u8 streams

Paste the page a video plays on: the app opens it in a hidden browser, presses play,
and grabs the .m3u8 (master playlist) it loads — like DevTools → Network. If no .m3u8
turns up, it falls back to yt-dlp.
"""

import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin, urlparse, parse_qs
from xml.sax.saxutils import escape

PORT = 8765
AVT = "urn:schemas-upnp-org:service:AVTransport:1"
RC = "urn:schemas-upnp-org:service:RenderingControl:1"

DEVICES = {}          # id -> {name, avt, rc, model}
DEVICES_LOCK = threading.Lock()


# ---------------------------------------------------------------- discovery
SSDP_ADDR = ("239.255.255.250", 1900)
SEARCH_TARGETS = ["urn:schemas-upnp-org:device:MediaRenderer:1", AVT, "ssdp:all"]
LAST_SCAN = {"responders": 0, "interfaces": []}   # for the "why no TVs?" hint


def _msearch(st, host="239.255.255.250:1900"):
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {host}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        f"ST: {st}\r\n\r\n"
    ).encode()


def local_ipv4s():
    """Every IPv4 address this computer has on a real network (Wi-Fi, Ethernet, …)."""
    ips = set()
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
        ips.update(re.findall(r"inet (?:addr:)?(\d+\.\d+\.\d+\.\d+)", out))
    except Exception:
        pass
    try:  # the one the computer uses by default
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith(("127.", "169.254.", "0.")))


def ssdp_search(timeout=4.0, unicast=()):
    """Ask the network who's a TV. Searches from every network connection (so a VPN or
    a second adapter can't swallow the search), plus directly at any IPs given."""
    import select
    socks = []
    for ip in local_ipv4s() or ["0.0.0.0"]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            if ip != "0.0.0.0":
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            s.bind((ip, 0))
            s.setblocking(False)
            socks.append((ip, s))
        except OSError as e:
            print(f"Can't search from {ip}: {e}")
    LAST_SCAN["interfaces"] = [ip for ip, _ in socks]
    for _ in range(2):
        for st in SEARCH_TARGETS:
            for ip, s in socks:
                try:
                    s.sendto(_msearch(st), SSDP_ADDR)
                except OSError as e:
                    if _ == 0 and st == SEARCH_TARGETS[0]:
                        print(f"Search from {ip} failed: {e}")
                for target in unicast:  # TVs also answer a search sent straight to them
                    try:
                        s.sendto(_msearch(st, f"{target}:1900"), (target, 1900))
                    except OSError:
                        pass
        time.sleep(0.2)
    locations, responders = set(), set()
    end = time.time() + timeout
    while time.time() < end and socks:
        ready, _, _ = select.select([s for _, s in socks], [], [], 0.3)
        for s in ready:
            try:
                data, addr = s.recvfrom(65507)
            except OSError:
                continue
            responders.add(addr[0])
            m = re.search(rb"(?im)^location:\s*(\S+)", data)
            if m:
                locations.add(m.group(1).decode(errors="ignore"))
    for _, s in socks:
        s.close()
    LAST_SCAN["responders"] = len(responders)
    return locations


# TVs found before are remembered, so they can be checked directly next time.
KNOWN_FILE = os.path.expanduser("~/.tv_cast_tvs.json")


def load_known():
    try:
        with open(KNOWN_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_known(devs):
    known = load_known()
    for d in devs:
        known[d["id"]] = {"name": d["name"], "ip": urlparse(d["id"]).hostname}
    try:
        with open(KNOWN_FILE, "w") as f:
            json.dump(known, f, indent=1)
    except OSError:
        pass


def _strip_ns(root):
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def describe(location):
    """Fetch a device description and pull out the control URLs we need."""
    with urllib.request.urlopen(location, timeout=4) as r:
        root = _strip_ns(ET.fromstring(r.read()))
    base = root.findtext("URLBase") or location
    for dev in root.iter("device"):
        avt = rc = cm = None
        for svc in dev.iter("service"):
            st = svc.findtext("serviceType") or ""
            ctrl = svc.findtext("controlURL")
            if not ctrl:
                continue
            if "AVTransport" in st and not avt:
                avt = (urljoin(base, ctrl), st)
            elif "RenderingControl" in st and not rc:
                rc = (urljoin(base, ctrl), st)
            elif "ConnectionManager" in st and not cm:
                cm = (urljoin(base, ctrl), st)
        if avt:
            return {
                "id": location,
                "name": dev.findtext("friendlyName") or urlparse(location).hostname,
                "model": " ".join(filter(None, [dev.findtext("manufacturer"),
                                                dev.findtext("modelName")])),
                "avt": avt,
                "rc": rc,
                "cm": cm,
            }
    return None


def _describe_all(locations, quiet=False):
    found = {}
    for loc in locations:
        try:
            d = describe(loc)
        except Exception as e:
            if not quiet:
                print(f"Skipping {loc}: {e}")
            continue
        if d:
            found[d["id"]] = d
    return found


def discover(extra_ips=()):
    known = load_known()
    ips = sorted({k["ip"] for k in known.values() if k.get("ip")} | set(extra_ips))
    locations = ssdp_search(unicast=ips)
    # Check remembered TVs directly too, in case the search itself was blocked.
    found = _describe_all(locations | set(known), quiet=True)
    if found:
        save_known(found.values())
    elif LAST_SCAN["responders"] == 0:
        print("Nothing on the network answered the search. If you're on macOS, allow Terminal in "
              "System Settings → Privacy & Security → Local Network, then restart the app.")
    with DEVICES_LOCK:
        DEVICES.clear()
        DEVICES.update(found)
    print(f"Found {len(found)} TV(s): " + ", ".join(d["name"] for d in found.values()))
    return found


# ---------------------------------------------------------------- SOAP
class SoapError(RuntimeError):
    """The TV answered, but said no."""


def _tag(xml, name):
    m = re.search(rf"<(?:[\w-]+:)?{name}[^>]*>(.*?)</(?:[\w-]+:)?{name}>", xml, re.S)
    return m.group(1).strip() if m else ""


def soap(service, action, args=""):
    url, st = service
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action} xmlns:u="{st}">{args}</u:{action}></s:Body>'
        "</s:Envelope>"
    ).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPACTION": f'"{st}#{action}"',
    })
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            return _strip_ns(ET.fromstring(r.read()))
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="ignore")
        code = _tag(detail, "errorCode") or str(e.code)
        desc = _tag(detail, "errorDescription") or _tag(detail, "faultstring")
        if desc.lower() in ("upnperror", "upnp error"):
            desc = ""
        raise SoapError(f"TV refused {action} (error {code}" + (f": {desc})" if desc else ")"))


def sink_formats(dev):
    """What the TV says it can play: [(mime, dlna-info), ...]. Cached per TV."""
    if "sink" in dev:
        return dev["sink"]
    out = []
    if dev.get("cm"):
        try:
            sink = soap(dev["cm"], "GetProtocolInfo").findtext(".//Sink") or ""
            for entry in sink.split(","):
                parts = entry.strip().split(":", 3)
                if len(parts) == 4 and parts[0] == "http-get":
                    out.append((parts[2].lower(), parts[3]))
            vids = sorted({m for m, _ in out if m.startswith("video/")})
            print(f"{dev['name']} says it plays: " + (", ".join(vids) or "(no video types listed)"))
        except Exception as e:
            print(f"Couldn't ask {dev['name']} what it plays: {e}")
    dev["sink"] = out
    return out


# Ways of labelling each kind of video, most common first. TVs differ in which they accept.
MIME_FAMILIES = {
    "ts": ["video/mpeg", "video/mp2t", "video/vnd.dlna.mpeg-tts"],
    "mp4": ["video/mp4", "video/mpeg4"],
    "m4v": ["video/mp4", "video/x-m4v"],
    "mov": ["video/quicktime", "video/mp4"],
    "mkv": ["video/x-matroska", "video/x-mkv"],
    "webm": ["video/webm"],
    "avi": ["video/x-msvideo", "video/avi"],
}
EXT_FOR_MIME = {"video/mpeg": "mpg"}  # some TVs also check the file extension in the link


def format_candidates(dev, family, base_flags):
    """(mime, dlna-flags) pairs to offer the TV, best bet first. flags None = no metadata."""
    mimes = MIME_FAMILIES.get(family) or [guess_mime("x." + family)]
    out = []
    known = dev.get("works", {}).get(family)
    if known:
        out.append(known)
    sink = sink_formats(dev)
    listed = [m for m in mimes if any(sm == m for sm, _ in sink)]
    for m in listed:  # formats the TV says it plays, in its own words
        out += [(m, f) for sm, f in sink if sm == m and f and f != "*"][:3]
    for m in listed + [m for m in mimes if m not in listed]:
        out += [(m, base_flags), (m, "*")]
    out.append((mimes[0], None))
    seen, uniq = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def guess_mime(url):
    path = urlparse(url).path.lower()
    if path.endswith(".m3u8"):
        return "application/vnd.apple.mpegurl"
    if path.endswith(".mkv"):
        return "video/x-matroska"
    if path.endswith(".webm"):
        return "video/webm"
    if path.endswith(".avi"):
        return "video/x-msvideo"
    if path.endswith(".ts"):
        return "video/mpeg"
    if path.endswith((".mp3",)):
        return "audio/mpeg"
    return "video/mp4"


DIRECT_EXT = (".mp4", ".m4v", ".mkv", ".webm", ".m3u8", ".mov", ".avi", ".mp3", ".ts")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
# Single-file streams with both video and audio, preferring mp4 (what TVs like best).
YTDLP_FORMAT = (
    "best[ext=mp4][protocol^=http][protocol!*=dash][vcodec!=none][acodec!=none]"
    "/best[protocol^=http][protocol!*=dash][vcodec!=none][acodec!=none]"
    "/best[vcodec!=none][acodec!=none]/best"
)
# DLNA tags many Samsung/LG/Sony TVs insist on before they'll play anything.
DLNA_FLAGS = "DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"
DLNA_SEEKABLE = "DLNA.ORG_OP=01;" + DLNA_FLAGS
DLNA_LIVE = "DLNA.ORG_OP=00;" + DLNA_FLAGS

STREAMS = {}  # token -> what the relay should fetch for the TV


def _extra_bin_dirs():
    """Folders pip/Homebrew install tools into that often aren't on PATH."""
    import glob, os, sysconfig
    dirs = [os.path.dirname(sys.executable), sysconfig.get_path("scripts")]
    for scheme in ("osx_framework_user", "posix_user"):
        try:
            dirs.append(sysconfig.get_path("scripts", scheme))
        except KeyError:
            pass
    dirs += glob.glob(os.path.expanduser("~/Library/Python/*/bin"))
    dirs += [os.path.expanduser("~/.local/bin"), "/opt/homebrew/bin", "/usr/local/bin"]
    return os.pathsep.join(d for d in dirs if d)


def find_tool(name):
    """Command list to run a tool, or None if it can't be found anywhere."""
    path = shutil.which(name) or shutil.which(name, path=_extra_bin_dirs())
    if path:
        return [path]
    if name == "yt-dlp":
        import importlib.util
        if importlib.util.find_spec("yt_dlp"):
            return [sys.executable, "-m", "yt_dlp"]
    return None


def site_headers(page=None):
    """Browser-like headers; with the page the video came from, many hosts stop refusing."""
    h = {"User-Agent": UA}
    if page:
        p = urlparse(page)
        h["Referer"] = page
        h["Origin"] = f"{p.scheme}://{p.netloc}"
    return h


def is_direct(url):
    return urlparse(url).path.lower().endswith(DIRECT_EXT)


class ScannerMissing(Exception):
    pass


def scanner_available():
    import importlib.util
    return importlib.util.find_spec("playwright") is not None


PLAY_JS = """() => {
  document.querySelectorAll('video').forEach(v => {
    v.muted = true; const p = v.play(); if (p && p.catch) p.catch(() => {}); });
  const b = document.querySelector('.jw-icon-display, .jw-display-icon-container, ' +
    '.vjs-big-play-button, .plyr__control--overlaid, .fp-play, [aria-label="Play"], ' +
    '[title="Play"], .play-button, .btn-play, .play-btn, #play, .play');
  if (b) b.click();
}"""


def find_stream_in_page(page_url, timeout=30, visible=False):
    """Open the page in a hidden browser, press play, and catch the .m3u8 it loads —
    the same thing you'd do by hand in DevTools → Network. With visible=True the
    window is shown so you can press play (or pass a "are you human" check) yourself."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ScannerMissing("the page scanner isn't installed "
                             "(run: python3 -m pip install playwright)")
    found = []  # [kind, request]

    def on_request(req):
        path = urlparse(req.url).path.lower()
        if ".m3u8" in path:
            found.append(["m3u8", req])
        elif path.endswith((".mp4", ".m4v", ".webm")):
            found.append(["mp4", req])

    def on_response(resp):
        ct = (resp.headers.get("content-type") or "").lower()
        if "mpegurl" in ct and not any(r is resp.request for _, r in found):
            found.append(["m3u8", resp.request])

    with sync_playwright() as p:
        browser, problem = None, None
        for opts in ({"channel": "chrome"}, {}):  # your Chrome first, else Playwright's own
            try:
                browser = p.chromium.launch(headless=not visible, **opts)
                break
            except Exception as e:
                problem = e
        if not browser:
            raise ScannerMissing("no browser for the page scanner "
                                 "(run: python3 -m playwright install chromium)")
        try:
            ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 720})
            page = ctx.new_page()
            page.on("request", on_request)
            page.on("response", on_response)
            try:
                page.goto(page_url, wait_until="domcontentloaded", timeout=25000)
            except Exception as e:
                if "Timeout" not in type(e).__name__ and "timeout" not in str(e).lower():
                    raise RuntimeError(f"couldn't open the page ({str(e).splitlines()[0]})")
            start, pokes, mp4_seen_at = time.time(), 0, None
            while time.time() - start < timeout:
                if page.is_closed():  # you closed the window
                    break
                if any(k == "m3u8" for k, _ in found):
                    page.wait_for_timeout(2000)  # give a master playlist a moment to show up
                    break
                if any(k == "mp4" for k, _ in found):
                    mp4_seen_at = mp4_seen_at or time.time()
                    if time.time() - mp4_seen_at > 5:
                        break
                # Players often wait for a click (and the first click is often an ad popup).
                # (In the visible window, only nudge a couple of times so we don't fight you.)
                if time.time() - start > 2 + pokes * 3 and pokes < (2 if visible else 6):
                    pokes += 1
                    try:
                        page.bring_to_front()
                        page.mouse.click(640, 360)
                    except Exception:
                        pass
                    for fr in page.frames:
                        try:
                            fr.evaluate(PLAY_JS)
                        except Exception:
                            pass
                popups = [o for o in ctx.pages if o != page]
                for other in popups:  # close ad popups
                    try:
                        other.close()
                    except Exception:
                        pass
                if popups and visible:
                    try:
                        page.bring_to_front()
                    except Exception:
                        pass
                try:
                    page.wait_for_timeout(500)
                except Exception:
                    break  # window was closed

            m3u8s = [r for k, r in found if k == "m3u8"]
            chosen, hls = None, True
            for r in m3u8s:  # the master playlist is the one listing the qualities
                try:
                    resp = r.response()
                    if resp and "#EXT-X-STREAM-INF" in resp.text():
                        chosen = r
                        break
                except Exception:
                    pass
            if not chosen and m3u8s:
                named = [r for r in m3u8s if "master" in r.url.lower()]
                chosen = (named or m3u8s)[0]
            if not chosen:
                mp4s = [r for k, r in found if k == "mp4"]
                if not mp4s:
                    return None
                chosen, hls = mp4s[0], False

            try:
                sent = chosen.all_headers()
            except Exception:
                sent = chosen.headers
            headers = {k.title(): sent[k] for k in ("user-agent", "referer", "origin", "cookie")
                       if sent.get(k)}
            if "Cookie" not in headers:
                cookies = ctx.cookies(chosen.url)
                if cookies:
                    headers["Cookie"] = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            headers.setdefault("User-Agent", UA)
            try:
                title = page.title().strip()
            except Exception:
                title = ""
            path = urlparse(chosen.url).path.lower()
            return {
                "url": chosen.url, "headers": headers, "hls": hls,
                "ext": "m3u8" if hls else (path.rsplit(".", 1)[-1] or "mp4"),
                "title": title or "Video",
                "note": "Found the " + ("master .m3u8" if hls else "video file") + " on the page",
            }
        finally:
            browser.close()


SCAN_VISIBLE_TIMEOUT = 150  # seconds you get to press play in the visible window


def scan_page(url, status=None):
    """(stream or None, reason it couldn't scan or None).
    Tries a hidden browser first; if that finds nothing, opens a visible window
    so you can press play yourself."""
    problem = None
    for visible in (False, True):
        if visible:
            msg = "Couldn't find it automatically — press play in the browser window that opened"
            print(msg)
            if status:
                status(msg)
        try:
            src = find_stream_in_page(url, timeout=SCAN_VISIBLE_TIMEOUT if visible else 30,
                                      visible=visible)
            if src:
                return src, None
        except ScannerMissing as e:
            print("Page scanner:", e)
            return None, str(e)
        except Exception as e:
            print("Page scan failed:", e)
            problem = f"the page scan failed ({e})"
    return None, problem


def resolve(url, page=None):
    """Work out the real stream behind a link, plus the headers it needs."""
    path = urlparse(url).path.lower()
    base = site_headers(page)
    if path.endswith(DIRECT_EXT):
        return {"url": url, "headers": base, "hls": path.endswith(".m3u8"),
                "ext": path.rsplit(".", 1)[-1], "title": "Video", "note": None}
    src, problem = scan_page(url)  # look for the .m3u8 first
    if src:
        return src
    ytdlp = find_tool("yt-dlp")
    if not ytdlp:
        raise RuntimeError("Couldn't find a video stream on that page"
                           + (f" — {problem}." if problem else "."))
    try:
        out = subprocess.run(
            ytdlp + ["-j", "--no-playlist", "--no-warnings", "-f", YTDLP_FORMAT]
            + (["--referer", page] if page else []) + [url],
            capture_output=True, text=True, timeout=90,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("yt-dlp took too long to find the stream. Try again.")
    if out.returncode != 0 or not out.stdout.strip():
        err = (out.stderr.strip().splitlines() or ["unknown error"])[-1]
        raise RuntimeError("Couldn't find a video on that page. No .m3u8 showed up"
                           + (f" ({problem})" if problem else "")
                           + ", and yt-dlp said: " + err.replace("ERROR: ", ""))
    info = json.loads(out.stdout.strip().splitlines()[0])
    media = info["url"]
    hls = "m3u8" in (info.get("protocol") or "") or urlparse(media).path.endswith(".m3u8")
    height = info.get("height")
    return {
        "url": media,
        "headers": {**base, **(info.get("http_headers") or {})},
        "hls": hls,
        "ext": info.get("ext") or "mp4",
        "title": info.get("title") or "Video",
        "note": f"Found “{info.get('title', 'video')}”" + (f" ({height}p)" if height else ""),
    }


def local_ip_for(host):
    """The address of this computer as seen from the TV's network."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 1900))
        return s.getsockname()[0]
    finally:
        s.close()


def is_local_path(text):
    return text.startswith(("/", "~/", "file://"))


class Feed:
    """Pulls an .m3u8 stream with ffmpeg into one continuous .ts file on disk, started
    *before* the TV is contacted, so the TV gets video the instant it asks."""

    def __init__(self, src):
        import tempfile
        fd, self.path = tempfile.mkstemp(prefix="tvcast-", suffix=".ts")
        os.close(fd)
        hdrs = "".join(f"{k}: {v}\r\n" for k, v in src["headers"].items())
        self.err, self.stopped = "", False
        self.proc = subprocess.Popen(
            find_tool("ffmpeg") + ["-y", "-nostdin", "-loglevel", "error", "-headers", hdrs,
                                   "-i", src["url"], "-map", "0:v:0?", "-map", "0:a:0?",
                                   "-c", "copy", "-f", "mpegts", self.path],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        threading.Thread(target=self._collect, daemon=True).start()

    def _collect(self):
        err = self.proc.stderr.read().strip()
        self.err = err.splitlines()[-1] if err else ""

    @property
    def done(self):
        return self.proc.poll() is not None

    def size(self):
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def wait_ready(self, min_bytes=512 * 1024, timeout=30):
        start = time.time()
        while time.time() - start < timeout:
            if self.size() >= min_bytes:
                return
            if self.done:
                time.sleep(0.3)
                if self.size() > 0 and self.proc.returncode == 0:
                    return
                msg = self.err or "ffmpeg stopped"
                if "403" in msg:
                    msg += " — the site refused it"
                raise RuntimeError("Couldn't read the stream: " + msg)
            time.sleep(0.2)
        raise RuntimeError("The stream took too long to start sending video.")

    def stop(self):
        self.stopped = True
        try:
            self.proc.kill()
        except Exception:
            pass
        try:
            os.remove(self.path)
        except OSError:
            pass


FEEDS = []


def stop_feeds():
    while FEEDS:
        FEEDS.pop().stop()


import atexit
atexit.register(stop_feeds)


def cast(dev, url, page=None):
    if is_local_path(url):  # a file path pasted into the link box
        from urllib.parse import unquote
        return cast_file(dev, os.path.expanduser(unquote(url[7:]) if url.startswith("file://") else url))
    src = resolve(url, page)
    if src["hls"]:
        # DLNA TVs can't play .m3u8 playlists; ffmpeg turns it into one plain stream.
        if not find_tool("ffmpeg"):
            raise RuntimeError("This is an .m3u8 stream, which TVs can't play directly. "
                               "Install ffmpeg (brew install ffmpeg) and restart the app.")
        stop_feeds()
        feed = Feed(src)
        FEEDS.append(feed)
        feed.wait_ready()
        return send_to_tv(dev, {**src, "feed": feed}, "ts")
    stop_feeds()
    return send_to_tv(dev, src, src["ext"])


def cast_file(dev, path):
    """Cast a video file that's on this computer (e.g. one the app downloaded)."""
    path = check_video(path)
    stop_feeds()
    ext = path.rsplit(".", 1)[-1].lower()
    src = {"path": path, "hls": False, "ext": ext,
           "title": os.path.basename(path).rsplit(".", 1)[0], "note": None}
    return send_to_tv(dev, src, ext)


def _didl(title, mime, flags, url):
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="0" parentID="-1" restricted="1">'
        f"<dc:title>{escape(title)}</dc:title>"
        "<upnp:class>object.item.videoItem</upnp:class>"
        f'<res protocolInfo="http-get:*:{mime}:{flags}">{escape(url)}</res>'
        "</item></DIDL-Lite>"
    )


def send_to_tv(dev, src, family):
    base_flags = DLNA_LIVE if src.get("feed") else DLNA_SEEKABLE
    token = secrets.token_urlsafe(12)
    STREAMS[token] = {**src, "hits": 0, "mime": guess_mime("x." + family), "flags": base_flags}
    tv_host = urlparse(dev["avt"][0]).hostname
    ip = local_ip_for(tv_host)
    try:
        soap(dev["avt"], "Stop", "<InstanceID>0</InstanceID>")
    except Exception:
        pass  # fine if nothing was playing

    # Offer the video in each format the TV might accept until it says yes.
    tried, relay = [], None
    for mime, flags in format_candidates(dev, family, base_flags):
        url = f"http://{ip}:{PORT}/stream/{token}.{EXT_FOR_MIME.get(mime, family)}"
        STREAMS[token].update(mime=mime, flags=base_flags if flags in (None, "*") else flags)
        meta = "" if flags is None else escape(_didl(src["title"], mime, flags, url))
        try:
            soap(dev["avt"], "SetAVTransportURI",
                 "<InstanceID>0</InstanceID>"
                 f"<CurrentURI>{escape(url)}</CurrentURI>"
                 f"<CurrentURIMetaData>{meta}</CurrentURIMetaData>")
            relay = url
            dev.setdefault("works", {})[family] = (mime, flags)
            print(f"{dev['name']} accepted it as {mime}" + ("" if flags else " (no metadata)"))
            break
        except SoapError as e:
            tried.append(mime)
            print(f"  {dev['name']} turned down {mime} [{(flags or 'no metadata')[:40]}]: {e}")
    if not relay:
        vids = sorted({m for m, _ in sink_formats(dev) if m.startswith("video/")})
        msg = (f"Your TV turned down this video in every format the app tried "
               f"({', '.join(dict.fromkeys(tried))}).")
        if vids:
            msg += " It says it can play: " + ", ".join(vids) + "."
        if src.get("feed"):
            msg += " Try ⬇ Download instead, then cast the saved .mp4 — TVs take mp4 files most reliably."
        raise RuntimeError(msg)
    time.sleep(0.5)
    soap(dev["avt"], "Play", "<InstanceID>0</InstanceID><Speed>1</Speed>")

    # Check that it actually started, so the page can say what went wrong if not.
    state = ""
    for _ in range(12):
        time.sleep(1)
        try:
            state = soap(dev["avt"], "GetTransportInfo",
                         "<InstanceID>0</InstanceID>").findtext(".//CurrentTransportState") or ""
        except Exception:
            continue
        if state == "PLAYING":
            return (src["note"] + " — playing on TV.") if src["note"] else "Playing on TV."
    if STREAMS[token]["hits"] == 0:
        raise RuntimeError("The TV never connected to this computer to fetch the video. "
                           "If your Mac asked whether Python may accept incoming connections, "
                           "click Allow (or allow it in System Settings → Network → Firewall), "
                           "then try again.")
    if STREAMS[token].get("upstream_error"):
        raise RuntimeError("The video site refused the stream: " + STREAMS[token]["upstream_error"])
    if state in ("STOPPED", "NO_MEDIA_PRESENT"):
        raise RuntimeError("The TV fetched the video but couldn't play it — probably a format "
                           f"it doesn't support ({src['ext']}). Try a different link.")
    return "Sent to TV — it's still loading."


# ---------------------------------------------------------------- downloads
import os

DL_DIR = os.path.expanduser("~/Downloads")
DOWNLOADS = {}   # id -> job info shown on the page
DL_LOCK = threading.Lock()


def _safe_name(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", name or "").strip(" .")
    return name[:120] or time.strftime("video-%Y%m%d-%H%M%S")


def _unique_path(name, ext):
    path = os.path.join(DL_DIR, f"{name}.{ext}")
    n = 2
    while os.path.exists(path):
        path = os.path.join(DL_DIR, f"{name} ({n}).{ext}")
        n += 1
    return path


def _update(job_id, **kw):
    with DL_LOCK:
        DOWNLOADS[job_id].update(kw)


def start_download(url, page=None, name=None):
    job_id = secrets.token_hex(6)
    with DL_LOCK:
        DOWNLOADS[job_id] = {"id": job_id, "name": name or "Starting…", "status": "running",
                             "percent": None, "path": None, "error": None,
                             "started": time.time(), "_proc": None, "_cancel": False}
    threading.Thread(target=_download, args=(job_id, url, page, name), daemon=True).start()
    return job_id


def _download(job_id, url, page, name):
    try:
        if is_local_path(url):
            raise RuntimeError("That's already a file on your computer — use Choose file… to cast it.")
        if is_direct(url):
            path = _download_direct(job_id, url, site_headers(page), name)
        else:
            _update(job_id, name="Looking for the video on the page…")
            src, problem = scan_page(url, status=lambda m: _update(job_id, name=m + "…"))
            if DOWNLOADS[job_id]["_cancel"]:
                raise RuntimeError("Cancelled")
            if src:
                _update(job_id, name="Found it — downloading…")
                path = _download_direct(job_id, src["url"], src["headers"], name or src["title"],
                                        hls=src["hls"], ext=src["ext"])
            else:
                try:
                    path = _download_ytdlp(job_id, url, page, name)
                except RuntimeError as e:
                    if str(e) == "Cancelled":
                        raise
                    raise RuntimeError("No .m3u8 showed up on the page"
                                       + (f" ({problem})" if problem else "")
                                       + f", and yt-dlp couldn't get it either: {e}")
        if DOWNLOADS[job_id]["_cancel"]:
            raise RuntimeError("Cancelled")
        _update(job_id, status="done", percent=100, path=path, name=os.path.basename(path))
        print("Downloaded:", path)
    except Exception as e:
        cancelled = DOWNLOADS[job_id]["_cancel"]
        _update(job_id, status="cancelled" if cancelled else "error",
                error=None if cancelled else str(e))


def _download_direct(job_id, url, headers, name, hls=None, ext=None):
    ffmpeg = find_tool("ffmpeg")
    is_hls = hls if hls is not None else ".m3u8" in urlparse(url).path.lower()
    if is_hls and not ffmpeg:
        raise RuntimeError("Saving .m3u8 streams needs ffmpeg — run: brew install ffmpeg")
    name = _safe_name(name)
    if not ffmpeg:  # plain file: just copy it down
        ext = ext or urlparse(url).path.rsplit(".", 1)[-1].lower() or "mp4"
        final = _unique_path(name, ext)
        _update(job_id, name=os.path.basename(final))
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r, open(final + ".part", "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while not DOWNLOADS[job_id]["_cancel"]:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    _update(job_id, percent=round(done * 100 / total, 1))
        if DOWNLOADS[job_id]["_cancel"]:
            os.remove(final + ".part")
            raise RuntimeError("Cancelled")
        os.rename(final + ".part", final)
        return final

    # ffmpeg: works for both .m3u8 streams and plain files, and saves a clean .mp4
    final = _unique_path(name, "mp4")
    tmp = final[:-4] + ".part.mp4"
    _update(job_id, name=os.path.basename(final))
    hdrs = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    duration = 0.0
    ffprobe = find_tool("ffprobe")
    if ffprobe:
        try:
            out = subprocess.run(ffprobe + ["-v", "error", "-headers", hdrs, "-show_entries",
                                            "format=duration", "-of", "csv=p=0", url],
                                 capture_output=True, text=True, timeout=30)
            duration = float(out.stdout.strip() or 0)
        except Exception:
            pass
    if DOWNLOADS[job_id]["_cancel"]:
        raise RuntimeError("Cancelled")
    p = subprocess.Popen(
        ffmpeg + ["-y", "-nostdin", "-loglevel", "error", "-headers", hdrs, "-i", url,
                  "-c", "copy", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", tmp],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    _update(job_id, _proc=p)
    for line in p.stdout:
        if line.startswith("out_time_us=") and duration:
            try:
                secs = int(line.split("=", 1)[1]) / 1e6
                _update(job_id, percent=min(99.0, round(secs * 100 / duration, 1)))
            except ValueError:
                pass
    p.wait()
    if p.returncode != 0 or DOWNLOADS[job_id]["_cancel"]:
        if os.path.exists(tmp):
            os.remove(tmp)
        if DOWNLOADS[job_id]["_cancel"]:
            raise RuntimeError("Cancelled")
        err = (p.stderr.read().strip().splitlines() or ["ffmpeg failed"])[-1]
        if "403" in err:
            err += " — the site refused it. Fill in the 'page it's from' box and try again."
        raise RuntimeError(err)
    os.rename(tmp, final)
    return final


def _download_ytdlp(job_id, url, page, name):
    ytdlp = find_tool("yt-dlp")
    if not ytdlp:
        raise RuntimeError("That's a page link — install yt-dlp (python3 -m pip install yt-dlp) "
                           "and restart the app.")
    template = (_safe_name(name) if name else "%(title).120B") + ".%(ext)s"
    if find_tool("ffmpeg"):  # best quality, video+audio merged into one mp4
        fmt = ["-f", "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b", "--merge-output-format", "mp4",
               "--ffmpeg-location", find_tool("ffmpeg")[0]]
    else:
        fmt = ["-f", "b[ext=mp4]/b"]
    cmd = ytdlp + ["--no-playlist", "--newline", "--no-overwrites", "--progress",
                   "--progress-template", "download:PCT %(progress._percent_str)s",
                   "--no-simulate", "--print", "before_dl:NAME %(filename)s",
                   "--print", "after_move:SAVED %(filepath)s",
                   "-P", DL_DIR, "-o", template] + fmt \
        + (["--referer", page] if page else []) + [url]
    if DOWNLOADS[job_id]["_cancel"]:
        raise RuntimeError("Cancelled")
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    _update(job_id, _proc=p, name="Finding video…")
    saved, last_err = None, ""
    for line in p.stdout:
        line = line.strip()
        if line.startswith("PCT"):
            m = re.search(r"([\d.]+)%", line)
            if m:
                _update(job_id, percent=min(99.0, float(m.group(1))))
        elif line.startswith("SAVED "):
            saved = line[6:]
        elif line.startswith("NAME "):
            _update(job_id, name=os.path.basename(line[5:]))
        elif line.startswith("ERROR:"):
            last_err = line[6:].strip()
    p.wait()
    if DOWNLOADS[job_id]["_cancel"]:
        raise RuntimeError("Cancelled")
    if p.returncode != 0 or not saved:
        raise RuntimeError(last_err or "yt-dlp couldn't download that link.")
    return saved


def cancel_download(job_id):
    with DL_LOCK:
        job = DOWNLOADS.get(job_id)
        if not job or job["status"] != "running":
            return
        job["_cancel"] = True
        p = job["_proc"]
    if p:
        p.terminate()


def list_downloads():
    with DL_LOCK:
        jobs = sorted(DOWNLOADS.values(), key=lambda j: -j["started"])
        return [{k: v for k, v in j.items() if not k.startswith("_")} for j in jobs]


VIDEO_EXT = (".mp4", ".m4v", ".mkv", ".mov", ".webm", ".avi", ".ts")


def pick_file():
    """Show the Mac's own file picker and return the chosen path (None if cancelled)."""
    if sys.platform != "darwin":
        raise RuntimeError("The file picker only works on a Mac. Paste the file's full path "
                           "into the Video link box instead.")
    out = subprocess.run(
        ["osascript", "-e", "activate", "-e",
         'POSIX path of (choose file with prompt "Choose a video to cast to your TV" '
         'default location (path to downloads folder))'],
        capture_output=True, text=True, timeout=900)
    if out.returncode != 0:
        if "-128" in out.stderr:  # user pressed Cancel
            return None
        raise RuntimeError("Couldn't open the file picker: " + out.stderr.strip())
    return out.stdout.strip() or None


def check_video(path):
    path = os.path.expanduser(path or "")
    if not os.path.isfile(path):
        raise RuntimeError("Couldn't find that file.")
    if not path.lower().endswith(VIDEO_EXT):
        raise RuntimeError("That doesn't look like a video file (try .mp4, .mkv, .mov or .m4v).")
    return path


def to_secs(t):
    try:
        h, m, s = t.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except Exception:
        return 0


def to_hms(secs):
    secs = max(0, int(secs))
    return f"{secs // 3600:d}:{secs % 3600 // 60:02d}:{secs % 60:02d}"


def status(dev):
    pos = soap(dev["avt"], "GetPositionInfo", "<InstanceID>0</InstanceID>")
    info = soap(dev["avt"], "GetTransportInfo", "<InstanceID>0</InstanceID>")
    out = {
        "state": info.findtext(".//CurrentTransportState") or "UNKNOWN",
        "position": to_secs(pos.findtext(".//RelTime") or "0:00:00"),
        "duration": to_secs(pos.findtext(".//TrackDuration") or "0:00:00"),
        "volume": None,
    }
    if dev.get("rc"):
        try:
            v = soap(dev["rc"], "GetVolume",
                     "<InstanceID>0</InstanceID><Channel>Master</Channel>")
            out["volume"] = int(v.findtext(".//CurrentVolume") or 0)
        except Exception:
            pass
    return out


def control(dev, action, value=None):
    iid = "<InstanceID>0</InstanceID>"
    if action == "play":
        soap(dev["avt"], "Play", iid + "<Speed>1</Speed>")
    elif action == "pause":
        soap(dev["avt"], "Pause", iid)
    elif action == "stop":
        soap(dev["avt"], "Stop", iid)
    elif action == "seek":
        soap(dev["avt"], "Seek", iid + f"<Unit>REL_TIME</Unit><Target>{to_hms(float(value))}</Target>")
    elif action == "volume":
        if not dev.get("rc"):
            raise RuntimeError("This TV doesn't allow volume control over the network.")
        soap(dev["rc"], "SetVolume",
             iid + f"<Channel>Master</Channel><DesiredVolume>{int(value)}</DesiredVolume>")
    else:
        raise RuntimeError(f"Unknown action {action}")


# ---------------------------------------------------------------- web UI
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TV Cast</title>
<style>
:root{--bg:#f4f3ef;--card:#fff;--ink:#1d1d1f;--mute:#6b6b70;--line:#e2e0da;--acc:#2f6fed;--bad:#c53030}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--card:#1e1e22;--ink:#ececf0;--mute:#9a9aa3;--line:#2e2e34;--acc:#5b8cff;--bad:#ff6b6b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,system-ui,Segoe UI,sans-serif}
main{max-width:560px;margin:0 auto;padding:28px 16px}
h1{font-size:22px;margin:0 0 4px}p.sub{color:var(--mute);margin:0 0 22px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:14px}
label{display:block;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mute);margin-bottom:6px}
select,input[type=text]{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:9px;background:var(--bg);color:var(--ink);font:inherit}
.row{display:flex;gap:8px;align-items:center}
button{padding:10px 14px;border:1px solid var(--line);border-radius:9px;background:var(--bg);color:var(--ink);font:inherit;cursor:pointer}
button.primary{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}
button:disabled{opacity:.5;cursor:default}
.controls{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
input[type=range]{width:100%;accent-color:var(--acc)}
.times{display:flex;justify-content:space-between;color:var(--mute);font-size:13px;font-variant-numeric:tabular-nums}
#msg{min-height:20px;font-size:14px;color:var(--mute)}#msg.err{color:var(--bad)}
.state{font-weight:600}
.two{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}
@media (max-width:480px){.two{grid-template-columns:1fr}}
.opt{text-transform:none;letter-spacing:0;opacity:.75}
.dl{padding:10px 0;border-top:1px solid var(--line)}.dl:first-child{border-top:0;padding-top:0}
.dl .top{display:flex;justify-content:space-between;gap:10px;font-size:14px}
.dl .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500}
.dl .st{color:var(--mute);white-space:nowrap;font-variant-numeric:tabular-nums}
.dl .st.err{color:var(--bad)}
.bar{height:6px;background:var(--line);border-radius:3px;margin-top:7px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--acc);width:0;transition:width .4s}
.bar.indet i{width:30%;animation:slide 1.2s infinite ease-in-out}
@keyframes slide{0%{transform:translateX(-100%)}100%{transform:translateX(340%)}}
.dl .controls{margin-top:8px}.dl button{padding:6px 10px;font-size:13px}
#nohelp{margin-top:12px}
.hint{margin:0 0 12px;font-size:14px;line-height:1.5;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:9px;padding:10px 12px}
.hint:empty{display:none}
.addlink{margin-top:8px;font-size:13px}.addlink a{color:var(--mute)}
.picked{color:var(--mute);font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.dl .why{font-size:13px;color:var(--bad);margin-top:4px;word-break:break-word}
</style></head><body><main>
<h1>TV Cast</h1>
<p class="sub">Send a video link to your TV.</p>

<div class="card">
  <label for="dev">TV</label>
  <div class="row">
    <select id="dev"><option>Searching…</option></select>
    <button id="scan" title="Search again">↻</button>
  </div>
  <div id="nohelp" hidden>
    <p class="hint" id="hint"></p>
    <label for="tvip">Add TV by IP address</label>
    <div class="row">
      <input id="tvip" type="text" inputmode="decimal" placeholder="e.g. 192.168.1.42">
      <button id="addip">Add</button>
    </div>
  </div>
  <div class="addlink"><a href="#" id="showadd">Add a TV by IP address</a></div>
</div>

<div class="card">
  <label for="url">Video link</label>
  <input id="url" type="text" placeholder="Paste the page the video plays on (or an .m3u8 / .mp4 link)">
  <div class="two">
    <div><label for="page">Page it's from <span class="opt">optional</span></label>
      <input id="page" type="text" placeholder="Only if the site refuses"></div>
    <div><label for="name">Save as <span class="opt">optional</span></label>
      <input id="name" type="text" placeholder="File name for downloads"></div>
  </div>
  <div class="controls">
    <button class="primary" id="cast">Cast to TV</button>
    <button id="dl">⬇ Download</button>
  </div>
</div>

<div class="card" id="dlcard" hidden>
  <label>Downloads <span class="opt" id="dlfolder"></span></label>
  <div id="dllist"></div>
</div>

<div class="card">
  <label>Video file on this computer</label>
  <div class="row">
    <button id="pick">Choose file…</button>
    <span class="picked" id="picked">No file chosen</span>
  </div>
  <div class="controls"><button class="primary" id="castfile" disabled>Cast file to TV</button></div>
</div>

<div class="card">
  <label>Now playing — <span class="state" id="state">—</span></label>
  <input type="range" id="seek" min="0" max="0" value="0">
  <div class="times"><span id="pos">0:00</span><span id="dur">0:00</span></div>
  <div class="controls">
    <button data-a="play">▶ Play</button><button data-a="pause">❚❚ Pause</button>
    <button data-a="stop">■ Stop</button>
    <button data-skip="-10">−10s</button><button data-skip="30">+30s</button>
  </div>
  <div style="margin-top:14px"><label for="vol">Volume <span id="volv"></span></label>
  <input type="range" id="vol" min="0" max="100" value="0"></div>
</div>
<div id="msg"></div>
</main>
<script>
const $=id=>document.getElementById(id);
let dragging=false,last={position:0,duration:0};
const fmt=s=>{s=Math.max(0,Math.floor(s));const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=String(s%60).padStart(2,'0');return h?`${h}:${String(m).padStart(2,'0')}:${x}`:`${m}:${x}`};
function say(t,err){$('msg').textContent=t||'';$('msg').className=err?'err':''}
async function api(path,body){
  const r=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});
  const j=await r.json(); if(!r.ok) throw new Error(j.error||'Request failed'); return j;
}
function showDevices(j,pick){
  const opts=j.devices.map(d=>`<option value="${encodeURIComponent(d.id)}">${esc(d.name)}${d.model?' — '+esc(d.model):''}</option>`).join('');
  $('dev').innerHTML=opts||'<option value="">No TVs found</option>';
  try{const saved=pick?encodeURIComponent(pick):localStorage.getItem('tvcast-dev');
    if(saved&&[...$('dev').options].some(o=>o.value===saved)){$('dev').value=saved;localStorage.setItem('tvcast-dev',saved)}}catch(e){}
  $('nohelp').hidden=!!j.devices.length; $('hint').textContent=j.hint||'';
  document.querySelector('.addlink').hidden=!j.devices.length;
  say(j.devices.length?'':'No TVs found.',!j.devices.length);
}
async function scan(){
  $('dev').innerHTML='<option>Searching…</option>';say('Looking for TVs on your network…');
  try{showDevices(await api('/api/devices?refresh=1'))}catch(e){say(e.message,true)}
}
$('showadd').onclick=e=>{e.preventDefault();$('nohelp').hidden=false;$('hint').textContent='';document.querySelector('.addlink').hidden=true;$('tvip').focus()};
$('addip').onclick=async()=>{
  const ip=$('tvip').value.trim(); if(!ip)return;
  $('addip').disabled=true;say('Checking '+ip+'…');
  try{const j=await api('/api/devices/add',{ip});showDevices(j,j.added);$('nohelp').hidden=true;say('Added — it will be remembered next time.')}
  catch(e){say(e.message,true)}
  $('addip').disabled=false;
};
$('tvip').onkeydown=e=>{if(e.key==='Enter')$('addip').click()};
const dev=()=>decodeURIComponent($('dev').value||'');
$('dev').onchange=()=>{try{localStorage.setItem('tvcast-dev',$('dev').value)}catch(e){}};
$('scan').onclick=scan;
$('cast').onclick=async()=>{
  const url=$('url').value.trim(); if(!url||!dev())return say('Pick a TV and paste a link first.',true);
  $('cast').disabled=true;say('Looking for the video on the page… If a browser window pops up, press play in it.');
  try{const j=await api('/api/cast',{device:dev(),url,page:$('page').value});say(j.note||'Playing on TV.')}catch(e){say(e.message,true)}
  $('cast').disabled=false;
};
$('dl').onclick=async()=>{
  const url=$('url').value.trim(); if(!url)return say('Paste a link first.',true);
  try{await api('/api/download',{url,page:$('page').value,name:$('name').value});
    say('Download started.');$('name').value='';pollDl()}catch(e){say(e.message,true)}
};
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
let dlSig='';
async function pollDl(){
  let j;try{j=await api('/api/downloads')}catch(e){return}
  $('dlcard').hidden=!j.downloads.length;
  $('dlfolder').textContent='— saved to '+j.folder.replace(/^\/Users\/[^/]+/,'~');
  const sig=JSON.stringify(j.downloads); if(sig===dlSig)return; dlSig=sig;
  $('dllist').innerHTML=j.downloads.map(d=>{
    const run=d.status==='running',pct=d.percent;
    const st=run?(pct!=null?pct.toFixed(0)+'%':'working…'):d.status==='done'?'Done':d.status==='cancelled'?'Cancelled':'Failed';
    const btns=run?`<button data-x="cancel" data-id="${d.id}">Cancel</button>`
      :d.status==='done'?`<button class="primary" data-x="cast" data-id="${d.id}">Cast to TV</button><button data-x="reveal" data-id="${d.id}">Show in Finder</button>`:'';
    return `<div class="dl"><div class="top"><span class="nm" title="${esc(d.name)}">${esc(d.name)}</span><span class="st${d.status==='error'?' err':''}">${st}</span></div>`+
      (run?`<div class="bar${pct==null?' indet':''}"><i style="width:${pct||0}%"></i></div>`:'')+
      (d.error?`<div class="why">${esc(d.error)}</div>`:'')+
      (btns?`<div class="controls">${btns}</div>`:'')+`</div>`}).join('');
}
$('dllist').onclick=async e=>{
  const b=e.target.closest('button[data-x]');if(!b)return;
  const x=b.dataset.x,id=b.dataset.id;
  if(x==='cast'&&!dev())return say('Pick a TV first.',true);
  b.disabled=true;if(x==='cast')say('Sending to your TV…');
  try{const r=await api('/api/download/'+x,{id,device:dev()});if(x==='cast')say(r.note||'Playing on TV.')}
  catch(err){say(err.message,true)}
  b.disabled=false;pollDl();
};
pollDl();setInterval(pollDl,1000);
const size=b=>b>=1e9?(b/1e9).toFixed(1)+' GB':(b/1e6).toFixed(0)+' MB';
let pickedPath=null;
try{const p=JSON.parse(localStorage.getItem('tvcast-file')||'null');if(p){pickedPath=p.path;$('picked').textContent=p.label;$('castfile').disabled=false}}catch(e){}
$('pick').onclick=async()=>{
  $('pick').disabled=true;say('Choose a video in the window that just opened…');
  try{const r=await api('/api/file/pick',{});
    if(r.path){pickedPath=r.path;const label=r.name+' · '+size(r.size);$('picked').textContent=label;$('picked').title=r.path;
      $('castfile').disabled=false;say('');try{localStorage.setItem('tvcast-file',JSON.stringify({path:r.path,label}))}catch(e){}}
    else say('');
  }catch(e){say(e.message,true)}
  $('pick').disabled=false;
};
$('castfile').onclick=async()=>{
  if(!pickedPath)return; if(!dev())return say('Pick a TV first.',true);
  $('castfile').disabled=true;say('Sending '+$('picked').textContent.split(' · ')[0]+' to your TV…');
  try{const r=await api('/api/file/cast',{path:pickedPath,device:dev()});say(r.note||'Playing on TV.')}catch(e){say(e.message,true)}
  $('castfile').disabled=false;
};
$('url').onkeydown=e=>{if(e.key==='Enter')$('cast').click()};
document.querySelectorAll('[data-a]').forEach(b=>b.onclick=()=>ctl(b.dataset.a));
document.querySelectorAll('[data-skip]').forEach(b=>b.onclick=()=>ctl('seek',last.position+ +b.dataset.skip));
async function ctl(action,value){if(!dev())return;try{await api('/api/control',{device:dev(),action,value});say('');poll()}catch(e){say(e.message,true)}}
$('seek').oninput=()=>{dragging=true;$('pos').textContent=fmt($('seek').value)};
$('seek').onchange=()=>{dragging=false;ctl('seek',+$('seek').value)};
$('vol').oninput=()=>$('volv').textContent=$('vol').value;
$('vol').onchange=()=>ctl('volume',+$('vol').value);
async function poll(){
  if(!dev())return;
  try{const s=await api('/api/status?device='+encodeURIComponent(dev()));last=s;
    $('state').textContent=s.state.replace(/_/g,' ').toLowerCase();
    if(!dragging){$('seek').max=s.duration||0;$('seek').value=s.position;$('pos').textContent=fmt(s.position)}
    $('dur').textContent=fmt(s.duration);
    if(s.volume!==null&&document.activeElement!==$('vol')){$('vol').value=s.volume;$('volv').textContent=s.volume}
  }catch(e){$('state').textContent='—'}
}
scan();setInterval(poll,2000);
</script></body></html>"""


def devices_payload():
    with DEVICES_LOCK:
        devs = [{"id": d["id"], "name": d["name"], "model": d["model"]} for d in DEVICES.values()]
    hint = ""
    if not devs:
        if LAST_SCAN["responders"] == 0:
            hint = ("Nothing on your network answered — not even your router — so your Mac is "
                    "probably blocking the app. Open System Settings → Privacy & Security → "
                    "Local Network, turn on Terminal, then quit Terminal, reopen it and run the "
                    "app again. A VPN can also cause this; try turning it off.")
        else:
            n = LAST_SCAN["responders"]
            hint = (f"{n} device{'s' if n != 1 else ''} on your network answered, but none was a TV "
                    "that accepts casting. Make sure the TV's media-sharing setting is on (often "
                    "called Screen Share, Media Sharing or DLNA). You can also add the TV by its "
                    "IP address below — it's in the TV's network settings.")
    return {"devices": devs, "hint": hint}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _dev(self, dev_id):
        with DEVICES_LOCK:
            d = DEVICES.get(dev_id)
        if not d:
            raise RuntimeError("That TV isn't in the list any more — hit ↻ to search again.")
        return d

    def _local_only(self):
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            self._json(403, {"error": "Forbidden"})
            return False
        return True

    def do_HEAD(self):
        if self.path.startswith("/stream/"):
            self._relay(head=True)
        else:
            self.send_response(404)
            self.end_headers()

    def _relay(self, head=False):
        """Fetch the video from the site (with the right headers) and pass it to the TV."""
        token = self.path.split("/stream/", 1)[1].split("?")[0].rsplit(".", 1)[0]
        s = STREAMS.get(token)
        if not s:
            self.send_response(404)
            self.end_headers()
            return
        s["hits"] += 1
        dlna = {"transferMode.dlna.org": "Streaming",
                "contentFeatures.dlna.org": s["flags"]}

        if s.get("path"):  # a file on this computer
            try:
                size = os.path.getsize(s["path"])
            except OSError:
                self.send_response(404)
                self.end_headers()
                return
            start, end = 0, size - 1
            m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                else:  # "bytes=-N" means the last N bytes
                    start = max(0, size - int(m.group(2)))
                end = min(end, size - 1)
                if start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", s["mime"])
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            for k, v in dlna.items():
                self.send_header(k, v)
            self.end_headers()
            if head:
                return
            try:
                with open(s["path"], "rb") as f:
                    f.seek(start)
                    left = end - start + 1
                    while left > 0:
                        chunk = f.read(min(256 * 1024, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        if s.get("feed"):  # an .m3u8 being pulled into one continuous stream
            feed = s["feed"]
            self.send_response(200)
            self.send_header("Content-Type", s["mime"])
            for k, v in dlna.items():
                self.send_header(k, v)
            self.end_headers()
            if head:
                return
            try:
                with open(feed.path, "rb") as f:
                    while not feed.stopped:
                        chunk = f.read(256 * 1024)
                        if chunk:
                            self.wfile.write(chunk)
                            continue
                        if feed.done:
                            rest = f.read()
                            if rest:
                                self.wfile.write(rest)
                            break
                        time.sleep(0.2)  # wait for ffmpeg to pull more
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return

        req = urllib.request.Request(s["url"], headers=s["headers"])
        if self.headers.get("Range"):
            req.add_header("Range", self.headers["Range"])
        try:
            up = urllib.request.urlopen(req, timeout=20)
        except urllib.error.HTTPError as e:
            if e.code != 416:
                s["upstream_error"] = f"HTTP {e.code} {e.reason}"
            self.send_response(e.code)
            self.end_headers()
            return
        except Exception as e:
            s["upstream_error"] = str(e)
            self.send_response(502)
            self.end_headers()
            return
        with up:
            self.send_response(up.status)
            self.send_header("Content-Type", s["mime"])
            for h in ("Content-Length", "Content-Range", "Accept-Ranges"):
                if up.headers.get(h):
                    self.send_header(h, up.headers[h])
            for k, v in dlna.items():
                self.send_header(k, v)
            self.end_headers()
            if head:
                return
            try:
                while True:
                    chunk = up.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # TV seeked or stopped; it'll open a new request

    def do_GET(self):
        if self.path.startswith("/stream/"):
            return self._relay()
        if not self._local_only():
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/":
                data = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif u.path == "/api/devices":
                if q.get("refresh") or not DEVICES:
                    discover()
                self._json(200, devices_payload())
            elif u.path == "/api/status":
                self._json(200, status(self._dev(q.get("device", [""])[0])))
            elif u.path == "/api/downloads":
                self._json(200, {"downloads": list_downloads(), "folder": DL_DIR})
            else:
                self._json(404, {"error": "Not found"})
        except Exception as e:
            self._json(500, {"error": str(e)})

    def do_POST(self):
        if not self._local_only():
            return
        # Only this app's own page may send commands (stops other websites poking it).
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._json(415, {"error": "Expected JSON"})
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
            return self._json(403, {"error": "Forbidden"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            page = (body.get("page") or "").strip() or None
            if self.path == "/api/devices/add":
                ip = (body.get("ip") or "").strip()
                if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip):
                    raise RuntimeError("That doesn't look like an IP address (e.g. 192.168.1.42).")
                discover(extra_ips=[ip])
                payload = devices_payload()
                match = [d for d in payload["devices"] if urlparse(d["id"]).hostname == ip]
                if not match:
                    raise RuntimeError(f"Nothing at {ip} answered as a TV. Double-check the address "
                                       "in the TV's network settings, and that its media-sharing / "
                                       "DLNA option is on.")
                payload["added"] = match[0]["id"]
                self._json(200, payload)
            elif self.path == "/api/cast":
                note = cast(self._dev(body.get("device", "")), body["url"].strip(), page)
                self._json(200, {"ok": True, "note": note})
            elif self.path == "/api/control":
                control(self._dev(body.get("device", "")), body["action"], body.get("value"))
                self._json(200, {"ok": True})
            elif self.path == "/api/download":
                job = start_download(body["url"].strip(), page,
                                     (body.get("name") or "").strip() or None)
                self._json(200, {"ok": True, "id": job})
            elif self.path == "/api/file/pick":
                path = pick_file()
                if path:
                    check_video(path)
                self._json(200, {"path": path, "name": path and os.path.basename(path),
                                 "size": path and os.path.getsize(path)})
            elif self.path == "/api/file/cast":
                note = cast_file(self._dev(body.get("device", "")), body.get("path"))
                self._json(200, {"ok": True, "note": note})
            elif self.path == "/api/download/cancel":
                cancel_download(body["id"])
                self._json(200, {"ok": True})
            elif self.path in ("/api/download/cast", "/api/download/reveal"):
                job = DOWNLOADS.get(body.get("id"))
                if not job or not job["path"]:
                    raise RuntimeError("That download isn't finished.")
                if self.path.endswith("reveal"):
                    subprocess.run(["open", "-R", job["path"]])
                    self._json(200, {"ok": True})
                else:
                    note = cast_file(self._dev(body.get("device", "")), job["path"])
                    self._json(200, {"ok": True, "note": note})
            else:
                self._json(404, {"error": "Not found"})
        except Exception as e:
            self._json(500, {"error": str(e)})


def main():
    # Listen on the network so the TV can pull the video; the controls stay local-only.
    global PORT
    ThreadingHTTPServer.allow_reuse_address = True
    srv = None
    for port in range(PORT, PORT + 10):
        try:
            srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
            break
        except OSError:
            if port == PORT:
                print(f"Port {PORT} is busy (is TV Cast already open in another Terminal window?). "
                      "Using the next free port instead.")
    if srv is None:
        sys.exit("Couldn't find a free port. Close other copies of TV Cast and try again.")
    PORT = srv.server_address[1]
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{PORT}"
    print(f"TV Cast running at {url}  (Ctrl+C to quit)")
    print("Page scanner:", "ready" if scanner_available() else
          "not installed (run: python3 -m pip install playwright && python3 -m playwright install chromium)")
    print("yt-dlp:", "found" if find_tool("yt-dlp") else "not installed (page links won't work)")
    print("ffmpeg:", "found" if find_tool("ffmpeg") else "not installed (m3u8 streams won't cast or download — brew install ffmpeg)")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nBye!")


if __name__ == "__main__":
    main()
