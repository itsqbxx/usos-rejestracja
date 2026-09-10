#!/usr/bin/env python3
"""
Automatyczna rejestracja bezpośrednia do grup (brdg2) w USOSweb UJ.

Przygotowanie:
  1. python.exe -m pip install requests beautifulsoup4 playwright
  2. python.exe usos_rej.py --login     (otwiera Edge -> logujesz się -> sesja trafia do cookie.txt)
  3. python.exe usos_rej.py --kreator   (wybór przedmiotów i grup -> config.json)

Użycie:
  python.exe usos_rej.py --login     # logowanie w oknie przeglądarki, zapisuje cookie.txt
  python.exe usos_rej.py --kreator   # interaktywny wybór przedmiotów i grup
  python.exe usos_rej.py --dry-run   # sprawdza sesję, parsuje strony, pokazuje plan (nic nie wysyła)
  python.exe usos_rej.py --probe     # JEDNA próba na przedmiot przed otwarciem tury (serwer ją
                                     # odrzuci) - pokazuje wysłane pola i surową odpowiedź serwera
  python.exe usos_rej.py             # czeka na otwarcie tury i rejestruje

Limit USOS: >10 prób rejestracji w ~20 s = czasowa blokada. Skrypt pilnuje wspólnego limitu
(domyślnie 8 prób / 20 s na wszystkie przedmioty), więc NIE klikaj "Rejestruj" w przeglądarce,
kiedy skrypt działa - Twoje kliknięcia liczą się do tego samego limitu.
"""

import argparse
import json
import re
import sys
import threading
import time
from collections import deque
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse

import requests
from bs4 import BeautifulSoup

HOST = "https://www.usosweb.uj.edu.pl"
CONTROLLER = HOST + "/kontroler.php"
HERE = Path(__file__).resolve().parent
PROFILE_DIR = HERE / ".profil_przegladarki"  # profil Edge do logowania (zawiera sesję UJ - nie udostępniaj)
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
ALREADY_REGISTERED = re.compile(r"ju[żz]\s+(jeste[śs]\s+)?zarejestrowan", re.I)
RATE_LIMITED = re.compile(r"pr[óo]b\s+rejestracji|zablokowan", re.I)
NOT_OPEN = re.compile(r"nie jest (teraz )?otwart|no open round", re.I)
COURSE_CODE = re.compile(r"\[([A-Za-z0-9_]+(?:\.[A-Za-z0-9_-]+)+)\]")

_print_lock = threading.Lock()


def log(msg):
    with _print_lock:
        print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}", flush=True)


def beep():
    try:
        import winsound
        winsound.Beep(1200, 400)
    except Exception:
        pass


class SessionExpired(Exception):
    pass


class LoginFailed(Exception):
    pass


class Stopped(Exception):
    pass


class NoGroupChoice(RuntimeError):
    pass


stop_event = threading.Event()  # ustawiany przyciskiem STOP w okienku (usos_gui.py)


def _sleep(seconds):
    """time.sleep, który da się przerwać przez stop_event."""
    end = time.time() + seconds
    while True:
        if stop_event.is_set():
            raise Stopped()
        left = end - time.time()
        if left <= 0:
            return
        time.sleep(min(left, 0.25))


class ServerClock:
    """Szacuje przesunięcie zegara serwera względem lokalnego.

    Serwer podaje czas obcięty do pełnej sekundy (licznik tury `data-now` albo nagłówek Date),
    wygenerowany gdzieś między wysłaniem a odebraniem żądania - każda próbka zawęża przedział
    możliwych przesunięć. Zegar aplikacji (data-now) ma pierwszeństwo, bo to on otwiera turę.
    """

    def __init__(self):
        self.lo = self.hi = None
        self.app_clock = False
        self._lock = threading.Lock()

    def sample(self, t_send, t_recv, server_epoch, app_clock=False):
        with self._lock:
            if self.app_clock and not app_clock:
                return
            if app_clock and not self.app_clock:
                self.app_clock, self.lo = True, None
            lo, hi = server_epoch - t_recv, server_epoch + 1 - t_send
            if self.lo is None or max(self.lo, lo) > min(self.hi, hi):
                self.lo, self.hi = lo, hi
            else:
                self.lo, self.hi = max(self.lo, lo), min(self.hi, hi)

    @property
    def offset(self):
        with self._lock:
            return 0.0 if self.lo is None else (self.lo + self.hi) / 2

    @property
    def uncertainty(self):
        with self._lock:
            return 1.0 if self.lo is None else (self.hi - self.lo) / 2

    def now(self):
        return time.time() + self.offset


class AttemptLimiter:
    """Wspólny limit prób rejestracji (POST) dla wszystkich przedmiotów."""

    def __init__(self, max_attempts, window):
        self.max, self.window = max_attempts, window
        self.times = deque()
        self.pause_until = 0.0
        self._lock = threading.Lock()

    def acquire(self):
        """Czeka na wolny slot; zwraca, ile sekund czekał."""
        start = time.time()
        while True:
            with self._lock:
                now = time.time()
                while self.times and now - self.times[0] > self.window:
                    self.times.popleft()
                wait = self.pause_until - now
                if wait <= 0 and len(self.times) < self.max:
                    self.times.append(now)
                    return now - start
                if wait <= 0:
                    wait = self.times[0] + self.window - now + 0.05
            _sleep(min(max(wait, 0.01), 1.0))

    def pause(self, seconds):
        with self._lock:
            self.pause_until = max(self.pause_until, time.time() + seconds)


# ---------------------------------------------------------------- parsowanie

def first_int(text):
    m = re.search(r"\d+", text or "")
    return int(m.group()) if m else None


def norm(s):
    return " ".join(str(s).split()).casefold()


def is_logged_in(html):
    m = re.search(r'user_id:\s*"(\d*)"', html)
    return bool(m and m.group(1))


def parse_course_page(html):
    if not is_logged_in(html):
        raise SessionExpired("strona nie zawiera zalogowanego użytkownika")

    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form", attrs={"name": "grupy"})
    if form is None:
        raise RuntimeError("nie znaleziono formularza rejestracji (form name='grupy')")

    hidden, seen = [], set()
    for inp in form.find_all("input", attrs={"type": "hidden"}):
        name = inp.get("name")
        if name and name not in seen:  # pola ukryte są zdublowane (góra i dół tabeli)
            seen.add(name)
            hidden.append((name, inp.get("value", "")))

    cols = {}
    header = next((tr for tr in form.find_all("tr") if tr.find("th")), None)
    if header is not None:
        for i, th in enumerate(header.find_all("th")):
            text = th.get_text(" ", strip=True)
            for key in ("Grupa", "Zapisanych", "Limit górny", "Prowadzący", "Termin"):
                if text.startswith(key) and key not in cols:
                    cols[key] = i

    def cell(tds, key):
        i = cols.get(key)
        return tds[i].get_text(" ", strip=True) if i is not None and i < len(tds) else ""

    classes = {}  # class_id -> {"typ": str, "grupy": {nr: {...}}}
    current_type = "?"
    for tr in form.find_all("tr"):
        if "midnote" in (tr.get("class") or []):
            current_type = tr.get_text(" ", strip=True)
            continue
        # uwaga: <input> ma zdublowany atrybut class, więc szukamy po name, nie po klasie
        radio = tr.find("input", attrs={"name": re.compile(r"^zajecia\[\d+\]\[\]$")})
        if radio is None:
            continue
        cid = re.search(r"\d+", radio["name"]).group()
        nr = int(radio.get("value"))
        tds = tr.find_all("td", recursive=False)
        zapisani, limit = first_int(cell(tds, "Zapisanych")), first_int(cell(tds, "Limit górny"))
        cls = classes.setdefault(cid, {"typ": current_type, "grupy": {}})
        cls["grupy"][nr] = {
            "nr": nr,
            "zapisani": zapisani,
            "limit": limit,
            "ma_miejsca": zapisani is None or limit is None or zapisani < limit,
            "prowadzacy": cell(tds, "Prowadzący"),
            "termin": cell(tds, "Termin"),
            "checked": radio.has_attr("checked"),
            "disabled": radio.has_attr("disabled"),
        }

    if not classes:
        raise NoGroupChoice("USOS nie pokazuje tu wyboru grup ani przycisku „Rejestruj” "
                            "(zwykle przedmiot z jedną grupą)")

    open_at = server_now = None
    cd = soup.find("span", attrs={"data-date": True, "data-now": True})
    if cd is not None:
        open_at = datetime.strptime(cd["data-date"], "%Y-%m-%d %H:%M:%S")
        server_now = datetime.strptime(cd["data-now"], "%Y-%m-%d %H:%M:%S")

    return {
        "action": form.get("action"),
        "hidden": hidden,
        "prgos_ok": any(k == "prgos_id" and v for k, v in hidden),
        "classes": classes,
        "open_at": open_at,
        "server_now": server_now,
        "closed_notice": "nie jest teraz otwarta" in html,
    }


def parse_course_list(html, default_cdyd):
    """Lista przedmiotów ze strony wyborPrzedmiotu danej rejestracji."""
    if not is_logged_in(html):
        raise SessionExpired("strona nie zawiera zalogowanego użytkownika")
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main") or soup
    found = {}
    for tr in main.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if not tds:
            continue
        first = tds[0].get_text(" ", strip=True)
        m = COURSE_CODE.search(first)
        if not m:
            continue
        cd = re.search(r"\b\d{2}/\d{2}[A-Z]?\b", tr.get_text(" ", strip=True))
        found.setdefault(m.group(1), {
            "nazwa": first[:m.start()].strip() or m.group(1),
            "prz_kod": m.group(1),
            "cdyd_kod": cd.group() if cd else default_cdyd,
        })
    for a in main.find_all("a", href=True):  # zapasowo: linki do stron grup
        if "grupyPrzedmiotu" in a["href"]:
            q = dict(parse_qsl(urlparse(a["href"]).query))
            kod = q.get("prz_kod")
            if kod and kod not in found:
                found[kod] = {"nazwa": a.get_text(" ", strip=True) or kod, "prz_kod": kod,
                              "cdyd_kod": q.get("cdyd_kod", default_cdyd)}
    return list(found.values())


def choose_groups(page, prefs):
    """Wybiera po jednej grupie dla każdych zajęć. Zwraca (wybory, ostrzeżenia)."""
    choices, warnings = [], []
    types = {norm(c["typ"]) for c in page["classes"].values()}
    for key in prefs:
        if norm(key) not in types and key not in page["classes"]:
            warnings.append(f"typ zajęć '{key}' z config.json nie występuje na stronie "
                            f"(dostępne: {', '.join(c['typ'] for c in page['classes'].values())})")

    for cid, cls in page["classes"].items():
        groups = cls["grupy"]
        wanted = next((v for k, v in prefs.items() if norm(k) == norm(cls["typ"]) or k == cid), None)
        if wanted is None:
            nr = next((n for n, g in groups.items() if g["checked"]), next(iter(groups)))
            choices.append({"cid": cid, "typ": cls["typ"], "nr": nr,
                            "uwaga": "brak w config.json - domyślna ze strony"})
            continue
        wanted = [wanted] if isinstance(wanted, int) else [int(n) for n in wanted]
        candidates = [n for n in wanted if n in groups and not groups[n]["disabled"]]
        free = [n for n in candidates if groups[n]["ma_miejsca"]]
        if free:
            nr = free[0]
            uwaga = "" if nr == wanted[0] else "zapasowa - wcześniejsze preferencje pełne/niedostępne"
        elif candidates:
            nr, uwaga = candidates[0], "wszystkie preferowane grupy wyglądają na pełne"
        else:
            nr, uwaga = None, f"żadna z grup {wanted} nie istnieje lub jest nieaktywna"
        choices.append({"cid": cid, "typ": cls["typ"], "nr": nr, "uwaga": uwaga})
    return choices, warnings


def interpret(resp):
    """Odpowiedź ajax USOSweb: {"type": "v"|"!", "pl": ..., "en": ..., "redirect": ...}."""
    try:
        j = resp.json()
    except ValueError:
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", resp.text)).strip()
        return "?", f"HTTP {resp.status_code}, nie-JSON: {text[:200]}"
    if j.get("redirect"):
        return "redirect", j["redirect"]
    msg = re.sub(r"<[^>]+>", " ", j.get("pl") or j.get("en") or "")
    return j.get("type"), " ".join(msg.split())


def print_groups(page, chosen=()):
    for cid, cls in page["classes"].items():
        print(f"  {cls['typ']} (zajęcia {cid}):")
        for nr, g in cls["grupy"].items():
            mark = "  <== WYBRANA" if (cid, nr) in chosen else ""
            full = "" if g["ma_miejsca"] else " [PEŁNA]"
            print(f"    gr {nr:>2}  {str(g['zapisani']) + '/' + str(g['limit']):>6}  "
                  f"{g['prowadzacy'][:26]:<26} {g['termin']}{full}{mark}")


# ---------------------------------------------------------------- przedmiot

def make_session(cookie, ua=None):
    s = requests.Session()
    s.headers.update({"User-Agent": ua or USER_AGENT, "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8"})
    domain = urlparse(HOST).hostname
    for part in cookie.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            s.cookies.set(name, value, domain=domain, path="/")
    return s


def check_host(resp):
    if urlparse(resp.url).hostname != urlparse(HOST).hostname:
        raise SessionExpired(f"przekierowanie do {urlparse(resp.url).hostname}")


_prgos_cache = {}  # rej_kod -> prgos_id


def prgos_from_html(html):
    inp = BeautifulSoup(html, "html.parser").find("input", attrs={"name": "prgos_id", "value": True})
    return inp["value"] if inp is not None and inp["value"] else None


def find_prgos_id(session, rej):
    """Program studiów (prgos_id) z listy przedmiotów rejestracji; bez niego USOS odrzuca zapis."""
    if rej not in _prgos_cache:
        r = session.get(CONTROLLER + "?" + urlencode(
            {"_action": "dla_stud/rejestracja/brdg2/wyborPrzedmiotu", "rej_kod": rej}), timeout=20)
        check_host(r)
        if not is_logged_in(r.text):
            raise SessionExpired("strona nie zawiera zalogowanego użytkownika")
        _prgos_cache[rej] = prgos_from_html(r.text)
    return _prgos_cache[rej]


class Course:
    def __init__(self, cfg, defaults, cookie, clock):
        self.prz_kod = cfg["prz_kod"]
        self.name = cfg.get("nazwa") or self.prz_kod
        self.prefs = cfg.get("grupy", {})
        self.rej_kod = cfg.get("rej_kod", defaults.get("rej_kod"))
        self.cdyd_kod = cfg.get("cdyd_kod", defaults.get("cdyd_kod"))
        self.prgos_id = cfg.get("prgos_id") or defaults.get("prgos_id")
        self.url = self._make_url()
        self.clock = clock
        self.page = None
        self.set_cookie(cookie)

    def _make_url(self):
        params = {
            "_action": "dla_stud/rejestracja/brdg2/grupyPrzedmiotu",
            "rej_kod": self.rej_kod,
            "prz_kod": self.prz_kod,
            "cdyd_kod": self.cdyd_kod,
            "odczyt": "0",  # tryb rejestracji: bez tego USOS zwraca 400, z odczyt=1 sam podgląd bez wyboru grup
        }
        if self.prgos_id:  # program studiów: bez niego USOS odpowiada "Brak poprawnego programu podpięcia"
            params["prgos_id"] = self.prgos_id
        return CONTROLLER + "?" + urlencode(params)

    def set_cookie(self, cookie):
        self.session = make_session(cookie)

    def _timed(self, method, url, **kw):
        t0 = time.time()
        r = self.session.request(method, url, timeout=20, **kw)
        return r, t0, time.time()

    def _sample_date(self, r, t0, t1):
        try:
            self.clock.sample(t0, t1, parsedate_to_datetime(r.headers["Date"]).timestamp())
        except (KeyError, TypeError, ValueError):
            pass

    def refresh(self):
        if not self.prgos_id:
            self.prgos_id = find_prgos_id(self.session, self.rej_kod)
            self.url = self._make_url()
        r, t0, t1 = self._timed("GET", self.url)
        check_host(r)
        r.raise_for_status()
        self.page = parse_course_page(r.text)
        if self.page["server_now"] is not None:
            self.clock.sample(t0, t1, self.page["server_now"].timestamp(), app_clock=True)
        else:
            self._sample_date(r, t0, t1)
        return self.page

    def open_time(self):
        """Czas otwarcia tury z licznika na stronie (w tej samej skali co ServerClock)."""
        return None if self.page["open_at"] is None else self.page["open_at"].timestamp()

    def show_plan(self):
        choices, warnings = choose_groups(self.page, self.prefs)
        print(f"\n=== {self.name} [{self.prz_kod}] ===")
        print_groups(self.page, {(c["cid"], c["nr"]) for c in choices})
        for c in choices:
            if c["uwaga"]:
                print(f"  ! {c['typ']}: {c['uwaga']}")
        for w in warnings:
            print(f"  ! {w}")
        if not self.page["prgos_ok"]:
            print("  ! UWAGA: w formularzu brak programu studiów (prgos_id) - USOS odrzuci rejestrację")
        return all(c["nr"] is not None for c in choices) and not warnings and self.page["prgos_ok"]

    def build_payload(self):
        choices, _ = choose_groups(self.page, self.prefs)
        data = list(self.page["hidden"])
        data += [(f"zajecia[{c['cid']}][]", str(c["nr"])) for c in choices if c["nr"] is not None]
        return data, ", ".join(f"{c['typ']} gr {c['nr']}" for c in choices)

    def post_once(self, limiter):
        data, desc = self.build_payload()
        waited = limiter.acquire()
        if waited > 1:
            log(f"{self.name}: czekałem {waited:.1f}s na slot (limit prób USOS)")
        try:
            r, t0, t1 = self._timed("POST", self.page["action"], data=data, headers={
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Origin": HOST,
                "Referer": self.url,
            })
            self._sample_date(r, t0, t1)
            kind, msg = interpret(r)
            raw = f"HTTP {r.status_code}: {r.text[:600]}"
        except requests.RequestException as e:
            kind, msg, raw = "net", f"błąd sieci: {e}", ""
        return kind, msg, desc, raw

    def register(self, t_open, deadline, interval, limiter, round_open=None, leader=True):
        """Dopóki tura nie jest potwierdzona jako otwarta, po odpowiedzi "nie jest otwarta" ponawia
        tylko lider (pierwszy przedmiot), a reszta czeka na niego - żeby nie zużywać limitu prób."""
        round_open = round_open or threading.Event()
        attempt, last_msg, last_refresh = 0, None, 0.0
        while self.clock.now() < deadline:
            attempt += 1
            kind, msg, desc, _ = self.post_once(limiter)
            not_open = kind == "!" and bool(NOT_OPEN.search(msg))
            if kind in ("v", "!") and not not_open:
                round_open.set()
            if kind == "v":
                log(f"SUKCES {self.name}: {msg} ({desc}, próba {attempt})")
                return True, msg
            if kind == "!" and ALREADY_REGISTERED.search(msg):
                log(f"{self.name}: {msg} - traktuję jako zarejestrowany")
                return True, msg
            if msg != last_msg:
                log(f"{self.name}: próba {attempt} [{kind}] {msg} ({desc})")
                last_msg = msg
            if not_open:
                if leader:
                    _sleep(0.3)
                else:
                    while not round_open.is_set() and self.clock.now() < deadline:
                        _sleep(0.05)
                continue
            if kind == "!" and RATE_LIMITED.search(msg):
                log(f"Ostrzeżenie o limicie prób - wstrzymuję wszystkie próby na {limiter.window:.0f} s")
                limiter.pause(limiter.window)
            opened = self.clock.now() > t_open + 1.0
            if kind == "redirect" or (opened and time.time() - last_refresh > 3):
                # odśwież CSRF i liczby zapisanych (żeby ewentualnie przejść na grupę zapasową)
                try:
                    self.refresh()
                except SessionExpired as e:
                    log(f"{self.name}: SESJA WYGASŁA ({e}) - przerywam")
                    return False, "sesja wygasła"
                except Exception as e:
                    log(f"{self.name}: nie udało się odświeżyć strony: {e}")
                last_refresh = time.time()
            _sleep(interval)
        log(f"{self.name}: koniec czasu, nie udało się zarejestrować (ostatnio: {last_msg})")
        return False, last_msg


def fetch_course_list(cookie, rej, cdyd):
    r = make_session(cookie).get(CONTROLLER + "?" + urlencode(
        {"_action": "dla_stud/rejestracja/brdg2/wyborPrzedmiotu", "rej_kod": rej}), timeout=20)
    check_host(r)
    courses = parse_course_list(r.text, cdyd)
    _prgos_cache[rej] = prgos = prgos_from_html(r.text)
    for c in courses:
        c["prgos_id"] = prgos
    return courses


def whoami(cookie):
    """Imię i nazwisko zalogowanego użytkownika (albo 'zalogowany'), None gdy sesja nieważna."""
    try:
        r = make_session(cookie).get(CONTROLLER + "?_action=home/index", timeout=15)
    except requests.RequestException:
        return None
    if urlparse(r.url).hostname != urlparse(HOST).hostname or not is_logged_in(r.text):
        return None
    m = re.search(r"logged-user='([^']*)'", r.text)
    return m.group(1) if m else "zalogowany"


# ---------------------------------------------------------------- kreator

def ask_numbers(prompt, allowed):
    while True:
        ans = input(prompt).strip()
        if not ans:
            return []
        try:
            nums = [int(x) for x in re.split(r"[\s,;]+", ans) if x]
        except ValueError:
            print("    podaj numery oddzielone spacją, np. 3 4")
            continue
        bad = [n for n in nums if n not in allowed]
        if bad:
            print(f"    nie ma takich numerów: {bad} (dostępne: {sorted(allowed)})")
            continue
        return list(dict.fromkeys(nums))


def wizard(cfg_path, cookie):
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    rej = input(f"Kod rejestracji [{cfg.get('rej_kod', 'WFAIS_26/27Z_INF')}]: ").strip() \
        or cfg.get("rej_kod", "WFAIS_26/27Z_INF")
    cdyd = cfg.get("cdyd_kod", "26/27Z")
    courses = fetch_course_list(cookie, rej, cdyd)
    if not courses:
        sys.exit("Nie znalazłem przedmiotów na stronie rejestracji - wyślij jej HTML autorowi skryptu.")

    current = {c["prz_kod"] for c in cfg.get("przedmioty", [])}
    print(f"\nPrzedmioty w rejestracji {rej}:")
    for i, c in enumerate(courses, 1):
        print(f"  {i:>2}. {'*' if c['prz_kod'] in current else ' '} {c['nazwa']} [{c['prz_kod']}]")
    print("  (* = już w config.json)")
    picks = ask_numbers("\nNumery przedmiotów w kolejności ważności (np. 2 5 1): ",
                        set(range(1, len(courses) + 1)))
    if not picks:
        sys.exit("Nic nie wybrano - config.json bez zmian.")

    clock, result = ServerClock(), []
    for i in picks:
        info = courses[i - 1]
        course = Course({**info, "rej_kod": rej}, cfg, cookie, clock)
        page = course.refresh()
        print(f"\n=== {info['nazwa']} [{info['prz_kod']}] ===")
        print_groups(page)
        prefs = {}
        for cls in page["classes"].values():
            nums = list(cls["grupy"])
            if len(nums) == 1:
                prefs[cls["typ"]] = nums
                print(f"  {cls['typ']}: jedna grupa -> {nums[0]}")
                continue
            chosen = ask_numbers(f"  {cls['typ']}: grupy w kolejności preferencji "
                                 f"(np. {nums[0]} {nums[-1]}; Enter = domyślna): ", set(nums))
            if chosen:
                prefs[cls["typ"]] = chosen
        result.append({"nazwa": info["nazwa"], "prz_kod": info["prz_kod"], "rej_kod": rej,
                       "cdyd_kod": info["cdyd_kod"], "prgos_id": info.get("prgos_id"), "grupy": prefs})

    new_cfg = {"rej_kod": rej, "cdyd_kod": cdyd, "przedmioty": result}
    text = json.dumps(new_cfg, ensure_ascii=False, indent=2)
    print("\nNowy config.json:\n" + text)
    if input("\nZapisać? [T/n]: ").strip().lower() in ("", "t", "tak", "y", "yes"):
        if cfg_path.exists():
            cfg_path.with_name(cfg_path.name + ".bak").write_text(
                cfg_path.read_text(encoding="utf-8"), encoding="utf-8")
        cfg_path.write_text(text + "\n", encoding="utf-8")
        print(f"Zapisano {cfg_path} (poprzednia wersja: {cfg_path.name}.bak). "
              f"Teraz: python.exe usos_rej.py --dry-run")
    else:
        print("Nie zapisano.")


# ---------------------------------------------------------------- logowanie

def session_logged_in(cookie, ua=None):
    try:
        r = make_session(cookie, ua).get(CONTROLLER + "?_action=home/index", timeout=15)
    except requests.RequestException:
        return False
    return urlparse(r.url).hostname == urlparse(HOST).hostname and is_logged_in(r.text)


def browser_login(cookie_path, timeout=600, headless=False):
    """Otwiera przeglądarkę na logowaniu UJ, czeka aż się zalogujesz i zapisuje cookie USOSweb.

    Hasło wpisujesz tylko na stronie UJ - skrypt go nie widzi. Co 3 s sprawdza osobnym
    zapytaniem, czy ciasteczka z przeglądarki dają zalogowaną sesję USOSweb (niezależnie od
    tego, która karta jest otwarta). Profil zostaje w PROFILE_DIR, więc z "Nie wylogowuj mnie"
    kolejne logowania mogą przejść bez hasła.
    """
    global USER_AGENT
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise LoginFailed("brak biblioteki Playwright - python.exe -m pip install playwright")
    try:
        import msvcrt
    except ImportError:
        msvcrt = None
    log("Otwieram przeglądarkę - zaloguj się do USOSweb. Okno zamknie się samo "
        "(gdyby nie - naciśnij Enter tutaj w terminalu).")
    with sync_playwright() as p:
        ctx, err = None, None
        for channel in ("msedge", "chrome", None):  # None = Chromium z `playwright install`
            try:
                ctx = p.chromium.launch_persistent_context(
                    str(PROFILE_DIR), channel=channel, headless=headless, no_viewport=True)
                break
            except Exception as e:
                err = e
        if ctx is None:
            raise LoginFailed(f"nie udało się uruchomić Edge/Chrome: {err}")
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            ua = None
            try:
                ua = page.evaluate("navigator.userAgent")
                page.goto(CONTROLLER + "?_action=logowaniecas/index")
            except Exception:
                pass
            deadline, last_check, last_info = time.time() + timeout, 0.0, time.time()
            while True:
                forced = False
                while msvcrt is not None and msvcrt.kbhit():
                    forced = msvcrt.getwch() in "\r\n" or forced
                if forced or time.time() - last_check > 3:
                    last_check = time.time()
                    try:
                        cookie = "; ".join(f"{c['name']}={c['value']}" for c in ctx.cookies(HOST + "/"))
                    except Exception:
                        raise LoginFailed("zamknięto okno przeglądarki przed zalogowaniem")
                    if cookie and session_logged_in(cookie, ua):
                        break
                    if forced:
                        log("USOSweb nie widzi jeszcze zalogowanej sesji - dokończ logowanie w oknie.")
                if time.time() - last_info > 20:
                    last_info = time.time()
                    try:
                        tabs = [f"{urlparse(pg.url).hostname or ''}{urlparse(pg.url).path}" for pg in ctx.pages]
                        domains = sorted({c["domain"] for c in ctx.cookies()})
                    except Exception:
                        tabs = domains = "?"
                    log(f"Czekam na zalogowanie... karty: {tabs} | ciasteczka z domen: {domains}")
                if time.time() > deadline:
                    raise LoginFailed("nie zalogowano w ciągu 10 minut")
                _sleep(0.2)
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    if ua:
        USER_AGENT = ua
    cookie_path.write_text(cookie + "\n" + (f"User-Agent: {ua}\n" if ua else ""), encoding="utf-8")
    log(f"Zalogowano - sesja zapisana w {cookie_path.name}.")
    return cookie


# ---------------------------------------------------------------- main

def read_cookie(path):
    """cookie.txt: linia z ciasteczkami + opcjonalnie 'User-Agent: ...' przeglądarki, z której są."""
    global USER_AGENT
    if not path.exists():
        sys.exit(f"Brak pliku {path}. Zaloguj się: python.exe usos_rej.py --login")
    raw = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.lower().startswith("user-agent:"):
            USER_AGENT = line[11:].strip()
        elif line.lower().startswith("cookie:"):
            raw = line[7:].strip()
        elif line and not raw:
            raw = line
    if "=" not in raw:
        sys.exit(f"{path} nie wygląda na nagłówek Cookie (oczekiwano np. 'PHPSESSID=...; ...').")
    return raw


def parse_at(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%H:%M:%S", "%H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:
            dt = datetime.combine(date.today(), dt.time())
        return dt.timestamp()
    sys.exit(f"Nie rozumiem --at '{s}'. Przykłady: 17:00, 17:00:00, '2026-09-10 17:00'.")


def fmt_left(sec):
    sec = max(0, int(sec))
    return f"{sec // 3600}:{sec // 60 % 60:02d}:{sec % 60:02d}"


def refresh_all(courses, cookie_path, state):
    """Odświeża strony wszystkich przedmiotów; przy wygaśnięciu sesji czeka na nowe cookie."""
    while True:
        try:
            for c in courses:
                c.refresh()
            state["warned"] = False
            return
        except SessionExpired as e:
            mtime = cookie_path.stat().st_mtime if cookie_path.exists() else 0
            if mtime != state["cookie_mtime"]:
                state["cookie_mtime"] = mtime
                cookie = read_cookie(cookie_path)
                for c in courses:
                    c.set_cookie(cookie)
                log("Wczytano nowe cookie.txt - sprawdzam sesję...")
                continue
            if not state.get("warned"):
                beep()
                state["warned"] = True
                log(f"SESJA WYGASŁA ({e}) - otwieram przeglądarkę do ponownego logowania...")
                try:
                    browser_login(cookie_path)
                    continue  # cookie.txt się zmienił - pętla wczyta nową sesję
                except LoginFailed as le:
                    log(f"Logowanie nieudane ({le}). Zaloguj się w drugim terminalu: "
                        f"python.exe usos_rej.py --login - ten skrypt sam wczyta nowe cookie.txt")
            _sleep(5)


def wait_until(target, clock, courses, cookie_path, state, keepalive=120):
    last_ka, last_print = time.time(), 0.0
    while True:
        left = target - clock.now()
        if left <= 0:
            return
        if time.time() - last_ka > keepalive and left > 5:
            try:
                refresh_all(courses, cookie_path, state)  # podtrzymuje sesję i kalibruje zegar
            except Exception as e:
                log(f"keep-alive: błąd {e}")
            last_ka = time.time()
            continue
        if time.time() - last_print >= (60 if left > 120 else 10):
            log(f"do startu {fmt_left(left)} | zegar serwera {clock.offset:+.2f}s "
                f"(±{clock.uncertainty:.2f}s)")
            last_print = time.time()
        _sleep(min(left, 1.0))


def sync_clock(course, clock, until, spacing=1.09):
    """Kilkanaście odczytów licznika z różnym przesunięciem względem pełnej sekundy."""
    log("Synchronizuję zegar z serwerem...")
    while clock.now() < until:
        t = time.time()
        try:
            course.refresh()
        except Exception as e:
            log(f"sync: {e}")
        _sleep(max(0.0, min(spacing - (time.time() - t), until - clock.now())))
    log(f"Zegar serwera: {clock.offset:+.3f}s względem komputera (±{clock.uncertainty:.3f}s)")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Auto-rejestracja do grup w USOSweb UJ")
    ap.add_argument("--config", type=Path, default=HERE / "config.json")
    ap.add_argument("--cookie", type=Path, default=HERE / "cookie.txt")
    ap.add_argument("--kreator", action="store_true", help="interaktywny wybór przedmiotów i grup")
    ap.add_argument("--dry-run", action="store_true", help="tylko pokaż plan, nic nie wysyłaj")
    ap.add_argument("--probe", action="store_true",
                    help="jedna próba na przedmiot PRZED otwarciem tury (test; serwer odrzuci)")
    ap.add_argument("--at", help="godzina otwarcia tury, np. 17:00 (domyślnie ze strony)")
    ap.add_argument("--delay", type=float, default=None,
                    help="pierwsza próba X s po otwarciu (domyślnie: niepewność zegara, 0.05-0.6 s)")
    ap.add_argument("--interval", type=float, default=0.5, help="min. odstęp prób jednego przedmiotu [s]")
    ap.add_argument("--max-attempts", type=int, default=8, help="max prób na okno (wszystkie przedmioty)")
    ap.add_argument("--window", type=float, default=20, help="okno limitu prób [s]")
    ap.add_argument("--duration", type=float, default=180, help="jak długo po otwarciu próbować [s]")
    ap.add_argument("--login", action="store_true", help="zaloguj się w oknie przeglądarki (zapisuje cookie.txt)")
    return ap.parse_args(argv)


def run(args):

    if args.login or not args.cookie.exists():
        browser_login(args.cookie)
        if args.login and not (args.kreator or args.dry_run or args.probe):
            return
    cookie = read_cookie(args.cookie)
    if args.kreator:
        try:
            wizard(args.config, cookie)
        except SessionExpired:
            log("Sesja z cookie.txt wygasła - loguję ponownie.")
            wizard(args.config, browser_login(args.cookie))
        return

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    state = {"cookie_mtime": args.cookie.stat().st_mtime}
    clock = ServerClock()
    limiter = AttemptLimiter(args.max_attempts, args.window)
    courses = [Course(c, cfg, cookie, clock) for c in cfg["przedmioty"]]
    if len(courses) > args.max_attempts:
        log(f"UWAGA: {len(courses)} przedmiotów > limit {args.max_attempts} prób/{args.window:.0f}s - "
            f"ostatnie w kolejce wystartują później.")

    refresh_all(courses, args.cookie, state)  # przy wygasłej sesji sam otworzy logowanie

    plan_ok = all([c.show_plan() for c in courses])
    print()
    if not plan_ok:
        log("UWAGA: plan ma ostrzeżenia (wyżej) - popraw config.json zanim tura się otworzy.")

    if args.at:
        t_open = parse_at(args.at)
    else:
        times = [t for t in (c.open_time() for c in courses) if t is not None]
        if times:
            t_open = min(times)
        elif not any(c.page["closed_notice"] for c in courses):
            t_open = clock.now()
            log("Brak licznika na stronie - tura wygląda na otwartą, rejestruję od razu.")
        else:
            sys.exit("Nie znam godziny otwarcia tury - podaj ją przez --at, np. --at 17:00")
    log(f"Otwarcie tury: {datetime.fromtimestamp(t_open).strftime('%Y-%m-%d %H:%M:%S')} "
        f"(za {fmt_left(t_open - clock.now())})")

    if args.dry_run:
        return

    if args.probe:
        if t_open - clock.now() < 60:
            sys.exit("--probe działa tylko >60 s przed otwarciem tury (żeby nie zarejestrować przypadkiem).")
        for c in courses:
            data, desc = c.build_payload()
            print(f"\n--- PROBE {c.name} ---\nPOST {c.page['action']}")
            for k, v in data:
                print(f"  {k} = {v}")
            kind, msg, desc, raw = c.post_once(limiter)
            print(f"Odpowiedź serwera: {raw}")
            log(f"PROBE {c.name}: [{kind}] {msg}")
        print("\nDobry wynik: [!] z komunikatem, że tura/rejestracja nie jest jeszcze otwarta. "
              "Każdy inny błąd oznacza problem - nie startuj, tylko go zgłoś.")
        return

    if t_open - clock.now() > 35:
        wait_until(t_open - 45, clock, courses, args.cookie, state)
        sync_clock(courses[0], clock, t_open - 25)
    wait_until(t_open - 20, clock, courses, args.cookie, state)
    log("Odświeżam strony (świeży token CSRF, liczby zapisanych)...")
    refresh_all(courses, args.cookie, state)
    if not all([c.show_plan() for c in courses]):
        log("UWAGA: plan ma ostrzeżenia - rejestruję mimo to.")
    # margines: za wczesna próba też zużywa limit prób USOS, a ~0.3 s później nic nie kosztuje
    delay = args.delay if args.delay is not None else min(max(clock.uncertainty + 0.15, 0.3), 0.8)
    log(f"Pierwsza próba {delay:.2f}s po otwarciu (zegar ±{clock.uncertainty:.2f}s)")
    wait_until(t_open + delay, clock, courses, args.cookie, state)

    log(">>> START <<<")
    deadline = t_open + args.duration
    results = {}

    round_open = threading.Event()

    def worker(c, leader):
        try:
            results[c.name] = c.register(t_open, deadline, args.interval, limiter, round_open, leader)
        except Stopped:
            results[c.name] = (False, "zatrzymano")

    threads = []
    for i, c in enumerate(courses):  # w kolejności z config.json - pierwszy dostaje pierwszy slot
        t = threading.Thread(target=worker, args=(c, i == 0))
        t.start()
        threads.append(t)
        time.sleep(0.01)
    for t in threads:
        t.join()

    beep()
    print("\n=== PODSUMOWANIE ===")
    for c in courses:
        ok, msg = results.get(c.name, (False, "?"))
        print(f"  {'OK ' if ok else 'BŁĄD'}  {c.name}: {msg}")
    print("Sprawdź wynik w przeglądarce (odśwież stronę przedmiotu).")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        run(parse_args())
    except LoginFailed as e:
        sys.exit(f"Logowanie nieudane: {e}")
    except (Stopped, KeyboardInterrupt):
        sys.exit("Przerwano.")


if __name__ == "__main__":
    main()
