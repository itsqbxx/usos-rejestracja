#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Okienkowa nakladka na usos_auto.py - klikanie zamiast edycji YAML-a.

  py usos_gui.py

Sam skrypt rejestrujacy dziala dalej niezaleznie; GUI tylko go uruchamia
i pokazuje jego wyjscie. Konfiguracje zapisuje do tego samego config.yaml,
wiec obie drogi mozna mieszac.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

import yaml

HERE = Path(__file__).resolve().parent
SKRYPT = HERE / "usos_auto.py"
CONFIG = HERE / "config.yaml"
GRUPY_JSON = HERE / ".grupy.json"
REJESTRACJE_URL = ("https://usosweb.uj.edu.pl/kontroler.php"
                   "?_action=dla_stud/rejestracja/brdg2/index")


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Rejestracja USOSweb UJ")
        self.geometry("980x760")
        self.minsize(820, 620)

        self.proc: subprocess.Popen | None = None
        self.kolejka: queue.Queue[str] = queue.Queue()
        self.grupy: list[dict] = []
        self.zaznaczone: list[tk.BooleanVar] = []

        self._buduj()
        self._wczytaj_config()
        self.after(100, self._pompuj_log)
        self.protocol("WM_DELETE_WINDOW", self._zamknij)

    # ------------------------------------------------------------------ #
    # budowa okna
    # ------------------------------------------------------------------ #

    def _buduj(self) -> None:
        pad = {"padx": 8, "pady": 4}

        krok1 = ttk.LabelFrame(self, text="1. Sesja USOSweb")
        krok1.pack(fill="x", **pad)
        ttk.Button(krok1, text="Zaloguj do USOS", command=self.zaloguj).pack(
            side="left", padx=8, pady=8)
        ttk.Label(krok1, text="Otworzy okno przegladarki. Powtorz tego samego dnia, "
                              "co rejestracja - ciasteczka CAS wygasaja.",
                  foreground="#555").pack(side="left", padx=4)

        krok2 = ttk.LabelFrame(self, text="2. Strona rejestracji")
        krok2.pack(fill="x", **pad)
        self.url = tk.StringVar(value=REJESTRACJE_URL)
        ttk.Entry(krok2, textvariable=self.url).pack(
            side="left", fill="x", expand=True, padx=8, pady=8)
        ttk.Button(krok2, text="Pobierz grupy", command=self.pobierz_grupy).pack(
            side="left", padx=8)

        krok3 = ttk.LabelFrame(self, text="3. Grupy do zlapania (zaznacz)")
        krok3.pack(fill="both", expand=True, **pad)
        plotno = tk.Canvas(krok3, height=180, highlightthickness=0)
        pasek = ttk.Scrollbar(krok3, orient="vertical", command=plotno.yview)
        self.lista = ttk.Frame(plotno)
        self.lista.bind("<Configure>",
                        lambda e: plotno.configure(scrollregion=plotno.bbox("all")))
        plotno.create_window((0, 0), window=self.lista, anchor="nw")
        plotno.configure(yscrollcommand=pasek.set)
        plotno.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        pasek.pack(side="right", fill="y", pady=8)
        self._pusta_lista()

        krok4 = ttk.LabelFrame(self, text="4. Kiedy startowac")
        krok4.pack(fill="x", **pad)
        self.start = tk.StringVar(
            value=(datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d 18:00:00"))
        ttk.Entry(krok4, textvariable=self.start, width=24).pack(
            side="left", padx=8, pady=8)
        ttk.Label(krok4, text="RRRR-MM-DD GG:MM:SS (czas serwera USOS)").pack(side="left")
        ttk.Button(krok4, text="Za 30 s (do testow)",
                   command=lambda: self.start.set("+30s")).pack(side="right", padx=8)

        krok5 = ttk.Frame(self)
        krok5.pack(fill="x", **pad)
        ttk.Button(krok5, text="Zapisz config.yaml", command=self.zapisz_config).pack(
            side="left", padx=4)
        ttk.Button(krok5, text="Proba na sucho (nic nie klika)",
                   command=lambda: self.uruchom("test")).pack(side="left", padx=4)
        self.btn_start = ttk.Button(krok5, text="START REJESTRACJI",
                                    command=self.start_rejestracji)
        self.btn_start.pack(side="left", padx=4)
        self.btn_stop = ttk.Button(krok5, text="Zatrzymaj", command=self.zatrzymaj,
                                   state="disabled")
        self.btn_stop.pack(side="left", padx=4)

        self.status = tk.StringVar(value="gotowy")
        ttk.Label(self, textvariable=self.status, foreground="#0a0").pack(
            anchor="w", padx=12)

        self.log = scrolledtext.ScrolledText(self, height=16, font=("Consolas", 9),
                                             bg="#111", fg="#ddd", insertbackground="#ddd")
        self.log.pack(fill="both", expand=True, padx=8, pady=(4, 8))

    def _pusta_lista(self) -> None:
        for w in self.lista.winfo_children():
            w.destroy()
        ttk.Label(self.lista, foreground="#777",
                  text="Brak grup. Kliknij 'Pobierz grupy' przy otwartej rejestracji.\n"
                       "Poza okresem rejestracji USOS nie pokazuje zadnych grup - "
                       "to normalne.").pack(anchor="w", padx=8, pady=8)

    # ------------------------------------------------------------------ #
    # uruchamianie usos_auto.py
    # ------------------------------------------------------------------ #

    def uruchom(self, *argumenty: str, po_zakonczeniu=None) -> None:
        if self.proc and self.proc.poll() is None:
            messagebox.showwarning("Zajete", "Poczekaj, az skonczy sie biezace zadanie.")
            return
        cmd = [sys.executable, str(SKRYPT), *argumenty]
        self._pisz(f"\n$ {' '.join(cmd[1:])}\n")
        self.status.set("pracuje...")
        self.btn_stop.configure(state="normal")

        srodowisko = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=str(HERE), env=srodowisko, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1)
        except Exception as e:
            self._pisz(f"!! nie udalo sie uruchomic: {e}\n")
            self.status.set("blad")
            return
        threading.Thread(target=self._czytaj, args=(self.proc, po_zakonczeniu),
                         daemon=True).start()

    def _czytaj(self, proc: subprocess.Popen, po_zakonczeniu) -> None:
        for linia in proc.stdout:
            self.kolejka.put(linia)
        kod = proc.wait()
        self.kolejka.put(f"[zakonczone, kod {kod}]\n")
        if po_zakonczeniu:
            self.kolejka.put(("CALLBACK", po_zakonczeniu, kod))

    def _pompuj_log(self) -> None:
        while True:
            try:
                el = self.kolejka.get_nowait()
            except queue.Empty:
                break
            if isinstance(el, tuple):
                _, fn, kod = el
                fn(kod)
                continue
            self._pisz(el)
            if el.startswith("[zakonczone"):
                self.status.set("gotowy")
                self.btn_stop.configure(state="disabled")
        self.after(100, self._pompuj_log)

    def _pisz(self, tekst: str) -> None:
        self.log.insert("end", tekst)
        self.log.see("end")

    def zatrzymaj(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self._pisz("\n!! zatrzymane przez uzytkownika\n")

    # ------------------------------------------------------------------ #
    # akcje
    # ------------------------------------------------------------------ #

    def zaloguj(self) -> None:
        messagebox.showinfo(
            "Logowanie",
            "Otworzy sie okno przegladarki - zaloguj sie przez CAS UJ.\n\n"
            "Skrypt czeka na Enter w konsoli, ktorej tu nie ma, wiec po zalogowaniu "
            "po prostu ZAMKNIJ okno przegladarki. Sesja zapisze sie sama.")
        self.uruchom("login")

    def pobierz_grupy(self) -> None:
        url = self.url.get().strip()
        if not url.startswith("http"):
            messagebox.showerror("Zly adres", "Wklej adres strony rejestracji z przegladarki.")
            return
        self.uruchom("list", "--url", url, "--json-out", str(GRUPY_JSON),
                     po_zakonczeniu=lambda kod: self._pokaz_grupy())

    def _pokaz_grupy(self) -> None:
        if not GRUPY_JSON.exists():
            self._pusta_lista()
            return
        try:
            self.grupy = json.loads(GRUPY_JSON.read_text(encoding="utf-8"))
        except Exception as e:
            self._pisz(f"!! nie moge odczytac listy grup: {e}\n")
            return
        for w in self.lista.winfo_children():
            w.destroy()
        self.zaznaczone = []
        if not self.grupy:
            self._pusta_lista()
            return
        for g in self.grupy:
            var = tk.BooleanVar(value=False)
            self.zaznaczone.append(var)
            etykieta = g["text"][:150] + (" [JUZ ZAPISANY]" if g["registered"] else "")
            ttk.Checkbutton(self.lista, text=etykieta, variable=var).pack(
                anchor="w", padx=8, pady=1)

    def _zebrane_cele(self) -> list[dict]:
        return [{"name": g["name"], "url": g["url"], "contains": g["contains"]}
                for g, v in zip(self.grupy, self.zaznaczone) if v.get()]

    def zapisz_config(self) -> bool:
        cele = self._zebrane_cele()
        if not cele:
            messagebox.showerror("Brak grup", "Zaznacz przynajmniej jedna grupe.")
            return False
        dane = {}
        if CONFIG.exists():
            dane = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
        dane["start_time"] = self.start.get().strip()
        dane.setdefault("timezone", "Europe/Warsaw")
        dane["targets"] = cele
        CONFIG.write_text(yaml.safe_dump(dane, allow_unicode=True, sort_keys=False),
                          encoding="utf-8")
        self._pisz(f"\nZapisano {len(cele)} cel(ow) do {CONFIG.name}\n")
        return True

    def start_rejestracji(self) -> None:
        if not self.zapisz_config():
            return
        cele = "\n".join(f"  - {c['name']}" for c in self._zebrane_cele())
        if messagebox.askyesno(
                "Potwierdzenie",
                f"O {self.start.get()} skrypt bedzie probowal zapisac Cie na:\n\n{cele}\n\n"
                "To klika naprawde. Uruchamiamy?"):
            self.uruchom("run")

    def _wczytaj_config(self) -> None:
        if not CONFIG.exists():
            return
        try:
            dane = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
        except Exception:
            return
        if dane.get("start_time"):
            self.start.set(str(dane["start_time"]))
        cele = dane.get("targets") or []
        if cele and str(cele[0].get("url", "")).startswith("http"):
            self.url.set(cele[0]["url"])
        if GRUPY_JSON.exists():
            self._pokaz_grupy()

    def _zamknij(self) -> None:
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno("Trwa zadanie", "Przerwac i zamknac?"):
                return
            self.proc.terminate()
        self.destroy()


if __name__ == "__main__":
    if not SKRYPT.exists():
        raise SystemExit(f"Nie znaleziono {SKRYPT}")
    App().mainloop()
