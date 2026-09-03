"""Makieta USOSweb - lokalna atrapa strony rejestracji do testow.

Odtwarza to, co w prawdziwym USOSweb ma znaczenie dla skryptu: zagniezdzone
tabele, ikony akcji z title="zarejestruj", osobny ekran potwierdzenia oraz
rejestracje, ktora otwiera sie dopiero po zadanym czasie.

  python makieta_usos.py 15 8765     # otwarcie za 15 s, port 8765

Wiersz "grupa nr 30" jest celowa pulapka: sprawdza, czy wzorzec na grupe 3
nie lapie przypadkiem grupy 30.
"""
import sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

OPEN_AT = time.time() + float(sys.argv[1]) if len(sys.argv) > 1 else time.time()
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8765
registered = set()

GROUPS = [
    ("WMI.IM-AM1-C", "grupa nr 3", "pon. 8:00, dr Kowalski"),
    ("WMI.IM-AM1-C", "grupa nr 30", "wt. 10:00, dr Nowak"),      # pulapka: zawiera "grupa nr 3"
    ("JCJ-ANG-B2", "grupa nr 1", "wt. 11:30, mgr Wisniewska"),
]

PAGE = """<html><head><meta charset="utf-8"><title>Rejestracja bezposrednia do grup</title></head>
<body><h1>Rejestracja bezposrednia do grup</h1>
<table border=1><tr><td>
  <table border=1>{rows}</table>
</td></tr></table>{note}</body></html>"""


def row(i, g):
    code, grp, info = g
    if i in registered:
        action = (f'<a href="/wyrejestruj?g={i}" title="wyrejestruj">'
                  f'<img src="/i/dzialanie_wyrejestruj.png" alt="wyrejestruj"></a>')
    elif time.time() >= OPEN_AT:
        action = (f'<a href="/zarejestruj?g={i}" title="zarejestruj">'
                  f'<img src="/i/dzialanie_zarejestruj.png" alt="zarejestruj"></a>')
    else:
        action = "&nbsp;"
    return (f"<tr><td>{code}</td><td>{grp}</td><td>{info}</td>"
            f"<td>15/20</td><td>{action}</td></tr>")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_html(self, html):
        b = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        g = int(q.get("g", [0])[0])
        if u.path == "/zarejestruj":
            self.send_html('<html><head><meta charset="utf-8"></head><body>'
                           '<h1>Potwierdzenie</h1><p>Czy na pewno?</p>'
                           f'<form action="/potwierdz" method="get">'
                           f'<input type="hidden" name="g" value="{g}">'
                           '<input type="submit" value="Zarejestruj"></form></body></html>')
        elif u.path == "/potwierdz":
            registered.add(g)
            self.send_html('<html><head><meta charset="utf-8"></head><body>'
                           "<p>Jesteś zarejestrowany na te zajęcia.</p></body></html>")
        elif u.path == "/wyrejestruj":
            registered.discard(g)
            self.send_html("<html><body>ok</body></html>")
        else:
            note = ("" if time.time() >= OPEN_AT
                    else "<p>Rejestracja jeszcze się nie rozpoczęła.</p>")
            self.send_html(PAGE.format(
                rows="".join(row(i, g) for i, g in enumerate(GROUPS)), note=note))


print(f"makieta USOS na http://127.0.0.1:{PORT}/rejestracja "
      f"(rejestracja otwiera sie za {OPEN_AT - time.time():.0f} s)", flush=True)
HTTPServer(("127.0.0.1", PORT), H).serve_forever()
