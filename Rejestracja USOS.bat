@echo off
title USOS rejestracja - NIE ZAMYKAJ tego okna
cd /d "%~dp0"
echo.
echo   Okno programu otworzy sie za chwile.
echo   To czarne okno musi zostac otwarte, dopoki program dziala (mozesz je zminimalizowac).
echo.
python.exe usos_gui.py
if errorlevel 1 (
  echo.
  echo   Program zakonczyl sie bledem - skopiuj tekst powyzej i wyslij go.
  pause
)
