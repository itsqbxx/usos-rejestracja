#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
USOSweb (UJ) - automatyczna rejestracja na zajecia.

Tryby:
  login  - jednorazowe logowanie przez CAS; sesja zapisywana w profilu przegladarki
  list   - wypisuje grupy widoczne na stronie rejestracji (pomaga wypelnic config)
  run    - czeka do godziny startu (czas serwera USOS) i rejestruje na wszystkie cele rownolegle
  test   - to samo co run, ale bez klikania przycisku rejestracji (dry-run)

Uzycie:
  python usos_auto.py login
  python usos_auto.py list  --url "https://usosweb.uj.edu.pl/kontroler.php?_action=..."
  python usos_auto.py test  -c config.yaml
  python usos_auto.py run   -c config.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import yaml
except ImportError:
    print("Brak PyYAML. Zainstaluj:  pip install -r requirements.txt", file=sys.stderr)
    raise SystemExit(1)

try:
    from playwright.async_api import async_playwright, Page, BrowserContext
except ImportError:
    print("Brak Playwright. Zainstaluj:  pip install -r requirements.txt "
          "&&  python -m playwright install chromium", file=sys.stderr)
    raise SystemExit(1)


HERE = Path(__file__).resolve().parent
# O stanie sesji swiadczy link wyloguj/zaloguj w naglowku. Uwaga: napis
# "Centralny System Uwierzytelniania" jest na KAZDEJ stronie USOSweb UJ,
# takze po zalogowaniu - nie nadaje sie na sygnal wylogowania.
LOGGED_IN_MARKERS = ("wyloguj", "log out", "logout")
LOGGED_OUT_MARKERS = ("zaloguj", "log in", "sign in")


# --------------------------------------------------------------------------- #
# konfiguracja
# --------------------------------------------------------------------------- #

@dataclass
class Target:
    name: str
    url: str
    contains: list[str] = field(default_factory=list)
    selector: str | None = None          # opcjonalny wlasny CSS/XPath do wiersza
    # stan runtime
    status: str = "oczekuje"
    attempts: int = 0
    detail: str = ""


@dataclass
class Config:
    start_time: str
    timezone: str = "Europe/Warsaw"
    lead_ms: int = 400                   # zacznij odswiezac tyle ms przed czasem
    interval_ms: int = 700               # odstep miedzy probami
    max_seconds: int = 300               # jak dlugo probowac po starcie
    headless: bool = False
    profile_dir: str = ".usos-profile"
    keepalive_s: int = 120               # odswiezanie strony przed startem
    targets: list[Target] = field(default_factory=list)

    @property
    def tz(self):
        try:
            return ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            # Windows nie ma systemowej bazy stref - brak pakietu 'tzdata'.
            # Awaryjnie uzywamy strefy lokalnej komputera.
            local = datetime.now().astimezone().tzinfo
            print(f"! Brak bazy stref czasowych dla {self.timezone!r} "
                  f"(zainstaluj: pip install tzdata). Uzywam strefy lokalnej: {local}.",
                  file=sys.stderr)
            return local


def load_config(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    targets = []
    for i, t in enumerate(raw.get("targets") or [], 1):
        if not t.get("url"):
            raise SystemExit(f"Cel #{i}: brakuje pola 'url'.")
        contains = t.get("contains") or []
        if isinstance(contains, str):
            contains = [contains]
        for c in contains:
            # W YAML-u w cudzyslowie "\b", "\d", "\s" to sekwencje sterujace i psuja
            # regexy. W apostrofach nic sie nie dzieje - stad ta podpowiedz.
            if any(ord(ch) < 32 for ch in str(c)):
                print(f"! Cel {t.get('name')!r}: wzorzec {c!r} zawiera znak sterujacy - "
                      "w config.yaml zapisz go w APOSTROFACH, np. '/grupa nr 3\\b/'.",
                      file=sys.stderr)
        if not contains and not t.get("selector"):
            raise SystemExit(f"Cel #{i} ({t.get('name')}): podaj 'contains' albo 'selector'.")
        targets.append(Target(
            name=t.get("name") or f"cel-{i}",
            url=t["url"],
            contains=[str(c) for c in contains],
            selector=t.get("selector"),
        ))
    if not targets:
        raise SystemExit("Config nie zawiera zadnych celow ('targets').")

    known = set(Config.__dataclass_fields__)
    kwargs = {k: v for k, v in raw.items() if k in known and k != "targets"}
    if "start_time" not in kwargs:
        raise SystemExit("Config nie zawiera 'start_time'.")
    return Config(targets=targets, **kwargs)


def parse_start_time(cfg: Config) -> float:
    """Zwraca epoch (wg zegara serwera USOS) momentu startu rejestracji."""
    s = str(cfg.start_time).strip()
    if s.lower() in ("now", "teraz"):
        return time.time()
    m = re.fullmatch(r"\+(\d+)([smh])", s.lower())
    if m:
        mult = {"s": 1, "m": 60, "h": 3600}[m.group(2)]
        return time.time() + int(m.group(1)) * mult
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=cfg.tz).timestamp()
        except ValueError:
            continue
    raise SystemExit(f"Nie umiem sparsowac start_time={s!r} "
                     "(format: 'YYYY-MM-DD HH:MM:SS', 'now' albo '+30s').")


# --------------------------------------------------------------------------- #
# synchronizacja z zegarem serwera
# --------------------------------------------------------------------------- #

async def server_clock_offset(ctx: BrowserContext, url: str, samples: int = 5) -> float:
    """offset = czas_serwera - czas_lokalny (s). Bierzemy probke o najmniejszym RTT.

    Zapytania ida przez stos sieciowy przegladarki - dzieki temu dzialaja te same
    certyfikaty i proxy co przy normalnym przegladaniu (Python sam potrafi tu
    polec na CERTIFICATE_VERIFY_FAILED).
    """
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}/"
    best_rtt, best_off = None, 0.0
    for _ in range(samples):
        t0 = time.time()
        date_hdr = None
        for method in ("head", "get"):
            try:
                resp = await getattr(ctx.request, method)(origin, timeout=10000)
                date_hdr = resp.headers.get("date")
                if date_hdr:
                    break
            except Exception:
                continue
        t1 = time.time()
        if not date_hdr:
            continue
        try:
            server = parsedate_to_datetime(date_hdr).timestamp()
        except Exception:
            continue
        rtt = t1 - t0
        # naglowek powstal mniej wiecej w polowie RTT; +0.5 s bo Date ma rozdzielczosc sekundy
        offset = (server + 0.5) - (t0 + rtt / 2)
        if best_rtt is None or rtt < best_rtt:
            best_rtt, best_off = rtt, offset
        await asyncio.sleep(0.15)
    if best_rtt is None:
        log("! Nie udalo sie odczytac czasu serwera - uzywam zegara lokalnego.")
        return 0.0
    log(f"Zegar serwera: offset {best_off:+.2f} s "
        f"(RTT {best_rtt * 1000:.0f} ms, dokladnosc ~0.5 s)")
    return best_off


# --------------------------------------------------------------------------- #
# pomocnicze
# --------------------------------------------------------------------------- #

def log(msg: str, tag: str = "") -> None:
    stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    prefix = f"[{stamp}]" + (f" [{tag}]" if tag else "")
    print(f"{prefix} {msg}", flush=True)


async def sleep_until(local_epoch: float) -> None:
    while True:
        d = local_epoch - time.time()
        if d <= 0:
            return
        await asyncio.sleep(d - 0.4 if d > 1.0 else min(d, 0.05))


# JS: znajdz w wierszu klikalny element rejestracji
JS_FIND_REGISTER = """
(row) => {
  const good = /zarejestruj|zapisz si|dopisz|rejestruj/i;
  const bad  = /wyrejestruj|wypisz|rezygn/i;
  const els = row.querySelectorAll(
    "a, button, input[type=image], input[type=submit], input[type=button]");
  for (const el of els) {
    const img = el.querySelector ? el.querySelector("img") : null;
    const bits = [
      el.getAttribute("title"), el.getAttribute("alt"), el.getAttribute("href"),
      el.getAttribute("src"), el.getAttribute("value"), el.getAttribute("onclick"),
      el.textContent,
      img && img.getAttribute("alt"), img && img.getAttribute("title"),
      img && img.getAttribute("src")
    ].filter(Boolean).join(" ");
    if (bad.test(bits)) continue;
    if (good.test(bits)) return el;
  }
  return null;
}
"""

JS_HAS_UNREGISTER = """
(row) => /wyrejestruj|wypisz/i.test(
  Array.from(row.querySelectorAll("a, button, input, img"))
    .map(e => [e.getAttribute("title"), e.getAttribute("alt"), e.getAttribute("href"),
               e.getAttribute("src"), e.getAttribute("value"), e.textContent]
              .filter(Boolean).join(" "))
    .join(" ")
)
"""

JS_DUMP_ROWS = r"""
() => {
  const good = /zarejestruj|zapisz si|dopisz/i;
  const out = [];
  for (const row of document.querySelectorAll("tr")) {
    if (row.querySelector("tr")) continue;            // tylko najglebsze wiersze
    const html = row.innerHTML || "";
    const txt = (row.innerText || "").replace(/\s+/g, " ").trim();
    if (!txt) continue;
    if (!good.test(html) && !/wyrejestruj/i.test(html)) continue;
    out.push({ text: txt.slice(0, 300), registered: /wyrejestruj/i.test(html) });
  }
  return out;
}
"""


# JS: wybierz najglebszy wiersz <tr> spelniajacy wszystkie warunki 'contains'.
# Szukamy w JS, a nie przez has_text Playwrighta, bo tam czesc regexow (m.in.
# granica slowa) nie przechodzi przez warstwe selektorow.
JS_MATCH_ROWS = r"""
(conds) => {
  const rows = Array.from(document.querySelectorAll("tr"))
                    .filter(r => !r.querySelector("tr"));   // tylko najglebsze
  const hits = rows.filter(r => {
    const txt = (r.innerText || "").replace(/\s+/g, " ").trim();
    return conds.every(c => c.re
      ? new RegExp(c.v, "i").test(txt)
      : txt.toLowerCase().includes(c.v.toLowerCase()));
  });
  return { count: hits.length, el: hits[0] || null, text: hits[0] ? hits[0].innerText.replace(/\s+/g, " ").trim().slice(0, 120) : "" };
}
"""


def _conds(t: Target) -> list[dict]:
    """Zwykly tekst = fragment; /wzorzec/ = wyrazenie regularne."""
    out = []
    for s in t.contains:
        if len(s) > 2 and s.startswith("/") and s.endswith("/"):
            out.append({"re": True, "v": s[1:-1]})
        else:
            out.append({"re": False, "v": s})
    return out


async def resolve_row(page: Page, t: Target):
    """Zwraca (uchwyt najglebszego pasujacego wiersza | None, opis)."""
    if t.selector:
        loc = page.locator(t.selector)
        n = await loc.count()
        if n == 0:
            return None, "nie znaleziono wiersza (selector)"
        return await loc.last.element_handle(timeout=5000), f"{n} dopasowan (selector)"

    res = await page.evaluate_handle(JS_MATCH_ROWS, _conds(t))
    count = await (await res.get_property("count")).json_value()
    if not count:
        return None, "nie znaleziono wiersza"
    el = (await res.get_property("el")).as_element()
    text = await (await res.get_property("text")).json_value()
    suffix = "" if count == 1 else f" - UWAGA, biore pierwszy: {text!r}"
    return el, f"{count} dopasowan{suffix}"


async def page_logged_out(page: Page) -> bool:
    if re.search(r"(cas|login)\.uj\.edu\.pl|_action=logowanie", page.url, re.I):
        return True
    try:
        body = (await page.inner_text("body")).lower()
    except Exception:
        return False
    if any(m in body for m in LOGGED_IN_MARKERS):   # jest link wylogowania
        return False
    if any(m in body for m in LOGGED_OUT_MARKERS):  # jest link logowania
        return True
    return False   # w razie watpliwosci nie przerywamy - blad i tak wyjdzie dalej


async def confirm_if_needed(page: Page) -> None:
    """USOS czasem pokazuje ekran potwierdzenia - klikamy przycisk potwierdzajacy."""
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:
        pass
    for sel in ('input[type=submit][value*="arejestruj" i]',
                'button:has-text("Zarejestruj")',
                'input[type=submit][value*="otwierd" i]',
                'button:has-text("Potwierdź")',
                'a:has-text("Potwierdź")'):
        try:
            btn = page.locator(sel).first
            if await btn.count() and await btn.is_visible():
                log(f"klikam potwierdzenie: {sel}")
                await btn.click(timeout=5000)
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
                return
        except Exception:
            continue


SUCCESS_RE = re.compile(
    r"jesteś zarejestrowan|zostałeś zarejestrowan|rejestracja zakończona sukcesem|"
    r"pomyślnie zarejestrowan|zarejestrowano cię|zapisano cię", re.I)
FAILURE_RE = re.compile(
    r"brak wolnych miejsc|limit miejsc|nie możesz się zarejestrować|"
    r"rejestracja jeszcze się nie rozpoczęła|rejestracja została zakończona|"
    r"nie jest jeszcze otwarta|nie masz uprawnień|kolizj", re.I)


# --------------------------------------------------------------------------- #
# glowna petla dla jednego celu
# --------------------------------------------------------------------------- #

async def work_target(context: BrowserContext, t: Target, cfg: Config,
                      start_local: float, dry_run: bool) -> None:
    page = await context.new_page()
    page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
    tag = t.name

    async def goto():
        await page.goto(t.url, wait_until="domcontentloaded", timeout=45000)

    try:
        await goto()
    except Exception as e:
        t.status, t.detail = "blad", f"nie moge otworzyc strony: {e}"
        log(f"BLAD otwarcia strony: {e}", tag)
        return

    if await page_logged_out(page):
        t.status, t.detail = "blad", "brak sesji - zaloguj sie (tryb 'login')"
        log("!! Nie jestes zalogowany. Uruchom: python usos_auto.py login", tag)
        return

    row, why = await resolve_row(page, t)
    if row is None:
        log(f"UWAGA: {why} - sprawdz 'contains'. Sprobuje ponownie po starcie.", tag)
    else:
        log(f"wiersz znaleziony ({why})", tag)

    # --- keepalive do momentu startu ---
    while True:
        remaining = start_local - (cfg.lead_ms / 1000.0) - time.time()
        if remaining <= 0:
            break
        if remaining > cfg.keepalive_s:
            await asyncio.sleep(cfg.keepalive_s)
            try:
                await goto()
                if await page_logged_out(page):
                    log("!! SESJA WYGASLA - zaloguj sie ponownie w otwartym oknie!", tag)
            except Exception as e:
                log(f"keepalive nieudany: {e}", tag)
        else:
            await sleep_until(start_local - cfg.lead_ms / 1000.0)
            break

    log("START - zaczynam proby rejestracji", tag)
    deadline = start_local + cfg.max_seconds

    while time.time() < deadline:
        t.attempts += 1
        try:
            await goto()

            if await page_logged_out(page):
                t.status, t.detail = "blad", "sesja wygasla"
                log("!! sesja wygasla w trakcie rejestracji", tag)
                return

            row, why = await resolve_row(page, t)
            if row is None:
                log(f"proba {t.attempts}: {why}", tag)
                await asyncio.sleep(cfg.interval_ms / 1000.0)
                continue

            handle = row
            if handle and await handle.evaluate(JS_HAS_UNREGISTER):
                t.status, t.detail = "OK", "juz zarejestrowany (jest link 'wyrejestruj')"
                log("*** ZAREJESTROWANY ***", tag)
                return

            btn = None
            if handle:
                jh = await handle.evaluate_handle(JS_FIND_REGISTER)
                btn = jh.as_element()

            if btn is None:
                body = await page.inner_text("body")
                m = FAILURE_RE.search(body)
                log(f"proba {t.attempts}: brak przycisku rejestracji"
                    + (f" ({m.group(0)})" if m else ""), tag)
                await asyncio.sleep(cfg.interval_ms / 1000.0)
                continue

            if dry_run:
                label = await btn.evaluate(
                    "e => (e.getAttribute('title') || e.getAttribute('alt') || "
                    "e.textContent || e.getAttribute('href') || '').trim().slice(0,120)")
                t.status, t.detail = "dry-run", f"znaleziony przycisk: {label!r}"
                log(f"[DRY-RUN] tutaj bym kliknal: {label!r}", tag)
                return

            log(f"proba {t.attempts}: klikam rejestracje", tag)
            await btn.click(timeout=8000)
            await confirm_if_needed(page)

            body = await page.inner_text("body")
            hit = SUCCESS_RE.search(body)
            if hit:
                t.status, t.detail = "OK", hit.group(0)
                log(f"*** ZAREJESTROWANY *** ({t.detail})", tag)
                return

            # weryfikacja przez ponowne wczytanie strony rejestracji
            await goto()
            row, _ = await resolve_row(page, t)
            if row is not None:
                if await row.evaluate(JS_HAS_UNREGISTER):
                    t.status, t.detail = "OK", "potwierdzone po odswiezeniu"
                    log("*** ZAREJESTROWANY *** (potwierdzone)", tag)
                    return

            m = FAILURE_RE.search(body)
            log(f"proba {t.attempts}: kliknieto, ale bez potwierdzenia"
                + (f" - {m.group(0)}" if m else ""), tag)

        except Exception as e:
            log(f"proba {t.attempts}: blad - {type(e).__name__}: {e}", tag)

        await asyncio.sleep(cfg.interval_ms / 1000.0)

    if t.status == "oczekuje":
        t.status = "nieudane"
        t.detail = f"limit czasu ({cfg.max_seconds} s), prob: {t.attempts}"
    log(f"koniec: {t.status} - {t.detail}", tag)


# --------------------------------------------------------------------------- #
# tryby
# --------------------------------------------------------------------------- #

async def open_context(pw, cfg: Config, headless: bool) -> BrowserContext:
    profile = (HERE / cfg.profile_dir).resolve()
    profile.mkdir(parents=True, exist_ok=True)
    return await pw.chromium.launch_persistent_context(
        user_data_dir=str(profile),
        headless=headless,
        viewport={"width": 1440, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
    )


async def mode_login(cfg: Config, start_url: str) -> None:
    async with async_playwright() as pw:
        ctx = await open_context(pw, cfg, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(start_url, wait_until="domcontentloaded")
        print("\n>>> Zaloguj sie w otwartym oknie (CAS UJ + ewentualne 2FA).")
        print(">>> Gdy zobaczysz swoje USOSweb, wroc tutaj i nacisnij Enter.\n")
        await asyncio.get_running_loop().run_in_executor(None, input)
        logged_out = await page_logged_out(page)
        await ctx.close()
        print("Sesja zapisana."
              if not logged_out else
              "Uwaga: strona nadal wyglada na wylogowana - sprobuj jeszcze raz.")


async def mode_list(cfg: Config, url: str) -> None:
    async with async_playwright() as pw:
        ctx = await open_context(pw, cfg, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        if await page_logged_out(page):
            print("Nie jestes zalogowany. Uruchom najpierw: python usos_auto.py login")
            await ctx.close()
            return
        rows = await page.evaluate(JS_DUMP_ROWS)
        if not rows:
            print("Nie znalazlem wierszy z przyciskiem rejestracji.\n"
                  "Upewnij sie, ze to strona 'Rejestracja bezposrednia do grup' "
                  "z widoczna lista grup.")
        for i, r in enumerate(rows, 1):
            flag = " [JUZ ZAREJESTROWANY]" if r["registered"] else ""
            print(f"\n--- {i}{flag} ---\n{r['text']}")
        print("\nSkopiuj z powyzszych fragmenty jednoznacznie identyfikujace grupe "
              "do pola 'contains' w config.yaml (np. kod przedmiotu + 'grupa nr 3').")
        await ctx.close()


async def mode_run(cfg: Config, dry_run: bool) -> None:
    start_server = parse_start_time(cfg)

    async with async_playwright() as pw:
        ctx = await open_context(pw, cfg, headless=cfg.headless)
        try:
            offset = await server_clock_offset(ctx, cfg.targets[0].url)
            start_local = start_server - offset

            log(f"Start rejestracji: "
                f"{datetime.fromtimestamp(start_server, cfg.tz):%Y-%m-%d %H:%M:%S %Z} "
                f"(lokalnie {datetime.fromtimestamp(start_local):%H:%M:%S})")
            log(f"Do startu: {max(0.0, start_local - time.time()):.1f} s "
                f"| celow: {len(cfg.targets)}"
                + (" | TRYB TESTOWY (bez klikania)" if dry_run else ""))

            await asyncio.gather(*(
                work_target(ctx, t, cfg, start_local, dry_run) for t in cfg.targets
            ))
        finally:
            print("\n================ PODSUMOWANIE ================")
            for t in cfg.targets:
                print(f"  {t.status:<10} {t.name}  ({t.attempts} prob) {t.detail}")
            print("==============================================")
            report = HERE / f"raport-{datetime.now():%Y%m%d-%H%M%S}.json"
            report.write_text(json.dumps(
                [{"name": t.name, "status": t.status, "attempts": t.attempts,
                  "detail": t.detail} for t in cfg.targets],
                ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Raport: {report.name}")
            if not cfg.headless:
                print("Okno przegladarki zamknie sie za 30 s "
                      "(mozesz w tym czasie sprawdzic wynik recznie).")
                await asyncio.sleep(30)
            await ctx.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Automatyczna rejestracja USOSweb UJ")
    ap.add_argument("mode", choices=["login", "list", "run", "test"])
    ap.add_argument("-c", "--config", default=str(HERE / "config.yaml"))
    ap.add_argument("--url", help="adres strony rejestracji (dla 'login' i 'list')")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    if cfg_path.exists():
        cfg = load_config(cfg_path)
    elif args.mode in ("login", "list"):
        cfg = Config(start_time="now", targets=[Target(name="tmp", url="x", contains=["x"])])
    else:
        raise SystemExit(f"Nie znaleziono configu: {cfg_path}")

    if args.mode == "login":
        url = args.url or (cfg.targets[0].url if cfg_path.exists() else
                           "https://usosweb.uj.edu.pl/kontroler.php?_action=news/default")
        asyncio.run(mode_login(cfg, url))
    elif args.mode == "list":
        url = args.url or (cfg.targets[0].url if cfg_path.exists() else None)
        if not url:
            raise SystemExit("Podaj --url do strony rejestracji.")
        asyncio.run(mode_list(cfg, url))
    else:
        asyncio.run(mode_run(cfg, dry_run=(args.mode == "test")))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nPrzerwane przez uzytkownika.")
