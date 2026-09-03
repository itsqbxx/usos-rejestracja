# Automatyczna rejestracja na zajęcia — USOSweb UJ

Skrypt loguje się Twoją sesją do USOSweb, czeka do sekundy otwarcia rejestracji
(zsynchronizowanej z **zegarem serwera**, nie Twoim) i klika „zarejestruj” przy
wybranych grupach — wszystkie równolegle, z ponawianiem prób.

> **Zanim użyjesz:** to obchodzenie ręcznego wyścigu o miejsca. Regulaminy uczelni
> potrafią zabraniać automatów; ryzyko po Twojej stronie. Nie zjeżdżaj z
> `interval_ms` poniżej ~400 ms — zbyt agresywne odpytywanie to najprostsza droga
> do blokady konta.

## 1. Instalacja (jednorazowo)

```bash
pip install -r requirements.txt
```

```bash
python -m playwright install chromium
```

## 2. Logowanie (raz, najlepiej tego samego dnia)

```bash
python usos_auto.py login
```

Otworzy się okno przeglądarki. Zaloguj się przez CAS UJ (login + hasło + ewentualne
2FA — wpisujesz je **Ty**, skrypt nigdy nie dotyka Twoich danych). Gdy zobaczysz
USOSweb, wróć do terminala i naciśnij Enter. Sesja zapisuje się w dwóch miejscach:
profil przeglądarki `.usos-profile/` oraz `.usos-session.json` z ciasteczkami
(te sesyjne nie zawsze przeżywają zamknięcie okna, więc dokładamy je przy starcie).

Oba te pliki to w praktyce klucz do Twojego konta USOS — `.gitignore` trzyma je poza
repozytorium i tak ma zostać. Sesja CAS i tak wygasa, więc **`login` powtórz tego
samego dnia co rejestrację**, najlepiej krótko przed nią.

## 3. Znajdź swoje grupy

Wejdź w USOSweb w **Dla studentów → Rejestracja → Rejestracja bezpośrednia do grup**,
otwórz interesującą Cię rejestrację i skopiuj adres z paska przeglądarki. Potem:

```bash
python usos_auto.py list --url "TU_WKLEJ_ADRES"
```

Skrypt wypisze wszystkie wiersze grup, które mają przycisk rejestracji. Z każdego
wybierz fragmenty tekstu jednoznacznie identyfikujące grupę (kod przedmiotu, numer
grupy, godzinę, nazwisko prowadzącego) i wpisz je do `config.yaml` jako `contains`.

## 4. Uzupełnij `config.yaml`

```yaml
start_time: "2026-09-15 18:00:00"   # moment otwarcia rejestracji
targets:
  - name: "Analiza — ćw. gr. 3"
    url: "https://usosweb.uj.edu.pl/kontroler.php?_action=home/rejestracje/rejestracjaBezposrednia&rej_kod=..."
    contains: ["WMI.IM-AM1-C", '/grupa nr 3\b/']
```

Wszystkie warunki z `contains` muszą wystąpić w tym samym wierszu tabeli
(porównanie ignoruje wielkość liter). Jeśli dopasowań jest kilka, skrypt bierze
pierwsze i głośno to zgłasza w logu — dodaj wtedy bardziej unikalny fragment.

Wpis w formie `/wzorzec/` to **wyrażenie regularne**. Przydaje się, gdy krótszy tekst
jest podciągiem dłuższego: samo `"grupa nr 3"` złapie też `grupa nr 30`, a `'/grupa nr 3\b/'`
już nie. **Regexy zapisuj w apostrofach** — w cudzysłowie YAML zamienia `\b`, `\d`, `\s`
na znaki sterujące i wzorzec przestaje działać (skrypt to wykrywa i ostrzega).

## 5. Test na sucho

```bash
python usos_auto.py test
```

To samo co bieg właściwy, ale **bez klikania** — skrypt tylko sprawdza, czy znajduje
wiersz i przycisk. Ustaw wtedy `start_time: "+10s"`, żeby nie czekać. Zrób to
koniecznie przed prawdziwą rejestracją.

## 6. Bieg właściwy

```bash
python usos_auto.py run
```

Odpal 5–10 minut wcześniej i **zostaw komputer włączonego** (bez usypiania). Skrypt:

1. mierzy różnicę między Twoim zegarem a zegarem serwera USOS,
2. co `keepalive_s` odświeża strony, żeby sesja nie wygasła (i głośno krzyczy w logu, jeśli wygaśnie),
3. o `start_time` minus `lead_ms` rusza z próbami — każdy cel w osobnej karcie, równolegle,
4. klika „zarejestruj”, obsługuje ekran potwierdzenia i weryfikuje wynik (po odświeżeniu przy grupie musi pojawić się „wyrejestruj”),
5. ponawia aż do sukcesu lub `max_seconds`,
6. drukuje podsumowanie i zapisuje `raport-*.json`.

## Rozwiązywanie problemów

| Objaw | Co zrobić |
|---|---|
| „nie znaleziono wiersza” | Zbyt wąskie/literówkowe `contains`. Uruchom `list` i skopiuj tekst dokładnie (polskie znaki mają znaczenie). |
| „N dopasowań”, N > 1 | Dodaj kolejny, bardziej unikalny fragment do `contains`. |
| „wzorzec … zawiera znak sterujący” | Regex w cudzysłowie — przepisz go na apostrofy. |
| „brak sesji / sesja wygasła” | Powtórz `python usos_auto.py login` tuż przed rejestracją. |
| „brak przycisku rejestracji” | Rejestracja jeszcze nieotwarta albo brak miejsc — skrypt i tak ponawia próby. |
| Zupełnie inny wygląd strony | Podaj własny `selector` (CSS/XPath do `<tr>` grupy) zamiast `contains`. |

## Ograniczenia

- Obsługuje **rejestrację bezpośrednią do grup** (klikany koszyk przy grupie). Rejestracja
  żetonowa dwuetapowa czy giełda grup mogą wymagać innych selektorów — wtedy użyj `selector`.
- Dokładność synchronizacji zegara to ~0,5 s (nagłówek HTTP `Date` ma rozdzielczość sekundy);
  dlatego skrypt startuje odrobinę wcześniej i ponawia próby.
- Nie omija captcha ani kolejki USOS — jeśli serwer wystawi kolejkę, po prostu czeka i próbuje dalej.
