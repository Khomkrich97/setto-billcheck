# -*- coding: utf-8 -*-
"""Local web UI for billcheck (the ad team's "check card charges" program).

START.bat copies this folder to %LOCALAPPDATA%\\NISIT\\billcheck\\app and starts it with pythonw. It serves
ui/ on 127.0.0.1 (random port, random key, Host/Origin checked) and opens it in an app window of the
Chromium that Playwright already installed. One instance per PC; a second start only opens a window.

  pythonw app.py              # open the window
  pythonw app.py --autostart  # from the Windows Run key: open the window, run once a day by itself

The NISIT login token (30 days) is kept encrypted with Windows DPAPI for the current Windows user.

Updates: on every start the program compares app/VERSION with github.com/Khomkrich97/setto-billcheck (main)
and, when they differ, swaps in the published app/ folder and restarts. START.bat in a git clone sets
BILLCHECK_NO_UPDATE so a developer's unpushed changes are not overwritten.
"""
import base64, copy, ctypes, datetime as dt, hashlib, http.server, io, json, os, re, secrets, socket, subprocess, sys, \
    threading, time, traceback, urllib.request, webbrowser, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import billcheck as bc   # noqa: E402

REPO = "Khomkrich97/setto-billcheck"


def local_version():
    try:
        with open(os.path.join(HERE, "VERSION"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


VERSION = local_version() or "?"
APP_DIR = bc.APP_DIR
TOKEN_FILE = os.path.join(APP_DIR, "login.bin")
CONFIG = os.path.join(APP_DIR, "config.json")
INSTANCE = os.path.join(APP_DIR, "instance.json")
LOG_FILE = os.path.join(APP_DIR, "app.log")
UI_PROFILE = os.path.join(APP_DIR, "ui-profile")
NISIT_PAGE = "https://ads.wokeuptown.com/nisit/topup.html#billcheck"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "NISIT BillCheck"
IDLE_EXIT = 45          # seconds without a window polling (and no run) before the program exits
FIRST_WINDOW_GRACE = 180

STEPS = [("nisit", "ระบบ NISIT"), ("data", "SMS เงินออก"), ("facebook", "Facebook"),
         ("bills", "โหลดบิล"), ("search", "หาบิลเพิ่ม"), ("match", "จับคู่และส่งขึ้นระบบ")]


def log_file(msg):
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 1_000_000:
            os.replace(LOG_FILE, LOG_FILE + ".1")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except OSError:
        pass


def message_box(text, title="เช็คเงินตัดแอด"):
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
    except Exception:
        pass


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


# ── DPAPI (token at rest, readable only by this Windows user) ───────────────────────────────────
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data, protect):
    buf = ctypes.create_string_buffer(data, len(data))
    src = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    dst = _Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(dst)):   # 0x1 = UI_FORBIDDEN
        raise OSError("DPAPI failed")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def token_load():
    try:
        with open(TOKEN_FILE, "rb") as f:
            return _dpapi(f.read(), False).decode("utf-8")
    except Exception:
        return None


def token_save(tok):
    os.makedirs(APP_DIR, exist_ok=True)
    with open(TOKEN_FILE, "wb") as f:
        f.write(_dpapi(tok.encode("utf-8"), True))


def token_clear():
    try:
        os.remove(TOKEN_FILE)
    except OSError:
        pass


def token_info(tok):
    """email + expiry from the token itself (the server re-checks everything; this is display only)."""
    try:
        parts = base64.b64decode(tok).decode("utf-8").split("|")
        issued = dt.datetime.strptime(parts[2], "%a %b %d %Y").date()
        exp = issued + dt.timedelta(days=30)
        return {"email": parts[0], "exp": exp.strftime("%d/%m/%Y"), "days_left": (exp - bc.today_bkk()).days}
    except Exception:
        return {"email": "", "exp": "", "days_left": 0}


# ── Windows Run key (start with Windows, per user, no admin) ───────────────────────────────────
def autostart_cmd():
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    return f'"{pyw}" "{os.path.abspath(__file__)}" --autostart'


def autostart_get():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, RUN_NAME)[0]
    except OSError:
        return None


def autostart_set(on):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, autostart_cmd())
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass


# ── shortcuts with the program's icon (a .bat cannot carry one) ───────────────────────────────
SHORTCUT_NAME = "เช็คเงินตัดแอด.lnk"


def ensure_shortcuts():
    """Desktop shortcut once (deleting it is respected), plus one next to START.bat whenever it is missing.
    Both start this installed copy directly, so they keep working if the unzipped folder is moved."""
    try:
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(pyw):
            pyw = sys.executable
        q = lambda s: "'" + s.replace("'", "''") + "'"   # PowerShell single-quoted literal
        cfg = load_json(CONFIG, {})
        dirs = []
        src = os.environ.get("BILLCHECK_FROM", "").rstrip("\\/")
        if src and os.path.isdir(src) and not os.path.exists(os.path.join(src, SHORTCUT_NAME)):
            dirs.append(q(src))
        if not cfg.get("desktop_shortcut"):
            dirs.append("[Environment]::GetFolderPath('Desktop')")
        if not dirs:
            return
        ps = "$w = New-Object -ComObject WScript.Shell\n" + "".join(
            f"$l = $w.CreateShortcut((Join-Path ({d}) {q(SHORTCUT_NAME)}))\n"
            f"$l.TargetPath = {q(pyw)}; $l.Arguments = {q(chr(34) + os.path.abspath(__file__) + chr(34))}\n"
            f"$l.WorkingDirectory = {q(HERE)}; $l.IconLocation = {q(os.path.join(HERE, 'icon.ico') + ',0')}\n"
            f"$l.Description = {q('เช็คเงินตัดแอด Meta')}; $l.Save()\n" for d in dirs)
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                            base64.b64encode(ps.encode("utf-16-le")).decode("ascii")],
                           capture_output=True, text=True, timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            log_file(f"shortcut failed: {(r.stderr or '')[-300:]}")
            return
        if not cfg.get("desktop_shortcut"):
            save_json(CONFIG, {**load_json(CONFIG, {}), "desktop_shortcut": True})
    except Exception as e:
        log_file(f"shortcut failed: {e}")


def pick_folder(current):
    """Windows folder picker (on top of the app window) -> chosen absolute path, or None."""
    q = lambda s: "'" + s.replace("'", "''") + "'"
    start = current if os.path.isdir(current) else bc.downloads_dir()
    ps = "\n".join([
        "Add-Type -AssemblyName System.Windows.Forms",
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8",
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog",
        f"$d.Description = {q('เลือกโฟลเดอร์ที่จะเก็บ PDF บิล Meta (โปรแกรมสร้างโฟลเดอร์ย่อยตามรอบตรวจให้เอง)')}",
        f"$d.SelectedPath = {q(start)}",
        "$o = New-Object System.Windows.Forms.Form -Property @{TopMost = $true}",
        "if ($d.ShowDialog($o) -eq [System.Windows.Forms.DialogResult]::OK) { $d.SelectedPath }"])
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Sta", "-EncodedCommand",
                            base64.b64encode(ps.encode("utf-16-le")).decode("ascii")],
                           capture_output=True, timeout=600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        p = r.stdout.decode("utf-8", "replace").strip().lstrip("\ufeff")
        return p if p and os.path.isabs(p) and os.path.isdir(p) else None
    except Exception as e:
        log_file(f"folder picker failed: {e}")
        return None


# ── run state shared with the web UI ───────────────────────────────────────────────────────────
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.token = token_load()
        self.last = None            # get_billcheck(p_days=0) answer
        self.last_err = ""
        self.net_wait = False
        self.thread = None
        self.stop_flag = threading.Event()
        self.last_poll = time.time()
        self.first_poll = False
        self.reset_run("idle")

    def reset_run(self, phase):
        self.run = {"phase": phase, "steps": [{"key": k, "label": l, "state": "todo"} for k, l in STEPS],
                    "progress": {"done": 0, "total": 0, "label": ""}, "accounts": [], "log": [],
                    "fb_login": False, "result": None, "error": "", "auth_error": False,
                    "started": "", "finished": ""}

    def snapshot(self):
        with self.lock:
            tok = self.token
            today = bc.today_bkk().isoformat()
            runs = (self.last or {}).get("runs") or []
            done_today = next((r for r in runs if r.get("status") == "done" and r.get("day") == today), None)
            busy = next((r for r in runs if r.get("status") == "running"), None)
            return copy.deepcopy({
                "version": VERSION, "auth": token_info(tok) if tok else None,
                "autostart": bool(autostart_get()), "run": self.run, "last": (self.last or {}).get("last"),
                "pdf_dir": bc.pdf_root(), "pdf_custom": bool(load_json(CONFIG, {}).get("pdf_dir")),
                "runs": runs[:5], "done_today": done_today,
                "busy_elsewhere": busy if self.run["phase"] != "running" else None,
                "last_err": self.last_err, "net_wait": self.net_wait})


S = State()


class UIReporter(bc.Reporter):
    def _log(self, msg):
        S.run["log"].append({"t": time.strftime("%H:%M:%S"), "m": msg})
        del S.run["log"][:-120]

    def step(self, key, text):
        with S.lock:
            seen = False
            for st in S.run["steps"]:
                if st["key"] == key:
                    st["state"], seen = "doing", True
                elif not seen and st["state"] in ("todo", "doing"):
                    st["state"] = "skip" if st["key"] == "search" else "done"
            self._log(text)

    def log(self, msg):
        with S.lock:
            self._log(msg)

    def progress(self, done, total, label=""):
        with S.lock:
            S.run["progress"] = {"done": done, "total": total, "label": label}

    def account(self, aid, name, state, info=""):
        with S.lock:
            for a in S.run["accounts"]:
                if a["id"] == aid:
                    a.update(state=state, info=info)
                    break
            else:
                S.run["accounts"].append({"id": aid, "name": name, "state": state, "info": info})
            if state not in ("loading", "ok", "none"):
                self._log(f"{name} โหลดไม่สำเร็จ {info}")

    def fb_login(self, needed):
        with S.lock:
            S.run["fb_login"] = needed

    def check_stop(self):
        if S.stop_flag.is_set():
            raise bc.Stopped()


def refresh_last(tries=1):
    tok = S.token
    if not tok:
        return
    for i in range(tries):
        try:
            r = bc.rpc("get_billcheck", {"p_token": tok, "p_days": 0}, timeout=20)
        except bc.Fail as e:
            with S.lock:
                S.last_err = str(e)
                S.net_wait = tries > 1
            if i < tries - 1:
                time.sleep(10)
            continue
        with S.lock:
            S.net_wait = False
            if r.get("ok"):
                S.last, S.last_err = r, ""
            elif r.get("error") == "unauthorized":
                S.token, S.last, S.last_err = None, None, bc.AUTH_MSG
                token_clear()
            else:
                S.last_err = f"อ่านผลล่าสุดจากระบบไม่ได้ ({r.get('error')})"
        return
    with S.lock:
        S.net_wait = False


def start_run(days=None):
    with S.lock:
        if S.thread and S.thread.is_alive():
            return False, "กำลังตรวจอยู่"
        if not S.token:
            return False, "ล็อกอินระบบ NISIT ก่อน"
        S.stop_flag.clear()
        S.reset_run("running")
        S.run["started"] = time.strftime("%d/%m/%Y %H:%M")
        # published and started under the lock: two quick clicks must not start two runs (astra 15/10)
        S.thread = threading.Thread(target=_worker, args=(S.token, days), daemon=True)
        S.thread.start()
    return True, ""


def _worker(tok, days):
    rep = UIReporter()
    try:
        res = bc.run_check(tok, rep, days)
        with S.lock:
            S.run["result"] = res
            S.run["phase"] = "done"
            for st in S.run["steps"]:
                if st["state"] in ("todo", "doing"):
                    st["state"] = "skip" if st["key"] == "search" and st["state"] == "todo" else "done"
    except bc.AuthFail as e:
        with S.lock:
            S.run.update(phase="error", error=str(e), auth_error=True)
            S.token = None
        token_clear()
    except bc.Fail as e:
        with S.lock:
            S.run.update(phase="error", error=str(e))
    except bc.Stopped:
        with S.lock:
            S.run.update(phase="error", error="หยุดแล้ว ยังไม่ได้ส่งผลขึ้นระบบ กดตรวจใหม่เมื่อพร้อม")
    except Exception as e:
        log_file("run crashed\n" + traceback.format_exc())
        with S.lock:
            S.run.update(phase="error", error=f"โปรแกรมขัดข้อง ({type(e).__name__}: {str(e)[:200]}) "
                                              f"ส่งภาพหน้าจอนี้กับไฟล์ {LOG_FILE} ให้ Master")
    finally:
        with S.lock:
            S.run["finished"] = time.strftime("%d/%m/%Y %H:%M")
            for st in S.run["steps"]:
                if st["state"] == "doing" and S.run["phase"] == "error":
                    st["state"] = "fail"
        refresh_last()


def do_login(email, password):
    email = (email or "").strip().lower()
    if not email or not password:
        return {"ok": False, "error": "กรอกอีเมลและรหัสผ่าน"}
    try:
        r = bc.rpc("verify_login", {"p_email": email,
                                    "p_hash": hashlib.sha256(password.encode("utf-8")).hexdigest()}, timeout=20)
    except bc.Fail as e:
        return {"ok": False, "error": str(e)}
    if r.get("locked"):
        return {"ok": False, "error": f"บัญชีนี้ถูกล็อกชั่วคราว ลองใหม่ในอีก "
                                      f"{int(r.get('retry_after_sec', 3600)) // 60 + 1} นาที"}
    if r.get("suspended"):
        return {"ok": False, "error": "บัญชีนี้ถูกระงับ ติดต่อ Master"}
    if not (r.get("ok") and r.get("token")):
        return {"ok": False, "error": "อีเมลหรือรหัสผ่านไม่ถูก (ผิด 3 ครั้ง ระบบล็อก 1 ชั่วโมง)"}
    tok = r["token"]
    try:
        g = bc.rpc("get_billcheck", {"p_token": tok, "p_days": 0}, timeout=20)
    except bc.Fail as e:
        return {"ok": False, "error": str(e)}
    if not g.get("ok"):
        return {"ok": False, "error": "บัญชีนี้ยังไม่มีสิทธิ์หน้าเติมงบแอด ให้ Master เปิดสิทธิ์ก่อน"}
    token_save(tok)
    save_json(CONFIG, {**load_json(CONFIG, {}), "email": email})
    with S.lock:
        S.token, S.last, S.last_err = tok, g, ""
    return {"ok": True}


# ── HTTP ───────────────────────────────────────────────────────────────────────────────────────
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8"),
          "/app.js": ("app.js", "application/javascript; charset=utf-8"),
          "/icon.png": ("icon.png", "image/png")}
CSP = ("default-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "billcheck"
    KEY = PORT = None

    def log_message(self, *a):
        pass

    def _host_ok(self):
        return self.headers.get("Host") == f"127.0.0.1:{self.PORT}"

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _api_ok(self):
        return self._host_ok() and secrets.compare_digest(self.headers.get("X-Key", ""), self.KEY)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"ok": False})
        path = self.path.split("?", 1)[0]
        if path in STATIC:
            name, ctype = STATIC[path]
            try:
                with open(os.path.join(HERE, "ui", name), "rb") as f:
                    return self._send(200, f.read(), ctype)
            except OSError:
                return self._send(404, b"missing", "text/plain")
        if path == "/api/status":
            if not self._api_ok():
                return self._send(403, {"ok": False})
            S.last_poll, S.first_poll = time.time(), True
            return self._send(200, S.snapshot())
        if path == "/api/ping":
            return self._send(200 if self._api_ok() else 403, {"ok": self._api_ok()})
        self._send(404, {"ok": False})

    def do_POST(self):
        if not self._api_ok() or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self._send(403, {"ok": False})
        origin = self.headers.get("Origin")
        if origin and origin != f"http://127.0.0.1:{self.PORT}":
            return self._send(403, {"ok": False})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(min(n, 20000)).decode("utf-8") or "{}")
        except Exception:
            return self._send(400, {"ok": False})
        path = self.path.split("?", 1)[0]
        try:
            self._send(200, self._post(path, body))
        except Exception as e:
            log_file(f"POST {path} failed\n" + traceback.format_exc())
            self._send(200, {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"})

    def _post(self, path, body):
        if path == "/api/login":
            return do_login(body.get("email"), body.get("password"))
        if path == "/api/logout":
            if S.thread and S.thread.is_alive():
                return {"ok": False, "error": "กำลังตรวจอยู่ หยุดก่อนแล้วค่อยออกจากระบบ"}
            token_clear()
            with S.lock:
                S.token, S.last = None, None
                S.reset_run("idle")
            return {"ok": True}
        if path == "/api/start":
            days = body.get("days")
            days = int(days) if isinstance(days, (int, str)) and str(days).isdigit() else None
            ok, err = start_run(days)
            return {"ok": ok, "error": err}
        if path == "/api/stop":
            S.stop_flag.set()
            return {"ok": True}
        if path == "/api/autostart":
            autostart_set(bool(body.get("on")))
            return {"ok": True, "autostart": bool(autostart_get())}
        if path == "/api/open":
            what = body.get("what")
            if what == "nisit":
                webbrowser.open(NISIT_PAGE)
            elif what == "pdfroot":
                folder = bc.pdf_root()
                os.makedirs(folder, exist_ok=True)
                os.startfile(folder)
            elif what == "folder":
                folder = ((S.run.get("result") or {}).get("folder")) or bc.pdf_root()
                os.makedirs(folder, exist_ok=True)
                os.startfile(folder)
            return {"ok": True}
        if path == "/api/pdfdir":
            if body.get("action") == "reset":
                cfg = load_json(CONFIG, {})
                cfg.pop("pdf_dir", None)
                save_json(CONFIG, cfg)
            elif body.get("action") == "pick":
                picked = pick_folder(bc.pdf_root())
                if picked:
                    save_json(CONFIG, {**load_json(CONFIG, {}), "pdf_dir": picked})
            return {"ok": True, "pdf_dir": bc.pdf_root(), "pdf_custom": bool(load_json(CONFIG, {}).get("pdf_dir"))}
        if path == "/api/refresh":
            threading.Thread(target=refresh_last, daemon=True).start()
            return {"ok": True}
        if path == "/api/reset":
            with S.lock:
                if S.run["phase"] != "running":
                    S.reset_run("idle")
            return {"ok": True}
        return {"ok": False, "error": "unknown"}


def chromium_path():
    cfg = load_json(CONFIG, {})
    p = cfg.get("chromium")
    if p and os.path.exists(p):
        return p
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            p = pw.chromium.executable_path
        if p and os.path.exists(p):
            save_json(CONFIG, {**cfg, "chromium": p})
            return p
    except Exception:
        log_file("chromium path lookup failed\n" + traceback.format_exc())
    return None


def edge_path():
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        p = base and os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")
        if p and os.path.exists(p):
            return p
    return None


def open_window(url):
    if os.environ.get("BILLCHECK_NOWINDOW"):   # tests drive the page from another browser
        return
    # Edge ships with every Windows 11 and shows no banner; Playwright's Chromium carries a permanent
    # "Chrome for Testing" bar, so it is only the fallback for the UI window (Facebook still uses it)
    exe = edge_path() or chromium_path()
    if exe:
        try:
            subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={UI_PROFILE}-{os.path.splitext(os.path.basename(exe))[0]}", "--no-first-run",
                              "--no-default-browser-check", "--disable-features=Translate,msEdgeSidebarV2",
                              "--disable-sync", "--lang=th", "--window-size=1200,880"], close_fds=True)
            return
        except OSError:
            log_file("app window failed\n" + traceback.format_exc())
    webbrowser.open(url)


def existing_instance():
    inst = load_json(INSTANCE, None)
    if not inst:
        return None
    url = f"http://127.0.0.1:{inst['port']}"
    try:
        req = urllib.request.Request(url + "/api/ping", headers={"X-Key": inst["key"]})
        with urllib.request.urlopen(req, timeout=3) as r:
            if json.loads(r.read().decode("utf-8")).get("ok"):
                return f"{url}/?k={inst['key']}"
    except Exception:
        pass
    return None


def watchdog(httpd, started):
    while True:
        time.sleep(5)
        running = S.thread is not None and S.thread.is_alive()
        if running:
            continue
        idle = time.time() - S.last_poll
        if (S.first_poll and idle > IDLE_EXIT) or (not S.first_poll and time.time() - started > FIRST_WINDOW_GRACE):
            httpd.shutdown()
            return


def autostart_flow():
    """Started with Windows: wait for the network, then check once a day (skip if someone already did)."""
    if not S.token:
        return
    refresh_last(tries=18)   # up to ~3 minutes for Wi-Fi after boot
    with S.lock:
        if not S.token or not S.last:
            return
        today = bc.today_bkk().isoformat()
        runs = S.last.get("runs") or []
        # only a finished run counts: a "running" row may be a crashed run whose lease begin() will expire,
        # and a live one makes begin() answer busy, which the window shows (astra 15/10)
        if any(r.get("status") == "done" and r.get("day") == today for r in runs):
            return
    start_run(None)


def ensure_deps(bat_text):
    """START.bat pins the libraries (DEPS line). Before an update is swapped in, install a changed list
    (and Chromium, for a new Playwright); -> False when that failed (update skipped, retried next start)."""
    try:
        m = re.search(r'set "DEPS=([^"]+)"', bat_text)
        if not m:
            return True
        deps = m.group(1).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.=\- ]{1,300}", deps):   # pins only, nothing else reaches pip
            log_file(f"bad DEPS line in update: {deps[:100]}")
            return False
        ready = os.path.join(os.path.dirname(os.path.dirname(sys.executable)), "ready.txt")
        try:
            with open(ready, encoding="utf-8") as f:
                if f.read().strip() == deps:
                    return True
        except OSError:
            pass
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", *deps.split()],
                           capture_output=True, text=True, timeout=900, creationflags=flags)
        if r.returncode == 0:
            r = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                               capture_output=True, text=True, timeout=1800, creationflags=flags)
        if r.returncode != 0:
            log_file(f"dependency update failed: {(r.stderr or '')[-500:]}")
            return False
        with open(ready, "w", encoding="utf-8") as f:
            f.write(deps + "\n")
        return True
    except Exception as e:
        log_file(f"dependency update failed: {e}")
        return False


def self_update():
    """Follow GitHub main: when app/VERSION there differs, replace this folder with the published app/
    files and restart. Any failure just keeps the current version (the next start tries again)."""
    if os.environ.get("BILLCHECK_NO_UPDATE"):
        return False
    try:
        with urllib.request.urlopen(f"https://raw.githubusercontent.com/{REPO}/main/app/VERSION", timeout=5) as r:
            remote = r.read().decode("utf-8").strip()
        if not remote or remote == local_version() or len(remote) > 40:
            return False
        with urllib.request.urlopen(f"https://codeload.github.com/{REPO}/zip/refs/heads/main", timeout=60) as r:
            z = zipfile.ZipFile(io.BytesIO(r.read()))
        root = z.namelist()[0].split("/")[0] + "/"
        staged, bat = [], None
        for name in z.namelist():
            rel = name[len(root):] if name.startswith(root) else ""
            if rel == "START.bat":
                bat = z.read(name).decode("utf-8", "replace")
            if not rel.startswith("app/") or name.endswith("/"):
                continue
            dest = os.path.normpath(os.path.join(HERE, rel[len("app/"):]))
            if not dest.startswith(os.path.normpath(HERE) + os.sep):   # no path tricks out of the folder
                raise ValueError(f"bad path in update: {name}")
            staged.append((dest, z.read(name)))
        if not any(os.path.basename(d) == "app.py" for d, _ in staged):
            raise ValueError("update has no app.py")
        # write every file beside its target first, swap them in only when all are on disk, VERSION last
        staged.sort(key=lambda x: os.path.basename(x[0]) == "VERSION")
        written = []
        try:
            for dest, blob in staged:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest + ".new", "wb") as f:
                    f.write(blob)
                written.append(dest)
            if bat is not None and not ensure_deps(bat):
                raise RuntimeError("libraries for the new version could not be installed")
        except Exception:
            for dest in written:
                try:
                    os.remove(dest + ".new")
                except OSError:
                    pass
            raise
        for dest in written:
            os.replace(dest + ".new", dest)
        log_file(f"updated to {local_version()} from GitHub")
        return True
    except Exception as e:
        log_file(f"update check failed: {e}")
        return False


def main():
    auto = "--autostart" in sys.argv
    url = existing_instance()
    if url:
        open_window(url)
        return
    if self_update():   # restart on the new files
        subprocess.Popen([sys.executable, os.path.abspath(__file__), *sys.argv[1:]], close_fds=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return
    key = secrets.token_urlsafe(24)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    Handler.KEY, Handler.PORT = key, port
    save_json(INSTANCE, {"port": port, "key": key, "pid": os.getpid()})
    # keep the Run entry pointing at this copy (START.bat refreshes the copy on every start)
    if autostart_get() and autostart_get() != autostart_cmd():
        try:
            autostart_set(True)
        except OSError:
            pass
    threading.Thread(target=watchdog, args=(httpd, time.time()), daemon=True).start()
    threading.Thread(target=autostart_flow if auto else refresh_last, daemon=True).start()
    threading.Thread(target=ensure_shortcuts, daemon=True).start()
    open_window(f"http://127.0.0.1:{port}/?k={key}")
    try:
        httpd.serve_forever()
    finally:
        try:
            if load_json(INSTANCE, {}).get("pid") == os.getpid():
                os.remove(INSTANCE)
        except OSError:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log_file("fatal\n" + traceback.format_exc())
        message_box(f"โปรแกรมเช็คเงินตัดแอดเปิดไม่ได้\nส่งไฟล์ {LOG_FILE} ให้ Master")
