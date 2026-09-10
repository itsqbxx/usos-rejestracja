import json, shutil, sys, threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import usos_rej as U

import tempfile
FIXTURES = Path(__file__).resolve().parent / "fixtures"
HERE = Path(tempfile.mkdtemp(prefix="usos_test_"))  # pliki robocze testu
SAMPLE = (FIXTURES / "sample.html").read_text(encoding="utf-8")
DEFAULT_UA = U.USER_AGENT
# po "logowaniu" strona otwiera dodatkową kartę (jak np. ekran powitalny Edge) - wykrywanie
# nie może zależeć od tego, która karta jest ostatnia
LOGGED = ('<script>JSGLOBALS = { user_id: "123456" };</script><p>Witaj</p>'
          '<script>window.open("about:blank")</script>')
gets = []

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        q = dict(parse_qsl(urlparse(self.path).query))
        cookie = self.headers.get("Cookie") or ""
        gets.append((q.get("_action"), cookie, self.headers.get("User-Agent")))
        extra = []
        if q.get("_action") == "logowaniecas/index":
            body, extra = LOGGED, [("Set-Cookie", "PHPSESSID=fromlogin; Path=/; HttpOnly")]
        else:
            now = datetime.now()
            body = SAMPLE.replace("2026-09-10 17:00:00", (now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")) \
                         .replace("2026-09-10 14:53:56", now.strftime("%Y-%m-%d %H:%M:%S")) \
                         .replace("https://www.usosweb.uj.edu.pl", BASE)
            if "PHPSESSID=fromlogin" not in cookie:
                body = body.replace('user_id: "123456"', 'user_id: ""')  # stara sesja = wylogowany
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        for k, v in extra:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
threading.Thread(target=srv.serve_forever, daemon=True).start()
U.HOST, U.CONTROLLER = BASE, BASE + "/kontroler.php"
U.PROFILE_DIR = HERE / "profil_test"
shutil.rmtree(U.PROFILE_DIR, ignore_errors=True)
orig_login = U.browser_login
U.browser_login = lambda path, **kw: orig_login(path, headless=True)  # w teście bez okna

CFG, COOKIE = HERE / "cfg_login.json", HERE / "cookie_login.txt"
CFG.write_text(json.dumps({"rej_kod": "R", "cdyd_kod": "26/27Z", "przedmioty": [
    {"nazwa": "Analityka", "prz_kod": "X", "grupy": {"Wykład": [1], "Ćwiczenia": [2]}}]}), encoding="utf-8")

# 1. wygasła sesja w cookie.txt -> skrypt sam loguje przez przeglądarkę i kontynuuje
COOKIE.write_text("PHPSESSID=old", encoding="utf-8")
sys.argv = ["x", "--config", str(CFG), "--cookie", str(COOKIE), "--dry-run"]
U.main()
saved = COOKIE.read_text()
print("cookie.txt po auto-logowaniu:\n" + saved)
assert saved.startswith("PHPSESSID=fromlogin") and "User-Agent: " in saved
browser_ua = saved.split("User-Agent: ")[1].strip()
assert gets[-1][1] == "PHPSESSID=fromlogin" and gets[-1][2] == browser_ua != DEFAULT_UA, gets[-1]
print("AUTO-RELOGIN OK (skrypt używa User-Agenta przeglądarki)\n")

# 2. brak cookie.txt + --login, potem świeży proces czyta UA z pliku
COOKIE.unlink()
sys.argv = ["x", "--config", str(CFG), "--cookie", str(COOKIE), "--login"]
U.main()
U.USER_AGENT = DEFAULT_UA
assert U.read_cookie(COOKIE) == "PHPSESSID=fromlogin" and U.USER_AGENT == browser_ua
print("--login OK")
