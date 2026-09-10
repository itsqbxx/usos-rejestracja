import json, sys, threading, time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import usos_rej as U
import usos_gui as G
import tkinter as tk

import tempfile
FIXTURES = Path(__file__).resolve().parent / "fixtures"
HERE = Path(tempfile.mkdtemp(prefix="usos_test_"))  # pliki robocze testu
SAMPLE = (FIXTURES / "sample.html").read_text(encoding="utf-8")
LIST_HTML = """<script>JSGLOBALS = { user_id: "123456" };</script>
<main id="layout-main-content"><table>
<tr><th>Przedmiot</th><th>Cykl dyd.</th><th>Zajęcia</th><th>Akcje</th></tr>
<tr><td>Absolwent na rynku pracy [WFAIS.IF-X210.0]</td><td>26/27Z</td><td>x</td><td></td></tr>
<tr><td>Bazy danych [WFAIS.IF-C202.0]</td><td>26/27Z</td><td>x</td><td></td></tr>
<tr><td>Techniki WWW [WFAIS.IF-X205.0]</td><td>26/27Z</td><td>x</td><td></td></tr>
</table></main>"""
T_OPEN = (datetime.now() + timedelta(hours=1)).replace(microsecond=0)

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
        html = "<cas-bar logged-user='Jan Testowy'></cas-bar>" + \
            SAMPLE.replace("2026-09-10 17:00:00", T_OPEN.strftime("%Y-%m-%d %H:%M:%S")) \
                  .replace("2026-09-10 14:53:56", datetime.now().strftime("%Y-%m-%d %H:%M:%S")) \
                  .replace("https://www.usosweb.uj.edu.pl", BASE)
        self.send(html, "text/html; charset=utf-8")
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send(json.dumps({"type": "!", "pl": "Tura rejestracji nie jest otwarta"}), "application/json")

srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
threading.Thread(target=srv.serve_forever, daemon=True).start()
U.HOST, U.CONTROLLER = BASE, BASE + "/kontroler.php"

G.CONFIG, G.COOKIE, G.LOGFILE = HERE / "gui_cfg.json", HERE / "gui_cookie.txt", HERE / "gui_log.txt"
G.COOKIE.write_text("PHPSESSID=fromlogin\n", encoding="utf-8")
G.CONFIG.write_text(json.dumps({"rej_kod": "WFAIS_26/27Z_INF", "cdyd_kod": "26/27Z", "przedmioty": [
    {"nazwa": "Bazy danych", "prz_kod": "WFAIS.IF-C202.0", "rej_kod": "WFAIS_26/27Z_INF",
     "grupy": {"Wykład": [1], "Ćwiczenia": [3, 4]}}]}, ensure_ascii=False), encoding="utf-8")
G.LOGFILE.unlink(missing_ok=True)

real_stdout = sys.__stdout__
def say(*a):
    real_stdout.write(" ".join(map(str, a)) + "\n"); real_stdout.flush()

root = tk.Tk()
app = G.App(root)

def pump(cond, timeout=30, what=""):
    end = time.time() + timeout
    while time.time() < end:
        root.update()
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError(f"timeout: {what}\n" + app.log.get("1.0", "end"))

logtext = lambda: app.log.get("1.0", "end")

# 1. sesja z cookie -> status, lista przedmiotów, przedmiot z configu zaznaczony i wczytany
pump(lambda: "WFAIS.IF-C202.0" in app.sections, what="auto-wczytanie")
say("status:", app.login_status["text"])
assert "Jan Testowy" in app.login_status["text"] and len(app.course_vars) == 3
c202 = app.sections["WFAIS.IF-C202.0"]["rows"]["Ćwiczenia"]["combos"]
say("C202 ćwiczenia:", [cb.get() for cb in c202])
assert c202[0].get().startswith("gr 3 ") and "PEŁNA" in c202[0].get()
assert c202[1].get().startswith("gr 4 ") and c202[2].get() == G.NONE
say("odliczanie:", app.countdown["text"])
assert app.countdown["text"].startswith("Tura: ")

# 2. zaznaczenie kolejnego przedmiotu, wybór grup, odznaczenie/ponowne zaznaczenie
def check(kod, on):
    app.course_vars[kod].set(on); app.toggle(kod)
check("WFAIS.IF-X210.0", True)
pump(lambda: "WFAIS.IF-X210.0" in app.sections, what="X210")
check("WFAIS.IF-X205.0", True)
pump(lambda: "WFAIS.IF-X205.0" in app.sections, what="X205")
check("WFAIS.IF-X205.0", False)
assert list(app.sections) == ["WFAIS.IF-C202.0", "WFAIS.IF-X210.0"]
x210 = app.sections["WFAIS.IF-X210.0"]["rows"]["Ćwiczenia"]["combos"]
pick = lambda cb, nr: cb.set(next(v for v in cb["values"] if v.startswith(f"gr {nr} ")))
pick(x210[0], 2); pick(x210[1], 5); pick(x210[2], 2)  # duplikat 2 ma zostać pominięty
cfg = app.collect_cfg()
say("collect_cfg:", json.dumps(cfg["przedmioty"], ensure_ascii=False))
assert [p["grupy"] for p in cfg["przedmioty"]] == [
    {"Wykład": [1], "Ćwiczenia": [3, 4]}, {"Wykład": [1], "Ćwiczenia": [2, 5]}]
assert app.save() and json.loads(G.CONFIG.read_text(encoding="utf-8"))["przedmioty"][1]["prz_kod"] == "WFAIS.IF-X210.0"

# 3. test (probe) przez okienko
app.run_task(["--probe"], "Test")
pump(lambda: "— Test: koniec —" in logtext(), what="probe")
assert logtext().count("Tura rejestracji nie jest otwarta") >= 2 and "zajecia[628405][] = 2" in logtext()
say("PROBE w okienku OK")

# 4. START, a potem STOP
app.run_task([], "Rejestracja")
pump(lambda: "Otwarcie tury" in logtext(), what="start")
assert str(app.stop_btn["state"]) == "normal" and str(app.start_btn["state"]) == "disabled"
time.sleep(1.2); root.update()
app.stop()
pump(lambda: not app.worker.is_alive() and str(app.start_btn["state"]) == "normal", what="stop")
assert "Zatrzymano." in logtext()
say("START/STOP OK")
assert "Zapisano ustawienia" in G.LOGFILE.read_text(encoding="utf-8")
say("\n--- ostatnie linie dziennika ---\n" + "\n".join(logtext().strip().splitlines()[-8:]))
root.destroy()
say("GUI OK")
