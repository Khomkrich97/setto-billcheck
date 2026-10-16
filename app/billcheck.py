# -*- coding: utf-8 -*-
"""Daily check: is every card charge the bank SMS'd really a Meta ad bill of ours?

v2 (15/10/2026): the ad team runs it (app.py, a local web UI) and the verdicts go into NISIT, where
accounting reads them in the topup page tab "เช็คเงินตัด". No Excel any more.

  1. billcheck_begin  -> one run at a time + the shared matcher memory (frozen pairs, unresolved SMS)
  2. billcheck_data   -> ad accounts (with the last day the system saw a charge), card -> bank account,
                         bank SMS outflows (read only)
  3. Playwright (the runner's own Facebook login) downloads the billing-activity PDF of every account
     the system saw charging; then, for bank accounts that still have an SMS without a bill, the other
     ad accounts on those cards ("search")
  4. pairs bill rows with SMS here on the PC (matcher unchanged, 9 astra rounds), then
     billcheck_finish writes one verdict per SMS + the matcher memory

Clicks only "Download" on Meta, never anything that pays. Separate from scripts/meta_invoice_batch.py.

  python billcheck.py                  # needs env BILLCHECK_TOKEN; normal use is app.py
  python billcheck.py --days 5         # SMS of the last 5 days + today
  python billcheck.py --only <id,...>  # some accounts, writes nothing (pilot)
"""
import argparse, collections, datetime as dt, json, os, re, shutil, socket, sys, time, urllib.error, urllib.parse, \
    urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SB_URL = "https://sfqcqendjllppyrszeor.supabase.co"
# public anon key (the same one every NISIT page ships); every RPC checks the login token itself
SB_ANON = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InNmcWNxZW5kamxscHB5cnN6ZW9yIiwi"
           "cm9sZSI6ImFub24iLCJpYXQiOjE3ODE4NDA4NzEsImV4cCI6MjA5NzQxNjg3MX0.wv0jSz4v8Uhvt2tiBlqioSGzlhoPvRU-b34jN1H102o")
BUSINESS_ID = "277898666205656"
BILLING_URL = ("https://business.facebook.com/latest/billing_hub/payment_activity/"
               "?payment_account_id={aid}&asset_id={aid}&business_id=" + BUSINESS_ID +
               "&nav_ref=billing_hub&date={s}_{e}")
HOME_URL = "https://business.facebook.com/latest/billing_hub/accounts?business_id=" + BUSINESS_ID

APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "NISIT", "billcheck")
PROFILE = os.environ.get("BILLCHECK_PROFILE") or os.path.join(APP_DIR, "fb-profile")
RUNS_DIR = os.path.join(APP_DIR, "runs")
KEEP_RUN_DAYS = 30

TZ = dt.timezone(dt.timedelta(hours=7))
THAI_MONTH = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
              "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]

DOWNLOAD_BTN = re.compile(r"^\s*(ดาวน์โหลด|Download)\s*$")
# The menu also has "ดาวน์โหลดธุรกรรมทั้งหมด (PDF)" (a different document) and "(CSV)"; take only the report PDF.
PDF_ITEM = re.compile(r"^\s*(ดาวน์โหลดรายงาน\s*\(PDF\)|Download report\s*\(PDF\))\s*$", re.I)
CLOSE_BTN = re.compile(r"^\s*(ปิด|Close|ไม่ใช่ตอนนี้|Not now)\s*$")
NO_TX = re.compile(r"ไม่มีธุรกรรม|ไม่มีกิจกรรมการชำระเงิน|No transactions|No payment activity", re.I)
LOGIN_URL = re.compile(r"login|checkpoint|two_step|/recover", re.I)
# other steps of a Facebook sign-in (device approval, code entry, consent): never navigate away from these
LOGIN_STEP = re.compile(r"auth|confirm|device|approv|code|consent|security|/r\.php|/reg", re.I)

ACCT_ID_RE = re.compile(r"จำนวน:\s*(\d+)")
ROW_RE = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{4})\n"                    # date
    r"([0-9\-\n]+?)\n"                              # transaction id (may wrap)
    r"(?:(?:Visa|Mastercard|JCB)[^\n]*?(\d{4})\n)?"  # per-row card (several cards in one period)
    r"฿([\d,]+\.\d{2}) THB\n"                       # amount
    r"([^\n]+)", re.M)
HDR_CARD_RE = re.compile(r"วิธีการชำระเงิน:\s*(?:Visa|Mastercard|JCB)[^\d]*(\d{4})")
HDR_CREDIT_RE = re.compile(r"วิธีการชำระเงิน:\s*เครดิตโฆษณา")

# Kasikorn accounts the team calls "ชุดที่ 1-4", keyed by the last 2 digits (same as topup.js ACCT_SET)
ACCT_SET = ["81", "33", "91", "87"]
# only this exact status is money that left the card ("ยังไม่ชำระ" also contains ชำระ)
PAID_RE = re.compile(r"^\s*(ชำระแล้ว|Paid)\s*$", re.I)

# matcher windows, measured on Sep 2026 bills vs SMS (see docs/pages/topup-bank.md#recon-invoice)
FEE = 2000           # satang: the bank adds 20 baht to some card charges
BULK_DAYS = 7        # one SMS can pay bill rows from up to a week back
INV_LATE = 3         # bill date can trail the SMS by up to 3 days
GAP_1TO1 = 4         # SMS up to 4 days after the bill date
BULK_MAX_ROWS = 22
BILL_BACK = 14       # bills downloaded this many days before the first judged SMS day
SMS_BACK = 10        # earlier SMS loaded as context so they keep their own bills
MAX_BACK = 31        # never chase an unpaired SMS further back than this
KEEP_OPEN = MAX_BACK + BULK_DAYS + INV_LATE + 5   # an unresolved SMS keeps challenging pairs this long
NONE_STABLE = 15     # seconds "no transactions" must stay on screen before it is believed


class Fail(Exception):
    """A stop with a message the runner can act on (shown as is in the UI)."""


class AuthFail(Fail):
    """The NISIT login is gone or lacks the topup page: sign in again."""


class Stopped(Exception):
    """The runner pressed stop."""


class Reporter:
    """Console reporter; app.py replaces it with one that feeds the web UI."""
    def step(self, key, text):
        print(f"\n[{key}] {text}", flush=True)

    def log(self, msg):
        print(msg, flush=True)

    def progress(self, done, total, label=""):
        pass

    def account(self, aid, name, state, info=""):
        print(f"  {name}: {state} {info}", flush=True)

    def fb_login(self, needed):
        if needed:
            print("ล็อกอิน Facebook ในหน้าต่างที่เด้งขึ้นมา", flush=True)

    def check_stop(self):
        pass


def today_bkk():
    return dt.datetime.now(TZ).date()


def ddmmyyyy(d):
    return d.strftime("%d/%m/%Y")


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|]+', "-", s).strip() or "account"


def set_label(acct, bank):
    digits = re.sub(r"\D", "", acct or "")
    name = bank or "บัญชี"
    if digits[-2:] in ACCT_SET:
        name += f" ชุดที่ {ACCT_SET.index(digits[-2:]) + 1}"
    return f"{name} ({acct})"


# ── NISIT ────────────────────────────────────────────────────────────────────────────────────
def _small_int(text):
    """Stored JSON is written by PCs; an absurdly long integer must not make every later reply unreadable
    (Python refuses ints over 4,300 digits). Real amounts are satang, far below this."""
    return int(text) if len(text) <= 18 else 0


def _finite_float(text):
    v = float(text)
    return v if v == v and abs(v) != float("inf") else 0.0


def rpc(name, payload, timeout=40):
    req = urllib.request.Request(
        f"{SB_URL}/rest/v1/rpc/{name}", data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"apikey": SB_ANON, "Authorization": "Bearer " + SB_ANON, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"), parse_int=_small_int, parse_float=_finite_float)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise Fail(f"ระบบ NISIT ตอบ HTTP {e.code} ที่ {name} ({body}) ลองใหม่อีกครั้ง "
                   f"ถ้ายังเป็นส่งภาพหน้าจอนี้ให้ Master")
    except (urllib.error.URLError, TimeoutError, socket.timeout) as e:
        raise Fail(f"ต่อระบบ NISIT ไม่ได้ที่ {name} ({e}) เช็คอินเทอร์เน็ตแล้วกดตรวจอีกครั้ง")
    except ValueError as e:   # unreadable JSON must surface as a message, not kill a thread
        raise Fail(f"อ่านคำตอบของระบบ NISIT ที่ {name} ไม่ได้ ({str(e)[:120]}) ส่งภาพหน้าจอนี้ให้ Master")


AUTH_MSG = "บัญชีนี้ยังไม่มีสิทธิ์หน้าเติมงบแอด หรือการล็อกอินหมดอายุ ล็อกอินใหม่ ถ้ายังไม่ได้ให้ Master เปิดสิทธิ์หน้าเติมงบแอด"


def load_data(token, d_from, d_to):
    r = rpc("billcheck_data", {"p_token": token, "p_from": d_from.isoformat(), "p_to": d_to.isoformat()})
    if not r.get("ok"):
        if r.get("error") == "unauthorized":
            raise AuthFail(AUTH_MSG)
        raise Fail(f"ดึงข้อมูลจากระบบไม่ได้ ({r.get('error')})")
    return r


# ── Meta billing PDFs ────────────────────────────────────────────────────────────────────────
def epochs(d_from, d_to):
    """Meta's date= param renders one day earlier than it says: start +1 day, end +2 (exclusive)."""
    s = dt.datetime(d_from.year, d_from.month, d_from.day, tzinfo=TZ) + dt.timedelta(days=1)
    e = dt.datetime(d_to.year, d_to.month, d_to.day, tzinfo=TZ) + dt.timedelta(days=2)
    return int(s.timestamp()), int(e.timestamp())


def range_label_re(d_from, d_to):
    m1, m2 = (re.escape(THAI_MONTH[d.month - 1]) for d in (d_from, d_to))
    return re.compile(rf"\b{d_from.day}\s*{m1}\s*(?:{d_from.year}\s*)?[-–]\s*{d_to.day}\s*{m2}\s*{d_to.year}")


def on_billing(url):
    # the login page carries ?next=...billing_hub..., so look at the path only
    u = urllib.parse.urlparse(url)
    return u.netloc == "business.facebook.com" and "/billing_hub/" in u.path and not LOGIN_URL.search(u.path)


def snap(page, out_dir, aid, tag):
    p = os.path.join(out_dir, "ภาพปัญหา", f"{aid}_{tag}.png")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    try:
        page.screenshot(path=p, full_page=True)
    except Exception:
        return ""
    return p


def dismiss_push_prompt(page):
    """Meta's "enable push notifications?" alertdialog blocks every click; decline it."""
    dlg = page.locator('[role="alertdialog"]').filter(has=page.get_by_role("button", name=CLOSE_BTN))
    for _ in range(3):
        if not dlg.count():
            return
        try:
            dlg.first.get_by_role("button", name=CLOSE_BTN).first.click(timeout=5000)
        except Exception:
            page.keyboard.press("Escape")
        page.wait_for_timeout(800)


# Thai fonts keep shifted vowel/tone-mark glyphs in the Private Use Area (U+F700-F71A); Meta's PDFs
# (Oct 2026) extract ไม้โท as U+F70B, so "ชำระแล้ว" came out as "ชำระแลว" and no bill counted as paid
THAI_PUA = str.maketrans({
    0xF700: "ฐ", 0xF701: "ิ", 0xF702: "ี", 0xF703: "ึ", 0xF704: "ื", 0xF705: "่", 0xF706: "้", 0xF707: "๊",
    0xF708: "๋", 0xF709: "์", 0xF70A: "่", 0xF70B: "้", 0xF70C: "๊", 0xF70D: "๋", 0xF70E: "์", 0xF70F: "ญ",
    0xF710: "ั", 0xF711: "ํ", 0xF712: "็", 0xF713: "่", 0xF714: "้", 0xF715: "๊", 0xF716: "๋", 0xF717: "์",
    0xF718: "ุ", 0xF719: "ู", 0xF71A: "ฺ"})


def pdf_text(path):
    import fitz
    doc = fitz.open(path)
    try:
        return "\n".join(p.get_text() for p in doc).translate(THAI_PUA)
    finally:
        doc.close()


def parse_pdf(path):
    text = pdf_text(path)
    m = ACCT_ID_RE.search(text)
    rows = []
    for b in text.split("การชำระเงิน โฆษณาบน Meta")[1:]:
        hm = HDR_CARD_RE.search(b)
        block_card = hm.group(1) if hm else ("เครดิตโฆษณา" if HDR_CREDIT_RE.search(b) else "")
        for d, tid, card, amt, status in ROW_RE.findall(b):
            dd, mm, yy = d.split("/")
            rows.append({"day": dt.date(int(yy), int(mm), int(dd)), "txid": tid.replace("\n", ""),
                         "card": card or block_card, "amount": float(amt.replace(",", "")),
                         "status": status.strip()})
    return (m.group(1) if m else ""), rows


def body_text(page):
    try:
        return page.inner_text("body") if page.locator("body").count() else ""
    except Exception:
        return ""


def fetch_one(page, aid, eps, label_re, dest, out_dir):
    """-> ('ok', path) | ('none', why) | ('login', why) | ('error', why)."""
    page.goto(BILLING_URL.format(aid=aid, s=eps[0], e=eps[1]), wait_until="domcontentloaded")
    btn = page.locator('[role="button"][aria-haspopup="menu"]', has_text=DOWNLOAD_BTN)
    end = time.time() + 45
    none_since = None
    state = None
    while time.time() < end:
        if LOGIN_URL.search(page.url):
            return "login", "Facebook หลุดล็อกอิน"
        body = body_text(page)
        if btn.count() and label_re.search(body):
            state = "btn"
            break
        # The table says "no transactions" while it is still loading (an account with 2.0M spend,
        # 02/10/2026): believe it only after it stayed on screen NONE_STABLE seconds with no button.
        if NO_TX.search(body) and label_re.search(body):
            none_since = none_since or time.time()
            if time.time() - none_since >= NONE_STABLE:
                state = "none"
                break
        else:
            none_since = None
        page.wait_for_timeout(1000)
    if state == "none":
        return "none", "ไม่มีธุรกรรมในช่วงนี้"
    if state is None:
        body = body_text(page)
        why = ("หน้าไม่ขึ้นช่วงวันที่ที่ขอ (อาจไม่มีสิทธิ์ดูบัญชีนี้)" if not label_re.search(body)
               else "หาปุ่มดาวน์โหลดไม่เจอ (Meta อาจเปลี่ยนหน้า)")
        return "error", f"{why} ภาพ {snap(page, out_dir, aid, 'nobutton')}"

    dismiss_push_prompt(page)
    btn.first.click()
    item = page.get_by_text(PDF_ITEM)
    try:
        item.first.wait_for(state="visible", timeout=10000)
    except Exception:
        return "error", f"กดดาวน์โหลดแล้วไม่มีเมนู PDF ภาพ {snap(page, out_dir, aid, 'nomenu')}"
    try:
        with page.expect_download(timeout=90000) as dl:
            item.first.click()
        dl.value.save_as(dest)
    except Exception as e:
        return "error", f"รอไฟล์ไม่มา ({type(e).__name__}) ภาพ {snap(page, out_dir, aid, 'nodownload')}"
    # every Meta download has the same file name; a late file can land under the wrong account
    got, _ = parse_pdf(dest)
    if got != aid:
        return "error", f"PDF ที่ได้เป็นของบัญชี {got or 'ไม่ทราบ'} ไม่ใช่ {aid}"
    return "ok", dest


class MetaBrowser:
    """Playwright's own Chromium on a kept profile (an installed Chrome closed itself on Meta's download
    popup, 02/10/2026). Minimised while it works; brought back only when Facebook wants a login."""

    def __init__(self, rep, out_dir):
        self.rep, self.out_dir = rep, out_dir
        self.pw = self.ctx = self.page = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        os.makedirs(PROFILE, exist_ok=True)
        self.pw = sync_playwright().start()
        try:
            self.ctx = self.pw.chromium.launch_persistent_context(
                user_data_dir=PROFILE, headless=False, accept_downloads=True,
                locale="th-TH", viewport={"width": 1300, "height": 850})
        except Exception as e:
            self.pw.stop()
            msg = str(e).splitlines()[0][:200]
            if "user data directory is already in use" in msg.lower() or "ProcessSingleton" in msg:
                raise Fail("หน้าต่าง Facebook ของโปรแกรมเปิดค้างอยู่ ปิดหน้าต่างนั้นก่อนแล้วกดตรวจอีกครั้ง")
            raise Fail(f"เปิดเบราว์เซอร์ไม่ได้ ({msg}) ปิดโปรแกรมแล้วเปิดใหม่")
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        return self

    def __exit__(self, *exc):
        for f in (lambda: self.ctx.close(), lambda: self.pw.stop()):
            try:
                f()
            except Exception:
                pass

    def _window(self, state):
        try:
            cdp = self.ctx.new_cdp_session(self.page)
            wid = cdp.send("Browser.getWindowForTarget")["windowId"]
            if state != "normal":
                cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
            cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": state}})
            cdp.detach()
        except Exception:
            pass

    def fresh_page(self):
        """A crashed tab stays dead; replace it."""
        try:
            if self.page and not self.page.is_closed():
                self.page.close()
        except Exception:
            pass
        self.page = self.ctx.new_page()
        self.page.goto(HOME_URL, wait_until="domcontentloaded")

    def _billing_page(self):
        """Any open tab already on the billing hub (Facebook may finish the login in a new tab)."""
        for p in self.ctx.pages:
            try:
                if not p.is_closed() and on_billing(p.url):
                    return p
            except Exception:
                pass
        return None

    def ensure_login(self, timeout_min=15):
        try:
            self.page.goto(HOME_URL, wait_until="domcontentloaded")
            self.page.wait_for_timeout(3000)
            if on_billing(self.page.url):
                self._window("minimized")
                return
        except Exception:
            self.fresh_page()
        self._window("normal")
        try:
            self.page.bring_to_front()
        except Exception:
            pass
        self.rep.fb_login(True)
        self.rep.log(f"รอล็อกอิน Facebook สูงสุด {timeout_min} นาที ล็อกอินครั้งเดียว ครั้งต่อไปจำไว้ให้")
        end = time.time() + timeout_min * 60
        away_since = None
        try:
            while time.time() < end:
                self.rep.check_stop()
                try:
                    if self.page.is_closed():
                        live = [p for p in self.ctx.pages if not p.is_closed()]
                        self.page = live[-1] if live else self.ctx.new_page()
                    # wait_for_timeout, not time.sleep: the sync API only takes in navigation events while
                    # it is inside a Playwright call, so with time.sleep page.url stayed on the login page
                    # after the user had logged in and the run sat here for 15 minutes (ad team 16/10/2026)
                    self.page.wait_for_timeout(2000)
                    hit = self._billing_page()
                    if hit:
                        self.page = hit
                        self.page.wait_for_timeout(3000)
                        self.rep.log("ล็อกอิน Facebook สำเร็จ")
                        self._window("minimized")
                        return
                    url = self.page.url
                    u = urllib.parse.urlparse(url)
                    # logged in but somewhere else (Business home, the facebook.com feed): after a few quiet
                    # seconds go to the billing hub; login, 2FA and checkpoint pages are left alone
                    if u.netloc.endswith("facebook.com") and not LOGIN_URL.search(u.path) and not LOGIN_STEP.search(u.path):
                        away_since = away_since or time.time()
                        if time.time() - away_since >= 6:
                            away_since = None
                            self.page.goto(HOME_URL, wait_until="domcontentloaded")
                    else:
                        away_since = None
                except Fail:
                    raise
                except Exception:
                    try:
                        self.fresh_page()
                    except Exception:
                        raise Fail("หน้าต่าง Facebook ถูกปิดก่อนล็อกอินเสร็จ กดตรวจอีกครั้งแล้วล็อกอินให้เสร็จ")
        finally:
            self.rep.fb_login(False)
        raise Fail(f"ยังไม่ได้ล็อกอิน Facebook ภายใน {timeout_min} นาที กดตรวจอีกครั้งแล้วล็อกอินในหน้าต่างที่เด้งขึ้นมา")

    def fetch(self, accounts, d_from, d_to, pdf_dir, done0=0, total=None):
        eps, label_re = epochs(d_from, d_to), range_label_re(d_from, d_to)
        total = total or len(accounts)
        result = {}
        for n, a in enumerate(accounts, 1):
            self.rep.check_stop()
            self.rep.account(a["id"], a["name"], "loading")
            dest = os.path.join(pdf_dir, f"{safe_name(a['name'])} {a['id']}.pdf")   # names can repeat
            st, info = "error", "ไม่ได้ลอง"
            for attempt in range(2):
                try:
                    st, info = fetch_one(self.page, a["id"], eps, label_re, dest, self.out_dir)
                except Exception as e:
                    st, info = "error", f"{type(e).__name__}: {str(e).splitlines()[0][:150]}"
                    try:
                        self.fresh_page()   # tab crashed or hung: one retry on a new tab
                    except Exception:
                        raise Fail("หน้าต่าง Facebook ถูกปิดระหว่างโหลดบิล กดตรวจอีกครั้ง แล้วอย่าปิดหน้าต่างนั้น"
                                   "จนโปรแกรมทำเสร็จ")
                    continue
                if st == "login":
                    self.ensure_login()
                    continue
                break
            result[a["id"]] = (st, info)
            self.rep.account(a["id"], a["name"], st, "" if st in ("ok", "none") else info)
            self.rep.progress(done0 + n, total, a["name"])
        return result


# ── matching (same windows and order as the monthly reconciliation) ───────────────────────────
def _subset(cands, target):
    """Indexes of >=2 amounts (satang) summing exactly to target; DFS, largest first, bounded."""
    order = sorted(range(len(cands)), key=lambda i: -cands[i])
    vals = [cands[i] for i in order]
    suffix = [0] * (len(vals) + 1)
    for i in range(len(vals) - 1, -1, -1):
        suffix[i] = suffix[i + 1] + vals[i]
    budget = [200000]
    pick = []

    def go(i, left):
        if left == 0:
            return len(pick) >= 2
        if i == len(vals) or left < 0 or suffix[i] < left or budget[0] <= 0:
            return False
        budget[0] -= 1
        if vals[i] <= left:
            pick.append(order[i])
            if go(i + 1, left - vals[i]):
                return True
            pick.pop()
        return go(i + 1, left)

    return list(pick) if go(0, target) else None


def match(inv, sms):
    """inv: [{txid, acct, day, amt}] · sms: [{id, acct, day, amt}] (amt in satang).
    Sets r['sms'] on paired bill rows; returns {sms_id: (kind, [bill rows])}."""
    used = set()
    out = collections.defaultdict(lambda: ["", []])

    def pair(r, s, kind):
        used.add(s["id"])
        r["sms"] = s["id"]
        out[s["id"]][0] = kind
        out[s["id"]][1].append(r)

    def has_exact(s, lo, hi):
        # an SMS equal to some unpaired bill row is that row's payment, not a fee/bulk candidate
        return any("sms" not in r and r["acct"] == s["acct"] and r["amt"] == s["amt"]
                   and lo <= (s["day"] - r["day"]).days <= hi for r in inv)

    def one_to_one(extra, kind, lo, hi):
        # same account + amount (many 30,000 a day, some SMS never arrive): align both time-ordered
        # lists for the most pairs, then the smallest total day gap
        groups = collections.defaultdict(lambda: ([], []))
        for r in inv:
            if "sms" not in r:
                groups[(r["acct"], r["amt"] + extra)][0].append(r)
        for s in sms:
            if s["id"] not in used and (s["acct"], s["amt"]) in groups and not (extra and has_exact(s, lo, hi)):
                groups[(s["acct"], s["amt"])][1].append(s)
        for R, S in groups.values():
            if not S:
                continue
            # the alignment below is only right on day-ordered lists; bill rows arrive per account and
            # newest first, so without this most same-amount rows found no partner (16/10)
            R.sort(key=lambda r: (r["day"], r["txid"]))
            S.sort(key=lambda s: (s["day"], str(s.get("at", "")), s["id"]))
            n, m = len(R), len(S)
            best = [[(0, 0)] * (m + 1) for _ in range(n + 1)]
            for i in range(1, n + 1):
                for j in range(1, m + 1):
                    b = max(best[i - 1][j], best[i][j - 1])
                    gap = (S[j - 1]["day"] - R[i - 1]["day"]).days
                    if lo <= gap <= hi:
                        p = best[i - 1][j - 1]
                        b = max(b, (p[0] + 1, p[1] - abs(gap)))
                    best[i][j] = b
            i, j = n, m
            while i and j:
                gap = (S[j - 1]["day"] - R[i - 1]["day"]).days
                p = best[i - 1][j - 1]
                if lo <= gap <= hi and best[i][j] == (p[0] + 1, p[1] - abs(gap)):
                    pair(R[i - 1], S[j - 1], kind)
                    i, j = i - 1, j - 1
                elif best[i][j] == best[i - 1][j]:
                    i -= 1
                else:
                    j -= 1

    def bulk(lo, hi):
        for s in sms:
            if s["id"] in used or has_exact(s, lo, hi):
                continue
            cands = [r for r in inv if "sms" not in r and r["acct"] == s["acct"]
                     and lo <= (s["day"] - r["day"]).days <= hi]
            cands = sorted(cands, key=lambda r: abs((s["day"] - r["day"]).days))[:BULK_MAX_ROWS]
            if len(cands) < 2:
                continue
            for extra in (0, FEE):
                hit = _subset([r["amt"] for r in cands], s["amt"] - extra)
                if hit:
                    for k in hit:
                        pair(cands[k], s, "bulk")
                    break

    # tight windows first so a common 30,000 row is not lent to a far SMS, then widen for the rest
    one_to_one(FEE, "fee", -1, 1)
    bulk(-1, 1)
    one_to_one(0, "exact", -1, 1)
    one_to_one(0, "exact", -INV_LATE, GAP_1TO1)
    one_to_one(FEE, "fee", -INV_LATE, GAP_1TO1)
    bulk(-INV_LATE, BULK_DAYS)
    return out


KIND_TXT = {"exact": "ยอดตรงกัน", "fee": "บิล + ค่าธรรมเนียม 20 บาท", "bulk": "SMS เดียวจ่ายหลายบิลรวมกัน"}
ORDER = {"bad": 0, "dup": 1, "wait": 2, "ok": 3, "plat": 4}


def card_map(cards):
    """card4 -> (acct, bank) when the card belongs to exactly one bank account, else (None, why)."""
    out, bank_of = {}, {}
    for card4, lst in cards.items():
        accts = {}
        for c in lst:
            if c["acct"] not in accts or accts[c["acct"]] in (None, "", "ไม่ทราบ"):
                accts[c["acct"]] = c.get("bank")
        for a, b in accts.items():
            if b and b != "ไม่ทราบ" or a not in bank_of:
                bank_of[a] = b
        out[card4] = next(iter(accts.items())) if len(accts) == 1 else (None, "ambiguous")
    return out, bank_of


def build_rows(data, bills, d_judge, today, frozen=None, ghosts=()):
    """-> (sms_rows for the judged window, bills in the window with no SMS, sms_rows of everything loaded).
    Mutates the bill dicts: pass copies when calling it more than once.
    frozen = {sms_id: {"k": kind, "tx": [txid], "day": iso}}: pairs cleared green on earlier runs. They
    keep their bills, so a new SMS can only take a bill nobody has paid yet. Without this, a same-amount
    fraud could push every older pair one slot back until a bill at the edge of the window, whose real
    SMS was never loaded, absorbs it (astra round 5).
    ghosts = unresolved SMS from earlier runs that this run did not load ({id, acct, amt, day}); they still
    count as rivals, so a pair does not turn green just because its rival aged out of the window."""
    frozen = frozen or {}
    cards, bank_of = card_map(data["cards"])
    sms_all = [{"id": s["id"], "acct": s["acct"], "at": dt.datetime.fromisoformat(s["at"]),
                "amount": float(s["amount"]), "plat": s.get("plat")} for s in data["sms"]]
    for s in sms_all:
        s["day"] = s["at"].date()
        s["amt"] = int(round(s["amount"] * 100))
    by_tx = {b["txid"]: b for b in bills}
    taken = {t for f in frozen.values() for t in f["tx"]}
    inv, floor = [], {}
    for b in bills:
        acct, why = cards.get(b["card"], (None, None)) if b["card"].isdigit() else (None, None)
        b["acct"], b["ambiguous"] = acct, why == "ambiguous"
        b["amt"] = int(round(b["amount"] * 100))
        b["paid"] = bool(PAID_RE.match(b["status"]))
        if b["acct"] and b["paid"]:
            # smallest paid bill row per bank account, frozen ones included (a newly arrived genuine SMS
            # must still be able to challenge a frozen pair, astra round 6)
            floor[b["acct"]] = min(floor.get(b["acct"], b["amt"]), b["amt"])
            if b["txid"] not in taken:
                inv.append(b)
    # manual platform tags (Google, TikTok...) in the topup page are not Meta money
    paired = match(inv, [s for s in sms_all if not s["plat"] and s["id"] not in frozen])
    for sid, f in frozen.items():
        # frozen amounts keep the floor alive when this run could not download that account's bills
        for a in f.get("amts", []):
            if f.get("acct"):
                floor[f["acct"]] = min(floor.get(f["acct"], a), a)
        brs = [by_tx[t] for t in f["tx"] if t in by_tx]
        for b in brs:
            b["sms"] = sid
        paired[sid] = [f["k"], brs]

    rows = []
    for s in sms_all:
        kind, brs = paired.get(s["id"], ("", []))
        if s["plat"]:
            st, note = "plat", f"แท็กไว้ในระบบว่า {s['plat']}"
        elif kind:
            st, note = "ok", KIND_TXT.get(kind, kind) + (" (จับคู่ไว้ตั้งแต่รอบก่อน)" if s["id"] in frozen else "")
        elif (today - s["day"]).days < INV_LATE:
            st, note = "wait", "บิลขึ้นช้าได้ 1 ถึง 3 วัน"
        else:
            st, note = "bad", ("บัญชีนี้ไม่มีบัตรที่ผูกกับแอด Meta" if s["acct"] not in bank_of
                               else "ไม่มีบิล Meta ยอดนี้ของบัญชีนี้ ให้เช็คกับธนาคาร")
        rows.append({**s, "status": st, "note": note, "bills": brs, "kind": kind,
                     "acct_label": set_label(s["acct"], bank_of.get(s["acct"]))})
    # SMS carry no merchant name, so a green pair is only as good as the absence of a rival. One SMS on a
    # bank account that is still unpaired (red, waiting, or before the judged window) and at least as big
    # as that account's smallest bill row could be the real payment of any nearby bill, directly, with
    # the 20 baht fee or inside a bulk sum, and a wrong pick can push other pairs along in a chain.
    # So every pair on that account within reach of it is uncertain, and so is everything within reach
    # of those, until nothing changes. False alarms over false clearance (astra rounds 1-4).
    span = BULK_DAYS + INV_LATE
    real = [s for s in sms_all if not s["plat"]]
    # an SMS still inside the bill-lag window is not a rival yet (most mornings have a few, and they would
    # turn every pair of the account yellow); if its bill never comes it turns red within INV_LATE days and
    # then pulls down whatever took that bill, frozen pairs included
    nodes = real + list(ghosts)   # ghosts both start and carry a chain (astra round 9)
    queue = [s for s in nodes
             if s["id"] not in paired and s["acct"] in floor and s["amt"] >= floor[s["acct"]]
             and (today - s["day"]).days >= INV_LATE]
    shaky = {s["id"] for s in queue}
    while queue:
        u = queue.pop()
        for p in nodes:
            if (p["id"] not in shaky and p["id"] in paired and p["acct"] == u["acct"]
                    and abs((p["day"] - u["day"]).days) <= span):
                shaky.add(p["id"])
                queue.append(p)
    for r in rows:
        if r["status"] == "ok" and r["id"] in shaky:
            r["status"], r["note"] = "dup", ("บัญชีนี้มี SMS เงินออกที่ยังไม่มีคู่ในช่วงใกล้กัน "
                                             "ให้เทียบเวลากับ statement ธนาคาร")
    rows_all = rows
    rows = sorted((r for r in rows_all if r["day"] >= d_judge), key=lambda r: (ORDER[r["status"]], r["at"]))

    lonely = [b for b in bills if "sms" not in b and b["day"] >= d_judge - dt.timedelta(days=INV_LATE)]
    for b in lonely:
        if b["card"] == "เครดิตโฆษณา":
            b["why"] = "จ่ายด้วยเครดิตโฆษณา ไม่ตัดบัตร"
        elif not b["paid"]:
            b["why"] = f"สถานะ {b['status']} เงินไม่ได้ออก"
        elif b["ambiguous"]:
            b["why"] = "เลขท้ายบัตรนี้ซ้ำกันหลายบัญชีธนาคาร ระบบไม่จับคู่ให้ แก้ในหน้าเติมงบแอด"
        elif not b["acct"]:
            b["why"] = "บัตรนี้ไม่ได้ผูกบัญชีที่มี SMS (เช่น บัตรเครดิต)"
        else:
            b["why"] = "ยังไม่เห็น SMS เงินออก"
    lonely.sort(key=lambda b: (b["day"], b["ad_name"]))
    return rows, lonely, rows_all


# ── run ──────────────────────────────────────────────────────────────────────────────────────
def judged_start(state, today, days):
    """Yesterday, or --days back; then further back to the oldest SMS still unresolved (--days can only
    widen the window, never hide a tracked SMS, astra round 8)."""
    start = today - dt.timedelta(days=1 if days is None else max(0, min(days, MAX_BACK)))
    floor = today - dt.timedelta(days=MAX_BACK)
    for o in state.get("open", {}).values():
        d = dt.date.fromisoformat(o["day"])
        if floor <= d < start:
            start = d
    return start


def prune_runs():
    cut = time.time() - KEEP_RUN_DAYS * 86400
    try:
        for name in os.listdir(RUNS_DIR):
            p = os.path.join(RUNS_DIR, name)
            if os.path.isdir(p) and os.path.getmtime(p) < cut:
                shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


def collect(got, name_of):
    bills, fails, nones = [], [], []
    for aid, (st, info) in got.items():
        if st == "ok":
            pid, rows = parse_pdf(info)
            if pid != aid:   # re-check at read time too: a later download must not stand in for this one
                fails.append((name_of[aid], f"PDF เป็นของบัญชี {pid or 'ไม่ทราบ'}"))
                continue
            for r in rows:
                bills.append({**r, "ad_id": aid, "ad_name": name_of[aid]})
        elif st == "none":
            nones.append(name_of[aid])
        else:
            fails.append((name_of[aid], info))
    return bills, fails, nones


def bill_out(b):
    return {"ad": b["ad_name"], "ad_id": b["ad_id"], "card": b["card"], "day": ddmmyyyy(b["day"]),
            "txid": b["txid"], "amount": b["amount"]}


def finish(token, run_id, payload, rep):
    """billcheck_finish is idempotent, so a lost reply is retried rather than reported as a failure."""
    last = None
    for attempt in range(4):
        try:
            r = rpc("billcheck_finish", {"p_token": token, "p_run": run_id, "p_result": payload}, timeout=60)
        except Fail as e:
            last = str(e)
            rep.log(f"ส่งผลขึ้นระบบยังไม่สำเร็จ ลองใหม่ ({attempt + 1}/4)")
            time.sleep(5 * (attempt + 1))
            continue
        if r.get("ok"):
            return
        if r.get("error") == "unauthorized":
            raise AuthFail(AUTH_MSG)
        raise Fail(f"ระบบไม่รับผลตรวจ ({r.get('error')}{' ' + str(r['n']) + ' แถว' if r.get('n') else ''}) "
                   f"รอบที่ {run_id} ส่งภาพหน้าจอนี้ให้ Master")
    raise Fail(f"ส่งผลตรวจขึ้นระบบไม่สำเร็จ 4 ครั้ง ({last}) เช็คอินเทอร์เน็ตแล้วกดตรวจอีกครั้ง")


def _iso(v):
    try:
        return dt.date.fromisoformat(v) and v
    except (TypeError, ValueError):
        return None


def clean_state(state, rep):
    """The matcher memory is shared by every PC; one malformed entry must not stop them all (astra 15/10).
    Entries that cannot be read are dropped and reported, the rest kept."""
    if not isinstance(state, dict):
        state = {}
    def ints(v):
        return isinstance(v, list) and all(isinstance(a, int) for a in v)

    def open_ok(o):
        return (isinstance(o, dict) and _iso(o.get("day")) and isinstance(o.get("acct"), str)
                and isinstance(o.get("amt"), int))

    def pair_ok(f):
        return (isinstance(f, dict) and f.get("k") in KIND_TXT and _iso(f.get("day"))
                and isinstance(f.get("acct"), (str, type(None)))
                and isinstance(f.get("tx"), list) and all(isinstance(t, str) for t in f["tx"])
                and ints(f.get("amts", [])))

    opens, pairs, bad = {}, {}, 0
    for k, o in (state.get("open") if isinstance(state.get("open"), dict) else {}).items():
        if open_ok(o):
            opens[k] = o
        else:
            bad += 1
    for k, f in (state.get("pairs") if isinstance(state.get("pairs"), dict) else {}).items():
        if pair_ok(f):
            pairs[k] = f
        else:
            bad += 1
    if bad:
        rep.log(f"ข้ามประวัติที่อ่านไม่ได้ {bad} รายการ")
    out = {"open": opens, "pairs": pairs}
    if _iso(state.get("since")):
        out["since"] = state["since"]
    return out


def run_check(token, rep=None, days=None, only=None, host=None, write=True):
    """One full check. Returns a dict for the UI. Raises Fail / AuthFail / Stopped."""
    rep = rep or Reporter()
    today = today_bkk()
    pilot = bool(only) or not write
    host = (host or os.environ.get("COMPUTERNAME") or socket.gethostname() or "")[:60]

    rep.step("nisit", "เชื่อมระบบ NISIT")
    begin = rpc("billcheck_begin", {"p_token": token, "p_host": host})
    if not begin.get("ok"):
        if begin.get("error") == "unauthorized":
            raise AuthFail(AUTH_MSG)
        if begin.get("error") == "busy":
            raise Fail(f"มีคนกำลังตรวจอยู่ ({begin.get('by')} เครื่อง {begin.get('host') or 'ไม่ทราบชื่อ'} "
                       f"เริ่ม {begin.get('since')}) รอให้เครื่องนั้นเสร็จก่อน แล้วดูผลในหน้าเติมงบแอด")
        raise Fail(f"เริ่มรอบตรวจไม่ได้ ({begin.get('error')})")
    run_id, state = begin["run_id"], begin.get("state") or {}
    state = clean_state(state, rep)
    try:
        out = _run(token, rep, today, state, days, only, run_id, pilot)
    except BaseException as e:
        why = "กดหยุด" if isinstance(e, Stopped) else (str(e) if isinstance(e, Fail) else f"{type(e).__name__}: {e}")
        try:
            rpc("billcheck_abort", {"p_token": token, "p_run": run_id, "p_error": why[:500]}, timeout=15)
        except Exception:
            pass   # the lease expires by itself
        raise
    return out


def _run(token, rep, today, state, days, only, run_id, pilot):
    d_judge = judged_start(state, today, days)
    bill_from = d_judge - dt.timedelta(days=BILL_BACK)
    sms_from = d_judge - dt.timedelta(days=SMS_BACK)

    rep.step("data", "ดึงรายชื่อบัญชีโฆษณาและ SMS เงินออก")
    data = load_data(token, sms_from, today)
    accounts = data["accounts"]
    if only:
        want = {x.strip() for x in only.split(",") if x.strip()}
        accounts = [a for a in accounts if a["id"] in want]
    name_of = {a["id"]: a["name"] for a in accounts}
    rep.log(f"บัญชีโฆษณา {len(accounts)} บัญชี SMS เงินออก {len(data['sms'])} รายการ "
            f"ตรวจ SMS ตั้งแต่ {ddmmyyyy(d_judge)}")

    frozen = {k: v for k, v in state.get("pairs", {}).items()
              if dt.date.fromisoformat(v["day"]) >= today - dt.timedelta(days=MAX_BACK + BILL_BACK)}
    loaded = {s["id"] for s in data["sms"]}
    ghosts = [{"id": k, "acct": v["acct"], "amt": v["amt"], "day": dt.date.fromisoformat(v["day"])}
              for k, v in state.get("open", {}).items()
              if k not in loaded and dt.date.fromisoformat(v["day"]) >= today - dt.timedelta(days=KEEP_OPEN)]

    # pass 1: accounts the system saw charging since the bill window opened
    first = accounts if only else [a for a in accounts if (a.get("last_charge") or "") >= bill_from.isoformat()]
    prune_runs()
    out_dir = os.path.join(RUNS_DIR, dt.datetime.now(TZ).strftime("%Y-%m-%d %H%M%S"))
    pdf_dir = os.path.join(out_dir, "บิล Meta")
    os.makedirs(pdf_dir, exist_ok=True)

    rep.step("facebook", "เปิด Facebook")
    with MetaBrowser(rep, out_dir) as mb:
        mb.ensure_login()
        rep.step("bills", f"โหลดบิลบัญชีที่ระบบเห็นว่าตัดเงิน {len(first)} บัญชี")
        rep.log(f"ช่วงบิล {ddmmyyyy(bill_from)} ถึง {ddmmyyyy(today)}")
        got = mb.fetch(first, bill_from, today, pdf_dir, 0, len(first))

        # pass 2 ("search"): a bank account that still has an SMS without a bill gets the bills of every
        # other ad account whose card pays from it, or whose card the system does not know yet
        bills, _, _ = collect(got, name_of)
        _, _, rows_all = build_rows(data, [dict(b) for b in bills], d_judge, today, frozen, ghosts)
        need = {r["acct"] for r in rows_all if r["status"] in ("bad", "wait", "dup")} | {g["acct"] for g in ghosts}
        cards, _ = card_map(data["cards"])
        second = [a for a in accounts if a["id"] not in got and need and
                  (not a.get("cards4") or any(cards.get(c, (None,))[0] in need for c in a["cards4"]))]
        if second:
            rep.step("search", f"ยังมี SMS ที่ไม่เจอบิล หาต่อในอีก {len(second)} บัญชี")
            got.update(mb.fetch(second, bill_from, today, pdf_dir, len(first), len(first) + len(second)))

    rep.step("match", "จับคู่บิลกับ SMS แล้วส่งผลขึ้นระบบ")
    bills, fails, nones = collect(got, name_of)
    rows, lonely, rows_all = build_rows(data, bills, d_judge, today, frozen, ghosts)
    cnt = collections.Counter(r["status"] for r in rows)
    sums = collections.Counter()
    for r in rows:
        sums[r["status"]] += r["amount"]
    summary = {"n": dict(cnt), "sum": {k: round(v, 2) for k, v in sums.items()}, "accounts": len(accounts),
               "pass1": len(first), "pass2": len(second), "dl_ok": sum(st == "ok" for st, _ in got.values()),
               "none": len(nones), "fails": len(fails), "first": not frozen, "today": today.isoformat()}

    # judged rows, plus context rows that are green or got pulled down to "dup": an SMS frozen green on an
    # earlier run must lose its green in the system the day a rival shows up, even when it is older than
    # the judged window (astra 15/10); a platform tag added later wins the same way. Context red/wait stay
    # out: their bills can sit before bill_from.
    upload = [r for r in rows_all if r["day"] >= d_judge or r["status"] in ("ok", "dup", "plat")]
    payload = {
        "d_judge": d_judge.isoformat(), "bill_from": bill_from.isoformat(), "summary": summary,
        "rows": [{"id": r["id"], "status": r["status"], "kind": r["kind"] or None, "note": r["note"],
                  "bills": [bill_out(b) for b in r["bills"]]} for r in upload],
        "lonely": [{**bill_out(b), "why": b["why"]} for b in lonely],
        "fails": [{"ad": n, "why": w} for n, w in fails]}
    if not pilot:
        # unresolved SMS stay tracked, context days included, with what they need to keep acting as a
        # rival after they leave the window (astra rounds 7-8). SMS from before the first run are not
        # chased. A run with failed downloads still records them, it only skips freezing new pairs.
        since = dt.date.fromisoformat(state["since"]) if state.get("since") else d_judge
        open_ = {r["id"]: {"day": r["day"].isoformat(), "acct": r["acct"], "amt": r["amt"]} for r in rows_all
                 if r["status"] in ("bad", "wait", "dup") and r["day"] >= since}
        for g in ghosts:   # not loaded this run, so not resolved either
            open_.setdefault(g["id"], {"day": g["day"].isoformat(), "acct": g["acct"], "amt": g["amt"]})
        # every full green pair loaded this run is kept, context days included (a pair left unsaved could
        # be shifted on a later run once its neighbour falls out of the window, astra round 6); yellow and
        # red stay free to be re-decided
        if not fails:
            for r in rows_all:
                if r["status"] == "ok" and r["id"] not in frozen:
                    frozen[r["id"]] = {"k": r["kind"], "tx": [b["txid"] for b in r["bills"]],
                                       "day": r["day"].isoformat(), "acct": r["acct"],
                                       "amts": [b["amt"] for b in r["bills"]]}
        payload["state"] = {"open": open_, "pairs": frozen, "since": since.isoformat(),
                            "last_run": today.isoformat()}
        finish(token, run_id, payload, rep)
        rep.log("ส่งผลขึ้นระบบ NISIT แล้ว")
    else:
        rpc("billcheck_abort", {"p_token": token, "p_run": run_id, "p_error": "ลองบางบัญชี ไม่บันทึกผล"}, timeout=15)
        rep.log("ลองบางบัญชี ไม่ได้ส่งผลขึ้นระบบ")

    return {"summary": summary, "d_judge": ddmmyyyy(d_judge), "bill_from": ddmmyyyy(bill_from),
            "today": ddmmyyyy(today), "saved": not pilot, "folder": out_dir,
            "rows": [{"at": r["at"].strftime("%d/%m/%Y %H:%M"), "acct": r["acct_label"], "amount": r["amount"],
                      "status": r["status"], "note": r["note"], "bills": [bill_out(b) for b in r["bills"]]}
                     for r in rows],
            "lonely": payload["lonely"], "fails": payload["fails"], "nones": nones}


def main():
    ap = argparse.ArgumentParser(description="เช็คเงินตัดแอด Meta เทียบ SMS ธนาคาร")
    ap.add_argument("--days", type=int, help="เช็ค SMS ย้อนหลังกี่วัน (ค่าเริ่มต้น เมื่อวาน + ที่ยังค้าง)")
    ap.add_argument("--only", help="เลขบัญชีโฆษณา คั่นด้วย comma (ลองบางบัญชี ไม่บันทึกผล)")
    ap.add_argument("--dry", action="store_true", help="ตรวจอย่างเดียว ไม่ส่งผลขึ้นระบบ")
    args = ap.parse_args()
    token = os.environ.get("BILLCHECK_TOKEN")
    if not token:
        raise SystemExit("ใช้งานปกติผ่าน START.bat (app.py) · ตัวนี้ต้องตั้ง env BILLCHECK_TOKEN")
    try:
        res = run_check(token, Reporter(), args.days, args.only, write=not args.dry)
    except (Fail, Stopped) as e:
        raise SystemExit(str(e) or "หยุดแล้ว")
    n = res["summary"]["n"]
    print(f"\nสรุป SMS {len(res['rows'])} รายการ: ตรงบิล {n.get('ok', 0)}, ยอดซ้ำต้องเทียบ {n.get('dup', 0)}, "
          f"รอบิล {n.get('wait', 0)}, ไม่เจอในบิล {n.get('bad', 0)}, แพลตฟอร์มอื่น {n.get('plat', 0)}")
    for r in res["rows"]:
        if r["status"] in ("bad", "dup"):
            print(f"  [{r['status']}] {r['at']} {r['acct']} {r['amount']:,.2f} {r['note']}")
    print(f"PDF อยู่ที่ {res['folder']}")


if __name__ == "__main__":
    main()
