#!/usr/bin/env python3
"""Amizon Watch Agent - checks your Amizone class attendance dots on a schedule
and notifies you when one turns red (absent) or blue (unmarked).

It runs in the background on your own computer (or a free cloud runner), logs in
to Amizone with YOUR account, reads the same class-schedule data the Home page
uses, and compares it with the last check. Nothing is shared with anyone.

Commands:  setup | run | once | history | export | debug | test-notify | autostart
"""
import argparse
import base64
import csv
import datetime as dt
import getpass
import json
import os
import platform
import random
import re
import secrets
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

BASE = os.environ.get("AMIZONE_BASE", "https://s.amizone.net").rstrip("/")
DATA_DIR = Path(os.environ.get("AMIZON_WATCH_HOME", Path.home() / ".amizon-watch"))
CONFIG = DATA_DIR / "config.json"
DB_PATH = DATA_DIR / "watch.db"
LOG_PATH = DATA_DIR / "agent.log"
KEYRING_SERVICE = "amizon-watch"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))  # Amizone times are India time

DEFAULTS = {
    "username": "",
    "interval_minutes": 30,   # how often to check (minimum 10, please be gentle)
    "lookback_days": 7,       # how many past days to keep an eye on
    "grace_minutes": 120,     # wait this long after a class ends before calling a blue dot "unmarked"
    "ntfy_topic": "",         # phone push via the free ntfy app (optional)
    "ntfy_server": "https://ntfy.sh",
    "desktop_notifications": True,
}


class LoginError(Exception):
    pass


class FetchError(Exception):
    pass


# ----------------------------------------------------------------- utilities
def log(msg):
    line = f"{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 1_000_000:
            LOG_PATH.write_text("")
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG.exists():
        cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
    if os.environ.get("AMIZONE_USER"):
        cfg["username"] = os.environ["AMIZONE_USER"]
    if os.environ.get("NTFY_TOPIC"):
        cfg["ntfy_topic"] = os.environ["NTFY_TOPIC"]
    if os.environ.get("NTFY_SERVER"):
        cfg["ntfy_server"] = os.environ["NTFY_SERVER"]
    if os.environ.get("AMIZON_DESKTOP") == "0":
        cfg["desktop_notifications"] = False
    return cfg


SECRET_FILE = DATA_DIR / "secret"  # fallback store, used only when there is no OS keychain (e.g. Termux)


def _fallback_get(username):
    if not SECRET_FILE.exists():
        return None
    try:
        data = json.loads(base64.b64decode(SECRET_FILE.read_bytes()))
        return data.get(username)
    except Exception:
        return None


def _fallback_set(username, password):
    data = {}
    if SECRET_FILE.exists():
        try:
            data = json.loads(base64.b64decode(SECRET_FILE.read_bytes()))
        except Exception:
            data = {}
    data[username] = password
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SECRET_FILE.write_bytes(base64.b64encode(json.dumps(data).encode()))
    try:
        os.chmod(SECRET_FILE, 0o600)
    except OSError:
        pass


def keychain_available():
    try:
        import keyring
        backend = keyring.get_keyring()
        return "fail" not in backend.__class__.__name__.lower() and "chainer" not in backend.__class__.__module__.lower()
    except Exception:
        return False


def save_secret(key, value):
    """Store in the OS keychain if there is one, else in the fallback file. Returns where."""
    try:
        import keyring
        keyring.set_password(KEYRING_SERVICE, key, value)
        if keychain_available():
            return "keychain"
    except Exception:
        pass
    _fallback_set(key, value)
    return "file"


def load_secret(key):
    try:
        import keyring
        v = keyring.get_password(KEYRING_SERVICE, key)
        if v:
            return v
    except Exception:
        pass
    return _fallback_get(key)


def get_password(username):
    return os.environ.get("AMIZONE_PASSWORD") or load_secret(username)


def get_cookie(username):
    return os.environ.get("AMIZONE_COOKIE") or load_secret(username + "#cookie")


def fmt_date(iso):
    try:
        return dt.date.fromisoformat(iso).strftime("%d %b %Y")
    except ValueError:
        return iso


# ------------------------------------------------------------- colour -> state
def _rgb_from_string(s):
    m = re.fullmatch(r"#?([0-9a-f]{6})", s)
    if m:
        h = m.group(1)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    m = re.fullmatch(r"#?([0-9a-f]{3})", s)
    if m:
        return tuple(int(c * 2, 16) for c in m.group(1))
    m = re.fullmatch(r"rgba?\(([^)]+)\)", s)
    if m:
        parts = [float(p) for p in m.group(1).split(",")[:3]]
        if len(parts) == 3:
            return tuple(parts)
    return None


def color_to_state(raw):
    """red -> absent, green -> present, blue -> unmarked, anything else -> unknown."""
    if not raw:
        return "unknown"
    s = str(raw).strip().lower()
    if re.fullmatch(r"[a-z]+", s):  # colour names such as "red", "lightgreen"
        for word, state in (("red", "absent"), ("green", "present"), ("blue", "unmarked")):
            if word in s:
                return state
        return "unknown"
    rgb = _rgb_from_string(s)
    if not rgb:
        return "unknown"
    r, g, b = (c / 255 for c in rgb)
    mx, mn = max(r, g, b), min(r, g, b)
    l = (mx + mn) / 2
    if mx == mn:
        return "unknown"
    d = mx - mn
    sat = d / (1 - abs(2 * l - 1)) if 0 < l < 1 else 0
    if mx == r:
        h = ((g - b) / d) % 6
    elif mx == g:
        h = (b - r) / d + 2
    else:
        h = (r - g) / d + 4
    h *= 60
    if sat < 0.35 or l < 0.2 or l > 0.85:
        return "unknown"
    if h < 20 or h > 340:
        return "absent"
    if 75 <= h <= 165:
        return "present"
    if 190 <= h <= 260:
        return "unmarked"
    return "unknown"


# ------------------------------------------------------------- Amizone client
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 AmizonWatch/1.0")
ERR_WORDS = re.compile(
    r"invalid|incorrect|wrong|captcha|locked|blocked|expired|too many|not allowed|try again|denied|"
    r"failed|unauthori[sz]ed|disabled", re.I)
TEXT_TYPES = ("text", "email", "tel", "number", "")


class CaptchaError(LoginError):
    pass


class _PageParser(HTMLParser):
    """Reads a page: its forms, input boxes, images and visible text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms, self.loose, self.imgs, self.text = [], [], [], []
        self._cur, self._skip = None, 0

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "form":
            self._cur = {"action": a.get("action", ""), "method": a.get("method", "get").lower(), "inputs": []}
            self.forms.append(self._cur)
        elif tag == "input":
            item = {"name": a.get("name", ""), "id": a.get("id", ""),
                    "type": (a.get("type") or "text").lower(), "value": a.get("value", "")}
            (self._cur["inputs"] if self._cur is not None else self.loose).append(item)
        elif tag == "img":
            self.imgs.append(a.get("src", ""))
        elif tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag == "form":
            self._cur = None
        elif tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.text.append(data.strip())


def parse_page(html):
    p = _PageParser()
    try:
        p.feed(html or "")
    except Exception:
        pass
    low = (html or "").lower()
    p.widget = any(w in low for w in ("g-recaptcha", "h-captcha", "cf-turnstile", "grecaptcha", "hcaptcha.com"))
    return p


def has_password_field(page):
    return any(i["type"] == "password" for f in page.forms for i in f["inputs"]) or \
        any(i["type"] == "password" for i in page.loose)


def pick_login_form(page):
    cands = [f for f in page.forms if any(i["type"] == "password" for i in f["inputs"])]
    for f in cands:
        if any(i["name"].lower() == "_username" for i in f["inputs"]):
            return f
    return cands[0] if cands else None


def captcha_in_form(page, form):
    for i in form["inputs"]:
        if "captcha" in (i["name"] + i["id"]).lower():
            return True
    if any("captcha" in src.lower() for src in page.imgs):
        return True
    return page.widget


def build_login_payload(form, username, password):
    """Fill the username and password boxes, keep every hidden field the page gave us."""
    inputs = form["inputs"]
    pw_i = next(i for i in inputs if i["type"] == "password")
    idx = inputs.index(pw_i)
    before = [i for i in inputs[:idx] if i["type"] in TEXT_TYPES and i["name"]]
    after = [i for i in inputs[idx + 1:] if i["type"] in TEXT_TYPES and i["name"]]
    pref = re.compile(r"user|login|enrol|roll|^id$", re.I)
    user_i = next((i for i in reversed(before) if pref.search(i["name"])), None) \
        or (before[-1] if before else None) or (after[0] if after else None)
    if not user_i:
        raise LoginError("Found a password box but no username box on the login page.")
    payload = {i["name"]: i["value"] for i in inputs if i["type"] == "hidden" and i["name"]}
    payload[user_i["name"]] = username
    payload[pw_i["name"]] = password
    return payload, user_i["name"], pw_i["name"]


def error_snippets(page, limit=3):
    out = []
    for t in page.text:
        if len(t) <= 160 and ERR_WORDS.search(t) and t not in out:
            out.append(t)
        if len(out) >= limit:
            break
    return out


class Amizone:
    def __init__(self, username, password):
        self.username, self.password = username, password
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"})
        self.logged_in = False
        self.last_login = 0.0
        self.trace = []  # plain-English steps, shown by the 'diagnose' command
        self.page = None
        self.form = None

    def _note(self, msg):
        self.trace.append(msg)

    def _is_logged_in(self):
        r = self.s.get(BASE + "/Home", timeout=30, allow_redirects=True)
        page = parse_page(r.text)
        path = urlparse(r.url).path.rstrip("/").lower()
        ok = r.status_code == 200 and path not in ("", "/login") and not has_password_field(page)
        self._note(f"opened /Home -> HTTP {r.status_code}, ended at {r.url}, "
                   f"login box still showing: {has_password_field(page)}")
        return ok

    def login_with_password(self):
        r = self.s.get(BASE + "/", timeout=30)
        self._note(f"opened login page -> HTTP {r.status_code}")
        if r.status_code >= 400:
            raise LoginError(f"Amizone's login page answered HTTP {r.status_code}. "
                             "It may be down, or blocking this computer/network (common for cloud servers).")
        page = parse_page(r.text)
        self.page = page
        form = pick_login_form(page)
        self.form = form
        if not form:
            raise LoginError("The login page has no form with a password box. Amizone probably logs in "
                             "through JavaScript now, which this tool cannot copy. Use cookie mode: "
                             "python amizon_watch.py cookie")
        if captcha_in_form(page, form):
            raise CaptchaError("The Amizone login page now shows a captcha ('I am not a robot'). "
                               "A program cannot solve it. Use cookie mode instead: python amizon_watch.py cookie")
        payload, ufield, pfield = build_login_payload(form, self.username, self.password)
        self._note(f"filled username box '{ufield}', password box '{pfield}', "
                   f"and {len(payload) - 2} hidden field(s) from the page")
        post_url = urljoin(r.url, form["action"] or r.url)
        parts = urlparse(post_url)
        r2 = self.s.post(post_url, data=payload, timeout=30, allow_redirects=True,
                         headers={"Referer": r.url, "Origin": f"{parts.scheme}://{parts.netloc}"})
        self._note(f"sent login to {post_url} -> HTTP {r2.status_code}, ended at {r2.url}")
        if self._is_logged_in():
            self.logged_in = True
            return
        msgs = error_snippets(parse_page(r2.text))
        if any("captcha" in m.lower() for m in msgs):
            raise CaptchaError("Amizone is asking for a captcha. Use cookie mode: python amizon_watch.py cookie")
        said = f" The page said: {' / '.join(m.rstrip('. ') for m in msgs)}." if msgs else ""
        raise LoginError("Amizone did not accept the login." + said +
                         " First check that the same enrollment number and password work in your browser, "
                         "and that you are not on the Parent tab. Then run: python amizon_watch.py diagnose")

    def login_with_cookie(self, cookie_text):
        host = urlparse(BASE).hostname
        text = re.sub(r"^\s*cookie\s*:\s*", "", cookie_text.strip(), flags=re.I)
        n = 0
        for part in text.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                if k:
                    self.s.cookies.set(k, v, domain=host, path="/")
                    n += 1
        self._note(f"loaded {n} cookie(s) from your saved browser session")
        if not n or not self._is_logged_in():
            raise LoginError("The saved browser session is no longer accepted (it expired). Log in to Amizone in "
                             "your browser, then run: python amizon_watch.py cookie")
        self.logged_in = True

    def login(self):
        if self.last_login and time.time() - self.last_login < 120:
            raise LoginError("Not logging in again so soon, to avoid locking your account.")
        self.last_login = time.time()
        cookie = get_cookie(self.username)
        if self.password:
            try:
                return self.login_with_password()
            except LoginError as e:
                if not cookie:
                    raise
                log(f"password login failed ({e}); trying the saved browser session")
        if cookie:
            return self.login_with_cookie(cookie)
        raise LoginError("No password or browser session saved. Run: python amizon_watch.py setup")

    def _get_events(self, start, end):
        if not self.logged_in:
            self.login()
        url = BASE + "/Calendar/home/GetDiaryEvents"
        params = {"start": start.isoformat(), "end": end.isoformat()}
        for attempt in (1, 2):
            r = self.s.get(url, params=params, headers={"X-Requested-With": "XMLHttpRequest"}, timeout=30)
            try:
                data = r.json()
            except ValueError:
                data = None
            if isinstance(data, list):
                return data
            if attempt == 1:  # session probably expired
                self.logged_in = False
                self.login()
        raise FetchError("Amizone did not return the class schedule (site down, or its format changed).")

    def diary_events(self, start, end):
        try:
            return self._get_events(start, end)
        except FetchError:
            out, d = [], start  # fall back to one request per day
            while d < end:
                out += self._get_events(d, d + dt.timedelta(days=1))
                d += dt.timedelta(days=1)
            return out


def _parse_dt(value):
    if not value:
        return None
    value = str(value).replace("T", " ")[:19]
    try:
        return dt.datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
    except ValueError:
        try:
            return dt.datetime.strptime(value[:16], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        except ValueError:
            return None


def parse_events(raw):
    out = {}
    for e in raw:
        if not isinstance(e, dict):
            continue
        if e.get("sType") and e["sType"] != "C":  # C = course; skip events and holidays
            continue
        start, end = _parse_dt(e.get("start")), _parse_dt(e.get("end"))
        if not start or not end:
            continue
        subject = (e.get("title") or e.get("CourseName") or "Unknown subject").strip()
        code = e.get("CourseCode") or subject
        key = f"{start:%Y-%m-%d %H:%M}|{code}"
        out[key] = {
            "key": key,
            "date": start.date().isoformat(),
            "time": f"{start:%H:%M} - {end:%H:%M}",
            "subject": subject,
            "end": end,
            "raw_color": e.get("AttndColor"),
            "state": color_to_state(e.get("AttndColor")),
        }
    return list(out.values())


# ------------------------------------------------------------------ database
def open_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute(
        "CREATE TABLE IF NOT EXISTS slots(key TEXT PRIMARY KEY, subject TEXT, class_date TEXT,"
        " class_time TEXT, state TEXT, alerted TEXT, updated REAL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, alerted_at TEXT,"
        " class_date TEXT, class_time TEXT, subject TEXT, kind TEXT, detail TEXT)"
    )
    return db


def decide(prev, ev, now, grace_minutes):
    """Return (kind, detail) if this class needs an alert, else (None, None)."""
    st = ev["state"]
    prev_state = prev["state"] if prev else None
    prev_alerted = prev["alerted"] if prev else None
    if st == "absent" and prev_alerted != "absent":
        if prev_state in ("present", "unmarked"):
            return "absent", f"changed from {prev_state} to absent"
        return "absent", "marked absent"
    if st == "unmarked":
        if prev_state in ("present", "absent") and prev_alerted != "unmarked":
            return "unmarked", f"was {prev_state}, now unmarked"
        if now >= ev["end"] + dt.timedelta(minutes=grace_minutes) and prev_alerted != "unmarked":
            return "unmarked", "attendance not marked yet"
    return None, None


# --------------------------------------------------------------- notifications
def _q_ps(s):
    return s.replace("'", "''")


def _desktop(title, msg, urgent):
    system = platform.system()
    if system == "Windows":
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
            "$n = New-Object System.Windows.Forms.NotifyIcon; "
            "$n.Icon = [System.Drawing.SystemIcons]::Warning; $n.Visible = $true; "
            f"$n.ShowBalloonTip(15000, '{_q_ps(title)}', '{_q_ps(msg)}', [System.Windows.Forms.ToolTipIcon]::Warning); "
            "Start-Sleep -Seconds 15; $n.Dispose()"
        )
        enc = base64.b64encode(ps.encode("utf-16-le")).decode()
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-EncodedCommand", enc],
            creationflags=0x08000000,
        )
    elif system == "Darwin":
        esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(["osascript", "-e", f'display notification "{esc(msg)}" with title "{esc(title)}"'], timeout=15)
    else:
        subprocess.run(["notify-send", "-u", "critical" if urgent else "normal", title, msg], timeout=15)


def notify(cfg, kind, title, message):
    """kind: absent (red) | unmarked (yellow) | info | error"""
    style = {
        "absent": ("🔴", "red_circle", 4),
        "unmarked": ("🟡", "yellow_circle", 3),
        "info": ("✅", "white_check_mark", 2),
        "error": ("⚠️", "warning", 4),
    }[kind]
    if cfg.get("desktop_notifications"):
        try:
            _desktop(f"{style[0]} {title}", message, kind in ("absent", "error"))
        except Exception as e:
            log(f"desktop notification failed: {e}")
    if cfg.get("ntfy_topic"):
        try:
            requests.post(
                cfg["ntfy_server"].rstrip("/") + "/",
                json={"topic": cfg["ntfy_topic"], "title": title, "message": message,
                      "priority": style[2], "tags": [style[1]], "click": BASE + "/Home"},
                timeout=15,
            )
        except Exception as e:
            log(f"phone notification failed: {e}")


def notify_alert(cfg, a):
    title = "Marked ABSENT" if a["kind"] == "absent" else "Attendance not marked"
    notify(cfg, a["kind"], title, f"{a['subject']}\n{a['time']}, {fmt_date(a['date'])}")


# ------------------------------------------------------------------ the check
def check_once(cfg, client):
    now = dt.datetime.now(IST)
    today = now.date()
    raw = client.diary_events(today - dt.timedelta(days=int(cfg["lookback_days"])), today + dt.timedelta(days=1))
    events = parse_events(raw)
    db = open_db()
    first_run = db.execute("SELECT COUNT(*) FROM slots").fetchone()[0] == 0
    counts, alerts = Counter(), []
    for ev in events:
        row = db.execute("SELECT state, alerted FROM slots WHERE key=?", (ev["key"],)).fetchone()
        prev = {"state": row[0], "alerted": row[1]} if row else None
        kind, detail = decide(prev, ev, now, int(cfg["grace_minutes"]))
        counts["upcoming" if ev["state"] == "unmarked" and ev["end"] > now else ev["state"]] += 1
        if kind:
            alerted = kind
        elif ev["state"] == "present":
            alerted = None
        else:
            alerted = prev["alerted"] if prev else None
        db.execute(
            "INSERT OR REPLACE INTO slots VALUES(?,?,?,?,?,?,?)",
            (ev["key"], ev["subject"], ev["date"], ev["time"], ev["state"], alerted, time.time()),
        )
        if kind:
            a = {"kind": kind, "detail": detail, "subject": ev["subject"], "date": ev["date"], "time": ev["time"]}
            alerts.append(a)
            db.execute(
                "INSERT INTO alerts(alerted_at,class_date,class_time,subject,kind,detail) VALUES(?,?,?,?,?,?)",
                (now.isoformat(timespec="seconds"), a["date"], a["time"], a["subject"], kind, detail),
            )
    db.execute("DELETE FROM slots WHERE updated < ?", (time.time() - 90 * 86400,))
    db.commit()
    db.close()

    log(f"checked {len(events)} classes: {counts.get('absent', 0)} absent, "
        f"{counts.get('unmarked', 0)} unmarked, {counts.get('present', 0)} present, "
        f"{counts.get('upcoming', 0)} upcoming")
    if counts.get("unknown"):
        colours = sorted({str(e["raw_color"]) for e in events if e["state"] == "unknown"})
        log(f"warning: {counts['unknown']} classes have an unrecognised colour {colours}. Run 'debug' and share the output.")

    if alerts and (first_run or len(alerts) > 4):
        red = sum(a["kind"] == "absent" for a in alerts)
        yellow = len(alerts) - red
        lines = [f"{a['subject']} ({a['time']}, {fmt_date(a['date'])})" for a in alerts[:5]]
        more = f"\n+{len(alerts) - 5} more" if len(alerts) > 5 else ""
        head = "Now watching Amizone. " if first_run else ""
        notify(cfg, "absent" if red else "unmarked",
               f"{red} absent, {yellow} not marked",
               head + f"Last {cfg['lookback_days']} days:\n" + "\n".join(lines) + more)
    elif alerts:
        for a in alerts:
            notify_alert(cfg, a)
    elif first_run:
        notify(cfg, "info", "Now watching Amizone",
               f"All {len(events)} recent classes look fine. You will be alerted on any change.")
    return alerts


def is_termux():
    return "com.termux" in os.environ.get("PREFIX", "")


def credentials(cfg):
    user = cfg["username"]
    pw = get_password(user) if user else None
    if not user or not (pw or get_cookie(user)):
        print("No saved login. Run:  python amizon_watch.py setup   (or set AMIZONE_USER and AMIZONE_PASSWORD)")
        sys.exit(1)
    return user, pw


# ------------------------------------------------------------------- commands
def cmd_once(args):
    cfg = load_config()
    client = Amizone(*credentials(cfg))
    try:
        check_once(cfg, client)
    except LoginError as e:
        log(f"LOGIN FAILED: {e}")
        notify(cfg, "error", "Amizon Watch login failed", str(e))
        sys.exit(2)
    except (FetchError, requests.RequestException) as e:
        log(f"check failed: {e}")
        sys.exit(3)


def cmd_run(args):
    cfg = load_config()
    client = Amizone(*credentials(cfg))
    interval = max(10, int(cfg["interval_minutes"]))
    if is_termux():
        try:
            subprocess.run(["termux-wake-lock"], timeout=10)
            log("Termux detected: holding a wake lock so Android doesn't pause the checks.")
        except Exception:
            log("Termux detected, but termux-wake-lock is not installed "
                "(pkg install termux-api) -- Android may pause checks to save battery.")
    log(f"Amizon Watch started. Checking every {interval} minutes.")
    errors = 0
    while True:
        try:
            check_once(cfg, client)
            errors = 0
        except LoginError as e:
            # Stop instead of retrying, so a wrong password can never lock the account.
            log(f"LOGIN FAILED: {e} Stopping. Fix it with 'setup', then start again.")
            notify(cfg, "error", "Amizon Watch stopped", f"Login failed: {e}")
            sys.exit(2)
        except (FetchError, requests.RequestException) as e:
            errors += 1
            log(f"check failed ({errors} in a row): {e}")
            if errors == 3:
                notify(cfg, "error", "Amizon Watch cannot reach Amizone",
                       "Three checks in a row failed. It will keep trying.")
        except Exception as e:  # never die on an unexpected bug
            errors += 1
            log(f"unexpected error: {e!r}")
        time.sleep(max(60, interval * 60 + random.uniform(-60, 60)))


COOKIE_HELP = """
How to copy your logged-in session (Chrome / Edge / Brave):
  1. Log in to Amizone normally in your browser.
  2. Press F12, open the 'Network' tab, then press F5 to reload the page.
  3. Click the first request in the list (usually 'Home'), find 'Cookie:' under 'Request Headers'.
  4. Copy everything after 'Cookie:' and paste it below.
This is as sensitive as your password: never share it. It stops working when Amizone
logs the session out, and then you repeat these steps."""


def _cookie_flow(user):
    """Ask for a browser cookie, test it, save it. Returns True if it worked."""
    print(COOKIE_HELP)
    ck = getpass.getpass("\nPaste the cookie text here (hidden), or press Enter to cancel: ").strip()
    if not ck:
        return False
    client = Amizone(user, None)
    try:
        client.login_with_cookie(ck)
    except (LoginError, requests.RequestException) as e:
        print(f"That session was not accepted: {e}")
        return False
    where = save_secret(user + "#cookie", ck)
    print("Session works and was saved" + (" in your system keychain." if where == "keychain" else
          f" in the scrambled file {SECRET_FILE}."))
    return True


def cmd_cookie(args):
    cfg = dict(DEFAULTS)
    if CONFIG.exists():
        cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
    user = input(f"Amizone enrollment number [{cfg['username']}]: ").strip() or cfg["username"]
    if not user:
        print("An enrollment number is needed.")
        sys.exit(1)
    if not _cookie_flow(user):
        sys.exit(1)
    cfg["username"] = user
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print("Done. If you have not run 'setup' yet, run it once for the interval and phone-alert options.")


def cmd_setup(args):
    cfg = dict(DEFAULTS)
    if CONFIG.exists():
        cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
    user = input(f"Amizone enrollment number [{cfg['username']}]: ").strip() or cfg["username"]
    pw = getpass.getpass("Amizone password (typing is hidden): ")
    print("Testing login (one attempt)...")
    mode = "password"
    try:
        Amizone(user, pw).login_with_password()
    except (LoginError, requests.RequestException) as e:
        print(f"\nPassword login failed: {e}")
        print("(Run 'python amizon_watch.py diagnose' to see exactly why.)")
        ans = input("\nUse your browser's logged-in session instead ('cookie mode')? [y/N]: ").strip().lower()
        if ans != "y" or not _cookie_flow(user):
            print("Nothing was saved.")
            sys.exit(1)
        mode = "cookie"
    if mode == "password":
        print("Login works.")
        where = save_secret(user, pw)
        if where == "keychain":
            print("Password saved in your system keychain (not in any file).")
        else:
            print(f"\nNo system keychain is available here (normal on phones/Termux).")
            ans = input(f"Keep the password in a scrambled file at {SECRET_FILE}, readable only by you? [y/N]: ").strip().lower()
            if ans == "y":
                print("Saved. This is NOT strong encryption, just not plain text in a config file.")
            else:
                if SECRET_FILE.exists():
                    try:
                        data = json.loads(base64.b64decode(SECRET_FILE.read_bytes()))
                        data.pop(user, None)
                        SECRET_FILE.write_bytes(base64.b64encode(json.dumps(data).encode()))
                    except Exception:
                        pass
                print("Not saved. Set the AMIZONE_PASSWORD environment variable each time you run it instead.")
    cfg["username"] = user
    iv = input(f"Check every how many minutes? (10 or more) [{cfg['interval_minutes']}]: ").strip()
    if iv.isdigit():
        cfg["interval_minutes"] = max(10, int(iv))
    topic = cfg["ntfy_topic"] or "amizon-watch-" + secrets.token_hex(6)
    print("\nPhone alerts: install the free 'ntfy' app and subscribe to this topic name:")
    print(f"    {topic}")
    ans = input("Use phone alerts? [Y/n]: ").strip().lower()
    cfg["ntfy_topic"] = "" if ans == "n" else topic
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    notify(cfg, "info", "Amizon Watch is set up", "You will get red/yellow alerts like this one.")
    print("\nDone. Start watching with:  python amizon_watch.py run")
    print("Start automatically at login with:  python amizon_watch.py autostart")
    if mode == "cookie":
        print("Note: browser-session mode stops working when Amizone logs that session out; you will get an\n"
              "alert, then repeat:  python amizon_watch.py cookie")


def cmd_diagnose(args):
    """Explain step by step why login works or fails. Never prints the password or cookies."""
    cfg = load_config()
    user = cfg["username"] or input("Amizone enrollment number: ").strip()
    pw = get_password(user) if user else None
    if not pw:
        pw = getpass.getpass("Amizone password (hidden, not saved): ")
    print(f"\nAmizone address: {BASE}")
    client = Amizone(user, pw)
    result, err = "ok", None
    try:
        client.login_with_password()
    except CaptchaError as e:
        result, err = "captcha", e
    except LoginError as e:
        result, err = "rejected", e
    except requests.RequestException as e:
        result, err = "network", e
    print("\nWhat the login page looks like:")
    if client.page is None:
        print("  (could not open it)")
    else:
        pg = client.page
        print(f"  {len(pg.forms)} form(s) found, captcha widget on page: {pg.widget}")
        for n, f in enumerate(pg.forms, 1):
            fields = ", ".join(f"{i['name'] or i['id'] or '?'}[{i['type']}]" for i in f["inputs"]) or "no fields"
            chosen = "  <-- used for login" if f is client.form else ""
            print(f"  form {n}: sends {f['method'].upper()} to '{f['action']}': {fields}{chosen}")
        if pg.loose:
            print("  fields outside any form: " + ", ".join(f"{i['name'] or i['id'] or '?'}[{i['type']}]" for i in pg.loose))
    print("\nSteps taken:")
    for t in client.trace:
        print("  - " + t)
    print("\nResult:")
    if result == "ok":
        print("  Login WORKS for this account.")
        try:
            today = dt.datetime.now(IST).date()
            n = len(client.diary_events(today - dt.timedelta(days=1), today + dt.timedelta(days=1)))
            print(f"  The class schedule request returned {n} event(s).")
            if not n:
                print("  (0 is normal on a holiday. Otherwise run 'debug' and share the output.)")
        except Exception as e:
            print(f"  Login works, but reading the schedule failed: {e}")
    else:
        print(f"  {err}")
        if result == "network":
            print("  Check your internet connection, and whether your network/VPN blocks amizone.net.")
    print("\nYou can paste this whole output into the chat. It contains no password or cookie.")
    if result != "ok":
        sys.exit(1)


def cmd_history(args):
    db = open_db()
    rows = db.execute(
        "SELECT alerted_at,kind,subject,class_time,class_date,detail FROM alerts ORDER BY id DESC LIMIT ?",
        (args.limit,),
    ).fetchall()
    if not rows:
        print("No alerts yet.")
    for at, kind, subj, ctime, cdate, detail in rows:
        label = "RED   ABSENT    " if kind == "absent" else "YELLOW NOT MARKED"
        print(f"{at[:16].replace('T', ' ')}  {label}  {subj}  {ctime}, {fmt_date(cdate)}  ({detail})")


def cmd_export(args):
    db = open_db()
    rows = db.execute(
        "SELECT alerted_at,class_date,class_time,subject,kind,detail FROM alerts ORDER BY id DESC"
    ).fetchall()
    with open(args.path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["alerted_at", "class_date", "class_time", "subject", "status", "detail"])
        w.writerows(rows)
    print(f"Wrote {len(rows)} alerts to {args.path}")


def cmd_debug(args):
    cfg = load_config()
    client = Amizone(*credentials(cfg))
    today = dt.datetime.now(IST).date()
    try:
        raw = client.diary_events(today - dt.timedelta(days=int(cfg["lookback_days"])), today + dt.timedelta(days=1))
    except (LoginError, FetchError, requests.RequestException) as e:
        print(f"Failed: {e}")
        sys.exit(1)
    print(f"{len(raw)} events returned. Fields (no personal details):")
    for e in raw:
        print(json.dumps({k: e.get(k) for k in ("sType", "title", "start", "end", "AttndColor")}),
              "->", color_to_state(e.get("AttndColor")))


def cmd_test_notify(args):
    cfg = load_config()
    notify(cfg, "absent", "Marked ABSENT", "Example subject\n09:15 - 10:10, 16 Sep 2026")
    time.sleep(1)
    notify(cfg, "unmarked", "Attendance not marked", "Example subject\n11:15 - 12:10, 16 Sep 2026")
    print("Sent a red and a yellow test alert.")


def cmd_autostart(args):
    system = platform.system()
    py, script = sys.executable, str(Path(__file__).resolve())
    if is_termux():
        boot_dir = Path.home() / ".termux/boot"
        boot_script = boot_dir / "amizon-watch"
        if args.remove:
            boot_script.unlink(missing_ok=True)
            print("Removed from startup. (Termux:Boot itself is untouched.)")
            return
        boot_dir.mkdir(parents=True, exist_ok=True)
        boot_script.write_text(
            "#!/data/data/com.termux/files/usr/bin/sh\n"
            "termux-wake-lock\n"
            f'cd "{Path(__file__).resolve().parent}"\n'
            f'"{py}" "{script}" run >> "{LOG_PATH}" 2>&1 &\n',
            encoding="utf-8",
        )
        os.chmod(boot_script, 0o700)
        print("Saved a Termux:Boot script. Requirements for this to actually run at boot:")
        print("  1. Install the 'Termux:Boot' app (F-Droid, same source as Termux) and open it once.")
        print("  2. Disable battery optimisation for Termux in Android's app settings.")
        print("Starting it now in the background too...")
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.Popen([py, script, "run"], stdout=open(LOG_PATH, "a"), stderr=subprocess.STDOUT)
        return
    if system == "Windows":
        startup = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"
        vbs = startup / "AmizonWatch.vbs"
        if args.remove:
            vbs.unlink(missing_ok=True)
            print("Removed from startup.")
            return
        pyw = py.replace("python.exe", "pythonw.exe")
        pyw = pyw if Path(pyw).exists() else py
        vbs.write_text(
            'Set s = CreateObject("WScript.Shell")\n'
            f's.Run """{pyw}"" ""{script}"" run", 0, False\n', encoding="utf-8")
        subprocess.Popen([pyw, script, "run"], creationflags=0x00000008 | 0x00000200)
        print("Will start at every login (no window). Started now too. Logs: " + str(LOG_PATH))
    elif system == "Darwin":
        plist = Path.home() / "Library/LaunchAgents/com.amizonwatch.agent.plist"
        if args.remove:
            subprocess.run(["launchctl", "unload", str(plist)])
            plist.unlink(missing_ok=True)
            print("Removed from startup.")
            return
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>\n'
            '<key>Label</key><string>com.amizonwatch.agent</string>\n'
            f'<key>ProgramArguments</key><array><string>{py}</string><string>{script}</string><string>run</string></array>\n'
            '<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>\n</dict></plist>\n', encoding="utf-8")
        subprocess.run(["launchctl", "load", str(plist)])
        print("Will start at every login. Started now too.")
    else:
        unit = Path.home() / ".config/systemd/user/amizon-watch.service"
        if args.remove:
            subprocess.run(["systemctl", "--user", "disable", "--now", "amizon-watch"])
            unit.unlink(missing_ok=True)
            print("Removed from startup.")
            return
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(
            "[Unit]\nDescription=Amizon Watch\n\n[Service]\n"
            f"ExecStart={py} {script} run\nRestart=on-failure\nRestartSec=60\n\n"
            "[Install]\nWantedBy=default.target\n", encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"])
        subprocess.run(["systemctl", "--user", "enable", "--now", "amizon-watch"])
        print("Will start at every login. Started now too.")


def main():
    p = argparse.ArgumentParser(description="Amizon Watch Agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="save your login and choose alert options").set_defaults(fn=cmd_setup)
    sub.add_parser("diagnose", help="explain why login works or fails").set_defaults(fn=cmd_diagnose)
    sub.add_parser("cookie", help="use your browser's logged-in session (if a captcha blocks login)").set_defaults(fn=cmd_cookie)
    sub.add_parser("run", help="check on a schedule, forever").set_defaults(fn=cmd_run)
    sub.add_parser("once", help="check one time and exit (for cron / cloud)").set_defaults(fn=cmd_once)
    h = sub.add_parser("history", help="show past alerts")
    h.add_argument("--limit", type=int, default=50)
    h.set_defaults(fn=cmd_history)
    e = sub.add_parser("export", help="save all alerts as CSV")
    e.add_argument("path", nargs="?", default="amizon-history.csv")
    e.set_defaults(fn=cmd_export)
    sub.add_parser("debug", help="show what Amizone returns").set_defaults(fn=cmd_debug)
    sub.add_parser("test-notify", help="send a red and a yellow test alert").set_defaults(fn=cmd_test_notify)
    a = sub.add_parser("autostart", help="start automatically when you log in")
    a.add_argument("--remove", action="store_true")
    a.set_defaults(fn=cmd_autostart)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
