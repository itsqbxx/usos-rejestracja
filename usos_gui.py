#!/usr/bin/env python3
"""Okienko do usos_rej.py: logowanie, wybór przedmiotów i grup, test i start rejestracji.

Uruchamianie: dwuklik na "Rejestracja USOS.bat" (albo: python.exe usos_gui.py).
"""

import json
import queue
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import usos_rej as U
except Exception:
    tk.Tk().withdraw()
    messagebox.showerror("Błąd uruchomienia", traceback.format_exc())
    raise

CONFIG = HERE / "config.json"
COOKIE = HERE / "cookie.txt"
LOGFILE = HERE / "usos_log.txt"
DEFAULT_REJ = "WFAIS_26/27Z_INF"
DEFAULT_CDYD = "26/27Z"
NONE = "— brak —"
DAYS = {"Poniedziałek": "Pon", "Wtorek": "Wt", "Środa": "Śr", "Czwartek": "Czw",
        "Piątek": "Pt", "Sobota": "Sob", "Niedziela": "Nd"}


def group_label(g):
    termin = g["termin"]
    for full, short in DAYS.items():
        termin = termin.replace(full, short)
    who = g["prowadzacy"].split()[-1] if g["prowadzacy"] and g["prowadzacy"] != "brak" else "brak prow."
    full = "" if g["ma_miejsca"] else " · PEŁNA"
    return f"gr {g['nr']} · {termin or '?'} · {who} · {g['zapisani']}/{g['limit']}{full}"


class QueueWriter:
    """Przekierowuje print() (także z wątków) do okna dziennika i do pliku usos_log.txt."""

    def __init__(self, q):
        self.q = q
        self.lock = threading.Lock()
        self.file = open(LOGFILE, "a", encoding="utf-8")
        self.file.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} start okna =====\n")

    def write(self, s):
        if s:
            self.q.put(("log", s))
            with self.lock:
                self.file.write(s)
                self.file.flush()

    def flush(self):
        pass


class ScrollFrame(ttk.Frame):
    def __init__(self, parent, **kw):
        super().__init__(parent, **kw)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.inner.bind("<Configure>",
                        lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(win, width=e.width))
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def clear(self):
        for w in self.inner.winfo_children():
            w.destroy()


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        sys.stdout = sys.stderr = QueueWriter(self.q)
        root.report_callback_exception = \
            lambda *exc: print("BŁĄD okna:\n" + "".join(traceback.format_exception(*exc)))
        self.cfg = self.load_cfg()
        self.courses = []      # [{nazwa, prz_kod, cdyd_kod}] z listy rejestracji
        self.course_vars = {}  # prz_kod -> BooleanVar
        self.sections = {}     # prz_kod -> {"frame", "info", "rows"}; kolejność = priorytet
        self.loading = set()
        self.worker = None
        self.logged = False
        self.open_at = None
        self._cd = None
        self.build()
        root.unbind_class("TCombobox", "<MouseWheel>")  # kółko nie zmienia przypadkiem grup
        root.bind_all("<MouseWheel>", self.on_wheel)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.poll()
        self.check_login()

    # ------------------------------------------------------------ wygląd

    def build(self):
        r = self.root
        r.title("USOS – automatyczna rejestracja do grup")
        r.geometry("1150x850")
        r.minsize(900, 620)
        style = ttk.Style()
        style.configure("Big.TLabel", font=("Segoe UI", 13, "bold"))
        style.configure("Head.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("Start.TButton", font=("Segoe UI", 10, "bold"))

        top = ttk.LabelFrame(r, text=" 1. Logowanie do USOS ", padding=8)
        top.pack(fill="x", padx=10, pady=(10, 4))
        self.login_status = ttk.Label(top, text="Sprawdzam sesję…")
        self.login_status.pack(side="left")
        self.login_btn = ttk.Button(top, text="Zaloguj przez przeglądarkę", command=self.login)
        self.login_btn.pack(side="right")

        mid = ttk.PanedWindow(r, orient="horizontal")
        mid.pack(fill="both", expand=True, padx=10, pady=4)

        left = ttk.LabelFrame(mid, text=" 2. Zaznacz przedmioty ", padding=8)
        row = ttk.Frame(left)
        row.pack(fill="x")
        ttk.Label(row, text="Kod rejestracji:").pack(side="left")
        self.rej_var = tk.StringVar(value=self.cfg.get("rej_kod", DEFAULT_REJ))
        ttk.Entry(row, textvariable=self.rej_var, width=20).pack(side="left", padx=5)
        self.list_btn = ttk.Button(row, text="Wczytaj", command=self.load_list)
        self.list_btn.pack(side="left")
        self.course_box = ScrollFrame(left)
        self.course_box.pack(fill="both", expand=True, pady=(8, 0))
        mid.add(left, weight=1)

        right = ttk.LabelFrame(mid, text=" 3. Wybierz grupy (kolejność przedmiotów = kolejność zaznaczania) ",
                               padding=8)
        self.group_box = ScrollFrame(right)
        self.group_box.pack(fill="both", expand=True)
        self.show_groups_hint()
        mid.add(right, weight=2)

        ctl = ttk.LabelFrame(r, text=" 4. Rejestracja ", padding=8)
        ctl.pack(fill="x", padx=10, pady=4)
        self.save_btn = ttk.Button(ctl, text="Zapisz ustawienia", command=self.save)
        self.test_btn = ttk.Button(ctl, text="Test (próba przed turą)", command=self.probe)
        self.start_btn = ttk.Button(ctl, text="▶ START – czekaj na turę i zarejestruj",
                                    style="Start.TButton", command=self.start)
        self.stop_btn = ttk.Button(ctl, text="■ STOP", command=self.stop, state="disabled")
        for b in (self.save_btn, self.test_btn, self.start_btn, self.stop_btn):
            b.pack(side="left", padx=(0, 6))
        self.countdown = ttk.Label(ctl, text="", style="Big.TLabel")
        self.countdown.pack(side="right")

        logf = ttk.LabelFrame(r, text=" Dziennik ", padding=4)
        logf.pack(fill="both", padx=10, pady=(4, 10))
        self.log = tk.Text(logf, height=13, wrap="word", font=("Consolas", 9), state="disabled")
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.log.tag_configure("ok", foreground="#0a7d20", font=("Consolas", 9, "bold"))
        self.log.tag_configure("err", foreground="#c0261c", font=("Consolas", 9, "bold"))

    def show_groups_hint(self):
        ttk.Label(self.group_box.inner, foreground="gray",
                  text="Zaznacz przedmioty po lewej – tutaj pojawią się ich grupy.").pack(anchor="w")

    def on_wheel(self, e):
        w = self.root.winfo_containing(e.x_root, e.y_root)
        while w is not None:
            if isinstance(w, tk.Text):
                return
            if isinstance(w, ScrollFrame):
                w.canvas.yview_scroll(int(-e.delta / 120), "units")
                return
            w = w.master

    # ------------------------------------------------------------ wątki i dziennik

    def poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    tag = ()
                    if "SUKCES" in payload or "Zalogowano" in payload:
                        tag = ("ok",)
                    elif any(k in payload for k in ("BŁĄD", "UWAGA", "WYGASŁA", "nieudane")):
                        tag = ("err",)
                    self.log.configure(state="normal")
                    self.log.insert("end", payload, tag)
                    self.log.configure(state="disabled")
                    self.log.see("end")
                else:
                    payload()
        except queue.Empty:
            pass
        self.tick()
        self.root.after(100, self.poll)

    def bg(self, job, done=None, busy=True, running=False, on_error=None):
        """Uruchamia job() w tle; done(wynik) wraca do wątku okna."""
        if busy and self.worker and self.worker.is_alive():
            messagebox.showinfo("Chwila", "Poczekaj, aż skończy się bieżące zadanie.")
            return

        def target():
            ok, res = False, None
            try:
                res, ok = job(), True
            except U.Stopped:
                print("Zatrzymano.")
            except U.SessionExpired as e:
                print(f"Sesja USOS wygasła ({e}) – kliknij „Zaloguj przez przeglądarkę”.")
                self.q.put(("call", lambda: self.set_logged(None)))
            except U.LoginFailed as e:
                print(f"Logowanie nieudane: {e}")
            except SystemExit as e:
                if e.code not in (None, 0):
                    print(f"BŁĄD: {e.code}")
            except Exception as e:
                print(f"BŁĄD: {type(e).__name__}: {e}")
                traceback.print_exc()
            if ok and done:
                self.q.put(("call", lambda: done(res)))
            if not ok and on_error:
                self.q.put(("call", on_error))
            if busy:
                self.q.put(("call", lambda: self.set_busy(False)))

        t = threading.Thread(target=target, daemon=True)
        if busy:
            self.set_busy(True, running)
            self.worker = t
        t.start()

    def set_busy(self, flag, running=False):
        state = "disabled" if flag else "normal"
        for b in (self.save_btn, self.test_btn, self.start_btn, self.login_btn, self.list_btn):
            b.configure(state=state)
        self.stop_btn.configure(state="normal" if flag and running else "disabled")

    def tick(self):
        text = ""
        if self.open_at is not None:
            left = self.open_at.timestamp() - time.time()
            text = (f"Tura: {self.open_at:%H:%M:%S}   (za {U.fmt_left(left)})" if left > 0
                    else f"Tura otwarta od {self.open_at:%H:%M}")
        if text != self._cd:
            self.countdown.configure(text=text)
            self._cd = text

    # ------------------------------------------------------------ logowanie

    def set_logged(self, name):
        self.logged = bool(name)
        if name:
            self.login_status.configure(text=f"✔ Zalogowany: {name}", foreground="#0a7d20")
            self.login_btn.configure(text="Zaloguj ponownie")
        else:
            self.login_status.configure(text="✖ Niezalogowany – kliknij przycisk po prawej",
                                        foreground="#c0261c")
            self.login_btn.configure(text="Zaloguj przez przeglądarkę")

    def check_login(self):
        def job():
            if not COOKIE.exists():
                return None
            try:
                return U.whoami(U.read_cookie(COOKIE))
            except SystemExit:
                return None

        def done(name):
            self.set_logged(name)
            if name:
                self.load_list()

        self.bg(job, done, busy=False)

    def login(self):
        print("Otwieram okno logowania – zaloguj się na stronie UJ, okno zamknie się samo.")

        def job():
            U.browser_login(COOKIE)
            return U.whoami(U.read_cookie(COOKIE))

        def done(name):
            self.set_logged(name)
            if name:
                self.load_list()

        self.bg(job, done)

    # ------------------------------------------------------------ przedmioty i grupy

    def load_cfg(self):
        try:
            return json.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def load_list(self):
        if not self.logged:
            messagebox.showinfo("Logowanie", "Najpierw zaloguj się (punkt 1).")
            return
        rej = self.rej_var.get().strip()

        def job():
            return U.fetch_course_list(U.read_cookie(COOKIE), rej, self.cfg.get("cdyd_kod", DEFAULT_CDYD))

        def done(courses):
            self.courses, self.course_vars, self.sections = courses, {}, {}
            self.loading.clear()
            self.open_at = None
            self.course_box.clear()
            self.group_box.clear()
            self.show_groups_hint()
            if not courses:
                print(f"UWAGA: nie znalazłem przedmiotów w rejestracji {rej} – sprawdź kod rejestracji.")
                return
            for c in courses:
                var = tk.BooleanVar(value=False)
                ttk.Checkbutton(self.course_box.inner, text=f"{c['nazwa']}  [{c['prz_kod']}]", variable=var,
                                command=lambda k=c["prz_kod"]: self.toggle(k)).pack(anchor="w", pady=1)
                self.course_vars[c["prz_kod"]] = var
            print(f"Wczytano {len(courses)} przedmiotów z rejestracji {rej}.")
            pre = [p["prz_kod"] for p in self.cfg.get("przedmioty", [])
                   if p["prz_kod"] in self.course_vars and p.get("rej_kod", self.cfg.get("rej_kod")) == rej]
            for k in pre:
                self.course_vars[k].set(True)
            if pre:
                print("Zaznaczam przedmioty z zapisanych ustawień…")
                self.load_groups(pre)

        self.bg(job, done)

    def toggle(self, kod):
        if self.course_vars[kod].get():
            if kod not in self.sections and kod not in self.loading:
                self.load_groups([kod])
        else:
            sec = self.sections.pop(kod, None)
            if sec:
                sec["frame"].destroy()
            if not self.sections and not self.loading:
                self.group_box.clear()
                self.show_groups_hint()

    def load_groups(self, kods):
        rej = self.rej_var.get().strip()
        infos = [c for k in kods for c in self.courses if c["prz_kod"] == k]
        self.loading.update(kods)

        def job():
            cookie, clock, out = U.read_cookie(COOKIE), U.ServerClock(), []
            for info in infos:
                course = U.Course({**info, "rej_kod": rej}, self.cfg, cookie, clock)
                try:
                    out.append((info, course.refresh()))
                except U.NoGroupChoice as e:
                    out.append((info, str(e)))
            return out

        def done(out):
            for info, page in out:
                self.loading.discard(info["prz_kod"])
                var = self.course_vars.get(info["prz_kod"])
                if var is not None and var.get() and info["prz_kod"] not in self.sections:
                    self.add_section(info, page)

        self.bg(job, done, busy=False, on_error=lambda: self.loading.difference_update(kods))

    def add_section(self, info, page):
        if not self.sections:
            self.group_box.clear()
        kod = info["prz_kod"]
        if isinstance(page, str):  # przedmiot bez formularza wyboru grup
            fr = ttk.Frame(self.group_box.inner, padding=(0, 2, 0, 12))
            fr.pack(fill="x", anchor="w")
            ttk.Label(fr, text=f"{info['nazwa']}  [{kod}]", style="Head.TLabel").pack(anchor="w")
            ttk.Label(fr, foreground="#b35c00", wraplength=650, justify="left",
                      text=f"Pomijam – {page}. Program nie ma czego tu wysłać; sprawdź w przeglądarce, "
                           f"jak zapisać się na ten przedmiot.").pack(anchor="w", padx=(12, 0))
            self.sections[kod] = {"frame": fr, "info": info, "rows": None}
            return
        prefs = next((p.get("grupy", {}) for p in self.cfg.get("przedmioty", []) if p["prz_kod"] == kod), {})
        fr = ttk.Frame(self.group_box.inner, padding=(0, 2, 0, 12))
        fr.pack(fill="x", anchor="w")
        ttk.Label(fr, text=f"{info['nazwa']}  [{kod}]", style="Head.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w")
        rows, r = {}, 1
        for cls in page["classes"].values():
            groups = cls["grupy"]
            ttk.Label(fr, text=cls["typ"] + ":").grid(row=r, column=0, sticky="nw", padx=(12, 8), pady=2)
            if len(groups) == 1:
                (n, g), = groups.items()
                ttk.Label(fr, text=group_label(g) + "   (jedyna grupa)").grid(row=r, column=1, columnspan=2,
                                                                              sticky="w", pady=2)
                rows[cls["typ"]] = {"single": n}
                r += 1
                continue
            labels = {group_label(g): n for n, g in groups.items()}
            wanted = prefs.get(cls["typ"])
            wanted = [wanted] if isinstance(wanted, int) else (wanted or [])
            chosen = [n for n in wanted if n in groups] or \
                [next((n for n, g in groups.items() if g["checked"]), next(iter(groups)))]
            combos = []
            for j, caption in enumerate(("1. wybór:", "2. zapasowa:", "3. zapasowa:")):
                ttk.Label(fr, text=caption, foreground="" if j == 0 else "gray").grid(
                    row=r, column=1, sticky="w", pady=1)
                cb = ttk.Combobox(fr, state="readonly", width=52,
                                  values=list(labels) if j == 0 else [NONE] + list(labels))
                cb.set(group_label(groups[chosen[j]]) if j < len(chosen) else NONE)
                cb.grid(row=r, column=2, sticky="w", padx=4, pady=1)
                combos.append(cb)
                r += 1
            rows[cls["typ"]] = {"combos": combos, "labels": labels}
        self.sections[kod] = {"frame": fr, "info": info, "rows": rows}
        if page["open_at"] is not None and (self.open_at is None or page["open_at"] < self.open_at):
            self.open_at = page["open_at"]

    # ------------------------------------------------------------ zapis i uruchamianie

    def collect_cfg(self):
        rej = self.rej_var.get().strip()
        przedmioty = []
        for kod, sec in self.sections.items():
            if sec["rows"] is None:
                continue
            grupy = {}
            for typ, row in sec["rows"].items():
                if "single" in row:
                    grupy[typ] = [row["single"]]
                    continue
                nrs = []
                for cb in row["combos"]:
                    n = row["labels"].get(cb.get())
                    if n is not None and n not in nrs:
                        nrs.append(n)
                if nrs:
                    grupy[typ] = nrs
            info = sec["info"]
            przedmioty.append({"nazwa": info["nazwa"], "prz_kod": kod, "rej_kod": rej,
                               "cdyd_kod": info["cdyd_kod"], "prgos_id": info.get("prgos_id"),
                               "grupy": grupy})
        return {"rej_kod": rej, "cdyd_kod": self.cfg.get("cdyd_kod", DEFAULT_CDYD), "przedmioty": przedmioty}

    def save(self):
        if self.loading:
            messagebox.showinfo("Chwila", "Poczekaj, aż wczytają się grupy.")
            return False
        cfg = self.collect_cfg()
        if not cfg["przedmioty"]:
            messagebox.showwarning("Brak przedmiotów", "Zaznacz po lewej co najmniej jeden przedmiot.")
            return False
        if CONFIG.exists():
            CONFIG.with_name(CONFIG.name + ".bak").write_text(CONFIG.read_text(encoding="utf-8"),
                                                              encoding="utf-8")
        CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.cfg = cfg
        print("Zapisano ustawienia:")
        for i, p in enumerate(cfg["przedmioty"], 1):
            print(f"  {i}. {p['nazwa']}: " +
                  "; ".join(f"{t} gr {' → '.join(map(str, n))}" for t, n in p["grupy"].items()))
        return True

    def run_task(self, extra_args, label):
        if not self.save():
            return
        U.stop_event.clear()
        argv = extra_args + ["--config", str(CONFIG), "--cookie", str(COOKIE)]
        self.bg(lambda: U.run(U.parse_args(argv)), lambda _: print(f"— {label}: koniec —"), running=True)

    def probe(self):
        if not self.logged:
            messagebox.showinfo("Logowanie", "Najpierw zaloguj się (punkt 1).")
            return
        if messagebox.askokcancel(
                "Test",
                "Wyślę po JEDNEJ próbie rejestracji na każdy zaznaczony przedmiot. Tura jest jeszcze "
                "zamknięta, więc USOS ją odrzuci – w dzienniku zobaczysz jego odpowiedź.\n\n"
                "Porównaj ją z komunikatem po kliknięciu „Rejestruj” w przeglądarce.\n"
                "Nie klikaj testu częściej niż co 20 sekund (limit prób USOS)."):
            self.run_task(["--probe"], "Test")

    def start(self):
        if not self.logged:
            messagebox.showinfo("Logowanie", "Najpierw zaloguj się (punkt 1).")
            return
        if messagebox.askyesno(
                "Start",
                "Program poczeka do otwarcia tury i zarejestruje Cię do wybranych grup.\n\n"
                "• nie zamykaj tego okna,\n• nie usypiaj komputera,\n"
                "• nie klikaj „Rejestruj” w przeglądarce (wspólny limit prób!).\n\nStartować?"):
            self.run_task([], "Rejestracja")

    def stop(self):
        U.stop_event.set()
        print("Zatrzymuję…")

    def on_close(self):
        if self.worker and self.worker.is_alive() and str(self.stop_btn["state"]) == "normal":
            if not messagebox.askyesno("Zamknąć?", "Rejestracja jest w toku. Na pewno zamknąć program?"):
                return
        U.stop_event.set()
        self.root.destroy()


def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # ostre czcionki przy skalowaniu ekranu
    except Exception:
        pass
    root = tk.Tk()
    try:
        App(root)
    except Exception:
        messagebox.showerror("Błąd", traceback.format_exc())
        raise
    root.mainloop()


if __name__ == "__main__":
    main()
