import builtins, io, json, sys, threading, time
from contextlib import redirect_stdout
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
SKEW = 0.3  # zegar atrapy serwera spieszy się o 0.3 s względem komputera

LIST_HTML = """<script>JSGLOBALS = { user_id: "123456" };</script>
<menu-left><a href='kontroler.php?_action=dla_stud/rejestracja/brdg2/grupyPrzedmiotu&prz_kod=SIDEBAR.X&cdyd_kod=26%2F27Z'>x</a></menu-left>
<main id="layout-main-content"><table>
<tr><th>Przedmiot</th><th>Cykl dyd.</th><th>Zajęcia</th><th>Akcje</th></tr>
<tr><td>Absolwent na rynku pracy [WFAIS.IF-X210.0]</td><td>26/27Z</td><td>Seminarium (1 grupa)</td>
    <td><a href='https://www.usosweb.uj.edu.pl/kontroler.php?_action=dla_stud/rejestracja/brdg2/grupyPrzedmiotu&amp;rej_kod=WFAIS_26%2F27Z_INF&amp;prz_kod=WFAIS.IF-X210.0&amp;cdyd_kod=26%2F27Z'>l</a></td></tr>
<tr><td>Bazy danych [WFAIS.IF-C202.0]</td><td>26/27Z</td><td>Wykład (1 grupa) Ćwiczenia (5 grup)</td><td></td></tr>
<tr><td>Teoria języków formalnych i metody translacji [WFAIS.IF-X206.0]</td><td>26/27Z</td><td>Wykład</td><td></td></tr>
</table></main>"""

# ---- 1. parser listy przedmiotów
lst = U.parse_course_list(LIST_HTML, "26/27Z")
print("lista:", lst)
assert [c["prz_kod"] for c in lst] == ["WFAIS.IF-X210.0", "WFAIS.IF-C202.0", "WFAIS.IF-X206.0"], lst
assert lst[0]["nazwa"] == "Absolwent na rynku pracy"

# ---- 2. limiter
lim = U.AttemptLimiter(3, 1.0)
t = time.time()
for _ in range(5):
    lim.acquire()
el = time.time() - t
print(f"limiter 5 prób przy 3/1s: {el:.2f}s")
assert 1.0 < el < 1.5

# ---- 3. atrapa serwera
T_OPEN = None
posts, blocked = [], []

def srv_now():
    return datetime.now() + timedelta(seconds=SKEW)

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, body, ctype):
        b = body.encode()
        self.send_response(200); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        q = dict(parse_qsl(urlparse(self.path).query))
        if q.get("_action", "").endswith("wyborPrzedmiotu"):
            return self.send(LIST_HTML, "text/html; charset=utf-8")
        html = SAMPLE.replace("2026-09-10 17:00:00", T_OPEN.strftime("%Y-%m-%d %H:%M:%S")) \
                     .replace("2026-09-10 14:53:56", srv_now().strftime("%Y-%m-%d %H:%M:%S")) \
                     .replace("https://www.usosweb.uj.edu.pl", BASE)
        self.send(html, "text/html; charset=utf-8")
    def do_POST(self):
        form = parse_qsl(self.rfile.read(int(self.headers["Content-Length"])).decode())
        now = srv_now()
        posts.append((now, form))
        recent = [p for p, _ in posts if (now - p).total_seconds() <= 20]
        if len(recent) > 10:
            blocked.append(now)
            resp = {"type": "!", "pl": "Rejestracja czasowo zablokowana"}
        elif now < T_OPEN:
            resp = {"type": "!", "pl": "Tura rejestracji nie jest otwarta"}
        else:
            resp = {"type": "v", "pl": "Zarejestrowano do grup"}
        self.send(json.dumps(resp), "application/json")

srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
threading.Thread(target=srv.serve_forever, daemon=True).start()
U.HOST, U.CONTROLLER = BASE, BASE + "/kontroler.php"
(HERE / "cookie.txt").write_text("Cookie: PHPSESSID=test123; other=1", encoding="utf-8")
CFG = HERE / "cfg.json"

# ---- 4. kreator (odpowiedzi podawane zamiast klawiatury)
T_OPEN = (srv_now() + timedelta(seconds=300)).replace(microsecond=0)
answers = iter(["", "2 1", "3 4", "9", "2", "t"])
builtins.input = lambda prompt="": (print(prompt, end=""), next(answers))[1]
sys.argv = ["x", "--config", str(CFG), "--cookie", str(HERE / "cookie.txt"), "--kreator"]
U.main()
cfg = json.loads(CFG.read_text(encoding="utf-8"))
assert [p["prz_kod"] for p in cfg["przedmioty"]] == ["WFAIS.IF-C202.0", "WFAIS.IF-X210.0"]
assert cfg["przedmioty"][0]["grupy"] == {"Wykład": [1], "Ćwiczenia": [3, 4]}
assert cfg["przedmioty"][1]["grupy"] == {"Wykład": [1], "Ćwiczenia": [2]}
cfg["przedmioty"].append({"nazwa": "Trzeci", "prz_kod": "C", "grupy": {"Ćwiczenia": [5]}})
CFG.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
print("\nKREATOR OK")

# ---- 5. probe
sys.argv = ["x", "--config", str(CFG), "--cookie", str(HERE / "cookie.txt"), "--probe"]
buf = io.StringIO()
with redirect_stdout(buf):
    U.main()
out = buf.getvalue()
print(out[-900:])
assert "zajecia[628405][] = 4" in out and "Tura rejestracji nie jest otwarta" in out
posts.clear()
print("PROBE OK")

# ---- 6. pełny przebieg: 3 przedmioty, zegar serwera przesunięty, limit prób
T_OPEN = (srv_now() + timedelta(seconds=40)).replace(microsecond=0)
sys.argv = ["x", "--config", str(CFG), "--cookie", str(HERE / "cookie.txt"), "--duration", "8"]
U.main()
print("\nT_OPEN (czas serwera):", T_OPEN.time())
for t, form in posts:
    print(" ", t.time(), [v for k, v in form if k.startswith("zajecia")])
early = [t for t, _ in posts if t < T_OPEN]
first = min(t for t, _ in posts if t >= T_OPEN)
print(f"strzałów przed otwarciem: {len(early)}, pierwszy po otwarciu po {(first - T_OPEN).total_seconds():.3f}s, "
      f"blokad: {len(blocked)}, POSTów łącznie: {len(posts)}")
assert not blocked and len(early) == 0 and (first - T_OPEN).total_seconds() < 0.9

# ---- 7. celowo za wczesny strzał: ponawia tylko lider, reszta czeka - bez blokady, wszyscy zapisani
posts.clear(); blocked.clear()
T_OPEN = (srv_now() + timedelta(seconds=30)).replace(microsecond=0)
sys.argv = ["x", "--config", str(CFG), "--cookie", str(HERE / "cookie.txt"), "--duration", "8", "--delay", "-0.6"]
U.main()
early = [t for t, _ in posts if t < T_OPEN]
late = [t for t, _ in posts if t >= T_OPEN]
print(f"\nza wcześnie: {len(early)} POSTów, po otwarciu: {len(late)}, blokad: {len(blocked)}, "
      f"ostatni zapis {(max(late) - T_OPEN).total_seconds():.2f}s po otwarciu")
assert not blocked and len(early) <= 3 + 2 and len(late) == 3 and (max(late) - T_OPEN).total_seconds() < 1.5
print("WSZYSTKO OK")
