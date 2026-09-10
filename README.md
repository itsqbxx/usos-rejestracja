# USOS group registration bot (Jagiellonian University)

Automates **first-come-first-served direct group registration** ("rejestracja bezpośrednia do grup")
in USOSweb at the Jagiellonian University: it waits for the registration round to open and submits
the registration a fraction of a second later — all from a small desktop GUI.

Built for personal use and verified in a live registration round (September 2026).
The UI and messages are in Polish.

## Features

- **Desktop GUI** (tkinter): log in, pick courses, choose groups with backups, dry-run test,
  start/stop, live log with a countdown to the round opening.
- **Login through a real browser window** (Playwright + Microsoft Edge). The password is typed only
  on the university's login page — the tool never sees or stores it. If the session expires while
  waiting, the login window re-opens automatically and the new session is picked up.
- **Everything is read live from USOSweb**: course list, groups, seat counts, study-programme id and
  the round opening time. Nothing about a particular round is hard-coded.
- **Server clock synchronisation.** USOSweb exposes the server time only with 1-second resolution.
  Each request yields an interval of possible clock offsets
  `[server_second − t_received, server_second + 1 − t_sent]`; intersecting the intervals from
  ~15 requests spaced 1.09 s apart (so they hit different sub-second phases) narrows the estimate to
  roughly the network round-trip (≈ ±50 ms). The first attempt fires ~0.3 s after the opening.
- **Respects the USOS rate limit** (more than 10 attempts in ~20 s triggers a temporary block):
  a global limiter allows at most 8 attempts per 20 s across all courses, and while the round is
  not confirmed open only one "leader" course re-probes so the others don't waste attempts.
- **Backup groups**: if a group fills up, seat counts are re-read every ~3 s and the next group from
  the preference list is used.
- **Pre-flight probe**: one real registration attempt before the round opens verifies the session,
  CSRF token and payload — the expected answer is *"no round is open at the moment"*.

## How it works

1. `wyborPrzedmiotu` page → list of courses in the registration + the student's programme id (`prgos_id`).
2. `grupyPrzedmiotu` page (with `odczyt=0` and `prgos_id`) → the registration form: hidden fields,
   CSRF token, class ids and groups, plus the round countdown (`data-date` / `data-now`).
3. Clock sync → wait → `POST brdg2/zarejestruj` as an AJAX form submit; USOSweb answers with JSON
   `{"type": "v" | "!", "pl": "...", "en": "..."}` (`v` = success).

## Setup (Windows 10/11)

```
python -m pip install -r requirements.txt
```

Microsoft Edge is used for the login window (Chrome works too), so no extra browser download is needed.

## Usage

Double-click **`Rejestracja USOS.bat`** (or run `python usos_gui.py`), then:

1. *Zaloguj przez przeglądarkę* — log in on the university page.
2. Tick courses (most important first) and pick groups (1st choice + optional backups), *Zapisz ustawienia*.
3. *Test (próba przed turą)* — expect `Żadna tura podanej rejestracji bezpośredniej nie jest teraz otwarta`.
4. *START* a few minutes before the round opens and leave it running.

Command-line equivalents:

```
python usos_rej.py --login      # log in, store the session
python usos_rej.py --kreator    # interactive course/group picker -> config.json
python usos_rej.py --dry-run    # show the plan, send nothing
python usos_rej.py --probe      # one attempt per course before the round opens
python usos_rej.py              # wait for the round and register
```

See `config.example.json` for the settings format (`config.json` is created by the GUI).

## Tests

End-to-end tests run against a local mock of USOSweb (no real requests):

```
python tests/test_rej.py     # parsing, rate limiter, wizard, probe, timing with a skewed server clock,
                             # deliberately early first shot
python tests/test_login.py   # browser login + automatic re-login (headless Edge)
python tests/test_gui.py     # GUI flows: auto-login check, course/group selection, probe, start/stop
```

`test_rej.py` takes ~1.5 min (it waits for two simulated round openings); `test_gui.py` briefly opens a window.

## Privacy

The tool stores your session locally in `cookie.txt` and `.profil_przegladarki/`, your choices in
`config.json` and a log in `usos_log.txt`. All of them are in `.gitignore` — never commit or share them.

## Disclaimer

Personal/educational project. It uses only your own account, does not bypass any security measure
and deliberately stays below the server's rate limits. Check your university's IT regulations before
using automated tools. Not affiliated with the Jagiellonian University or MUCI (the USOS developers).
