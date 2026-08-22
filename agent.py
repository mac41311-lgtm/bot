#!/usr/bin/env python3
import xml.etree.ElementTree as ET
# -*- coding: utf-8 -*-

"""
AEL-MINI AUTONOMOUS AGENT v21

ARCHITEKTURA:

                    USER
                     |
                     v
                 DEEPSEEK
                     |
        +------------+------------+
        |            |            |
      MAIN        PLANNER     RESEARCHER
        |            |            |
        +------- CRITIC ----------+
                     |
                  BROWSER
                     |
                     v
                 TASK QUEUE
                     |
                     v
              GEMINI WORKER
                     |
          +----------+----------+
          |          |          |
       CHROME     ANDROID     SHELL
          |          |          |
          +----------+----------+
                     |
                     v
                  RESULT
                     |
                     v
                   MAIN

Gemini:
    Interactions API
    previous_interaction_id
    prawidłowe function_result/call_id

Chrome:
    tylko istniejące karty
    BRAK /json/new

DeepSeek:
    5 ról utrzymywanych w jednym procesie
    pamięć zapisywana na dysku

"""

import atexit
import os
import sys
import json
import time
import re
import shutil
import subprocess
import traceback
import uuid
from pathlib import Path
from web_search import web_search
from datetime import datetime



# ============================================================
# AGENT WAKE LOCK
# ============================================================

_WAKE_LOCK_ACTIVE = False


def agent_wake_lock():
    """
    Utrzymuje urządzenie aktywne podczas pracy agenta.

    Nie zatrzymuje działania Termuxa ani Androida.
    Jeżeli termux-wake-lock nie istnieje, agent może
    normalnie pracować dalej.
    """
    global _WAKE_LOCK_ACTIVE

    if _WAKE_LOCK_ACTIVE:
        return {
            "ok": True,
            "status": "ALREADY_ACTIVE"
        }

    try:
        import shutil
        import subprocess

        cmd = shutil.which("termux-wake-lock")

        if not cmd:
            log(
                "ANDROID",
                "termux-wake-lock niedostępny — pomijam wake-lock"
            )

            return {
                "ok": False,
                "status": "NOT_AVAILABLE",
                "error": "Nie znaleziono termux-wake-lock"
            }

        result = subprocess.run(
            [cmd],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode == 0:
            _WAKE_LOCK_ACTIVE = True

            log(
                "ANDROID",
                "Wake-lock AKTYWNY — ekran nie powinien wygasać"
            )

            return {
                "ok": True,
                "status": "ACTIVE"
            }

        return {
            "ok": False,
            "status": "ERROR",
            "returncode": result.returncode,
            "stderr": result.stderr[-1000:]
        }

    except Exception as e:

        return {
            "ok": False,
            "status": "ERROR",
            "error": str(e)
        }


def agent_wake_unlock():
    """
    Zwolnienie wake-lock po zakończeniu agenta.
    """

    global _WAKE_LOCK_ACTIVE

    if not _WAKE_LOCK_ACTIVE:
        return {
            "ok": True,
            "status": "NOT_ACTIVE"
        }

    try:
        import shutil
        import subprocess

        cmd = shutil.which("termux-wake-unlock")

        if not cmd:
            _WAKE_LOCK_ACTIVE = False

            return {
                "ok": False,
                "status": "NOT_AVAILABLE"
            }

        result = subprocess.run(
            [cmd],
            capture_output=True,
            text=True,
            timeout=10
        )

        _WAKE_LOCK_ACTIVE = False

        log(
            "ANDROID",
            "Wake-lock ZWOLNIONY"
        )

        return {
            "ok": result.returncode == 0,
            "status": "RELEASED",
            "returncode": result.returncode
        }

    except Exception as e:

        _WAKE_LOCK_ACTIVE = False

        return {
            "ok": False,
            "status": "ERROR",
            "error": str(e)
        }




atexit.register(agent_wake_unlock)

# ============================================================
# IMPORTY
# ============================================================

try:
    import opendeep
except Exception as e:
    print("[KRYTYCZNY] Brak opendeep")
    print(e)
    print("pip install -U opendeep")
    sys.exit(1)


try:
    import uiautomator2 as u2
except Exception as e:
    print("[KRYTYCZNY] Brak uiautomator2")
    print(e)
    print("pip install -U uiautomator2")
    sys.exit(1)


try:
    import requests
except Exception as e:
    print("[KRYTYCZNY] Brak requests")
    print(e)
    sys.exit(1)


try:
    import websocket
except Exception as e:
    print("[KRYTYCZNY] Brak websocket-client")
    print(e)
    print("pip install -U websocket-client")
    sys.exit(1)


try:
    from google import genai
    from google.genai import types
    GEMINI_LIBRARY_OK = True
except Exception as e:
    GEMINI_LIBRARY_OK = False
    print("[GEMINI] Brak google-genai:", e)


# ============================================================
# ŚCIEŻKI
# ============================================================

HOME = Path.home()
AGENT_DIR = HOME / "agent"

TOKEN_FILE = HOME / "api_token.txt"

GEMINI_KEY_FILE = AGENT_DIR / "gemini_api_key.txt"
GEMINI_KEYS_DIR = AGENT_DIR / "gemini_keys"

STATE_DIR = AGENT_DIR / "state"
QUEUE_DIR = AGENT_DIR / "queue"
RESULTS_DIR = AGENT_DIR / "results"
MEMORY_DIR = AGENT_DIR / "memory"

MAIN_STATE = STATE_DIR / "main.json"
PLANNER_STATE = STATE_DIR / "planner.json"
RESEARCHER_STATE = STATE_DIR / "researcher.json"
CRITIC_STATE = STATE_DIR / "critic.json"
BROWSER_STATE = STATE_DIR / "browser.json"

GEMINI_STATE_FILE = STATE_DIR / "gemini.json"

LAST_RESULT_FILE = AGENT_DIR / "last_result.json"


for d in [
    AGENT_DIR,
    STATE_DIR,
    QUEUE_DIR,
    RESULTS_DIR,
    MEMORY_DIR,
    GEMINI_KEYS_DIR,
]:
    d.mkdir(
        parents=True,
        exist_ok=True
    )


# ============================================================
# KONFIGURACJA
# ============================================================

DEEPSEEK_MODEL = os.environ.get(
    "DEEPSEEK_MODEL",
    "deepseek-reasoner"
)

GEMINI_MODEL = os.environ.get(
    "GEMINI_MODEL",
    "gemini-3.5-flash-lite"
)

MAX_STEPS = int(
    os.environ.get(
        "AGENT_MAX_STEPS",
        "40"
    )
)

GEMINI_MAX_TOOL_CALLS = int(
    os.environ.get(
        "GEMINI_MAX_TOOL_CALLS",
        "25"
    )
)

# Ile razy Gemini może w JEDNYM TASKu poprosić o podpowiedź przez
# ask_deepseek(), zanim dalsze prośby zostaną ucięte (patrz
# gemini_execute_task()). Celowo niskie — to awaryjna konsultacja
# w trakcie zadania, nie zamiennik zwykłego przepływu MAIN -> team,
# którego częstotliwość dopiero co ograniczyliśmy (v8).
ASK_DEEPSEEK_MAX_PER_TASK = int(
    os.environ.get(
        "ASK_DEEPSEEK_MAX_PER_TASK",
        "2"
    )
)

COMMAND_TIMEOUT = int(
    os.environ.get(
        "COMMAND_TIMEOUT",
        "120"
    )
)

CDP_HOST = os.environ.get(
    "CDP_HOST",
    "127.0.0.1"
)

CDP_PORT = int(
    os.environ.get(
        "CDP_PORT",
        "9222"
    )
)

ANDROID_LIMIT = 8000
CHROME_TEXT_LIMIT = 10000
RESULT_LIMIT = 7000

# Po ilu identycznych zadaniach (na poziomie decyzji MAIN)
# wymuszamy zmianę strategii.
REPEAT_LIMIT = 3

# Po ilu identycznych porażkach TEGO SAMEGO narzędzia z TYMI
# SAMYMI argumentami wywołujemy automatycznie CODE_REVIEWER/
# CODE_FIXER zamiast czekać, aż MAIN sam na to wpadnie.
# Celowo niższe niż REPEAT_LIMIT — to sygnał bardziej precyzyjny
# (dotyczy konkretnego wywołania, nie tylko podobnej treści TASK-u).
TOOL_REPEAT_LIMIT = 2

# ============================================================
# NOWE ŚCIEŻKI / PLIKI (naprawa + weryfikacja)
# ============================================================

# Ustrukturyzowany log (JSON Lines) — obok czytelnego dla
# człowieka log(). Jedna linia = jedno zdarzenie.
EVENTS_LOG_FILE = AGENT_DIR / "agent_events.jsonl"

# Trwały licznik powtarzających się porażek tego samego
# (narzędzie, argumenty) — przeżywa restart agenta.
TOOL_ATTEMPTS_FILE = STATE_DIR / "tool_attempts.json"

# Zapamiętany, niedokończony cel — pozwala wznowić sesję po
# Ctrl+C zamiast zaczynać rozmowę od zera.
GOAL_FILE = AGENT_DIR / "current_goal.txt"

# Ostatnio używany adres bezprzewodowego ADB (host:port). Android
# losuje NOWY port debugowania bezprzewodowego przy każdym jego
# ponownym włączeniu / restarcie WiFi na telefonie — stary
# "adb connect ip:port" przestaje wtedy działać. Ten plik jest
# zapisywany przez prompt_adb_target() przy starcie programu i
# odczytywany automatycznie przez find_adb(), gdy `adb devices`
# nic nie zwróci.
ADB_CONNECT_FILE = AGENT_DIR / "adb_connect.txt"

# Lista top-level katalogów/plików pod $HOME, w których Gemini
# faktycznie coś zapisał (termux_mkdir/termux_write_file/
# termux_patch_file/write_engineer_code_to) — jedyny sposób, żeby
# przy nowym celu zaproponować usunięcie WYGENEROWANYCH PLIKÓW
# PROJEKTU (kod gry, build.gradle itd.), skoro nazwa katalogu
# projektu zmienia się za każdym razem (OpenWorld3D, Game3D,
# 3dgame...). Osobna, dodatkowa lista od danych samego agenta
# (kolejka/wyniki) i NIGDY nie zawiera AGENT_DIR ani kluczy API —
# patrz _track_project_path()/maybe_clear_generated_project_files().
PROJECT_DIRS_FILE = AGENT_DIR / "project_dirs.json"

# Gdzie run_agent() spodziewa się finalnego APK i FINAL_OK.txt —
# używane wyłącznie do FIZYCZNEJ weryfikacji przed przyjęciem DONE.
APK_OUTPUT_DIR = AGENT_DIR / "apk_output"
FINAL_OK_TOKEN = "ANDROID_GAME_BUILD_OK"

APK_OUTPUT_DIR.mkdir(
    parents=True,
    exist_ok=True
)

# Katalog na NOWE narzędzia projektowane przez DeepSeek i zapisywane
# przez Gemini jako osobne pliki .py — NIE modyfikują agent.py.
# Każdy plik jest wykrywany, walidowany i ładowany automatycznie
# (patrz load_custom_tools()), bez restartu agenta. Zepsuty plik
# jest po prostu pomijany z jasnym komunikatem — nigdy nie wywraca
# reszty agenta.
CUSTOM_TOOLS_DIR = AGENT_DIR / "custom_tools"

CUSTOM_TOOLS_DIR.mkdir(
    parents=True,
    exist_ok=True
)

# Wzorce zadań, które użytkownik jawnie zabronił (np. pobieranie
# gotowej gry zamiast tworzenia jej od zera przez Gemini).
# Dopisz tu kolejne wzorce, jeśli MAIN znajdzie nowy sposób na
# obejście wymagania "wszystko tworzy Gemini w Termux".
FORBIDDEN_TASK_PATTERNS = (
    "standoff",
    "gotowy apk",
    "gotowej gry",
    "gotowa gra",
    "pobierz apk",
    "pobierz gotow",
    "download apk",
    "download a ready",
    "ready-made apk",
    "ready made apk",
    "pobierz plik apk",
    "ściągnij apk",
    "ściągnij gotow",
)

# Komendy pasujące do tych fraz są z góry uznawane za
# długotrwałe i automatycznie idą w tło (termux_run_background)
# zamiast czekać na COMMAND_TIMEOUT.
LONG_RUNNING_HINTS = (
    "gradle",
    "gradlew",
    "npm install",
    "npm ci",
    "npm run build",
    "yarn install",
    "yarn build",
    "pip install",
    "pip3 install",
    "apt install",
    "apt upgrade",
    "apt-get install",
    "apt-get upgrade",
    "apt update",
    "git clone",
    "curl -o",
    "curl -O",
    "wget ",
    "unzip ",
    "tar -x",
    "make ",
    "cmake ",
    "javac ",
    "./configure",
    "assembleDebug",
    "assembleRelease",
)


# ============================================================
# GLOBALNE
# ============================================================

android_device = None
adb_target = None

deepseek_model = None

sessions = {}

gemini_clients = {}

gemini_disabled = False

# trwała interakcja wykonawcy
gemini_interaction_id = None

# blokada Gemini
gemini_lock = None


# ============================================================
# POMOCNICZE
# ============================================================

def now():
    return datetime.now().strftime("%H:%M:%S")


def log(tag, message):
    print(
        f"[{now()}] [{tag}] {message}",
        flush=True
    )


def short(value, limit=1000):

    if value is None:
        return ""

    value = str(value)

    if len(value) <= limit:
        return value

    return (
        value[:limit]
        + "\n...[skrócono]..."
    )


def read_text(path):

    try:
        if not path.exists():
            return ""

        return path.read_text(
            encoding="utf-8",
            errors="ignore"
        )

    except Exception:
        return ""


def write_text(path, value):

    try:
        path.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        path.write_text(
            str(value),
            encoding="utf-8"
        )

        return True

    except Exception as e:
        log("FILE", str(e))
        return False


def read_json(path, default=None):

    try:
        if not path.exists():
            return default

        return json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

    except Exception:
        return default


def write_json(path, value):

    try:
        path.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        path.write_text(
            json.dumps(
                value,
                ensure_ascii=False,
                indent=2
            ),
            encoding="utf-8"
        )

        return True

    except Exception as e:
        log("FILE", str(e))
        return False


# Nazwy top-level wpisów pod $HOME, które NIGDY nie trafiają na
# listę "wygenerowanych plików projektu" do ewentualnego kasowania
# — własny katalog agenta (klucze API, kod, kolejka) i typowe
# ukryte pliki konfiguracyjne Termuksa.
_PROJECT_TRACKING_EXCLUDED_NAMES = {
    AGENT_DIR.name,
    TOKEN_FILE.name,
    ".termux",
    ".termux_run_command_scripts",
    ".bashrc",
    ".bash_profile",
    ".profile",
    ".shortcuts",
}


def _track_project_path(path):
    """
    Zapisuje top-level katalog/plik pod $HOME, w którym Gemini
    właśnie coś zapisał, do PROJECT_DIRS_FILE — jedyny sposób,
    żeby przy kolejnym NOWYM celu wiedzieć, co właściwie zostało
    wygenerowane w POPRZEDNIEJ sesji (nazwa katalogu projektu jest
    inna za każdym razem, agent jej nie narzuca).

    Woływane z termux_mkdir/termux_write_file/termux_patch_file
    oraz z obsługi write_engineer_code_to w run_agent(). Nigdy nie
    zgłasza wyjątku wyżej — to czysto pomocnicze śledzenie, błąd
    tutaj nie może wywrócić właściwej operacji na pliku.
    """

    try:
        target = Path(str(path)).expanduser().resolve()
        home = Path.home().resolve()

        if home not in target.parents and target != home:
            # Ścieżka spoza $HOME (np. /data/data/com.termux/...
            # poza katalogiem domowym) — nic tu nie śledzimy, to
            # nie jest "projekt użytkownika" w sensie, w jakim tu
            # chodzi.
            return

        relative = target.relative_to(home)

        if not relative.parts:
            return

        top_level_name = relative.parts[0]

        if top_level_name in _PROJECT_TRACKING_EXCLUDED_NAMES:
            return

        if top_level_name.startswith("."):
            return

        top_level_path = str(home / top_level_name)

        tracked = read_json(PROJECT_DIRS_FILE, [])

        if not isinstance(tracked, list):
            tracked = []

        if top_level_path not in tracked:
            tracked.append(top_level_path)
            write_json(PROJECT_DIRS_FILE, tracked)

    except Exception:
        pass


def append_memory(path, timestamp, content):
    """
    Dopisuje wpis do pliku pamięci (Markdown).

    Używane m.in. przez review_code() do zapisu analiz
    CODE_REVIEWERA i CODE_FIXERA. Wcześniej ta funkcja była
    wywoływana, ale nigdzie w pliku nie istniała — pierwsze
    realne wywołanie review_code() kończyłoby się NameError
    (po cichu połkniętym przez szerokie except Exception).
    """

    try:
        path.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        entry = (
            "\n\n## "
            + str(timestamp)
            + "\n\n"
            + str(content)
            + "\n"
        )

        with open(path, "a", encoding="utf-8") as f:
            f.write(entry)

        return True

    except Exception as e:
        log("FILE", "append_memory: " + str(e))
        return False


def log_event(event_type, data=None):
    """
    Dopisuje jedno zdarzenie (jeden obiekt JSON na linię) do
    AGENT_DIR/agent_events.jsonl.

    To jest ustrukturyzowany odpowiednik log() — log() jest dla
    człowieka czytającego terminal, log_event() jest dla
    CODE_REVIEWERA i dla ciebie, gdybyś chciał później
    przeanalizować przebieg sesji skryptem zamiast czytać
    dekoracyjne logi tekstowe.
    """

    try:
        entry = {
            "ts": datetime.now().isoformat(),
            "event": event_type,
        }

        entry.update(data or {})

        EVENTS_LOG_FILE.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        with open(EVENTS_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    entry,
                    ensure_ascii=False,
                    default=str
                )
                + "\n"
            )

    except Exception:
        # Logowanie nigdy nie może wywrócić właściwej operacji.
        pass


# ============================================================
# BANNER
# ============================================================

def banner():

    print()
    print("=" * 72)
    print("             AEL-MINI AUTONOMOUS AGENT v21")
    print("=" * 72)
    print(" DeepSeek/OpenDeep : GŁÓWNY MÓZG")
    print(" DeepSeek roles    : MAIN / PLANNER / RESEARCHER / CRITIC / BROWSER")
    print(" Gemini            : WYKONAWCA")
    print(" Gemini model      : " + GEMINI_MODEL)
    print(" Chrome/CDP        : ISTNIEJĄCE KARTY")
    print(" Android/u2        : NARZĘDZIE")
    print(" Termux/Shell      : NARZĘDZIE")
    print("=" * 72)
    print(" KOMUNIKACJA:")
    print(" DeepSeek team -> TASK -> Gemini")
    print(" Gemini -> tools -> result -> MAIN")
    print("=" * 72)
    print()


# ============================================================
# DEEPSEEK
# ============================================================

def init_deepseek():

    global deepseek_model

    token = read_text(
        TOKEN_FILE
    ).strip()

    if not token:
        print(
            "[KRYTYCZNY] Brak tokena:"
        )
        print(TOKEN_FILE)
        return False

    try:

        opendeep.configure(
            api_key=token
        )

        deepseek_model = (
            opendeep.GenerativeModel(
                DEEPSEEK_MODEL
            )
        )

        log(
            "DEEPSEEK",
            "OpenDeep OK — "
            + DEEPSEEK_MODEL
        )

        return True

    except Exception as e:

        log(
            "DEEPSEEK",
            "ERROR: " + str(e)
        )

        return False


# ============================================================
# PROMPTY
# ============================================================

MAIN_PROMPT = r"""
Jesteś MAIN — głównym mózgiem autonomicznego agenta.

Ty podejmujesz decyzje.

Masz zespół:
PLANNER
RESEARCHER
CRITIC
BROWSER

Gemini jest wykonawcą.

Twoim zadaniem jest:
- analizować cel,
- konsultować się z zespołem,
- tworzyć logiczne zadania dla Gemini,
- analizować raporty,
- zmieniać strategię,
- nie powtarzać bez końca tej samej czynności.

Nie twórz zadania typu:
"kliknij X".

Twórz całe bloki:
"Sprawdź stronę X, przejdź przez cały proces,
wykonaj konieczne działania i potwierdź wynik."

Jeżeli Gemini nie może wykonać zadania:
przeanalizuj raport i wybierz inną drogę.

Jeżeli nie ma postępu:
zmień strategię.

Nie zakładaj sukcesu bez dowodu.

Decyzja musi być jednym z:

TASK:
{
  "type": "TASK",
  "reason": "...",
  "task": "...",
  "success_condition": "...",
  "priority": "high",
  "write_engineer_code_to": "WYMAGANE, gdy dotyczy — patrz niżej"
}

============================================================
OBOWIĄZKOWE: write_engineer_code_to
============================================================

Uwaga o nazwie: rola "ANDROID_GAME_ENGINEER" nazywa się tak
historycznie, ale w praktyce jest to Twój specjalista TECHNICZNY
od budowy DOWOLNEGO projektu — gry, aplikacji Android, skryptu,
narzędzia CLI, automatyzacji, strony itd. — dostosowuje swoje
rady do aktualnego CELU, nie tylko do gier. Poniższe zasady
dotyczą jej kodu niezależnie od rodzaju projektu.

To NIE jest opcja do rozważenia — to TWÓJ OBOWIĄZEK w konkretnej
sytuacji: jeżeli ANDROID_GAME_ENGINEER podał w swojej odpowiedzi
gotowy blok kodu (sekcja "POLECENIE / KOD:", wewnątrz ```...```),
A Twój TASK dotyczy ZAPISANIA tego kodu do pliku — MASZ UŻYĆ pola
"write_engineer_code_to" z docelową ścieżką (np.
"/data/data/com.termux/files/home/game3d/game.py"). Python zapisze
kod do pliku SAM, zanim TASK w ogóle trafi do Gemini — zero zużycia
Gemini na przepisywanie.

ZABRONIONE w tej sytuacji: opisywanie kodu słownie w "task" i
liczenie, że Gemini sam go napisze/odtworzy przez termux_write_file.
To marnuje limit Gemini (którego brakuje) i wprowadza błędy
przepisywania — dokładnie to, czego ten mechanizm ma unikać. Jeżeli
zauważysz, że ostatni TASK kazał Gemini samodzielnie napisać duży
plik, mimo że ANDROID_GAME_ENGINEER miał gotowy kod — to był błąd,
napraw podejście w następnym TASKu.

Kiedy używasz write_engineer_code_to, pole "task" ma dotyczyć
WYŁĄCZNIE uruchomienia i przetestowania już zapisanego pliku (np.
"Uruchom ~/game3d/game.py i sprawdź czy proces nie kończy się
błędem"), NIE jego tworzenia — plik już tam będzie, zanim Gemini
zacznie pracować.

Jeżeli w ostatniej odpowiedzi ANDROID_GAME_ENGINEER NIE MA bloku
kodu (```...```), pole zostanie odrzucone z jasnym błędem — nie
zgaduj, poproś ANDROID_GAME_ENGINEER o konkretny kod albo zrób
zwykły TASK bez tego pola (dozwolone tylko gdy naprawdę nie ma
gotowego kodu do zapisania).

============================================================
UWAGA — write_engineer_code_to NADPISUJE CAŁY PLIK
============================================================

write_engineer_code_to zastępuje CAŁĄ zawartość pliku blokiem kodu
ANDROID_GAME_ENGINEER. To bezpieczne TYLKO gdy ten blok to PEŁNA,
kompletna zawartość pliku (np. pierwszy zapis nowego pliku, albo
ANDROID_GAME_ENGINEER świadomie podał cały plik od nowa).

NIE UŻYWAJ write_engineer_code_to, jeżeli ANDROID_GAME_ENGINEER
podał tylko FRAGMENT/POPRAWKĘ istniejącego pliku (np. "zmień tę
jedną funkcję") — nadpisałbyś resztę pliku tym fragmentem i
zniszczył wszystko inne. Do poprawek fragmentu istniejącego pliku
zrób zamiast tego zwykły TASK instruujący Gemini, żeby użyło
termux_patch_file (search/replace) — poproś Gemini, żeby najpierw
odczytało plik (termux_read_file), znalazło dokładny fragment do
zmiany, i podmieniło go przez termux_patch_file. To jest właściwe
narzędzie do poprawek fragmentów, write_engineer_code_to jest do
zapisu całych plików.
============================================================

DONE:
{
  "type": "DONE",
  "reason": "..."
}

FAILED:
{
  "type": "FAILED",
  "reason": "..."
}

Zwracaj tylko JSON.


============================================================
GEMINI JEST WYKONAWCĄ SYSTEMOWYM
============================================================

MAIN jest mózgiem.
Gemini jest wykonawcą.

Gemini posiada:

TERMUX:
- termux_mkdir
- termux_ls
- termux_write_file
- termux_read_file
- termux_run
- termux_run_background
- termux_processes
- termux_check_process
- termux_stop_process
- termux_start_second_session (otwiera drugą sesję Termuksa — PUSTĄ)

ANDROID (działa na CAŁYM ekranie systemu, nie tylko w oknie
Termuksa — Gemini widzi i obsługuje dowolną aplikację na
urządzeniu, Termux jest tylko jedną z wielu):
- android_state
- android_click
- android_click_resource (klik po resource-id — dokładniejszy niż tekst)
- android_tap
- android_type
- android_press
- android_swipe (gest przesunięcia)
- android_screenshot (prawdziwy zrzut ekranu PNG, do fizycznego dowodu)
- android_launch_app (otwórz DOWOLNĄ zainstalowaną aplikację po
  nazwie pakietu — np. zbudowaną i zainstalowaną grę, żeby ją
  faktycznie zobaczyć na ekranie, a nie tylko sprawdzić plik .apk)
- android_run_in_new_window (uruchom komendę w NOWYM, widocznym
  oknie Termuksa — konkretna komenda, nie puste okno jak
  termux_start_second_session; do procesów, które mają być
  widoczne osobno od głównego logu, np. serwer albo długi build)

CHROME:
- chrome_tabs
- chrome_inspect
- chrome_open
- chrome_click
- chrome_type

SHELL:
- shell

Jeżeli zadanie wymaga:
- katalogu,
- pliku,
- kodu,
- programu,
- instalacji,
- serwera,
- gry,
- procesu,
- Termuxa,
- Androida,
- Chrome,

MAIN MUSI przekazać wykonanie do Gemini.

MAIN NIE MOŻE zwracać FAILED tylko dlatego,
że sam nie posiada terminala.

Gemini ma wykonać cały logiczny blok zadania,
a następnie sprawdzić rezultat.

============================================================
ZAKAZ FAŁSZYWEGO FAILED
============================================================

Nie zwracaj FAILED z powodów:

- brak terminala,
- brak execute_command,
- brak shell,
- brak dostępu do plików,
- brak możliwości utworzenia katalogu,
- brak możliwości zapisania kodu,
- brak możliwości uruchomienia programu,

jeżeli działanie może wykonać Gemini.
============================================================

============================================================
DONE JEST FIZYCZNIE WERYFIKOWANE
============================================================

Zanim zwrócisz DONE, agent SAM sprawdzi na dysku i przez ADB:
- ZAWSZE, niezależnie od rodzaju celu: czy istnieje FINAL_OK.txt
  z dokładną treścią ustaloną z użytkownikiem (to Twój dowód, że
  wykonawca sam potwierdził zakończenie — zleć to Gemini przed
  DONE, niezależnie czy budujecie grę, skrypt czy stronę),
- TYLKO gdy cel faktycznie dotyczy zbudowania apki/gry Android
  (agent sam to rozpozna z treści celu): czy w katalogu
  wyjściowym jest prawdziwy, poprawny plik .apk (nie pusty, nie
  uszkodzony) — dla innych rodzajów celu (skrypt, narzędzie CLI,
  strona, automatyzacja) ten warunek NIE jest wymagany,
- informacyjnie, jeśli dotyczy: czy pakiet jest zainstalowany.

Jeżeli którykolwiek z WYMAGANYCH dla TEGO celu dowodów nie
przejdzie, TWOJE DONE ZOSTANIE ODRZUCONE i zamienione z powrotem
na informację o tym, czego brakuje. Nie zgłaszaj DONE na
podstawie samej deklaracji Gemini "zrobione" — poczekaj na twarde
dowody właściwe dla rodzaju celu.

============================================================
MOŻESZ ZLECIĆ GEMINI DODANIE NOWEGO NARZĘDZIA
============================================================

Jeżeli istniejące narzędzia (Termux/Android/Chrome/Shell) nie
wystarczają do jakiejś POWTARZALNEJ czynności — zamiast wymyślać
to za każdym razem od nowa przez surowe komendy shell, możesz
zlecić Gemini UTWORZENIE NOWEGO NARZĘDZIA jako osobnego pliku.
Nie modyfikuje to agent.py i nie wymaga restartu — nowe narzędzie
pojawi się automatycznie w Twojej następnej konsultacji.

Zleć TASK, w którym Gemini zapisze (termux_write_file) plik pod
ścieżką ~/agent/custom_tools/<nazwa>.py w DOKŁADNIE takim
kontrakcie:

TOOL_NAME = "nazwa_narzedzia"          # str
TOOL_DESCRIPTION = "Co robi."          # str
TOOL_PARAMETERS = {                    # JSON Schema
    "type": "object",
    "properties": {"x": {"type": "string"}},
    "required": ["x"]
}

def run(x):
    return {"ok": True, ...}

Zasady:
- Plik musi być samowystarczalny (własne importy na górze).
- run() musi zwracać dict z kluczem "ok".
- Błąd składni, brak wymaganych atrybutów albo kolizja nazwy z
  istniejącym narzędziem = plik jest po cichu odrzucany (agent
  loguje dokładny powód) — poproś Gemini o poprawki, jeśli tak
  się stanie.
- Używaj tego do rzeczy, które będą wywoływane WIELOKROTNIE z
  różnymi argumentami (np. "sprawdź rozmiar i typ pliku",
  "policz linie kodu w katalogu") — nie do jednorazowych operacji,
  te po prostu zleć przez zwykły shell.

============================================================
ZABRONIONE ZADANIA
============================================================

Zadania próbujące pobrać/skopiować gotowe rozwiązanie zamiast
zbudować je samodzielnie (np. gotową grę, gotowy APK typu
Standoff 2, gotowy cudzy projekt jako całość) są automatycznie
blokowane, zanim trafią do Gemini — CHYBA że użytkownik w CELU
wyraźnie poprosił o zainstalowanie/użycie konkretnego istniejącego
narzędzia (wtedy to nie jest obchodzenie zakazu, tylko zgodność z
celem). Domyślnie: cel ma powstać od zera w Termuxie/Androidzie za
pośrednictwem Gemini, niezależnie czy to gra, aplikacja, skrypt
czy inne narzędzie. Nie próbuj obchodzić tego zakazu innym
sformułowaniem tego samego pomysłu.
============================================================
"""


PLANNER_PROMPT = """
Jesteś PLANNEREM w uniwersalnym autonomicznym agencie, który
realizuje DOWOLNY cel techniczny zlecony przez użytkownika: może
to być gra Android, zwykła aplikacja Android, skrypt Pythona,
narzędzie CLI, automatyzacja, strona/serwer, scraper, cokolwiek
da się zbudować i uruchomić przez Termux, ADB/Android albo
Chrome. NIE zakładaj z góry, że cel dotyczy gry — rozpoznaj to
z treści CELU, który dostajesz w każdej wiadomości.

Dostajesz: cel, ostatni wynik, stan systemu.

TWOJE ZADANIE: wybrać JEDEN konkretny, realistyczny następny krok.

ZASADY:
- Nie planuj więcej niż 3 kroki naprzód.
- Każdy krok ma być zrozumiały dla Gemini jako komenda Termux lub
  konkretny plik do utworzenia.
- Jeżeli poprzedni krok zakończył się timeoutem — zaproponuj
  podejście krokami (np. najpierw setup, potem build, potem check).
- Nie powtarzaj tego samego kroku po raz trzeci.
- Nie sugeruj pobrania/skopiowania gotowego rozwiązania (gotowej
  gry, gotowej apki, gotowego cudzego projektu) zamiast budowania
  go samodzielnie — chyba że użytkownik w CELU wyraźnie o to
  poprosił (np. "zainstaluj istniejące narzędzie X").

Format odpowiedzi:

AKTUALNY STAN:
(jeden akapit — co faktycznie istnieje na urządzeniu)

PLAN (max 3 kroki):
1. ...
2. ...
3. ...

NASTĘPNY KROK (konkretne polecenie/plik):
...

WARUNEK SUKCESU TEGO KROKU:
(jak Gemini ma sprawdzić, że krok się udał)
"""


RESEARCHER_PROMPT = """
Jesteś RESEARCHEREM w uniwersalnym agencie działającym w
Termux/Android — cel bieżącego projektu może być DOWOLNY (gra,
aplikacja Android, skrypt, narzędzie CLI, automatyzacja, strona,
serwer, integracja z API itd.), rozpoznaj go z treści CELU, nie
zakładaj z góry, że chodzi o grę.

Specjalizujesz się w (dobierz to, co pasuje do AKTUALNEGO celu):
- Bibliotekach/frameworkach dostępnych w Termux dla danej
  technologii (np. do gier: pygame, kivy, libgdx, cocos2d-x; do
  innych celów: odpowiednie biblioteki Pythona/Javy/Node itd.).
- Rozwiązywaniu błędów budowania (Android SDK/Gradle w Termux,
  ale też błędów pip/npm/kompilacji dla innych technologii).
- Komendach apt/pip/npm/gradle działających bez root w Termux.

Jeżeli potrzebujesz świeżej informacji z internetu,
wypisz JEDNĄ linię:
WEB_SEARCH: <precyzyjne zapytanie po angielsku>

Nie wymyślaj faktów.
Nie powtarzaj tego samego zapytania jeżeli właśnie dostałeś wyniki.
Odpowiadaj krótko — maksymalnie 5 zdań.
"""


CRITIC_PROMPT = """
Jesteś CRITIC w uniwersalnym autonomicznym agencie — cel projektu
może być DOWOLNY (gra Android, aplikacja, skrypt, narzędzie CLI,
automatyzacja, strona, serwer itd.), rozpoznaj go z treści CELU
zamiast zakładać z góry, że chodzi o grę.

Szukasz błędów logicznych zanim MAIN wyśle zadanie do Gemini.

Sprawdź KONIECZNIE:
- Czy ten sam krok nie był już wykonywany i kończył się timeoutem?
  Jeżeli tak — zaprotestuj i zaproponuj użycie termux_run_background
  lub podział na mniejsze kroki.
- Czy warunek sukcesu jest MIERZALNY (konkretny plik, exitcode 0,
  konkretny komunikat)?
- Czy zadanie nie jest za ogólne ("zrób grę"/"zrób program") —
  powinno być jeden konkretny krok.
- Czy nie próbujemy pobrać/skopiować gotowego rozwiązania zamiast
  je zbudować, skoro cel tego wymaga?
- Czy MAIN przypadkiem zmierza do DONE bez namacalnego dowodu
  WŁAŚCIWEGO DLA TEGO KONKRETNEGO CELU: dla gry/aplikacji Android
  — zbudowany i zainstalowany APK potwierdzony zrzutem ekranu; dla
  skryptu/narzędzia CLI — uruchomienie z oczekiwanym wynikiem lub
  kodem wyjścia 0; dla strony/serwera — potwierdzenie w
  przeglądarce/odpowiedź serwera. Sam plik/kod bez uruchomienia i
  dowodu działania to NIE jest ukończenie celu.

Format:

OCENA: OK / OSTRZEŻENIE / BLOKUJ

PROBLEM (jeżeli OSTRZEŻENIE lub BLOKUJ):
...

POPRAWKA:
...
"""



CODE_REVIEWER_PROMPT = r"""
Jesteś CODE_REVIEWEREM autonomicznego agenta.

Twoim zadaniem jest ANALIZOWANIE kodu.

Nie zmieniaj plików.
Nie wykonuj patcha samodzielnie.

Jeżeli MAIN zgłosi błąd:

1. sprawdź rzeczywisty plik,
2. znajdź dokładne miejsce problemu,
3. sprawdź kontekst funkcji,
4. sprawdź zależności,
5. sprawdź czy problem nie jest skutkiem wcześniejszego patcha,
6. zaproponuj minimalną poprawkę.

Nigdy nie zgaduj struktury pliku.

Jeżeli nie masz wystarczających danych:
powiedz dokładnie, czego potrzebujesz.

Raport:

PLIK:
...

PROBLEM:
...

DOKŁADNE MIEJSCE:
...

PRZYCZYNA:
...

PROPONOWANA ZMIANA:
...

RYZYKO:
...

TEST:
...

"""


CODE_FIXER_PROMPT = r"""
Jesteś CODE_FIXEREM autonomicznego agenta.

Otrzymujesz analizę CODE_REVIEWERA oraz dokładny fragment
istniejącego kodu (nie zgadujesz, jak on wygląda).

Twoim zadaniem jest przygotowanie BEZPIECZNEJ, MINIMALNEJ
poprawki — TY piszesz TREŚĆ patcha, ale backup / nałożenie /
py_compile / rollback wykonuje kod agenta (apply_patch_from_
fixer_text), NIE ty. Twoja odpowiedź musi być w formacie, który
ten kod potrafi sparsować — inaczej patch zostanie odrzucony,
bez względu na to, jak dobry jest pomysł.

ZASADY:

1. Nie przepisuj całego programu.
2. Nie zmieniaj architektury bez wyraźnego polecenia MAIN.
3. Nie zgaduj nazw funkcji ani treści kodu — używaj WYŁĄCZNIE
   fragmentu podanego ci w wiadomości.
4. Zmień tylko wymagany fragment, jak najmniejszy.
5. Fragment SZUKAJ musi być skopiowany 1:1 z podanego kodu
   (dokładnie te same wcięcia, dokładnie te same znaki) i musi
   występować w pliku dokładnie raz — inaczej patch zostanie
   odrzucony automatycznie.

Odpowiedz WYŁĄCZNIE w tym formacie:

<<<<<<< SZUKAJ
...dokładny, unikalny fragment istniejącego kodu...
=======
...nowa wersja tego fragmentu...
>>>>>>> ZAMIEŃ

Jeżeli nie masz wystarczająco bezpiecznej poprawki, zamiast
bloku patcha napisz dokładnie: BRAK BEZPIECZNEGO PATCHA — i
wyjaśnij dlaczego. To poprawna odpowiedź, lepsza niż zgadywanie.

"""


BROWSER_PROMPT = """
Jesteś BROWSER SPECIALIST.

Analizujesz stan Chrome/CDP.

Sprawdzasz:
- istniejące karty,
- URL,
- tytuły,
- możliwe następne działania.

Nie tworzysz nowych kart.

Nie wykonujesz działań.

Odpowiadasz krótko.
"""


ANDROID_GAME_ENGINEER_PROMPT = """
Jesteś głównym INŻYNIEREM TECHNICZNYM autonomicznego agenta
działającego w Termux/Android. Nazwa roli "ANDROID GAME ENGINEER"
jest historyczna — Twoja faktyczna rola jest SZERSZA: budujesz
KAŻDY rodzaj projektu, o jaki poprosi użytkownik — nie tylko gry
Android, ale też zwykłe aplikacje Android, skrypty Pythona,
narzędzia CLI, automatyzację, scrapery, serwery, integracje z API,
strony itd. ZAWSZE najpierw rozpoznaj z treści CELU, jakiego
rodzaju projekt budujesz, i dostosuj do tego swoje rady — sekcje
poniżej dotyczące gier/APK/Gradle stosuj TYLKO gdy cel faktycznie
jest o budowie gry lub aplikacji Android; dla innych celów je
pomiń i opieraj się na ogólnej wiedzy inżynierskiej (Python, shell,
biblioteki, API, formaty plików itd.).

Dostajesz aktualny stan projektu i raport ostatniego zadania.

Twój jedyny cel: przygotować KONKRETNE, WYKONALNE polecenie lub
blok kodu, który Gemini może natychmiast uruchomić w Termux.

ZASADY:
1. Mów wyłącznie o budowie AKTUALNEGO projektu — nie planuj
   marketingu, grafiki marketingowej, dokumentacji, sklepów itp.
2. Każda Twoja rekomendacja ma być konkretna: pełna komenda,
   pełna zawartość pliku albo pełny fragment kodu do wklejenia.
3. Znaj różnicę między krokami budowania (dostosuj do rodzaju
   projektu — poniżej przykład dla gry/apki Android, ale ten sam
   podział stosuje się do dowolnego projektu):
   - setup środowiska (Python/Java/Gradle/Android SDK/venv/npm),
   - struktura projektu (pliki, katalogi, manifest/konfiguracja),
   - właściwy kod (logika, pętle, grafika — albo funkcje,
     endpointy, przetwarzanie danych — zależnie od celu),
   - budowanie/uruchomienie (gradlew assembleDebug / python
     skrypt.py / npm start — zależnie od technologii),
   - dla apek Android: podpisywanie i instalacja (adb install);
     dla innych projektów: właściwy dla nich dowód działania
     (kod wyjścia, plik wynikowy, odpowiedź HTTP itp.).
4. Jeżeli poprzednie podejście zakończyło się timeoutem:
   zaproponuj podejście lżejsze (mniejszy plik, mniej zależności)
   albo podziel build na mniejsze kroki.
5. Nie sugeruj pobierania gotowego rozwiązania (gotowej gry, APK,
   cudzego projektu) zamiast budowania go samodzielnie — projekt
   ma powstać od zera w Termux, chyba że cel wyraźnie prosi o
   użycie/zainstalowanie konkretnego istniejącego narzędzia.

============================================================
PONIŻSZE DWIE SEKCJE DOTYCZĄ WYŁĄCZNIE GIER/APLIKACJI ANDROID —
POMIŃ JE, JEŻELI AKTUALNY CEL NIE JEST O BUDOWIE APK
============================================================

============================================================
KRYTYCZNE OGRANICZENIE TERMUXA — BRAK SERWERA WYŚWIETLANIA
============================================================

Termux to terminal tekstowy działający jako zwykła apka Android.
NIE MA X11, NIE MA GLX, NIE MA żadnego okna. Dlatego:

- pyglet — `pyglet.gl` ładuje libGL przez GLX. W Termuxie zawsze
  wyleci błędem przy imporcie. NIE proponuj pyglet.
- kivy / pygame z prawdziwym oknem (SDL2 window) — SDL2 w
  Termuxie też nie ma do czego się podłączyć. `python game.py`
  odpalone bezpośrednio w Termuksie NIE POKAŻE żadnej grafiki,
  nawet jeśli proces "działa" bez błędu w logach.
- Jedyne dwie DZIAŁAJĄCE drogi do gry widocznej na ekranie
  telefonu:
    1. Prawdziwa aplikacja Android (Java/Kotlin, ewentualnie
       LibGDX) budowana przez Gradle do .apk, renderowana przez
       Android SurfaceView/Canvas/OpenGL ES — instalowana przez
       `adb install` i uruchamiana jak normalna apka.
    2. Kivy/Python spakowany przez python-for-android / buildozer
       do .apk. Kivy na Androidzie działa TYLKO jako zbudowana i
       zainstalowana aplikacja — nigdy jako skrypt odpalony
       bezpośrednio w Termuksie.
- Android SDK cmdline-tools (potrzebne do Gradle) NIE są
  pakietem apt — `pkg install sdkmanager` nie istnieje. Trzeba je
  pobrać ręcznie (wget) z developer.android.com i rozpakować.
- NIE proponuj jako "testu działania" samego uruchomienia skryptu
  w Termuksie (`python game.py` / sprawdzenie że proces nie
  crashuje) — to nie dowodzi niczego o tym, co widać na ekranie.
  Jedynym akceptowalnym dowodem działania jest zbudowany i
  zainstalowany .apk, URUCHOMIONY przez android_launch_app i
  potwierdzony przez android_screenshot (prawdziwy zrzut ekranu z
  widoczną grą) — obie te akcje działają na CAŁYM ekranie systemu,
  nie tylko w oknie Termuksa.

============================================================
ZNANA PUŁAPKA — NIEZGODNOŚĆ WERSJI GRADLE / AGP W TERMUXIE
============================================================

`pkg install gradle` w Termuxie instaluje NAJNOWSZĄ dostępną
wersję Gradle (często dużo nowszą niż jakikolwiek konkretny
Android Gradle Plugin, np. Gradle 8.x/9.x). Jeśli w
`build.gradle` (root) wpiszesz classpath ze STARĄ, "bezpieczną"
wersją AGP na pamięć (np. `com.android.tools.build:gradle:7.4.2`)
bez sprawdzenia, jaki Gradle jest faktycznie zainstalowany —
build padnie błędem w stylu:

  'org.gradle.api.artifacts.Dependency
   org.gradle.api.artifacts.dsl.DependencyHandler.module(...)'

albo innym MissingMethodException/NoSuchMethodError w skrypcie
Gradle. To ZAWSZE oznacza niezgodność wersji Gradle<->AGP, nie
błąd w kodzie gry. NIE próbuj tego naprawiać przez zmianę
zależności aplikacji (appcompat itp.) — to nie jest przyczyna.

Poprawna procedura:
1. Każ Gemini najpierw wykonać `gradle -v` i odczytać dokładną
   zainstalowaną wersję Gradle.
2. Dobierz wersję `com.android.tools.build:gradle` (AGP) z
   oficjalnej tabeli zgodności Gradle<->AGP pasującą do TEJ
   zainstalowanej wersji Gradle (nowszy Gradle -> nowszy AGP,
   zwykle AGP i Gradle w tej samej "generacji", np. Gradle 8.x
   -> AGP 8.x).
3. Jeśli build nadal się nie zgadza, zamiast zgadywać kolejne
   pary wersji — każ zbudować/skonfigurować Gradle Wrapper
   (`gradle wrapper --gradle-version <dokładna wersja pasująca
   do wybranego AGP>`) i budować przez `./gradlew`, nie przez
   systemowy `gradle`, żeby wersja była deterministyczna i nie
   zależała od tego, co akurat zaktualizował `pkg`.

Odpowiedź:

AKTUALNY ETAP BUDOWY:
...

NASTĘPNY KONKRETNY KROK:
...

POLECENIE / KOD:
```
...
```

POTENCJALNE PROBLEMY:
...
"""


PROGRESS_ESTIMATOR_PROMPT = """
Jesteś PROGRESS_ESTIMATOR — oceniasz procentowy postęp realizacji
celu autonomicznego agenta budującego aplikację/grę Android.

Dostajesz cel oraz listę kilku ostatnio wykonanych zadań (status,
skrócony raport/błąd). Na tej podstawie oceniasz, jaki procent
CAŁEGO celu jest już faktycznie zrealizowany.

Bądź REALISTYCZNY, nie optymistyczny. "Utworzono plik" to nie to
samo co "gra działa". Pełny cel zwykle wymaga: struktury projektu,
napisanego kodu, zbudowanego APK, instalacji, i fizycznego
potwierdzenia że aplikacja się uruchamia i renderuje (nie samej
deklaracji sukcesu w raporcie).

Jeżeli w ostatnich zadaniach widzisz powtarzające się błędy bez
postępu — obniż ocenę, nawet jeśli poprzednio było wyżej.

Zwróć WYŁĄCZNIE JSON, bez żadnego dodatkowego tekstu:
{
  "percent": <liczba całkowita 0-100>,
  "summary": "krótkie uzasadnienie po polsku, maksymalnie 2 zdania"
}
"""


# ============================================================
# SESJE DEEPSEEK
# ============================================================

def session_state_file(name):

    mapping = {
        "MAIN": MAIN_STATE,
        "PLANNER": PLANNER_STATE,
        "RESEARCHER": RESEARCHER_STATE,
        "CRITIC": CRITIC_STATE,
        "BROWSER": BROWSER_STATE,
    }

    # Fallback dla ról bez dedykowanego pliku stanu
    # (CODE_REVIEWER, CODE_FIXER, ANDROID_GAME_ENGINEER).
    return mapping.get(
        name,
        STATE_DIR / (name.lower() + ".json")
    )


def start_session(name, system_prompt):

    try:

        session = (
            deepseek_model.start_chat()
        )

        # Jednorazowa instrukcja roli.
        session.send_message(
            system_prompt
        )

        sessions[name] = session

        log(
            "DEEPSEEK",
            f"Sesja {name}: OK"
        )

        return session

    except Exception as e:

        log(
            "DEEPSEEK",
            f"Sesja {name} ERROR: {e}"
        )

        return None


def init_team():

    # 7 sesji — 5 oryginalnych + CODE_REVIEWER + CODE_FIXER
    # + 1 nowa: ANDROID_GAME_ENGINEER (specjalista od budowania
    # gier w Termux — kluczowy dla tego konkretnego projektu).

    start_session(
        "MAIN",
        MAIN_PROMPT
    )

    start_session(
        "PLANNER",
        PLANNER_PROMPT
    )

    start_session(
        "RESEARCHER",
        RESEARCHER_PROMPT
    )

    start_session(
        "CRITIC",
        CRITIC_PROMPT
    )

    start_session(
        "BROWSER",
        BROWSER_PROMPT
    )

    start_session(
        "CODE_REVIEWER",
        CODE_REVIEWER_PROMPT
    )

    start_session(
        "CODE_FIXER",
        CODE_FIXER_PROMPT
    )

    start_session(
        "ANDROID_GAME_ENGINEER",
        ANDROID_GAME_ENGINEER_PROMPT
    )

    start_session(
        "PROGRESS_ESTIMATOR",
        PROGRESS_ESTIMATOR_PROMPT
    )

    log(
        "DEEPSEEK",
        "Aktywne sesje: "
        + ", ".join(
            sessions.keys()
        )
    )


import threading as _threading

# Jeden lock na rolę — opendeep serializuje po swojej stronie,
# ale wiele wątków może próbować pisać do tej samej sesji.
_session_locks: dict = {}
_session_locks_lock = _threading.Lock()


def _get_session_lock(name):
    with _session_locks_lock:
        if name not in _session_locks:
            _session_locks[name] = _threading.Lock()
        return _session_locks[name]


def deepseek(name, message):
    """
    Wyślij wiadomość do trwałej sesji roli `name`.

    Sesja jest tworzona raz w init_team() i kontynuowana przez
    cały czas życia procesu — DeepSeek widzi pełną historię
    rozmowy z daną rolą, co oznacza, że MAIN, PLANNER itd.
    "pamiętają" kontekst poprzednich kroków bez powtarzania go
    w każdej wiadomości.

    Jeżeli sesja padła (wyjątek), próbujemy ją zrestartować
    jednokrotnie zamiast zwracać cichy błąd, który MAIN bierze
    za normalną odpowiedź.
    """

    lock = _get_session_lock(name)

    with lock:

        session = sessions.get(name)

        if session is None:

            log(
                "DEEPSEEK",
                f"Sesja {name} niedostępna — "
                "próba restartu."
            )

            # Pobierz prompt dla tej roli i zrestartuj.
            prompt_map = {
                "MAIN": MAIN_PROMPT,
                "PLANNER": PLANNER_PROMPT,
                "RESEARCHER": RESEARCHER_PROMPT,
                "CRITIC": CRITIC_PROMPT,
                "BROWSER": BROWSER_PROMPT,
                "CODE_REVIEWER": CODE_REVIEWER_PROMPT,
                "CODE_FIXER": CODE_FIXER_PROMPT,
                "ANDROID_GAME_ENGINEER":
                    ANDROID_GAME_ENGINEER_PROMPT,
                "PROGRESS_ESTIMATOR":
                    PROGRESS_ESTIMATOR_PROMPT,
            }

            prompt = prompt_map.get(name)

            if prompt:
                session = start_session(name, prompt)

            if session is None:
                return (
                    "DEEPSEEK ERROR: sesja "
                    + name
                    + " niedostępna i nie udało się jej wznowić."
                )

        for attempt in range(2):

            try:

                response = session.send_message(message)

                text = getattr(response, "text", None)

                if not text:
                    text = str(response)

                log(
                    "DEEPSEEK",
                    f"{name}: "
                    + str(len(text))
                    + " znaków"
                )

                return text

            except Exception as e:

                log(
                    "DEEPSEEK",
                    f"{name} próba {attempt + 1} błąd: {e}"
                )

                if attempt == 0:
                    # Pierwsza awaria — próba restartu sesji.
                    log(
                        "DEEPSEEK",
                        f"Restartuję sesję {name}..."
                    )

                    prompt_map = {
                        "MAIN": MAIN_PROMPT,
                        "PLANNER": PLANNER_PROMPT,
                        "RESEARCHER": RESEARCHER_PROMPT,
                        "CRITIC": CRITIC_PROMPT,
                        "BROWSER": BROWSER_PROMPT,
                        "CODE_REVIEWER": CODE_REVIEWER_PROMPT,
                        "CODE_FIXER": CODE_FIXER_PROMPT,
                        "ANDROID_GAME_ENGINEER":
                            ANDROID_GAME_ENGINEER_PROMPT,
                        "PROGRESS_ESTIMATOR":
                            PROGRESS_ESTIMATOR_PROMPT,
                    }

                    prompt = prompt_map.get(name)

                    if prompt:
                        new_session = start_session(
                            name, prompt
                        )

                        if new_session:
                            session = new_session
                            continue

                return (
                    "DEEPSEEK ERROR ["
                    + name
                    + "]: "
                    + str(e)
                )


# ============================================================
# ANDROID
# ============================================================

def adb_connect_target():
    """
    Ostatnio znany / skonfigurowany adres bezprzewodowego ADB
    (host:port).

    Kolejność: zmienna środowiskowa ADB_CONNECT > plik zapisany
    przez prompt_adb_target() przy starcie programu.
    """

    env = os.environ.get(
        "ADB_CONNECT",
        ""
    ).strip()

    if env:
        return env

    saved = read_text(
        ADB_CONNECT_FILE
    ).strip()

    return saved or None


def adb_try_connect(target, timeout=10):
    """
    Wykonuje `adb connect <host:port>`.

    Przy sukcesie zapamiętuje adres w ADB_CONNECT_FILE — przydatne,
    gdy port akurat się nie zmienił od poprzedniego uruchomienia.
    """

    target = str(target or "").strip()

    if not target:
        return False

    try:

        result = subprocess.run(
            ["adb", "connect", target],
            capture_output=True,
            text=True,
            timeout=timeout
        )

        output = (
            (result.stdout or "")
            + (result.stderr or "")
        )

        ok = (
            "connected" in output.lower()
            and "cannot" not in output.lower()
            and "failed" not in output.lower()
        )

        if ok:

            log(
                "ANDROID",
                "ADB connect OK -> " + target
            )

            write_text(
                ADB_CONNECT_FILE,
                target
            )

        else:

            log(
                "ANDROID",
                "ADB connect nieudane -> "
                + target
                + " : "
                + short(output.strip(), 300)
            )

        return ok

    except Exception as e:

        log(
            "ANDROID",
            "ADB connect ERROR: " + str(e)
        )

        return False


def find_adb(auto_reconnect=True):
    """
    Znajduje działające urządzenie ADB (`adb devices` -> "device").

    Jeżeli nic nie zostanie znalezione, a auto_reconnect=True,
    próbujemy RAZ automatycznie podłączyć się ponownie przy użyciu
    ostatnio skonfigurowanego adresu (patrz adb_connect_target()) —
    to naprawia najczęstszy przypadek: restart WiFi na telefonie
    losuje NOWY port debugowania bezprzewodowego, więc stare
    połączenie ADB milczy, dopóki ktoś (albo my sami) nie wykona
    `adb connect` na nowy adres.
    """

    def _list_devices():

        try:

            result = subprocess.run(
                ["adb", "devices"],
                capture_output=True,
                text=True,
                timeout=10
            )

            for line in result.stdout.splitlines():

                line = line.strip()

                if (
                    not line
                    or line.startswith("List")
                ):
                    continue

                parts = line.split()

                if (
                    len(parts) >= 2
                    and parts[1] == "device"
                ):
                    return parts[0]

        except Exception:
            pass

        return None

    device = _list_devices()

    if device or not auto_reconnect:
        return device

    target = adb_connect_target()

    if target and adb_try_connect(target):
        device = _list_devices()

    return device


def init_android():

    global android_device
    global adb_target

    adb_target = find_adb()

    try:

        if adb_target:
            android_device = u2.connect(
                adb_target
            )
        else:
            android_device = u2.connect()

        android_device.settings[
            "wait_timeout"
        ] = 3

        log(
            "ANDROID",
            "uiautomator2 OK"
        )

        return True

    except Exception as e:

        log(
            "ANDROID",
            "ERROR: " + str(e)
        )

        android_device = None

        return False


def android_summary():
    """
    Zwraca aktualny stan Androida.

    Ważne:
    - korzysta z istniejącego android_device,
    - jeśli go nie ma, próbuje ponownie wykonać init_android(),
    - nie zgłasza fałszywego 'Android niedostępny',
      jeżeli ADB/uiautomator2 faktycznie działa.
    """

    global android_device
    global adb_target

    if android_device is None:
        try:
            if not init_android():
                target = find_adb()

                if target:
                    adb_target = target

                    try:
                        android_device = u2.connect(
                            target
                        )

                        android_device.settings[
                            "wait_timeout"
                        ] = 3

                    except Exception:
                        android_device = None

                if android_device is None:
                    return (
                        "Android niedostępny."
                    )

        except Exception as e:
            return (
                "Android state error: "
                + str(e)
            )

    def _parse_hierarchy(xml):

        root = ET.fromstring(xml)

        lines = []

        for node in root.iter("node"):
            text = node.attrib.get(
                "text",
                ""
            ).strip()

            desc = node.attrib.get(
                "content-desc",
                ""
            ).strip()

            resource = node.attrib.get(
                "resource-id",
                ""
            ).strip()

            clickable = node.attrib.get(
                "clickable",
                "false"
            )

            focusable = node.attrib.get(
                "focusable",
                "false"
            )

            enabled = node.attrib.get(
                "enabled",
                "true"
            )

            bounds = node.attrib.get(
                "bounds",
                ""
            )

            if not (
                text
                or desc
                or resource
                or clickable == "true"
                or focusable == "true"
            ):
                continue

            if "/" in resource:
                resource = resource.split(
                    "/"
                )[-1]

            label = (
                text
                or desc
                or resource
            )

            lines.append(
                f"{label} | "
                f"click={clickable} | "
                f"focus={focusable} | "
                f"enabled={enabled} | "
                f"bounds={bounds}"
            )

            if len(
                "\n".join(lines)
            ) >= ANDROID_LIMIT:
                break

        return lines

    # Dwie próby: jeżeli pierwsza padnie (np. WiFi na telefonie
    # zrestartowało się w międzyczasie i stary obiekt android_device
    # wskazuje na już martwy port debugowania bezprzewodowego),
    # traktujemy go jako martwy, próbujemy podłączyć się od nowa
    # (find_adb() samo spróbuje adb_try_connect na zapisany adres)
    # i wykonujemy dump_hierarchy jeszcze raz, zanim zgłosimy błąd.

    for attempt in range(2):

        try:
            xml = android_device.dump_hierarchy(
                compressed=False
            )

            lines = _parse_hierarchy(xml)

            if not lines:
                return (
                    "Android OK — brak "
                    "czytelnych elementów UI."
                )

            return "\n".join(lines)

        except Exception as e:

            if attempt == 0:

                log(
                    "ANDROID",
                    "Połączenie padło ("
                    + str(e)
                    + ") — próbuję podłączyć się ponownie..."
                )

                android_device = None

                if init_android():
                    continue

            return (
                "Android state error: "
                + str(e)
            )



def android_click_text(text):
    """
    Inteligentne kliknięcie elementu Android.

    Kolejność:
    1. text
    2. textContains
    3. content-desc / description
    4. XML -> content-desc -> bounds -> coordinate click
    5. XML -> text -> bounds -> coordinate click
    """

    global android_device

    if android_device is None:
        if not init_android():
            return {
                "ok": False,
                "action": "click_text",
                "text": text,
                "error": "Android niedostępny."
            }

    target = str(text)

    # --------------------------------------------------------
    # 1. EXACT TEXT
    # --------------------------------------------------------

    try:
        obj = android_device(text=target)

        if obj.exists:
            obj.click()

            return {
                "ok": True,
                "action": "click_text",
                "text": target,
                "method": "text"
            }
    except Exception:
        pass

    # --------------------------------------------------------
    # 2. TEXT CONTAINS
    # --------------------------------------------------------

    try:
        obj = android_device(textContains=target)

        if obj.exists:
            obj.click()

            return {
                "ok": True,
                "action": "click_text_contains",
                "text": target,
                "method": "textContains"
            }
    except Exception:
        pass

    # --------------------------------------------------------
    # 3. CONTENT-DESC
    # --------------------------------------------------------

    try:
        obj = android_device(description=target)

        if obj.exists:
            try:
                obj.click()

                return {
                    "ok": True,
                    "action": "click_text",
                    "text": target,
                    "method": "content-desc"
                }
            except Exception:
                pass

            # ------------------------------------------------
            # CONTENT-DESC -> BOUNDS -> REAL COORDINATE CLICK
            # ------------------------------------------------

            try:
                info = obj.info

                bounds = info.get("bounds")

                if bounds:
                    left = int(bounds["left"])
                    top = int(bounds["top"])
                    right = int(bounds["right"])
                    bottom = int(bounds["bottom"])

                    x = (left + right) // 2
                    y = (top + bottom) // 2

                    android_device.click(x, y)

                    return {
                        "ok": True,
                        "action": "click_text",
                        "text": target,
                        "method": "content-desc-bounds",
                        "x": x,
                        "y": y,
                        "bounds": bounds
                    }

            except Exception:
                pass

    except Exception:
        pass

    # --------------------------------------------------------
    # 4/5. XML FALLBACK
    # --------------------------------------------------------

    try:
        import xml.etree.ElementTree as ET

        xml_data = android_device.dump_hierarchy(
            compressed=False
        )

        root = ET.fromstring(xml_data)

        for node in root.iter("node"):

            attrs = node.attrib

            node_text = attrs.get("text", "").strip()
            node_desc = attrs.get("content-desc", "").strip()
            clickable = attrs.get("clickable", "false")
            enabled = attrs.get("enabled", "true")
            bounds = attrs.get("bounds", "")

            matched = (
                node_text == target
                or node_desc == target
                or target in node_text
                or target in node_desc
            )

            if not matched:
                continue

            if enabled != "true":
                continue

            if not bounds:
                continue

            match = re.match(
                r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",
                bounds
            )

            if not match:
                continue

            left = int(match.group(1))
            top = int(match.group(2))
            right = int(match.group(3))
            bottom = int(match.group(4))

            x = (left + right) // 2
            y = (top + bottom) // 2

            try:
                android_device.click(x, y)

                return {
                    "ok": True,
                    "action": "click_text",
                    "text": target,
                    "method": "xml-bounds",
                    "x": x,
                    "y": y,
                    "bounds": bounds,
                    "node_text": node_text,
                    "node_desc": node_desc,
                    "clickable": clickable
                }

            except Exception:
                continue

    except Exception:
        pass

    # --------------------------------------------------------
    # FAIL
    # --------------------------------------------------------

    return {
        "ok": False,
        "action": "click_text",
        "text": target,
        "error": "Nie znaleziono elementu ani jego współrzędnych."
    }


def android_tap(x, y):

    if android_device is None:
        return {
            "ok": False,
            "error": "Android niedostępny"
        }

    try:

        android_device.click(
            int(x),
            int(y)
        )

        return {
            "ok": True,
            "x": x,
            "y": y
        }

    except Exception as e:

        return {
            "ok": False,
            "error": str(e)
        }


def android_type(text):

    if android_device is None:
        return {
            "ok": False,
            "error": "Android niedostępny"
        }

    try:

        android_device.send_keys(
            str(text),
            clear=True
        )

        return {
            "ok": True
        }

    except Exception as e:

        return {
            "ok": False,
            "error": str(e)
        }


def android_press(key):

    if android_device is None:
        return {
            "ok": False,
            "error": "Android niedostępny"
        }

    try:

        android_device.press(
            str(key)
        )

        return {
            "ok": True,
            "key": key
        }

    except Exception as e:

        return {
            "ok": False,
            "error": str(e)
        }


def android_click_resource(resource):
    """
    Kliknięcie po resource-id — bardziej niezawodne niż tekst
    dla przycisków bez czytelnego tekstu (ikony, gry).

    UWAGA: ta funkcja i android_swipe() istniały już wcześniej
    w tym pliku, ale były zdefiniowane PO
    "if __name__ == '__main__': main()", czyli w praktyce nigdy
    nie zdążyły zostać zdefiniowane przed uruchomieniem main() —
    dispatch_tool() ich po prostu nie widział. Przeniesione tutaj,
    żeby faktycznie działały.
    """

    global android_device

    if android_device is None:
        if not init_android():
            return {
                "ok": False,
                "error": "Android niedostępny."
            }

    try:
        obj = android_device(
            resourceId=resource
        )

        if obj.exists:
            obj.click()

            return {
                "ok": True,
                "action": "click_resource",
                "resource": resource
            }

        return {
            "ok": False,
            "action": "click_resource",
            "resource": resource,
            "error": "Nie znaleziono resource-id."
        }

    except Exception as e:
        return {
            "ok": False,
            "action": "click_resource",
            "resource": resource,
            "error": str(e)
        }


def android_swipe(
    x1,
    y1,
    x2,
    y2,
    duration=0.3
):
    """
    Wykonuje swipe po współrzędnych — potrzebne dla gier/UI
    sterowanych gestem, list do przewinięcia itp.
    """

    global android_device

    if android_device is None:
        if not init_android():
            return {
                "ok": False,
                "error": "Android niedostępny."
            }

    try:
        android_device.swipe(
            int(x1),
            int(y1),
            int(x2),
            int(y2),
            duration=float(duration)
        )

        return {
            "ok": True,
            "action": "swipe",
            "from": [int(x1), int(y1)],
            "to": [int(x2), int(y2)]
        }

    except Exception as e:
        return {
            "ok": False,
            "action": "swipe",
            "error": str(e)
        }


def android_screenshot(path=None):
    """
    Prawdziwy zrzut ekranu (PNG), nie tylko tekstowy dump drzewa
    UI. Potrzebne do fizycznej weryfikacji na końcu (np. "czy gra
    faktycznie się uruchomiła i coś rysuje na ekranie" to co innego
    niż "czy proces istnieje").

    Wcześniej android_screen() było tylko aliasem do
    android_summary() (tekst), więc nic realnie nie fotografowało
    ekranu.
    """

    global android_device

    if android_device is None:
        if not init_android():
            return {
                "ok": False,
                "error": "Android niedostępny."
            }

    try:
        target = Path(
            str(path)
            if path
            else str(AGENT_DIR / "screenshots" /
                (datetime.now().strftime("%Y%m%d_%H%M%S") + ".png"))
        ).expanduser()

        target.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        android_device.screenshot(
            str(target)
        )

        return {
            "ok": target.exists(),
            "action": "screenshot",
            "path": str(target),
            "size_bytes": (
                target.stat().st_size
                if target.exists() else 0
            )
        }

    except Exception as e:
        return {
            "ok": False,
            "action": "screenshot",
            "error": str(e)
        }


def android_launch_app(package):
    """
    Uruchamia zainstalowaną aplikację po nazwie pakietu przez
    `adb shell monkey` — w odróżnieniu od `am start -n pakiet/
    .Activity` nie wymaga znajomości nazwy głównej activity, więc
    działa dla DOWOLNEJ aplikacji na urządzeniu (nie tylko tej
    budowanej w bieżącym projekcie).

    To jest brakujący kawałek "rąk": android_screenshot/
    android_click/android_state już działają na CAŁYM ekranie
    systemu (nie tylko w oknie Termuksa), ale wcześniej nie było
    jawnego narzędzia, żeby SAMEMU otworzyć inną aplikację — np.
    zbudowaną i zainstalowaną (`adb install`) grę, zanim zrobi się
    zrzut ekranu jako dowód, że coś faktycznie się renderuje.
    """

    package = str(package or "").strip()

    if not package:
        return {
            "ok": False,
            "error": "Pusta nazwa pakietu."
        }

    result = execute_shell(
        "adb shell monkey -p " + package
        + " -c android.intent.category.LAUNCHER 1",
        timeout=20
    )

    started = bool(
        result.get("ok")
        and "Events injected: 1" in result.get("stdout", "")
    )

    return {
        "ok": started,
        "action": "launch_app",
        "package": package,
        "detail": short(
            result.get("stdout", "")
            + result.get("stderr", ""),
            500
        )
    }


def _ensure_termux_allow_external_apps():
    """
    RUN_COMMAND (uruchomienie komendy w NOWYM, widocznym oknie/
    sesji Termuksa) wymaga allow-external-apps=true w
    ~/.termux/termux.properties. Domyślnie to WYŁĄCZONE.

    Dopisuje brakującą linię, jeśli jej nie ma — ale Termux czyta
    ten plik TYLKO przy starcie aplikacji (albo po ręcznym "Reload
    Settings" z powiadomienia Termuksa), więc jeśli dopiero co
    dopisaliśmy tę linię, RUN_COMMAND i tak nie zadziała, dopóki
    ktoś nie zrestartuje Termuksa / nie kliknie Reload Settings.

    Zwraca True, jeśli linia JUŻ była obecna wcześniej (można
    próbować od razu), False jeśli właśnie ją dopisano.
    """

    props_path = Path(
        "~/.termux/termux.properties"
    ).expanduser()

    content = read_text(props_path)

    if re.search(
        r"^\s*allow-external-apps\s*=\s*true\s*$",
        content,
        re.MULTILINE
    ):
        return True

    props_path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    new_content = (
        content.rstrip("\n")
        + ("\n" if content.strip() else "")
        + "allow-external-apps=true\n"
    )

    write_text(props_path, new_content)

    log(
        "TERMUX",
        "Dopisano allow-external-apps=true do "
        + str(props_path)
        + " — RUN_COMMAND zadziała dopiero po restarcie Termuksa "
        "albo 'Reload Settings' z powiadomienia Termuksa."
    )

    return False


def android_run_in_new_window(command, background=False):
    """
    Uruchamia komendę w NOWYM, widocznym oknie/sesji Termuksa
    przez wbudowany w Termux mechanizm RUN_COMMAND — w odróżnieniu
    od termux_run_background (ten sam proces, log tylko w pliku),
    to faktycznie OTWIERA nowe okno Termuksa z tą komendą, więc
    output jest widoczny na żywo, osobno od głównej sesji agenta.

    Wymaga allow-external-apps=true w termux.properties — jeśli
    dopiero teraz zostało włączone, ta i kolejne próby mogą się
    nie udać, dopóki Termux nie zostanie zrestartowany (patrz pole
    "needs_termux_restart" w wyniku).
    """

    command = str(command or "").strip()

    if not command:
        return {
            "ok": False,
            "error": "Pusta komenda."
        }

    already_enabled = _ensure_termux_allow_external_apps()

    script_dir = Path(
        "~/.termux_run_command_scripts"
    ).expanduser()

    script_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    script_path = script_dir / (uuid.uuid4().hex + ".sh")

    script_path.write_text(
        "#!/data/data/com.termux/files/usr/bin/sh\n"
        + command
        + "\n",
        encoding="utf-8"
    )

    try:
        script_path.chmod(0o700)
    except Exception:
        pass

    background_flag = "true" if background else "false"

    result = execute_shell(
        "adb shell am start -n "
        "com.termux/com.termux.app.RunCommandActivity "
        "-a com.termux.RUN_COMMAND "
        "--es com.termux.RUN_COMMAND_PATH '"
        + str(script_path)
        + "' --ez com.termux.RUN_COMMAND_BACKGROUND "
        + background_flag,
        timeout=15
    )

    output = (
        result.get("stdout", "")
        + result.get("stderr", "")
    )

    denied = (
        "SecurityException" in output
        or "Permission Denial" in output
    )

    started = bool(
        result.get("ok")
        and "Starting: Intent" in output
        and not denied
    )

    detail = short(output, 500)

    if not already_enabled:
        detail = (
            "UWAGA: allow-external-apps właśnie zostało włączone "
            "w termux.properties — RUN_COMMAND zadziała dopiero "
            "po restarcie Termuksa albo 'Reload Settings' z "
            "powiadomienia Termuksa. "
        ) + detail

    if denied:
        detail = (
            "ODRZUCONE przez system — allow-external-apps "
            "prawdopodobnie nadal wyłączone (Termux nie został "
            "zrestartowany po włączeniu, albo właściwości nie "
            "zostały przeładowane). "
        ) + detail

    return {
        "ok": started,
        "action": "run_in_new_window",
        "command": command,
        "script": str(script_path),
        "background": bool(background),
        "needs_termux_restart": not already_enabled,
        "detail": detail
    }


def android_install_apk(path, reinstall=True):
    """
    Instaluje APK przez `adb install` i PARSUJE wynik zamiast
    zgadywać po returncode — `adb install` potrafi zwrócić
    returncode=0 nawet gdy instalacja faktycznie się nie powiodła
    (błąd jest tylko w tekście stdout, np.
    "INSTALL_FAILED_UPDATE_INCOMPATIBLE" albo
    "INSTALL_FAILED_VERSION_DOWNGRADE"). Wcześniej Gemini musiałoby
    to zgadywać przez surowe `shell("adb install ...")`.

    reinstall=True dodaje flagę -r (nadpisz istniejącą instalację,
    zachowując dane) — domyślne, bo to typowy przypadek przy
    iteracyjnym budowaniu tej samej aplikacji.
    """

    path = str(path or "").strip()

    if not path:
        return {
            "ok": False,
            "error": "Pusta ścieżka do APK."
        }

    flags = "-r" if reinstall else ""

    result = execute_shell(
        "adb install " + flags + " " + path,
        timeout=90
    )

    output = (
        result.get("stdout", "")
        + result.get("stderr", "")
    )

    success = "Success" in output

    failure_match = re.search(
        r"(INSTALL_FAILED_[A-Z_]+)",
        output
    )

    return {
        "ok": success,
        "action": "install_apk",
        "path": path,
        "failure_reason": (
            failure_match.group(1) if failure_match else None
        ),
        "detail": short(output, 800)
    }


def android_uninstall_app(package):
    """
    Odinstalowuje aplikację po nazwie pakietu przez
    `adb uninstall`. Przydatne przed czystą reinstalacją, gdy
    android_install_apk zwróci błąd typu
    INSTALL_FAILED_UPDATE_INCOMPATIBLE (podpis/wersja niezgodne z
    poprzednią instalacją) — wtedy zwykłe -r nie wystarczy, trzeba
    najpierw odinstalować.
    """

    package = str(package or "").strip()

    if not package:
        return {
            "ok": False,
            "error": "Pusta nazwa pakietu."
        }

    result = execute_shell(
        "adb uninstall " + package,
        timeout=30
    )

    output = (
        result.get("stdout", "")
        + result.get("stderr", "")
    )

    success = "Success" in output

    return {
        "ok": success,
        "action": "uninstall_app",
        "package": package,
        "detail": short(output, 500)
    }


def android_logcat(package=None, lines=200):
    """
    Zrzuca ostatnie wpisy logcat (adb logcat -d = zrzuć i wyjdź,
    NIE zawiesza się czekając na nowe logi) — kluczowe do
    diagnozowania APLIKACJI, KTÓRA SIĘ ZAINSTALOWAŁA, ALE PADA PRZY
    URUCHOMIENIU. Wcześniej agent nie miał ŻADNEGO sposobu, żeby
    zobaczyć crash stack trace — jedyne dostępne narzędzia
    (android_screenshot, android_state) nic nie pokażą, jeśli
    aplikacja zdąży się wywalić, zanim cokolwiek narysuje.

    Jeżeli podano `package`, filtruje po PID tego procesu (przez
    `adb shell pidof <package>`) — jeśli proces już nie działa
    (bo padł), logi PID-u nadal są w buforze logcat, więc to wciąż
    działa tuż po crashu.
    """

    lines = int(lines or 200)

    pid = None

    if package:

        package = str(package).strip()

        pid_result = execute_shell(
            "adb shell pidof " + package,
            timeout=15
        )

        pid_out = pid_result.get("stdout", "").strip()

        if pid_out:
            pid = pid_out.split()[0]

    command = "adb logcat -d -t " + str(lines)

    if pid:
        command += " --pid=" + pid

    result = execute_shell(
        command,
        timeout=30
    )

    output = result.get("stdout", "")

    crashed = bool(
        re.search(
            r"FATAL EXCEPTION|AndroidRuntime.*FATAL",
            output
        )
    )

    return {
        "ok": result.get("ok", False),
        "action": "logcat",
        "package": package,
        "pid": pid,
        "crash_detected": crashed,
        "log": short(output, 6000)
    }


# ============================================================
# BEZPIECZEŃSTWO: POTWIERDZENIE PRZED USUWANIEM
# ============================================================
#
# Żadne narzędzie nie usuwało dotąd niczego bez pytania — Gemini
# mogło uruchomić `rm -rf cokolwiek` przez shell/termux_run bez
# żadnej bramki. Poniższe dwie funkcje to naprawiają: wykrywają
# komendy wyglądające na usuwanie i zatrzymują się, pytając
# operatora w terminalu, zanim cokolwiek faktycznie zniknie.
# ============================================================

_DELETE_COMMAND_PATTERN = re.compile(
    r"\b(rm|rmdir|unlink)\b|\bfind\b[^\n]*-delete\b",
    re.IGNORECASE
)


def _looks_like_delete_command(command):
    return bool(
        _DELETE_COMMAND_PATTERN.search(str(command or ""))
    )


def _confirm_destructive_action(description):
    """
    Blokuje i pyta operatora w terminalu, zanim agent wykona
    nieodwracalną operację (usunięcie pliku/katalogu).

    Zwraca True TYLKO przy jawnym potwierdzeniu. Każdy inny
    przypadek — odmowa, brak terminala (EOFError), Ctrl+C — jest
    traktowany jako odmowa. To bezpieczny domyślny wybór: agent
    może czasem niepotrzebnie zapytać (np. o nieszkodliwe "grep
    -rm" w komentarzu), ale nigdy nie usunie niczego bez pytania.
    """

    try:
        print()
        print("⚠️  AGENT CHCE WYKONAĆ OPERACJĘ USUWANIA:")
        print("   " + str(description))

        answer = input(
            "   Zezwolić? [t/N] > "
        ).strip().lower()

        return answer in ("t", "tak", "y", "yes")

    except (EOFError, KeyboardInterrupt):
        print()
        return False


# ============================================================
# SHELL
# ============================================================

def execute_shell(command, timeout=None):

    command = str(
        command or ""
    ).strip()

    if not command:
        return {
            "ok": False,
            "error": "Pusta komenda"
        }

    if _looks_like_delete_command(command):

        if not _confirm_destructive_action(
            "Komenda usuwająca: " + command
        ):
            return {
                "ok": False,
                "error": (
                    "Operacja usuwania odrzucona (brak "
                    "potwierdzenia operatora). Jeżeli to "
                    "naprawdę potrzebne, zapytaj użytkownika "
                    "wprost i poczekaj na jego decyzję zamiast "
                    "ponawiać tę samą komendę."
                ),
                "command": command,
                "blocked_by_safety_gate": True
            }

    effective_timeout = int(
        timeout or COMMAND_TIMEOUT
    )

    started = datetime.now()

    try:

        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=effective_timeout
        )

        return {
            "ok":
                result.returncode == 0,
            "returncode":
                result.returncode,
            "stdout":
                short(
                    result.stdout,
                    6000
                ),
            "stderr":
                short(
                    result.stderr,
                    6000
                ),
            "command": command,
            "timeout": effective_timeout,
            "duration_s": round(
                (datetime.now() - started).total_seconds(),
                1
            )
        }

    except subprocess.TimeoutExpired as e:

        # WAŻNE: TimeoutExpired NIESIE ze sobą to, co proces
        # zdążył wypisać przed zabiciem (jeśli capture_output=True).
        # Wcześniej ta informacja była bezpowrotnie tracona —
        # dokładnie to widać w logach ("Timeout" i nic więcej).

        partial_stdout = getattr(e, "stdout", None)
        partial_stderr = getattr(e, "stderr", None)

        if isinstance(partial_stdout, bytes):
            partial_stdout = partial_stdout.decode(
                "utf-8", errors="replace"
            )

        if isinstance(partial_stderr, bytes):
            partial_stderr = partial_stderr.decode(
                "utf-8", errors="replace"
            )

        return {
            "ok": False,
            "error": "Timeout",
            "command": command,
            "timeout": effective_timeout,
            "duration_s": round(
                (datetime.now() - started).total_seconds(),
                1
            ),
            "stdout_partial": short(partial_stdout or "", 4000),
            "stderr_partial": short(partial_stderr or "", 4000)
        }

    except Exception as e:

        return {
            "ok": False,
            "error": str(e),
            "command": command,
            "timeout": effective_timeout
        }


# ============================================================
# CHROME
# ============================================================


def ensure_chrome_cdp_forward(retries=3):
    """
    Ustawia `adb forward` dla CDP i sprawdza /json/version.

    WAŻNE: urządzenie jest szukane przez find_adb(), które samo
    próbuje automatycznego reconnectu (adb_try_connect) na ostatnio
    skonfigurowany adres WiFi, gdy `adb devices` nic nie zwróci —
    to naprawia najczęstszą przyczynę "brak urządzenia" po
    restarcie WiFi na telefonie.

    Druga częsta usterka to przejściowy błąd samego requestu HTTP
    zaraz po (re)ustawieniu forwardu — ADB/atx-agent/Chrome
    potrzebuje chwili, żeby transport wstał, i wtedy
    RemoteDisconnected("Remote end closed connection without
    response") jest zwykle jednorazowym zacięciem, nie trwałą
    awarią. Dlatego cały blok forward+request jest powtarzany do
    `retries` razy z krótką przerwą, zamiast poddawać się od razu.
    """

    device = find_adb()

    if not device:
        log("CHROME", "ADB: brak urządzenia")
        return False

    log("CHROME", f"ADB device: {device}")

    last_error = None

    for attempt in range(1, retries + 1):

        try:

            subprocess.run(
                [
                    "adb",
                    "-s",
                    device,
                    "forward",
                    "--remove",
                    f"tcp:{CDP_PORT}"
                ],
                capture_output=True,
                text=True,
                timeout=5
            )

            proc = subprocess.run(
                [
                    "adb",
                    "-s",
                    device,
                    "forward",
                    f"tcp:{CDP_PORT}",
                    "localabstract:chrome_devtools_remote"
                ],
                capture_output=True,
                text=True,
                timeout=8
            )

            if proc.returncode != 0:
                last_error = (
                    "ADB forward ERROR: "
                    + (proc.stderr.strip() or "unknown error")
                )
                log("CHROME", last_error)
                time.sleep(1)
                continue

            log(
                "CHROME",
                f"ADB forward OK -> tcp:{CDP_PORT}"
            )

            r = requests.get(
                f"http://{CDP_HOST}:{CDP_PORT}/json/version",
                timeout=5
            )

            r.raise_for_status()

            info = r.json()

            log(
                "CHROME",
                "CDP OK -> "
                + str(info.get("Browser", "Chrome"))
            )

            return True

        except Exception as e:

            last_error = str(e)

            log(
                "CHROME",
                f"CDP próba {attempt}/{retries} nieudana: "
                + last_error
            )

            time.sleep(1)

            # Urządzenie mogło zniknąć w międzyczasie (WiFi) —
            # spróbuj je odnaleźć/reconnect na kolejną próbę.
            refreshed = find_adb()

            if refreshed:
                device = refreshed

    log(
        "CHROME",
        f"CDP AUTO ERROR (po {retries} próbach): {last_error}"
    )

    return False


def chrome_tabs():

    if not ensure_chrome_cdp_forward():
        return []

    try:

        r = requests.get(
            f"http://{CDP_HOST}:{CDP_PORT}/json",
            timeout=5
        )

        r.raise_for_status()

        data = r.json()

        result = []

        for tab in data:

            if (
                tab.get("type") == "page"
                and tab.get(
                    "webSocketDebuggerUrl"
                )
            ):

                result.append({
                    "id":
                        tab.get("id", ""),
                    "title":
                        tab.get("title", ""),
                    "url":
                        tab.get("url", ""),
                    "webSocketDebuggerUrl":
                        tab.get(
                            "webSocketDebuggerUrl"
                        )
                })

        return result

    except Exception:

        return []


def chrome_summary():

    tabs = chrome_tabs()

    if not tabs:
        return "Brak dostępnych kart Chrome/CDP."

    lines = []

    for tab in tabs:

        lines.append(
            f"[{tab['id']}] "
            f"{short(tab['title'], 100)} | "
            f"{short(tab['url'], 300)}"
        )

    return "\n".join(lines)


def find_tab(
    tab_id=None,
    contains=None
):

    tabs = chrome_tabs()

    if tab_id:

        for tab in tabs:

            if tab["id"] == str(tab_id):
                return tab

    if contains:

        needle = str(
            contains
        ).lower()

        for tab in tabs:

            value = (
                tab["title"]
                + " "
                + tab["url"]
            ).lower()

            if needle in value:
                return tab

    return None


def cdp_connect(tab):

    try:

        return websocket.create_connection(
            tab["webSocketDebuggerUrl"],
            timeout=15,
            suppress_origin=True
        )

    except Exception as e:

        log(
            "CHROME",
            "CDP ERROR: " + str(e)
        )

        return None


def cdp_call(
    ws,
    msg_id,
    method,
    params=None,
    timeout=20
):

    message = {
        "id": msg_id,
        "method": method
    }

    if params is not None:
        message["params"] = params

    try:

        ws.send(
            json.dumps(message)
        )

        ws.settimeout(timeout)

        while True:

            raw = ws.recv()

            data = json.loads(raw)

            if data.get("id") != msg_id:
                continue

            if "error" in data:

                return {
                    "ok": False,
                    "error": data["error"]
                }

            return {
                "ok": True,
                "result": data.get(
                    "result",
                    {}
                )
            }

    except Exception as e:

        return {
            "ok": False,
            "error": str(e)
        }


def chrome_eval(
    tab,
    javascript
):

    ws = cdp_connect(tab)

    if ws is None:
        return {
            "ok": False,
            "error": "CDP connect failed"
        }

    try:

        result = cdp_call(
            ws,
            1,
            "Runtime.evaluate",
            {
                "expression":
                    javascript,
                "returnByValue":
                    True,
                "awaitPromise":
                    True
            }
        )

    finally:

        try:
            ws.close()
        except Exception:
            pass

    if not result.get("ok"):
        return result

    return (
        result
        .get("result", {})
        .get("result", {})
        .get("value")
    )


# ============================================================
# CHROME INSPECT
# ============================================================

def chrome_inspect(
    tab_id=None,
    contains=None
):

    tab = find_tab(
        tab_id,
        contains
    )

    if tab is None:

        return {
            "ok": False,
            "error":
                "Nie znaleziono istniejącej karty"
        }

    javascript = r"""
(() => {

    const clean = (v) =>
        (v || "")
        .replace(/\s+/g, " ")
        .trim();

    const controls = [];

    document.querySelectorAll(
        'a,button,input,textarea,select,' +
        '[role="button"],' +
        '[contenteditable="true"]'
    ).forEach((el, index) => {

        const r =
            el.getBoundingClientRect();

        if (
            r.width <= 0 ||
            r.height <= 0
        ) {
            return;
        }

        controls.push({
            i: index,
            tag: el.tagName,
            text: clean(
                el.innerText ||
                el.value ||
                el.getAttribute(
                    "aria-label"
                )
            ),
            id: el.id || "",
            role:
                el.getAttribute(
                    "role"
                ) || "",
            href:
                el.href || "",
            type:
                el.getAttribute(
                    "type"
                ) || "",
            x:
                Math.round(r.x),
            y:
                Math.round(r.y),
            width:
                Math.round(r.width),
            height:
                Math.round(r.height)
        });

    });

    return {
        title:
            document.title,

        url:
            location.href,

        text:
            clean(
                document.body
                    ? document.body.innerText
                    : ""
            ).slice(
                0,
                10000
            ),

        controls:
            controls.slice(
                0,
                120
            )
    };

})()
"""

    value = chrome_eval(
        tab,
        javascript
    )

    if not isinstance(value, dict):

        return {
            "ok": False,
            "error": "Brak danych strony"
        }

    return {
        "ok": True,
        "tab_id":
            tab["id"],
        "title":
            value.get(
                "title",
                ""
            ),
        "url":
            value.get(
                "url",
                ""
            ),
        "text":
            short(
                value.get(
                    "text",
                    ""
                ),
                CHROME_TEXT_LIMIT
            ),
        "controls":
            value.get(
                "controls",
                []
            )
    }


# ============================================================
# CHROME NAVIGATE
# ============================================================

CDP_403 = False

def chrome_open(
    url,
    tab_id=None,
    contains=None
):

    global CDP_403

    # Jeżeli Chrome wcześniej odrzucił WebSocket 403,
    # nie próbujemy ponownie tego samego mechanizmu.
    if CDP_403:

        return {
            "ok": False,
            "error":
                "CDP wcześniej zwróciło 403. "
                "Użyj Androida do obsługi Chrome."
        }

    tab = find_tab(
        tab_id,
        contains
    )

    # WAŻNE:
    # NIE TWORZYMY NOWEJ KARTY.
    if tab is None:

        return {
            "ok": False,
            "error":
                "Nie znaleziono istniejącej karty. "
                "Nowe karty są zablokowane."
        }

    ws = cdp_connect(tab)

    if ws is None:

        return {
            "ok": False,
            "error": "CDP niedostępne"
        }

    try:

        result = cdp_call(
            ws,
            1,
            "Page.navigate",
            {
                "url": str(url)
            },
            timeout=20
        )

    finally:

        try:
            ws.close()
        except Exception:
            pass

    time.sleep(1.5)

    return {
        "ok":
            result.get(
                "ok",
                False
            ),
        "tab_id":
            tab["id"],
        "url":
            str(url)
    }


# ============================================================
# CHROME CLICK
# ============================================================

def chrome_click(
    text,
    tab_id=None,
    contains=None
):

    tab = find_tab(
        tab_id,
        contains
    )

    if tab is None:

        return {
            "ok": False,
            "error":
                "Brak istniejącej karty"
        }

    target = json.dumps(
        str(text),
        ensure_ascii=False
    )

    javascript = f"""
(() => {{

    const target =
        {target}.toLowerCase();

    const clean = (v) =>
        (v || "")
        .replace(/\\s+/g, " ")
        .trim();

    const elements =
        Array.from(
            document.querySelectorAll(
                'a,button,input,' +
                '[role="button"]'
            )
        );

    const el =
        elements.find(
            e =>
                clean(
                    e.innerText ||
                    e.value ||
                    e.getAttribute(
                        "aria-label"
                    )
                )
                .toLowerCase()
                .includes(target)
        );

    if (!el) {{

        return {{
            ok: false,
            error:
                "Nie znaleziono: " +
                {target}
        }};

    }}

    el.scrollIntoView({{
        block: "center"
    }});

    el.click();

    return {{
        ok: true,
        clicked:
            clean(
                el.innerText ||
                el.value ||
                el.getAttribute(
                    "aria-label"
                )
            )
    }};

}})()
"""

    return chrome_eval(
        tab,
        javascript
    )


# ============================================================
# CHROME TYPE
# ============================================================

def chrome_type(
    text,
    tab_id=None,
    contains=None
):

    tab = find_tab(
        tab_id,
        contains
    )

    if tab is None:

        return {
            "ok": False,
            "error":
                "Brak istniejącej karty"
        }

    value = json.dumps(
        str(text),
        ensure_ascii=False
    )

    javascript = f"""
(() => {{

    const value =
        {value};

    const el =
        document.activeElement;

    if (!el) {{
        return {{
            ok: false,
            error:
                "Brak aktywnego elementu"
        }};
    }}

    const tag =
        el.tagName.toLowerCase();

    if (
        tag !== "input" &&
        tag !== "textarea" &&
        !el.isContentEditable
    ) {{

        return {{
            ok: false,
            error:
                "Aktywny element nie jest polem"
        }};

    }}

    if (
        el.isContentEditable
    ) {{

        el.textContent =
            value;

    }} else {{

        el.focus();
        el.value =
            value;

    }}

    el.dispatchEvent(
        new Event(
            "input",
            {{
                bubbles: true
            }}
        )
    );

    el.dispatchEvent(
        new Event(
            "change",
            {{
                bubbles: true
            }}
        )
    );

    return {{
        ok: true
    }};

}})()
"""

    return (
        chrome_eval(
            tab,
            javascript
        )
        or {
            "ok": False,
            "error": "Brak wyniku"
        }
    )


# ============================================================
# GEMINI KEYS
# ============================================================

def load_gemini_keys():

    keys = []

    main = read_text(
        GEMINI_KEY_FILE
    ).strip()

    if main:
        keys.append(
            ("main", main)
        )

    for path in sorted(
        GEMINI_KEYS_DIR.glob("*.txt")
    ):

        value = read_text(
            path
        ).strip()

        if value:

            if not any(
                key == value
                for _, key in keys
            ):

                keys.append(
                    (
                        path.name,
                        value
                    )
                )

    env = os.environ.get(
        "GEMINI_API_KEY",
        ""
    ).strip()

    if env:

        if not any(
            key == env
            for _, key in keys
        ):

            keys.append(
                ("environment", env)
            )

    return keys


def gemini_state():

    value = read_json(
        GEMINI_STATE_FILE,
        {}
    )

    if not isinstance(value, dict):
        return {}

    return value


def save_gemini_state(value):

    write_json(
        GEMINI_STATE_FILE,
        value
    )


def mark_quota(key_name):

    state = gemini_state()

    state[key_name] = {
        "status":
            "QUOTA_EXHAUSTED",
        "time":
            time.time()
    }

    save_gemini_state(
        state
    )


def key_disabled(key_name):

    state = gemini_state()

    info = state.get(
        key_name
    )

    if not info:
        return False

    return (
        info.get("status")
        == "QUOTA_EXHAUSTED"
    )


def init_gemini():

    global gemini_clients

    if not GEMINI_LIBRARY_OK:
        return False

    keys = load_gemini_keys()

    if not keys:

        log(
            "GEMINI",
            "Brak klucza API"
        )

        return False

    for name, key in keys:

        if key_disabled(name):
            continue

        try:

            client = genai.Client(
                api_key=key
            )

            gemini_clients[
                name
            ] = client

            log(
                "GEMINI",
                "Klucz dostępny: "
                + name
            )

        except Exception as e:

            log(
                "GEMINI",
                f"{name}: {e}"
            )

    if gemini_clients:

        log(
            "GEMINI",
            "API OK — "
            + GEMINI_MODEL
        )

        return True

    log(
        "GEMINI",
        "Brak aktywnych kluczy"
    )

    return False


def get_gemini_client():

    for name, _ in load_gemini_keys():

        if key_disabled(name):
            continue

        client = gemini_clients.get(
            name
        )

        if client is not None:
            return name, client

    return None, None


# ============================================================
# GEMINI TOOLS
# ============================================================

def _gemini_tools_legacy():

    return [

        {
            "type": "function",
            "name": "termux_mkdir",
            "description": "Utwórz katalog w Termuxie.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"}
                },
                "required": ["path"]
            }
        },

        {
            "type": "function",
            "name": "termux_ls",
            "description": "Wyświetl zawartość katalogu Termuxa.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"}
                }
            }
        },

        {
            "type": "function",
            "name": "termux_write_file",
            "description": "Zapisz plik bezpośrednio w Termuxie. Używaj do dużego kodu.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"}
                },
                "required": ["path", "content"]
            }
        },

        {
            "type": "function",
            "name": "termux_read_file",
            "description": "Odczytaj plik z Termuxa.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "max_bytes": {"type": "integer"}
                },
                "required": ["path"]
            }
        },

        {
            "type": "function",
            "name": "termux_run",
            "description": "Wykonaj krótką komendę w Termuxie.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"}
                },
                "required": ["command"]
            }
        },

        {
            "type": "function",
            "name": "termux_run_background",
            "description": "Uruchom długi proces w tle.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "workdir": {"type": "string"},
                    "log_file": {"type": "string"}
                },
                "required": ["command"]
            }
        },

        {
            "type": "function",
            "name": "termux_processes",
            "description": "Sprawdź procesy Termuxa.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        },

        {
            "type": "function",
            "name": "termux_check_process",
            "description": "Sprawdź czy PID nadal działa.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pid": {"type": "integer"}
                },
                "required": ["pid"]
            }
        },

        {
            "type": "function",
            "name": "termux_stop_process",
            "description": "Zatrzymaj proces po PID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pid": {"type": "integer"}
                },
                "required": ["pid"]
            }
        },

        {
            "type": "function",
            "name": "termux_start_second_session",
            "description": "Uruchom drugą sesję Termuxa.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        },

        {
            "type": "function",
            "name": "termux_file_exists",
            "description": (
                "Szybkie sprawdzenie czy plik lub katalog istnieje "
                "na urządzeniu. Używaj do weryfikacji kroków budowania "
                "(np. czy APK powstał) zamiast wywoływać ls przez "
                "termux_run."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"}
                },
                "required": ["path"]
            }
        },

        {
            "type": "function",
            "name": "termux_delete",
            "description": (
                "Usuń plik lub katalog. UŻYWAJ TEGO zamiast `rm` "
                "przez shell/termux_run — operator zobaczy "
                "dokładnie jaką ścieżkę i ile w niej jest, i musi "
                "to jawnie potwierdzić w terminalu, zanim cokolwiek "
                "zniknie. Jeśli odmówi, dostaniesz błąd — nie "
                "próbuj obchodzić tego inną komendą, zapytaj MAIN "
                "co dalej."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"}
                },
                "required": ["path"]
            }
        },

        {
            "type": "function",
            "name": "termux_check_apk",
            "description": (
                "Sprawdź czy plik pod daną ścieżką jest prawdziwym, "
                "poprawnym APK (istnieje, nie jest pusty, zawiera "
                "AndroidManifest.xml i classes.dex). Używaj PRZED "
                "adb install, żeby nie tracić czasu na instalację "
                "uszkodzonego pliku."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"}
                },
                "required": ["path"]
            }
        },

        {
            "type": "function",
            "name": "termux_patch_file",
            "description": (
                "Podmień DOKŁADNIE JEDNO wystąpienie fragmentu "
                "'search' na 'replace' w istniejącym pliku "
                "projektu (kod gry, build.gradle itp.) — NIE "
                "agent.py. UŻYWAJ TEGO zamiast termux_write_file, "
                "gdy zmieniasz fragment DUŻEGO, już istniejącego "
                "pliku — nie przepisuj całej zawartości od nowa, "
                "to marnuje tokeny i ryzykuje zgubienie reszty "
                "pliku. termux_write_file zostaw dla NOWYCH plików "
                "albo bardzo małych. 'search' musi wystąpić w "
                "pliku dokładnie raz (skopiuj 1:1 z tego, co "
                "wcześniej odczytałeś przez termux_read_file) — "
                "inaczej patch zostanie odrzucony. Dla plików .py "
                "automatycznie sprawdzany jest py_compile z "
                "rollbackiem przy błędzie składni."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "search": {"type": "string"},
                    "replace": {"type": "string"}
                },
                "required": ["path", "search", "replace"]
            }
        },

        {
            "type": "function",
            "name": "ask_deepseek",
            "description": (
                "Zapytaj o KRÓTKĄ podpowiedź, jeśli utknąłeś W "
                "TRAKCIE tego zadania i nie jesteś pewien jak "
                "kontynuować (np. nie wiesz dokładnie jaki "
                "fragment podać jako 'search' do "
                "termux_patch_file, bo zgubiłeś kontekst pliku). "
                "Odpowiedź wraca od razu jako wynik tego narzędzia "
                "— możesz kontynuować zadanie, NIE musisz go "
                "kończyć błędem. Limitowane do "
                "kilku razy na zadanie — nie nadużywaj, najpierw "
                "spróbuj sam (np. termux_read_file, żeby zobaczyć "
                "aktualną zawartość pliku)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"}
                },
                "required": ["question"]
            }
        },

        {
            "type": "function",
            "name": "chrome_tabs",
            "description": "Pobierz listę istniejących kart Chrome.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        },

        {
            "type": "function",
            "name": "chrome_inspect",
            "description": "Sprawdź stan istniejącej karty Chrome.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tab_id": {"type": "string"},
                    "contains": {"type": "string"}
                }
            }
        },

        {
            "type": "function",
            "name": "chrome_open",
            "description": "Otwórz URL w istniejącej karcie Chrome.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "tab_id": {"type": "string"},
                    "contains": {"type": "string"}
                },
                "required": ["url"]
            }
        },

        {
            "type": "function",
            "name": "chrome_click",
            "description": "Kliknij element Chrome po tekście.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "tab_id": {"type": "string"},
                    "contains": {"type": "string"}
                },
                "required": ["text"]
            }
        },

        {
            "type": "function",
            "name": "chrome_type",
            "description": "Wpisz tekst do aktywnego pola Chrome.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "tab_id": {"type": "string"},
                    "contains": {"type": "string"}
                },
                "required": ["text"]
            }
        },

        {
            "type": "function",
            "name": "android_state",
            "description": "Sprawdź aktualny interfejs Androida.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        },

        {
            "type": "function",
            "name": "android_click",
            "description": "Kliknij element Androida po tekście.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"}
                },
                "required": ["text"]
            }
        },

        {
            "type": "function",
            "name": "android_tap",
            "description": "Kliknij Androida po współrzędnych.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"}
                },
                "required": ["x", "y"]
            }
        },

        {
            "type": "function",
            "name": "android_type",
            "description": "Wpisz tekst do aktywnego pola Androida.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"}
                },
                "required": ["text"]
            }
        },

        {
            "type": "function",
            "name": "android_press",
            "description": "Naciśnij klawisz Androida.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"}
                },
                "required": ["key"]
            }
        },

        {
            "type": "function",
            "name": "android_click_resource",
            "description": (
                "Kliknij element Androida po resource-id "
                "(dokładniejsze niż android_click po tekście — "
                "użyj gdy przycisk nie ma czytelnego tekstu, "
                "np. ikony w grze)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "resource": {"type": "string"}
                },
                "required": ["resource"]
            }
        },

        {
            "type": "function",
            "name": "android_swipe",
            "description": (
                "Wykonaj gest przesunięcia (swipe) po "
                "współrzędnych — do list, kart, sterowania grą."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "x1": {"type": "integer"},
                    "y1": {"type": "integer"},
                    "x2": {"type": "integer"},
                    "y2": {"type": "integer"},
                    "duration": {"type": "number"}
                },
                "required": ["x1", "y1", "x2", "y2"]
            }
        },

        {
            "type": "function",
            "name": "android_screenshot",
            "description": (
                "Zrób prawdziwy zrzut ekranu (PNG) i zapisz go "
                "na dysku. Używaj do fizycznego potwierdzenia, że "
                "coś faktycznie wyświetla się na ekranie (np. że "
                "gra się uruchomiła), a nie tylko że proces działa."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"}
                }
            }
        },

        {
            "type": "function",
            "name": "android_launch_app",
            "description": (
                "Uruchom zainstalowaną aplikację po nazwie pakietu "
                "(np. po adb install .apk) — działa dla DOWOLNEJ "
                "aplikacji na urządzeniu, nie tylko dla tej "
                "budowanej w projekcie. Używaj do faktycznego "
                "odpalenia zbudowanej gry PRZED zrobieniem "
                "android_screenshot jako dowodu, że coś się "
                "renderuje na ekranie."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "package": {"type": "string"}
                },
                "required": ["package"]
            }
        },

        {
            "type": "function",
            "name": "android_run_in_new_window",
            "description": (
                "Uruchom komendę w NOWYM, widocznym oknie/sesji "
                "Termuksa (nie w tle, nie w bieżącej sesji) — "
                "użyj, gdy chcesz żeby proces (np. serwer, gra, "
                "długi build) był widoczny OSOBNO, a nie zmieszany "
                "z logiem głównej sesji agenta. Różni się od "
                "termux_start_second_session tym, że od razu "
                "odpala w nowym oknie KONKRETNĄ komendę, nie samo "
                "puste okno. Wymaga allow-external-apps w "
                "termux.properties — agent włącza to samo, ale "
                "pierwsza próba może wymagać restartu Termuksa "
                "(patrz needs_termux_restart w wyniku)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "background": {"type": "boolean"}
                },
                "required": ["command"]
            }
        },

        {
            "type": "function",
            "name": "android_install_apk",
            "description": (
                "Zainstaluj plik .apk przez `adb install` z "
                "poprawnym rozpoznaniem sukcesu/porażki (w "
                "odróżnieniu od surowego shell — `adb install` "
                "potrafi zwrócić kod 0 mimo że instalacja się nie "
                "powiodła, błąd jest tylko w tekście). Sprawdź "
                "najpierw termux_check_apk. Jeśli dostaniesz "
                "failure_reason=INSTALL_FAILED_UPDATE_INCOMPATIBLE "
                "— użyj najpierw android_uninstall_app, potem "
                "spróbuj ponownie."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "reinstall": {"type": "boolean"}
                },
                "required": ["path"]
            }
        },

        {
            "type": "function",
            "name": "android_uninstall_app",
            "description": (
                "Odinstaluj aplikację po nazwie pakietu — używaj "
                "przed czystą reinstalacją, gdy android_install_apk "
                "zwróci INSTALL_FAILED_UPDATE_INCOMPATIBLE albo "
                "INSTALL_FAILED_VERSION_DOWNGRADE (zwykłe -r wtedy "
                "nie wystarcza)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "package": {"type": "string"}
                },
                "required": ["package"]
            }
        },

        {
            "type": "function",
            "name": "android_logcat",
            "description": (
                "Zrzuć ostatnie logi systemowe Androida (adb "
                "logcat -d — zrzuca i kończy, NIE zawiesza się). "
                "UŻYWAJ TEGO, gdy aplikacja się zainstalowała i "
                "uruchomiła (android_launch_app), ale nic nie "
                "widać na zrzucie ekranu albo podejrzewasz crash — "
                "to jedyny sposób, żeby zobaczyć prawdziwy błąd "
                "(stack trace), zamiast zgadywać z samego zrzutu "
                "ekranu. Podaj 'package', żeby przefiltrować po "
                "PID tej aplikacji. Pole 'crash_detected' w wyniku "
                "mówi wprost, czy w logu jest FATAL EXCEPTION."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "package": {"type": "string"},
                    "lines": {"type": "integer"}
                }
            }
        },

        {
            "type": "function",
            "name": "shell",
            "description": "Wykonaj komendę w Termuxie.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"}
                },
                "required": ["command"]
            }
        }
    ]

# ============================================================
# TOOL DISPATCH
# ============================================================



# ============================================================
# TERMUX DIRECT TOOLS
# ============================================================




def gemini_tools():
    """
    Adapter narzędzi dla Gemini Interactions API.

    Oryginalne deklaracje narzędzi są zachowane.
    FunctionDeclaration jest konwertowane do zwykłego dict.
    """

    declarations = _gemini_tools_legacy()

    result = []

    for declaration in declarations:

        if isinstance(declaration, dict):
            data = dict(declaration)

        elif hasattr(declaration, "model_dump"):
            data = declaration.model_dump(
                exclude_none=True,
                by_alias=True
            )

        elif hasattr(declaration, "to_dict"):
            data = declaration.to_dict()

        else:
            data = {
                "name": getattr(
                    declaration,
                    "name",
                    None
                ),
                "description": getattr(
                    declaration,
                    "description",
                    None
                ),
                "parameters": getattr(
                    declaration,
                    "parameters",
                    None
                ),
            }

        # ----------------------------------------------------
        # Interactions API
        # ----------------------------------------------------

        data = {
            k: v
            for k, v in data.items()
            if v is not None
        }

        data["type"] = "function"

        # ----------------------------------------------------
        # parameters może nadal być obiektem Schema.
        # Zamieniamy go na zwykły dict.
        # ----------------------------------------------------

        parameters = data.get("parameters")

        if parameters is not None:

            if hasattr(parameters, "model_dump"):
                parameters = parameters.model_dump(
                    exclude_none=True,
                    by_alias=True
                )

            elif hasattr(parameters, "to_dict"):
                parameters = parameters.to_dict()

            elif not isinstance(parameters, dict):
                parameters = {
                    "type": getattr(
                        parameters,
                        "type",
                        "object"
                    ),
                    "properties": getattr(
                        parameters,
                        "properties",
                        {}
                    ),
                }

            data["parameters"] = parameters

        result.append(data)

    # Narzędzia dopisane przez DeepSeek jako osobne pliki w
    # custom_tools/ (patrz load_custom_tools()) — dołączane obok
    # wbudowanych, bez modyfikowania _gemini_tools_legacy().
    for name, entry in CUSTOM_TOOLS.items():

        result.append({
            "type": "function",
            "name": name,
            "description": entry.get("description", ""),
            "parameters": entry.get(
                "parameters",
                {"type": "object", "properties": {}}
            )
        })

    print(
        "[GEMINI] Interactions tools:",
        len(result),
        "(w tym niestandardowe:",
        str(len(CUSTOM_TOOLS)) + ")"
    )

    return result
def termux_mkdir(path):
    try:
        p = Path(str(path)).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        _track_project_path(p)
        return {
            "ok": True,
            "path": str(p)
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e)
        }


def termux_ls(path="."):
    try:
        p = Path(str(path or ".")).expanduser()

        if not p.exists():
            return {
                "ok": False,
                "error": "Ścieżka nie istnieje",
                "path": str(p)
            }

        items = []

        for x in sorted(
            p.iterdir(),
            key=lambda z: z.name.lower()
        ):
            try:
                size = x.stat().st_size if x.is_file() else None
            except Exception:
                size = None

            items.append({
                "name": x.name,
                "type": "directory" if x.is_dir() else "file",
                "size": size
            })

        return {
            "ok": True,
            "path": str(p),
            "items": items[:500]
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e)
        }


def termux_write_file(path, content):
    try:
        p = Path(str(path)).expanduser()

        p.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        data = str(
            content if content is not None else ""
        )

        p.write_text(
            data,
            encoding="utf-8"
        )

        _track_project_path(p)

        return {
            "ok": True,
            "path": str(p),
            "bytes": len(
                data.encode("utf-8")
            )
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e)
        }


def termux_read_file(path, max_bytes=20000):
    try:
        p = Path(str(path)).expanduser()

        if not p.exists():
            return {
                "ok": False,
                "error": "Plik nie istnieje"
            }

        data = p.read_bytes()

        limit = int(
            max_bytes or 20000
        )

        truncated = len(data) > limit

        data = data[:limit]

        return {
            "ok": True,
            "path": str(p),
            "content": data.decode(
                "utf-8",
                errors="replace"
            ),
            "truncated": truncated
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e)
        }


def _looks_long_running(command):
    """
    Czy komenda pasuje do wzorca operacji, które zwykle trwają
    dłużej niż COMMAND_TIMEOUT (build, instalacja pakietów,
    pobieranie, kompilacja)?

    To jest heurystyka, nie wyrocznia — celowo prosta i czytelna,
    żeby łatwo dopisać kolejne wzorce w LONG_RUNNING_HINTS,
    zamiast polegać wyłącznie na tym, że Gemini za każdym razem
    poprawnie zgadnie termux_run vs termux_run_background.
    """

    lowered = str(command or "").lower()

    return any(
        hint in lowered
        for hint in LONG_RUNNING_HINTS
    )


def termux_run(command):
    try:
        command_str = str(command or "")

        if _looks_long_running(command_str):

            bg = termux_run_background(command_str)

            if bg.get("ok"):
                bg["status"] = "AUTO_BACKGROUNDED"
                bg["reason"] = (
                    "Komenda rozpoznana jako długotrwała "
                    "(pasuje do wzorca gradle/npm/apt/pip/git/"
                    "curl/wget/unzip/tar/make itp.) — uruchomiona "
                    "w tle automatycznie, zamiast czekać na "
                    "COMMAND_TIMEOUT=" + str(COMMAND_TIMEOUT) + "s. "
                    "Monitoruj przez termux_check_process(pid) i "
                    "termux_read_file(log_file). NIE uruchamiaj "
                    "tej samej komendy ponownie przez termux_run."
                )

            return bg

        result = execute_shell(command_str)

        if (
            not result.get("ok")
            and result.get("error") == "Timeout"
        ):
            # Komenda NIE została rozpoznana jako długa, a mimo to
            # przekroczyła limit. Celowo NIE uruchamiamy jej
            # ponownie automatycznie — mogła już zdążyć częściowo
            # zmienić stan (np. częściowe pobieranie/instalacja),
            # a bezmyślne powtórzenie tej samej komendy to dokładnie
            # ta pętla, która wcześniej prowadziła donikąd.
            # Zamiast tego zwracamy WSZYSTKO, co przechwyciliśmy,
            # plus jednoznaczną, praktyczną podpowiedź.

            result["suggested_next_step"] = (
                "Komenda przekroczyła "
                + str(result.get("timeout"))
                + "s i została zatrzymana. NIE uruchamiaj jej "
                "ponownie tym samym poleceniem przez termux_run. "
                "Najpierw sprawdź faktyczny stan (termux_ls / "
                "termux_read_file na spodziewany plik wynikowy). "
                "Jeżeli operacja rzeczywiście musi trwać dłużej, "
                "uruchom ją przez termux_run_background i "
                "monitoruj przez termux_check_process / "
                "termux_processes zamiast wołać ją ponownie "
                "synchronicznie."
            )

        return result

    except Exception as e:
        return {
            "ok": False,
            "error": str(e)
        }


def termux_run_background(
    command,
    workdir=None,
    log_file=None
):
    try:
        command = str(
            command or ""
        ).strip()

        if not command:
            return {
                "ok": False,
                "error": "Pusta komenda"
            }

        if _looks_like_delete_command(command):

            if not _confirm_destructive_action(
                "Komenda usuwająca (w tle): " + command
            ):
                return {
                    "ok": False,
                    "error": (
                        "Operacja usuwania odrzucona (brak "
                        "potwierdzenia operatora)."
                    ),
                    "command": command,
                    "blocked_by_safety_gate": True
                }

        cwd = None

        if workdir:
            cwd = str(
                Path(str(workdir)).expanduser()
            )

            Path(cwd).mkdir(
                parents=True,
                exist_ok=True
            )

        if log_file:
            log = Path(
                str(log_file)
            ).expanduser()
        else:
            log = Path(
                "/data/data/com.termux/files/home/"
                "agent_background.log"
            )

        log.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        import subprocess

        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=open(
                log,
                "a",
                encoding="utf-8"
            ),
            stderr=subprocess.STDOUT,
            start_new_session=True
        )

        return {
            "ok": True,
            "pid": proc.pid,
            "command": command,
            "workdir": cwd,
            "log_file": str(log)
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e)
        }


def termux_processes():
    try:
        import subprocess

        r = subprocess.run(
            ["ps"],
            capture_output=True,
            text=True,
            timeout=10
        )

        return {
            "ok": r.returncode == 0,
            "stdout": r.stdout[:15000],
            "stderr": r.stderr[:4000]
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e)
        }


def termux_check_process(pid):
    try:
        pid = int(pid)

        import os

        os.kill(pid, 0)

        return {
            "ok": True,
            "running": True,
            "pid": pid
        }

    except ProcessLookupError:
        return {
            "ok": True,
            "running": False,
            "pid": pid
        }

    except PermissionError:
        return {
            "ok": True,
            "running": True,
            "pid": pid
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e)
        }


def termux_stop_process(pid):
    try:
        import os

        pid = int(pid)

        os.kill(
            pid,
            15
        )

        return {
            "ok": True,
            "pid": pid
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e)
        }


def termux_start_second_session():
    try:
        import subprocess

        r = subprocess.run(
            [
                "am",
                "start",
                "-n",
                "com.termux/.app.TermuxActivity"
            ],
            capture_output=True,
            text=True,
            timeout=10
        )

        return {
            "ok": r.returncode == 0,
            "stdout": r.stdout[:3000],
            "stderr": r.stderr[:3000]
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e)
        }


def termux_file_exists(path):
    """
    Szybkie sprawdzenie czy plik/katalog istnieje.

    Tańsze niż termux_run("ls ...") — nie uruchamia powłoki,
    tylko sprawdza przez subprocess test. Używaj do weryfikacji
    wyników kroków budowania (np. "czy classes.dex powstał?")
    zamiast wywoływać termux_run za każdym razem.
    """

    path = str(path or "").strip()

    if not path:
        return {
            "ok": False,
            "exists": False,
            "error": "Pusta ścieżka."
        }

    result = execute_shell(
        "test -e " + path
        + " && echo EXISTS || echo MISSING",
        timeout=10
    )

    exists = "EXISTS" in result.get("stdout", "")

    return {
        "ok": True,
        "exists": exists,
        "path": path
    }


def termux_delete(path):
    """
    Usuwa plik lub katalog PO POTWIERDZENIU operatora w terminalu.

    To zamierzony, jawny sposób usuwania czegokolwiek — czystszy
    niż `rm` przez shell/termux_run (też objęte tą samą bramką
    bezpieczeństwa, patrz _looks_like_delete_command()), bo
    operator widzi dokładnie JAKĄ ścieżkę i ile w niej jest, a nie
    surową komendę powłoki.
    """

    path = str(path or "").strip()

    if not path:
        return {
            "ok": False,
            "error": "Pusta ścieżka."
        }

    target = Path(path).expanduser()

    if not target.exists():
        return {
            "ok": False,
            "error": "Ścieżka nie istnieje: " + str(target)
        }

    # Twarda podłoga bezpieczeństwa — niezależna od potwierdzenia
    # operatora, bo to niemal na pewno pomyłka (Gemini pomyliło
    # ścieżkę), a szkoda byłaby katastrofalna.
    try:
        resolved = target.resolve()
        home = Path.home().resolve()
    except Exception:
        resolved = target
        home = Path.home()

    if resolved == home or str(resolved) == "/":
        return {
            "ok": False,
            "error": (
                "Odmowa: cel to katalog domowy albo root systemu "
                "plików — to prawie na pewno pomyłka, nie zostanie "
                "wykonane nawet z potwierdzeniem."
            )
        }

    is_dir = target.is_dir()

    try:
        if is_dir:
            item_count = sum(1 for _ in target.rglob("*"))
            size_info = "katalog, " + str(item_count) + " elementów wewnątrz"
        else:
            size_info = "plik, " + str(target.stat().st_size) + " B"
    except Exception:
        size_info = "nieznany rozmiar"

    if not _confirm_destructive_action(
        "USUNIĘCIE: " + str(target) + " (" + size_info + ")"
    ):
        return {
            "ok": False,
            "error": (
                "Usunięcie odrzucone — brak potwierdzenia operatora."
            ),
            "path": str(target)
        }

    try:
        if is_dir:
            shutil.rmtree(target)
        else:
            target.unlink()

    except Exception as e:
        return {
            "ok": False,
            "error": "Błąd podczas usuwania: " + str(e)
        }

    log(
        "TERMUX",
        "Usunięto (potwierdzone przez operatora): " + str(target)
    )

    return {
        "ok": True,
        "action": "delete",
        "path": str(target),
        "was_directory": is_dir
    }


def termux_check_apk(path):
    """
    Sprawdza czy plik to prawdziwy APK gotowy do instalacji:
    - istnieje i nie jest pusty (> 1KB),
    - jest archiwum ZIP z AndroidManifest.xml i classes.dex.

    Używaj PRZED adb install — oszczędza czas gdy build produkuje
    pusty lub uszkodzony plik.
    """

    path = str(path or "").strip()

    if not path:
        return {
            "ok": False,
            "valid": False,
            "error": "Pusta ścieżka APK."
        }

    size_result = execute_shell(
        "wc -c < " + path
        + " 2>/dev/null || echo 0",
        timeout=10
    )

    try:
        size = int(
            size_result.get("stdout", "0").strip()
        )
    except Exception:
        size = 0

    if size < 1024:
        return {
            "ok": False,
            "valid": False,
            "path": path,
            "size_bytes": size,
            "error": (
                "APK za mały ("
                + str(size)
                + "B) — pusty lub uszkodzony."
            )
        }

    zip_result = execute_shell(
        "unzip -l " + path
        + " 2>/dev/null | grep -E"
        + " 'AndroidManifest|classes.dex'",
        timeout=20
    )

    stdout = zip_result.get("stdout", "")

    has_manifest = "AndroidManifest" in stdout
    has_dex = "classes.dex" in stdout

    valid = has_manifest and has_dex

    return {
        "ok": valid,
        "valid": valid,
        "path": path,
        "size_bytes": size,
        "has_AndroidManifest": has_manifest,
        "has_classes_dex": has_dex,
        "error": (
            None if valid else
            "Brak AndroidManifest.xml lub classes.dex w APK."
        )
    }


def termux_patch_file(path, search, replace):
    """
    Podmienia DOKŁADNIE JEDNO wystąpienie fragmentu `search` na
    `replace` w istniejącym pliku PROJEKTU (kod gry, build.gradle
    itp. — NIE agent.py, to osobny mechanizm od
    apply_patch_from_fixer_text, który naprawia samego agenta).

    Używaj zamiast termux_write_file, gdy zmieniasz fragment
    DUŻEGO, już istniejącego pliku — nie trzeba przepisywać całej
    zawartości (oszczędność tokenów), a ryzyko przypadkowego
    "zgubienia" reszty pliku przy przepisywaniu od zera znika.

    Bezpieczeństwo identyczne jak przy naprawie agenta: `search`
    musi wystąpić w pliku dokładnie raz (inaczej patch jest
    odrzucony), zawsze robiony jest backup obok pliku, a dla
    plików .py dodatkowo sprawdzane jest py_compile z automatycznym
    rollbackiem przy błędzie składni.
    """

    path = str(path or "").strip()

    if not path:
        return {
            "ok": False,
            "error": "Pusta ścieżka."
        }

    target = Path(path).expanduser()

    if not target.exists():
        return {
            "ok": False,
            "error": "Plik nie istnieje: " + str(target)
        }

    try:
        source = target.read_text(
            encoding="utf-8",
            errors="ignore"
        )
    except Exception as e:
        return {
            "ok": False,
            "error": "Nie udało się odczytać pliku: " + str(e)
        }

    search = str(search or "")
    replace = str(replace if replace is not None else "")

    if not search:
        return {
            "ok": False,
            "error": "Puste 'search' — nic do znalezienia."
        }

    occurrences = source.count(search)

    if occurrences == 0:
        return {
            "ok": False,
            "error": (
                "Fragment 'search' nie występuje w pliku dokładnie "
                "1:1 (sprawdź wcięcia i białe znaki — muszą się "
                "zgadzać co do znaku)."
            )
        }

    if occurrences > 1:
        return {
            "ok": False,
            "error": (
                "Fragment 'search' występuje "
                + str(occurrences)
                + " razy — patch odrzucony, musi być jednoznaczny "
                "(dodaj więcej kontekstu do 'search')."
            )
        }

    backup_path = target.with_name(
        target.name
        + ".bak_"
        + datetime.now().strftime("%Y%m%d_%H%M%S")
    )

    try:
        backup_path.write_text(source, encoding="utf-8")
    except Exception as e:
        return {
            "ok": False,
            "error": "Nie udało się utworzyć backupu: " + str(e)
        }

    new_source = source.replace(search, replace, 1)

    try:
        target.write_text(new_source, encoding="utf-8")
    except Exception as e:
        return {
            "ok": False,
            "error": "Nie udało się zapisać patcha: " + str(e),
            "backup": str(backup_path)
        }

    if target.suffix == ".py":

        try:
            compile_check = subprocess.run(
                [sys.executable, "-m", "py_compile", str(target)],
                capture_output=True,
                text=True,
                timeout=20
            )

            if compile_check.returncode != 0:

                target.write_text(source, encoding="utf-8")

                return {
                    "ok": False,
                    "rolled_back": True,
                    "error": (
                        "py_compile nie przeszedł po patchu — "
                        "przywrócono poprzednią wersję pliku."
                    ),
                    "compile_error": short(
                        compile_check.stderr, 1500
                    ),
                    "backup": str(backup_path)
                }

        except Exception:
            # py_compile samo w sobie niedostępne — nie blokuj
            # patcha z tego powodu, tylko pomiń dodatkową walidację.
            pass

    _track_project_path(target)

    return {
        "ok": True,
        "path": str(target),
        "backup": str(backup_path),
        "bytes_before": len(source.encode("utf-8")),
        "bytes_after": len(new_source.encode("utf-8"))
    }


def ask_deepseek_hint(question):
    """
    Pozwala Gemini poprosić o krótką podpowiedź W TRAKCIE
    wykonywania zadania, zamiast kończyć cały TASK błędem, gdy
    czegoś nie wie (typowy przypadek: nie jest pewien dokładnego
    fragmentu kodu do podania jako 'search' w termux_patch_file,
    bo zgubił kontekst pliku).

    Odpowiedzi udziela sesja CODE_FIXER — już istniejąca, skupiona
    na kodzie — NIE tworzymy dla tego osobnej, nowej persystentnej
    sesji (to podniosłoby liczbę równoległych rozmów na koncie
    DeepSeek, dokładnie to, co ograniczyliśmy w consult_team()).

    Limit wywołań na TASK pilnowany jest w gemini_execute_task()
    (ASK_DEEPSEEK_MAX_PER_TASK) — to awaryjna konsultacja, nie
    zamiennik zwykłego przepływu MAIN -> team -> TASK.
    """

    question = str(question or "").strip()

    if not question:
        return {
            "ok": False,
            "error": "Puste pytanie."
        }

    answer = deepseek(
        "CODE_FIXER",
        "Gemini (wykonawca) utknął W TRAKCIE wykonywania zadania "
        "i prosi o krótką podpowiedź — NIE pełny patch w formacie "
        "SZUKAJ/ZAMIEŃ, po prostu odpowiedz krótko i konkretnie na "
        "poniższe pytanie, żeby mógł kontynuować:\n\n"
        + question
    )

    return {
        "ok": True,
        "answer": short(str(answer or ""), 3000)
    }


# ============================================================
# NOWE NARZĘDZIA PROJEKTOWANE PRZEZ DEEPSEEK (custom_tools/)
# ============================================================
#
# Kontrakt pliku ~/agent/custom_tools/<cokolwiek>.py:
#
#   TOOL_NAME = "moje_narzedzie"          # str, unikalna nazwa
#   TOOL_DESCRIPTION = "Co to robi."      # str, dla Gemini
#   TOOL_PARAMETERS = {                   # JSON Schema, jak reszta
#       "type": "object",
#       "properties": {"x": {"type": "string"}},
#       "required": ["x"]
#   }
#
#   def run(x):
#       return {"ok": True, "wynik": ...}
#
# Plik NIGDY nie dotyka agent.py. Jest wykrywany i ładowany
# automatycznie (load_custom_tools() jest wołane raz na krok
# w run_agent() — nowe/zmienione pliki pojawiają się bez
# restartu). Błąd składni, brak wymaganych atrybutów albo
# kolizja nazwy z istniejącym narzędziem = plik jest POMIJANY
# z jasnym logiem, nigdy nie wywraca reszty agenta.

CUSTOM_TOOLS = {}

_custom_tool_file_state = {}


def _builtin_tool_names():
    return {
        decl.get("name")
        for decl in _gemini_tools_legacy()
        if isinstance(decl, dict)
    }


def _load_one_custom_tool(path):
    """
    Wczytuje i waliduje JEDEN plik z custom_tools/.

    Zwraca (name, entry) przy sukcesie, albo (None, powód_błędu).
    """

    try:
        compile_check = subprocess.run(
            [sys.executable, "-m", "py_compile", str(path)],
            capture_output=True,
            text=True,
            timeout=15
        )

        if compile_check.returncode != 0:
            return None, (
                "Błąd składni: "
                + short(compile_check.stderr, 500)
            )

    except Exception as e:
        return None, "py_compile nie powiódł się: " + str(e)

    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "custom_tool_" + path.stem + "_" + uuid.uuid4().hex[:6],
            str(path)
        )

        module = importlib.util.module_from_spec(spec)

        spec.loader.exec_module(module)

    except Exception as e:
        return None, (
            "Błąd importu: "
            + type(e).__name__
            + ": "
            + str(e)
        )

    tool_name = getattr(module, "TOOL_NAME", None)
    description = getattr(module, "TOOL_DESCRIPTION", None)
    parameters = getattr(module, "TOOL_PARAMETERS", None)
    run_fn = getattr(module, "run", None)

    if not isinstance(tool_name, str) or not tool_name.strip():
        return None, "Brak poprawnego TOOL_NAME (str)."

    if not isinstance(description, str) or not description.strip():
        return None, "Brak poprawnego TOOL_DESCRIPTION (str)."

    if not isinstance(parameters, dict):
        return None, "Brak poprawnego TOOL_PARAMETERS (dict)."

    if not callable(run_fn):
        return None, "Brak funkcji run(...)."

    if tool_name in _builtin_tool_names():
        return None, (
            "Nazwa '"
            + tool_name
            + "' koliduje z wbudowanym narzędziem."
        )

    existing = CUSTOM_TOOLS.get(tool_name)

    if existing and existing.get("source_file") != str(path):
        return None, (
            "Nazwa '"
            + tool_name
            + "' już jest zajęta przez inne narzędzie z pliku "
            + str(existing.get("source_file"))
        )

    return tool_name, {
        "description": description,
        "parameters": parameters,
        "run": run_fn,
        "source_file": str(path)
    }


def load_custom_tools():
    """
    Skanuje CUSTOM_TOOLS_DIR i (re)ładuje pliki, których mtime się
    zmienił od poprzedniego wywołania. Usuwa z rejestru narzędzia,
    których plik źródłowy zniknął. Tanie do wołania na każdym
    kroku run_agent() — bez zmian w katalogu to tylko stat() na
    plikach.
    """

    try:
        current_files = sorted(
            CUSTOM_TOOLS_DIR.glob("*.py")
        )
    except Exception:
        return

    current_paths = {str(p) for p in current_files}

    # Usuń narzędzia, których plik zniknął.
    for name in list(CUSTOM_TOOLS.keys()):

        source = CUSTOM_TOOLS[name].get("source_file")

        if source not in current_paths:
            del CUSTOM_TOOLS[name]
            _custom_tool_file_state.pop(source, None)

    for path in current_files:

        path_str = str(path)

        try:
            mtime = path.stat().st_mtime
        except Exception:
            continue

        if _custom_tool_file_state.get(path_str) == mtime:
            # Nic się nie zmieniło od ostatniego razu.
            continue

        _custom_tool_file_state[path_str] = mtime

        # Usuń starą wersję tego narzędzia (jeśli to reload pliku),
        # zanim spróbujemy załadować nową — unika sytuacji, w
        # której zepsuty reload zostawia nieaktualną, ale wciąż
        # "działającą" starą wersję pod tą samą nazwą.
        for existing_name in list(CUSTOM_TOOLS.keys()):
            if CUSTOM_TOOLS[existing_name].get(
                "source_file"
            ) == path_str:
                del CUSTOM_TOOLS[existing_name]

        name, result = _load_one_custom_tool(path)

        if name is None:

            log(
                "CUSTOM_TOOL",
                "ODRZUCONO "
                + path.name
                + ": "
                + str(result)
            )

            log_event(
                "custom_tool_rejected",
                {"file": path_str, "reason": str(result)}
            )

            continue

        CUSTOM_TOOLS[name] = result

        log(
            "CUSTOM_TOOL",
            "Załadowano nowe narzędzie: "
            + name
            + " (" + path.name + ")"
        )

        log_event(
            "custom_tool_loaded",
            {"tool": name, "file": path_str}
        )


def dispatch_tool(
    name,
    args
):
    """
    Cienki wrapper wokół _dispatch_tool_inner(): dodaje pomiar
    czasu i zapis do agent_events.jsonl, nie zmieniając samej
    logiki wywoływania narzędzi (ta została w _dispatch_tool_inner
    bez zmian).
    """

    args = args or {}

    if not isinstance(args, dict):
        args = {}

    started = datetime.now()

    result = _dispatch_tool_inner(
        name,
        args
    )

    duration_s = round(
        (datetime.now() - started).total_seconds(),
        2
    )

    if isinstance(result, dict):

        result.setdefault(
            "duration_s",
            duration_s
        )

        # Sygnatura tej dokładnej czynności — używana przez
        # run_next_task() do wykrywania powtarzających się porażek
        # NIEZALEŻNIE od tego, jak MAIN akurat sformułował TASK.
        ok = result.get("ok")

        if ok is True:
            reset_tool_attempts(name, args)

    log_event(
        "tool_call",
        {
            "tool": name,
            "arguments": args,
            "ok": (
                result.get("ok")
                if isinstance(result, dict) else None
            ),
            "status": (
                result.get("status")
                if isinstance(result, dict) else None
            ),
            "duration_s": duration_s,
            "result_summary": short(
                json.dumps(
                    result,
                    ensure_ascii=False,
                    default=str
                ),
                1500
            )
        }
    )

    return result


# Częste, wiarygodnie brzmiące nazwy narzędzi, które słabszy model
# (Gemini flash-lite) czasem "wymyśla" zamiast prawdziwej nazwy —
# argumenty pokrywają się z narzędziem docelowym, więc bezpieczne
# jest ciche przekierowanie zamiast marnowania całego TASK-u na
# "Nieznane narzędzie: ...".
_TOOL_NAME_ALIASES = {
    "termux_check_pid": "termux_check_process",
    "termux_kill_process": "termux_stop_process",
    "termux_kill_pid": "termux_stop_process",
    "termux_run_bg": "termux_run_background",
    "termux_background_run": "termux_run_background",
    "termux_exists": "termux_file_exists",
    "termux_file_exist": "termux_file_exists",
    "android_launch": "android_launch_app",
    "android_open_app": "android_launch_app",
    "android_start_app": "android_launch_app",
    "android_run_new_window": "android_run_in_new_window",
    "android_new_window": "android_run_in_new_window",
}


def _dispatch_tool_inner(
    name,
    args
):

    aliased = _TOOL_NAME_ALIASES.get(name)

    if aliased:

        log(
            "GEMINI",
            "Alias narzędzia: '"
            + name
            + "' -> '"
            + aliased
            + "'"
        )

        name = aliased

    try:

        # ====================================================
        # TERMUX
        # ====================================================

        if name == "termux_mkdir":
            return termux_mkdir(
                args.get("path")
            )

        if name == "termux_ls":
            return termux_ls(
                args.get("path", ".")
            )

        if name == "termux_write_file":
            return termux_write_file(
                args.get("path"),
                args.get("content", "")
            )

        if name == "termux_read_file":
            return termux_read_file(
                args.get("path"),
                args.get("max_bytes", 20000)
            )

        if name == "termux_run":
            return termux_run(
                args.get("command", "")
            )

        if name == "termux_run_background":
            return termux_run_background(
                args.get("command", ""),
                args.get("workdir"),
                args.get("log_file")
            )

        if name == "termux_processes":
            return termux_processes()

        if name == "termux_check_process":
            return termux_check_process(
                args.get("pid")
            )

        if name == "termux_stop_process":
            return termux_stop_process(
                args.get("pid")
            )

        if name == "termux_start_second_session":
            return termux_start_second_session()

        if name == "termux_file_exists":
            return termux_file_exists(
                args.get("path", "")
            )

        if name == "termux_delete":
            return termux_delete(
                args.get("path", "")
            )

        if name == "termux_check_apk":
            return termux_check_apk(
                args.get("path", "")
            )

        if name == "termux_patch_file":
            return termux_patch_file(
                args.get("path", ""),
                args.get("search", ""),
                args.get("replace", "")
            )

        if name == "ask_deepseek":
            return ask_deepseek_hint(
                args.get("question", "")
            )

        # ====================================================
        # SHELL
        # ====================================================

        if name == "shell":
            return execute_shell(
                args.get("command", "")
            )

        # ====================================================
        # ANDROID ALIASES
        # ====================================================

        # Gemini: android_state
        # Python: android_summary
        if name == "android_state":

            fn = globals().get(
                "android_summary"
            )

            if callable(fn):
                return fn()

            return {
                "ok": False,
                "error":
                    "Brak implementacji android_summary()."
            }

        # Gemini: android_click
        # Python: android_click_text
        if name == "android_click":

            fn = globals().get(
                "android_click_text"
            )

            if not callable(fn):
                return {
                    "ok": False,
                    "error":
                        "Brak implementacji android_click_text()."
                }

            text = (
                args.get("text")
                or args.get("label")
                or args.get("content_desc")
            )

            if text is None:
                return {
                    "ok": False,
                    "error":
                        "android_click wymaga text.",
                    "arguments": args
                }

            return fn(text)

        # Gemini: android_tap
        # Python: android_tap
        if name == "android_tap":

            fn = globals().get(
                "android_tap"
            )

            if not callable(fn):
                return {
                    "ok": False,
                    "error":
                        "Brak implementacji android_tap()."
                }

            x = args.get("x")
            y = args.get("y")

            if x is None or y is None:
                return {
                    "ok": False,
                    "error":
                        "android_tap wymaga x i y.",
                    "arguments": args
                }

            return fn(
                int(x),
                int(y)
            )

        # Gemini: android_type
        # Python: android_type
        if name == "android_type":

            fn = globals().get(
                "android_type"
            )

            if not callable(fn):
                return {
                    "ok": False,
                    "error":
                        "Brak implementacji android_type()."
                }

            text = args.get("text")

            if text is None:
                return {
                    "ok": False,
                    "error":
                        "android_type wymaga text.",
                    "arguments": args
                }

            return fn(text)

        # Gemini: android_press
        # Python: android_press
        if name == "android_press":

            fn = globals().get(
                "android_press"
            )

            if not callable(fn):
                return {
                    "ok": False,
                    "error":
                        "Brak implementacji android_press()."
                }

            key = args.get(
                "key"
            )

            if key is None:
                return {
                    "ok": False,
                    "error":
                        "android_press wymaga key.",
                    "arguments": args
                }

            return fn(key)

        # Gemini: android_click_resource
        # Python: android_click_resource
        if name == "android_click_resource":

            fn = globals().get(
                "android_click_resource"
            )

            if not callable(fn):
                return {
                    "ok": False,
                    "error":
                        "Brak implementacji android_click_resource()."
                }

            resource = args.get("resource")

            if resource is None:
                return {
                    "ok": False,
                    "error":
                        "android_click_resource wymaga resource.",
                    "arguments": args
                }

            return fn(resource)

        # Gemini: android_swipe
        # Python: android_swipe
        if name == "android_swipe":

            fn = globals().get(
                "android_swipe"
            )

            if not callable(fn):
                return {
                    "ok": False,
                    "error":
                        "Brak implementacji android_swipe()."
                }

            return _call_tool_function(
                fn,
                args
            )

        # Gemini: android_screenshot
        # Python: android_screenshot
        if name == "android_screenshot":

            fn = globals().get(
                "android_screenshot"
            )

            if not callable(fn):
                return {
                    "ok": False,
                    "error":
                        "Brak implementacji android_screenshot()."
                }

            return fn(
                args.get("path")
            )

        # Gemini: android_launch_app
        # Python: android_launch_app
        if name == "android_launch_app":

            fn = globals().get(
                "android_launch_app"
            )

            if not callable(fn):
                return {
                    "ok": False,
                    "error":
                        "Brak implementacji android_launch_app()."
                }

            package = args.get("package")

            if not package:
                return {
                    "ok": False,
                    "error":
                        "android_launch_app wymaga package.",
                    "arguments": args
                }

            return fn(package)

        # Gemini: android_run_in_new_window
        # Python: android_run_in_new_window
        if name == "android_run_in_new_window":

            fn = globals().get(
                "android_run_in_new_window"
            )

            if not callable(fn):
                return {
                    "ok": False,
                    "error":
                        "Brak implementacji "
                        "android_run_in_new_window()."
                }

            command = args.get("command")

            if not command:
                return {
                    "ok": False,
                    "error":
                        "android_run_in_new_window wymaga command.",
                    "arguments": args
                }

            return _call_tool_function(
                fn,
                args
            )

        # Gemini: android_install_apk
        # Python: android_install_apk
        if name == "android_install_apk":

            fn = globals().get(
                "android_install_apk"
            )

            if not callable(fn):
                return {
                    "ok": False,
                    "error":
                        "Brak implementacji android_install_apk()."
                }

            path = args.get("path")

            if not path:
                return {
                    "ok": False,
                    "error":
                        "android_install_apk wymaga path.",
                    "arguments": args
                }

            return _call_tool_function(
                fn,
                args
            )

        # Gemini: android_uninstall_app
        # Python: android_uninstall_app
        if name == "android_uninstall_app":

            fn = globals().get(
                "android_uninstall_app"
            )

            if not callable(fn):
                return {
                    "ok": False,
                    "error":
                        "Brak implementacji android_uninstall_app()."
                }

            package = args.get("package")

            if not package:
                return {
                    "ok": False,
                    "error":
                        "android_uninstall_app wymaga package.",
                    "arguments": args
                }

            return fn(package)

        # Gemini: android_logcat
        # Python: android_logcat
        if name == "android_logcat":

            fn = globals().get(
                "android_logcat"
            )

            if not callable(fn):
                return {
                    "ok": False,
                    "error":
                        "Brak implementacji android_logcat()."
                }

            return _call_tool_function(
                fn,
                args
            )

        # ====================================================
        # CHROME
        # ====================================================

        if name == "chrome_tabs":

            fn = globals().get(
                "chrome_tabs"
            )

            if callable(fn):
                return fn()

            return {
                "ok": False,
                "error":
                    "Brak implementacji chrome_tabs()."
            }

        if name == "chrome_inspect":

            fn = globals().get(
                "chrome_inspect"
            )

            if not callable(fn):
                return {
                    "ok": False,
                    "error":
                        "Brak implementacji chrome_inspect()."
                }

            return _call_tool_function(
                fn,
                args
            )

        if name == "chrome_open":

            fn = globals().get(
                "chrome_open"
            )

            if not callable(fn):
                return {
                    "ok": False,
                    "error":
                        "Brak implementacji chrome_open()."
                }

            return _call_tool_function(
                fn,
                args
            )

        if name == "chrome_click":

            fn = globals().get(
                "chrome_click"
            )

            if not callable(fn):
                return {
                    "ok": False,
                    "error":
                        "Brak implementacji chrome_click()."
                }

            return _call_tool_function(
                fn,
                args
            )

        if name == "chrome_type":

            fn = globals().get(
                "chrome_type"
            )

            if not callable(fn):
                return {
                    "ok": False,
                    "error":
                        "Brak implementacji chrome_type()."
                }

            return _call_tool_function(
                fn,
                args
            )

        # ====================================================
        # NARZĘDZIA DOPISANE PRZEZ DEEPSEEK (custom_tools/)
        # ====================================================

        custom = CUSTOM_TOOLS.get(name)

        if custom is not None:

            try:
                return _call_tool_function(
                    custom["run"],
                    args
                )

            except Exception as e:
                return {
                    "ok": False,
                    "error":
                        "Błąd w niestandardowym narzędziu '"
                        + name + "': "
                        + type(e).__name__ + ": " + str(e),
                    "source_file": custom.get("source_file")
                }

        # ====================================================
        # NIEZNANE
        # ====================================================

        return {
            "ok": False,
            "error":
                "Nieznane narzędzie: "
                + str(name)
        }

    except Exception as e:

        return {
            "ok": False,
            "error": str(e),
            "error_type": type(e).__name__,
            "tool": name
        }


# ============================================================
# Uniwersalne wywoływanie istniejących funkcji Chrome
# ============================================================

def _call_tool_function(fn, args):
    """
    Przekazuje do funkcji tylko argumenty, które rzeczywiście
    przyjmuje jej podpis.
    """

    import inspect

    try:
        sig = inspect.signature(fn)
    except Exception:
        return fn(**args)

    params = sig.parameters

    # **kwargs
    if any(
        p.kind == inspect.Parameter.VAR_KEYWORD
        for p in params.values()
    ):
        return fn(**args)

    accepted = {}

    # Normalne argumenty nazwane
    for param_name, param in params.items():

        if param_name in args:
            accepted[param_name] = args[param_name]

    # Czasami Gemini może użyć alternatywnej nazwy tab_id/tabId.
    aliases = {
        "tabId": "tab_id",
        "tab": "tab_id",
        "url": "url",
        "contains": "contains",
        "text": "text",
        "selector": "selector",
        "x": "x",
        "y": "y"
    }

    for source, target in aliases.items():

        if (
            source in args
            and target in params
            and target not in accepted
        ):
            accepted[target] = args[source]

    # Brakujące wymagane argumenty
    missing = []

    for param_name, param in params.items():

        if param.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY
        ):

            if (
                param.default is inspect.Parameter.empty
                and param_name not in accepted
            ):
                missing.append(param_name)

    if missing:
        return {
            "ok": False,
            "error":
                "Brak wymaganych argumentów.",
            "missing":
                missing,
            "received":
                args,
            "function":
                getattr(
                    fn,
                    "__name__",
                    str(fn)
                )
        }

    return fn(
        **accepted
    )



def gemini_execute_task(task_id, task, success_condition=''):
    """
    Gemini executor — Interactions API.

    Przepływ:

        DeepSeek
            ↓
        Gemini
            ↓
        FunctionCallStep
            ↓
        dispatch_tool()
            ↓
        FunctionResponse
            ↓
        previous_interaction_id
            ↓
        Gemini
            ↓
        kolejny tool / raport

    Jedna interakcja Gemini jest kontynuowana przez
    previous_interaction_id.
    """

    global gemini_disabled

    # ========================================================
    # GEMINI CLIENT
    # ========================================================

    if gemini_disabled:
        return {
            "ok": False,
            "status": "GEMINI_DISABLED",
            "error": "Gemini jest obecnie wyłączony."
        }

    key_name, client = get_gemini_client()

    if client is None:
        return {
            "ok": False,
            "status": "NO_GEMINI_CLIENT",
            "error": "Brak aktywnego klienta Gemini."
        }

    # ========================================================
    # PROMPT
    # ========================================================

    prompt = f"""
Jesteś wykonawcą autonomicznego agenta.

DeepSeek jest głównym mózgiem.
Ty jesteś wykonawcą.

Twoim zadaniem jest REALNIE wykonać poniższe zadanie
za pomocą dostępnych narzędzi.

NIE pytaj użytkownika o zgodę.
NIE kończ po samym zaplanowaniu.
NIE zgłaszaj sukcesu bez sprawdzenia rezultatu.

Masz dostęp do:

- Termux
- shell
- zapisu plików
- Android/uiautomator2
- Chrome/CDP, jeżeli jest dostępny

ZASADY:

1. Jeżeli trzeba UTWORZYĆ NOWY plik (albo mały plik):
   użyj termux_write_file.

   Jeżeli trzeba ZMIENIĆ FRAGMENT JUŻ ISTNIEJĄCEGO, dużego pliku
   (np. poprawka błędu w kodzie gry): użyj termux_patch_file
   (search/replace), NIE przepisuj całego pliku przez
   termux_write_file — to marnuje tokeny i ryzykuje literówkę przy
   przepisywaniu reszty, której nie musiałeś dotykać. Najpierw
   termux_read_file, żeby skopiować dokładny fragment do 'search'.

   NIGDY nie używaj `sed -i 'N,Md'` ani innego usuwania/zamiany PO
   NUMERZE LINII do edycji plików projektu (build.gradle, kod gry
   itp.) — zaobserwowany realny przypadek: `sed -i '10,12d'` urwał
   środek bloku `allprojects {{ repositories {{ ... }} }}`, zostawiając
   uszkodzony plik, który psuł build przez kilka kolejnych kroków.
   Numery linii są kruche — jedna wcześniejsza zmiana i usuwasz
   coś innego niż zamierzałeś. termux_patch_file (dopasowanie po
   TREŚCI, nie numerze linii) nie ma tego problemu.

2. Jeżeli trzeba wykonać krótką komendę:
   użyj termux_run.

3. Jeżeli trzeba uruchomić długi proces:
   użyj termux_run_background.

4. Jeżeli uruchamiasz program, serwer albo aplikację:
   sprawdź czy rzeczywiście działa.

5. Jeżeli narzędzie zwróci błąd:
   NIE NAPRAWIAJ GO SAMODZIELNIE.

   Natychmiast:
   - zatrzymaj bieżący TASK,
   - zachowaj dokładny błąd,
   - zwróć go do MAIN.

   MAIN / DeepSeek jest odpowiedzialny za:
   - analizę błędu,
   - decyzję o zmianie strategii,
   - przygotowanie PATCHA,
   - przygotowanie następnego TASK-u.

6. Nie wykonuj tej samej czynności ponownie po błędzie,
   chyba że MAIN dostarczy nowy TASK lub PATCH.

7. Po każdym udanym działaniu sprawdź faktyczny rezultat.

8. Jeżeli monitorujesz proces uruchomiony przez
   termux_run_background (termux_check_process /
   termux_read_file na log_file): wykonaj NAJWYŻEJ 2-3 takie
   sprawdzenia w TYM zadaniu. Jeżeli proces nadal działa,
   ZAKOŃCZ raport stwierdzeniem, że instalacja/build nadal trwa
   w tle, podaj PID i ścieżkę do log_file — NIE zapętlaj się w
   sprawdzaniu aż do wyczerpania limitu narzędzi. MAIN utworzy
   kolejny TASK sprawdzający ten sam proces później.

9. NIGDY nie zapisuj plików w /tmp — to katalog systemu Android,
   Termux (jako zwykła aplikacja) nie ma tam praw zapisu
   ("Permission denied"). Pliki tymczasowe zapisuj w $HOME (~)
   albo w $PREFIX/tmp (czyli
   /data/data/com.termux/files/usr/tmp), np. zamiast
   "echo OK > /tmp/x.txt" użyj "echo OK > ~/x.txt".

10. Jeżeli w trakcie zadania NIE JESTEŚ PEWIEN jak kontynuować
    (typowy przypadek: masz zrobić poprawkę przez
    termux_patch_file, ale nie wiesz dokładnie jaki fragment
    podać jako 'search') — NAJPIERW spróbuj sam ustalić to przez
    termux_read_file. Jeśli to nie wystarczy, użyj ask_deepseek —
    dostaniesz krótką podpowiedź i możesz kontynuować TEN SAM
    TASK, zamiast kończyć go błędem. Limitowane do kilku razy na
    zadanie — nie zastępuj tym normalnego czytania plików.

11. Do usuwania plików/katalogów UŻYWAJ termux_delete, NIE `rm`
    przez shell/termux_run. Obie ścieżki wymagają potwierdzenia
    operatora w terminalu, ale termux_delete jest czytelniejsze
    (operator widzi konkretną ścieżkę, nie surową komendę). Jeżeli
    operator odmówi — NIE próbuj obejść tego inną komendą ani
    innym sformułowaniem, zakończ zadanie i zgłoś odmowę do MAIN.

============================================================
WARUNEK SUKCESU:

{success_condition}

============================================================
TASK ID:

{task_id}

============================================================
TASK:

{task}

============================================================

Na końcu przygotuj raport:

CO ZROBIŁEM:
...

CO ZNALAZŁEM:
...

CO SIĘ UDAŁO:
...

CO NIE ZADZIAŁAŁO:
...

AKTUALNY STAN:
...

CZY CEL ZOSTAŁ OSIĄGNIĘTY:
TAK / NIE / CZĘŚCIOWO

CO POWINIEN ZROBIĆ MAIN:
...
"""

    # ========================================================
    # PIERWSZA INTERAKCJA
    # ========================================================

    try:
        interaction = client.interactions.create(
            model=GEMINI_MODEL,
            input=prompt,
            tools=gemini_tools()
        )

        interaction_id = getattr(
            interaction,
            "id",
            None
        )

        if interaction_id:
            write_json(
                GEMINI_STATE_FILE,
                {
                    "task_id": task_id,
                    "interaction_id": interaction_id,
                    "updated": datetime.now().isoformat()
                }
            )

        tool_calls = 0
        ask_deepseek_calls = 0

        # ====================================================
        # PĘTLA INTERACTIONS
        # ====================================================

        while tool_calls < GEMINI_MAX_TOOL_CALLS:

            # ------------------------------------------------
            # POBIERZ FUNCTION CALLS
            # ------------------------------------------------

            steps = getattr(
                interaction,
                "steps",
                None
            ) or []

            function_calls = []

            for step in steps:

                step_type = getattr(
                    step,
                    "type",
                    ""
                )

                name = getattr(
                    step,
                    "name",
                    None
                )

                arguments = getattr(
                    step,
                    "arguments",
                    None
                )

                if (
                    step_type in (
                        "function_call",
                        "tool_call"
                    )
                    or (
                        name
                        and arguments is not None
                    )
                ):
                    function_calls.append(step)

            # ------------------------------------------------
            # BRAK FUNCTION CALL
            # ------------------------------------------------

            if not function_calls:

                text = getattr(
                    interaction,
                    "output_text",
                    None
                )

                if not text:
                    pieces = []

                    for step in steps:

                        content = getattr(
                            step,
                            "content",
                            None
                        ) or []

                        for item in content:

                            item_text = getattr(
                                item,
                                "text",
                                None
                            )

                            if item_text:
                                pieces.append(
                                    str(item_text)
                                )

                    text = "\n".join(pieces)

                if not text:
                    text = (
                        "Gemini zakończył interakcję "
                        "bez raportu."
                    )

                return {
                    "ok": True,
                    "status": "COMPLETED",
                    "key": key_name,
                    "report": short(
                        str(text),
                        RESULT_LIMIT
                    ),
                    "tool_calls": tool_calls,
                    "interaction_id": interaction_id
                }

            # ------------------------------------------------
            # WYKONAJ FUNCTION CALLS
            # ------------------------------------------------

            responses = []

            for call in function_calls:

                if tool_calls >= GEMINI_MAX_TOOL_CALLS:
                    break

                tool_calls += 1

                name = getattr(
                    call,
                    "name",
                    ""
                )

                args = getattr(
                    call,
                    "arguments",
                    None
                )

                if args is None:
                    args = getattr(
                        call,
                        "args",
                        {}
                    )

                # --------------------------------------------
                # ARGUMENTS
                # --------------------------------------------

                if isinstance(args, str):

                    try:
                        args = json.loads(args)

                    except Exception:
                        args = {}

                if not isinstance(args, dict):

                    try:
                        args = dict(args)

                    except Exception:
                        args = {}

                log(
                    "GEMINI",
                    f"narzędzie #{tool_calls}: {name}"
                )

                # --------------------------------------------
                # LIMIT ask_deepseek NA TASK
                #
                # Przechwytujemy TUTAJ, przed dispatch_tool(), żeby
                # przekroczenie limitu nie kosztowało ani jednego
                # dodatkowego wywołania DeepSeeka — to dokładnie to,
                # co miało być ograniczone (v8: mniej spamu do
                # chat.deepseek.com).
                # --------------------------------------------

                if name == "ask_deepseek":

                    ask_deepseek_calls += 1

                    if ask_deepseek_calls > ASK_DEEPSEEK_MAX_PER_TASK:

                        log(
                            "GEMINI",
                            "Limit ask_deepseek w tym zadaniu "
                            "wyczerpany ("
                            + str(ASK_DEEPSEEK_MAX_PER_TASK)
                            + ") — pomijam wywołanie DeepSeeka."
                        )

                        result = {
                            "ok": True,
                            "answer": (
                                "Limit podpowiedzi w tym zadaniu "
                                "wyczerpany (max "
                                + str(ASK_DEEPSEEK_MAX_PER_TASK)
                                + "). Kontynuuj samodzielnie na "
                                "podstawie tego co już wiesz, albo "
                                "zakończ zadanie i zwróć dokładny "
                                "raport do MAIN."
                            ),
                            "limit_reached": True
                        }

                        responses.append({
                            "type": "function_result",
                            "name": name,
                            "call_id": getattr(call, "id", None),
                            "result": [{
                                "type": "text",
                                "text": json.dumps(
                                    result,
                                    ensure_ascii=False,
                                    default=str
                                )
                            }]
                        })

                        continue

                # --------------------------------------------
                # DISPATCH
                # --------------------------------------------

                try:
                    result = dispatch_tool(
                        name,
                        args
                    )

                except Exception as e:

                    result = {
                        "ok": False,
                        "error": str(e),
                        "error_type": type(e).__name__
                    }

                log(
                    "GEMINI",
                    "wynik: "
                    + short(
                        json.dumps(
                            result,
                            ensure_ascii=False,
                            default=str
                        ),
                        1000
                    )
                )

                # --------------------------------------------
                # KRYTYCZNA ZASADA:
                #
                # Gemini NIE NAPRAWIA BŁĘDU SAMODZIELNIE.
                #
                # Jeżeli narzędzie zwróci ok=False,
                # kończymy bieżący TASK i zwracamy dokładny
                # raport do MAIN / DeepSeek.
                #
                # MAIN zdecyduje o następnym TASK-u / PATCHU.
                # --------------------------------------------

                if isinstance(result, dict):
                    tool_failed = (
                        result.get("ok") is False
                    )
                else:
                    tool_failed = False

                if tool_failed:

                    error_report = {
                        "task_id": task_id,
                        "status": "GEMINI_TOOL_ERROR",
                        "ok": False,
                        "key": key_name,
                        "tool_calls": tool_calls,
                        "tool": name,
                        "arguments": args,
                        "tool_result": result,
                        "message": (
                            "Narzędzie zakończyło się błędem. "
                            "Gemini nie wykonuje samodzielnej naprawy. "
                            "MAIN / DeepSeek musi przygotować następny TASK lub PATCH."
                        ),
                        "interaction_id": interaction_id
                    }

                    log(
                        "GEMINI",
                        "BŁĄD NARZĘDZIA -> MAIN / DEEPSEEK"
                    )

                    write_json(
                        LAST_RESULT_FILE,
                        error_report
                    )

                    return error_report

                # --------------------------------------------
                # FUNCTION RESPONSE
                #
                # WAŻNE:
                # Interactions API wymaga id odpowiadającego
                # FunctionCallStep.id.
                # --------------------------------------------

                call_id = getattr(
                    call,
                    "id",
                    None
                )

                responses.append(
                    {
                        "type": "function_result",
                        "name": name,
                        "call_id": call_id,
                        "result": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    result,
                                    ensure_ascii=False,
                                    default=str
                                )
                            }
                        ]
                    }
                )

            # ------------------------------------------------
            # BRAK ID
            # ------------------------------------------------

            if not interaction_id:

                return {
                    "ok": False,
                    "status": "NO_INTERACTION_ID",
                    "key": key_name,
                    "error": (
                        "Gemini nie zwrócił "
                        "interaction_id."
                    ),
                    "tool_calls": tool_calls
                }

            # ------------------------------------------------
            # KONTYNUACJA TEJ SAMEJ INTERAKCJI
            # ------------------------------------------------

            interaction = client.interactions.create(
                model=GEMINI_MODEL,
                input=responses,
                previous_interaction_id=interaction_id,
                tools=gemini_tools()
            )

            new_id = getattr(
                interaction,
                "id",
                None
            )

            if new_id:
                interaction_id = new_id

                write_json(
                    GEMINI_STATE_FILE,
                    {
                        "task_id": task_id,
                        "interaction_id": interaction_id,
                        "updated": datetime.now().isoformat()
                    }
                )

        # ====================================================
        # LIMIT NARZĘDZI
        # ====================================================

        return {
            "ok": False,
            "status": "TOOL_LIMIT",
            "key": key_name,
            "error": (
                "Gemini osiągnął limit narzędzi."
            ),
            "tool_calls": tool_calls,
            "interaction_id": interaction_id
        }

    # ========================================================
    # EXCEPTION
    # ========================================================

    except Exception as e:

        error_text = str(e)

        log(
            "GEMINI",
            "BŁĄD EXECUTORA: "
            + short(error_text, 3000)
        )

        # ----------------------------------------------------
        # QUOTA
        # ----------------------------------------------------

        if (
            "429" in error_text
            or "RESOURCE_EXHAUSTED" in error_text
            or "quota" in error_text.lower()
        ):

            log(
                "GEMINI",
                f"QUOTA dla klucza {key_name}"
            )

            try:
                mark_quota(
                    key_name
                )

            except Exception:
                pass

            next_name, next_client = (
                get_gemini_client()
            )

            if (
                next_client is not None
                and next_name != key_name
            ):

                log(
                    "GEMINI",
                    f"Przełączam klucz "
                    f"{key_name} -> {next_name}"
                )

                return gemini_execute_task(
                    task_id,
                    task,
                    success_condition
                )

            gemini_disabled = True

            return {
                "ok": False,
                "status": "QUOTA_EXHAUSTED",
                "key": key_name,
                "error": short(
                    error_text,
                    3000
                )
            }

        return {
            "ok": False,
            "status": "GEMINI_EXECUTOR_ERROR",
            "key": key_name,
            "error": short(
                error_text,
                3000
            )
        }



# ============================================================
# CREATE TASK
# ============================================================

def _check_forbidden_task(task_text):
    """
    Twarda (nie tylko promptowa) ochrona jawnego wymagania
    użytkownika: żadnej gotowej gry, żadnego gotowego APK — cały
    projekt ma powstać w Termuxie za pośrednictwem Gemini.

    Bez tego wymaganie żyje tylko jako jednorazowa instrukcja na
    początku rozmowy z DeepSeek/Gemini i może "wyparować" po
    kilkudziesięciu krokach długiej autonomicznej sesji.
    """

    lowered = str(task_text or "").lower()

    for pattern in FORBIDDEN_TASK_PATTERNS:

        if pattern in lowered:

            return (
                "Zadanie pasuje do zabronionego wzorca '"
                + pattern
                + "'. Użytkownik jawnie zabronił pobierania "
                "gotowej gry/APK — wszystkie pliki projektu mają "
                "być utworzone przez Gemini w Termux."
            )

    return None


def create_task(
    task,
    success_condition,
    reason="",
    priority="normal"
):
    """
    Tworzy zadanie dla kolejki Gemini.

    Zwraca None (zamiast task_id), jeżeli treść zadania łamie
    jawne wymagania użytkownika (patrz _check_forbidden_task) —
    wywołujący MUSI to sprawdzić przed dalszym działaniem.
    """

    blocked_reason = _check_forbidden_task(task)

    if blocked_reason:

        log(
            "MAIN",
            "TASK ZABLOKOWANY (ochrona wymagań użytkownika): "
            + blocked_reason
        )

        log_event(
            "task_blocked",
            {
                "task": short(task, 500),
                "reason": blocked_reason
            }
        )

        return None

    task_id = (
        datetime.now().strftime(
            "%Y%m%d_%H%M%S"
        )
        + "_"
        + uuid.uuid4().hex[:6]
    )

    data = {
        "task_id": task_id,
        "created": datetime.now().isoformat(),
        "status": "PENDING",
        "priority": priority,
        "reason": reason,
        "task": task,
        "success_condition": success_condition
    }

    path = (
        QUEUE_DIR
        / f"{task_id}.json"
    )

    write_json(
        path,
        data
    )

    log(
        "QUEUE",
        "Dodano " + task_id
    )

    return task_id


def pending_task():

    files = sorted(
        QUEUE_DIR.glob(
            "*.json"
        )
    )

    for path in files:

        data = read_json(
            path,
            {}
        )

        if (
            data.get("status")
            == "PENDING"
        ):

            return path, data

    return None, None


def run_next_task():

    path, task = pending_task()

    if task is None:
        return None

    if gemini_disabled:

        return {
            "ok": False,
            "status":
                "WAITING_FOR_GEMINI",
            "task_id":
                task["task_id"]
        }

    task["status"] = "RUNNING"
    task["started"] = (
        datetime.now().isoformat()
    )

    write_json(
        path,
        task
    )

    log(
        "MAIN",
        "TASK -> "
        + task["task_id"]
    )

    result = gemini_execute_task(
        task["task_id"],
        task["task"],
        task.get(
            "success_condition",
            ""
        )
    )

    task["finished"] = (
        datetime.now().isoformat()
    )

    task["result"] = result

    # ----------------------------------------------------------
    # NIE każde ok=True oznacza DONE celu.
    # COMPLETED oznacza zakończenie wykonania bloku.
    # MAIN nadal ocenia cel.
    # ----------------------------------------------------------

    if (
        result.get("status")
        == "GEMINI_TOOL_ERROR"
    ):

        task["status"] = "ERROR"

        # ----------------------------------------------------
        # Wcześniej KAŻDY GEMINI_TOOL_ERROR kończył się tylko
        # wysłaniem surowego błędu do MAIN, licząc, że MAIN
        # samo wpadnie na to, żeby skonsultować CODE_REVIEWERA.
        # CODE_REVIEWER/CODE_FIXER istniały (sesje, promptY,
        # review_code()), ale nic nigdy ich realnie nie wołało.
        #
        # Teraz: pierwsza porażka konkretnej (narzędzie,
        # argumenty) to normalny sygnał dla MAIN — niech samo
        # spróbuje innego podejścia w kolejnym TASKu. Dopiero
        # TOOL_REPEAT_LIMIT-ta identyczna porażka automatycznie
        # uruchamia CODE_REVIEWER -> CODE_FIXER -> realny patch
        # (backup / py_compile / rollback w kodzie, nie w opisie).
        # ----------------------------------------------------

        attempt_count = record_tool_attempt(
            result.get("tool"),
            result.get("arguments"),
            result.get("tool_result")
            if isinstance(result.get("tool_result"), dict)
            else {}
        )

        result["attempt_count"] = attempt_count

        if attempt_count >= TOOL_REPEAT_LIMIT:

            log(
                "MAIN",
                "To samo narzędzie zawiodło "
                + str(attempt_count)
                + "x z tymi samymi argumentami -> "
                "CODE_REVIEWER / CODE_FIXER"
            )

            review = review_code({
                "task_id": result.get("task_id"),
                "tool": result.get("tool"),
                "arguments": result.get("arguments"),
                "tool_result": result.get("tool_result"),
                "interaction_id": result.get("interaction_id"),
                "attempt_count": attempt_count
            })

            result["code_review"] = review

            log_event(
                "code_review_triggered",
                {
                    "tool": result.get("tool"),
                    "attempt_count": attempt_count,
                    "patch_applied": (
                        review.get("patch_result", {}).get("applied")
                        if isinstance(review, dict) else None
                    )
                }
            )

        else:

            result["hint"] = (
                "To próba nr " + str(attempt_count) + " tej "
                "dokładnej czynności (to samo narzędzie + te same "
                "argumenty). MAIN: spróbuj innego podejścia w "
                "zwykłym TASKu. Dopiero po " + str(TOOL_REPEAT_LIMIT)
                + ". identycznej porażce agent automatycznie "
                "konsultuje CODE_REVIEWERA."
            )

    elif result.get("ok"):

        task["status"] = "EXECUTED"

    elif (
        result.get("status")
        == "WAITING_FOR_GEMINI"
    ):

        task["status"] = "PENDING"

    else:

        task["status"] = "ERROR"

    write_json(
        path,
        task
    )

    write_json(
        RESULTS_DIR /
        f"{task['task_id']}.json",
        result
    )

    # Ostatni raport jest źródłem prawdy dla MAIN.
    # Przy błędzie zawiera pełne dane narzędzia,
    # aby DeepSeek mógł przygotować kolejny TASK / PATCH.

    write_json(
        LAST_RESULT_FILE,
        result
    )

    status = result.get("status", "?")

    log(
        "GEMINI",
        "Task zakończony: " + str(status)
    )

    # ── Diagnostyka kroku ────────────────────────────────────
    # Czytelne podsumowanie w terminalu — operator widzi od razu
    # co się stało bez parsowania surowego JSON.

    sep = "─" * 60

    print()
    print(sep)
    print(f"  TASK: {task.get('task_id', '?')}")
    print(f"  STATUS: {status}")
    print(f"  NARZĘDZIA: {result.get('tool_calls', 0)}")

    if status == "GEMINI_TOOL_ERROR":
        print(f"  NARZĘDZIE: {result.get('tool', '?')}")
        tr = result.get("tool_result", {})
        err = (tr or {}).get("error", "")
        if err:
            print(f"  BŁĄD: {short(err, 200)}")
        attempts = result.get("attempt_count", "")
        if attempts:
            print(f"  PRÓBY: {attempts}")
        cr = result.get("code_review", {})
        if cr:
            patch = (cr.get("patch_result") or {})
            applied = patch.get("applied")
            print(
                "  CODE_REVIEW: patch="
                + ("OK ✓" if applied else "nie nałożony")
            )

    elif status == "COMPLETED":
        report = short(result.get("report", ""), 300)
        if report:
            print(f"  RAPORT: {report}")

    elif status == "DONE_REJECTED_VERIFICATION_FAILED":
        checks = result.get("checks", [])
        for c in checks:
            ok_str = "✓" if c.get("ok") else "✗"
            print(f"  [{ok_str}] {c.get('check')}: {short(c.get('detail',''),120)}")

    elif status == "TOOL_LIMIT":
        print(
            "  HINT: za duży TASK — podziel na 2 mniejsze."
        )

    hint = result.get("hint", "")
    if hint:
        print(f"  HINT: {short(hint, 200)}")

    print(sep)
    print()

    return result


# ============================================================
# DEEPSEEK TEAM
# ============================================================


def extract_function_source(source, function_name):
    """
    Wyciąga kod jednej funkcji top-level po nazwie — od
    'def <nazwa>(' do kolejnego 'def '/'class ' na poziomie
    wcięcia 0. Plik nie ma klas i funkcje top-level nie są
    zagnieżdżane w sobie na tym poziomie, więc to wystarczy —
    dzięki temu CODE_REVIEWER dostaje RZECZYWIŚCIE potrzebny
    fragment zamiast przypadkowej końcówki pliku (poprzednio
    source[-16000:] — dla pliku >120KB to ostatnie ~12%; błąd w
    execute_shell() czy termux_run(), które leżą znacznie
    wcześniej w pliku, w ogóle nie trafiał do CODE_REVIEWERA).
    """

    if not function_name:
        return ""

    pattern = re.compile(
        r"^def "
        + re.escape(str(function_name))
        + r"\(",
        re.MULTILINE
    )

    match = pattern.search(source)

    if not match:
        return ""

    start = match.start()

    next_def = re.search(
        r"^(?:def |class )",
        source[match.end():],
        re.MULTILINE
    )

    if next_def:
        end = match.end() + next_def.start()
    else:
        end = len(source)

    return source[start:end].rstrip()


def extract_code_block(text):
    """
    Wyciąga zawartość PIERWSZEGO bloku ```...``` z tekstu.

    ANDROID_GAME_ENGINEER_PROMPT każe zwracać kod w sekcji
    "POLECENIE / KOD:" wewnątrz dokładnie takiego bloku — ta
    funkcja pozwala Pythonowi wyciąć ten kod i zapisać go
    bezpośrednio do pliku (patrz write_engineer_code_to w
    run_agent()), zamiast zmuszać Gemini do przepisywania go od
    nowa z opisu słownego przygotowanego przez MAIN. Oszczędza to
    tokeny/limit Gemini i eliminuje błędy przepisywania — kod trafia
    do pliku 1:1 taki, jaki wymyślił DeepSeek.

    Ignoruje opcjonalny znacznik języka po otwierających ``` (np.
    ```python, ```java).
    """

    match = re.search(
        r"```[a-zA-Z0-9_+-]*\n(.*?)```",
        text or "",
        re.DOTALL
    )

    if not match:
        return None

    code = match.group(1).rstrip("\n")

    return code if code.strip() else None


_SHELL_SCRIPT_MARKERS = re.compile(
    r"^#!/|<<\s*['\"]?EOF['\"]?\s*$|^\s*cat\s+>>?\s|\$\(",
    re.MULTILINE
)


def _looks_like_shell_script(code, target_path):
    """
    Wykrywa, czy blok kodu wygląda jak SKRYPT POWŁOKI (komendy do
    URUCHOMIENIA), a nie treść pliku do zapisania 1:1.

    Zaobserwowany realny przypadek: ANDROID_GAME_ENGINEER podał w
    bloku "POLECENIE / KOD" komendę w stylu
    `cat > plik << 'EOF' ... EOF` (czyli: "uruchom to, żeby
    zapisać plik"), a write_engineer_code_to zapisało ten skrypt
    DOSŁOWNIE jako zawartość build.gradle, zamiast go wykonać.
    Gemini akurat to zauważyło i naprawiło samo, ale to był traf,
    nie zabezpieczenie.

    Nie ostrzega dla plików .sh (tam skrypt powłoki jest właściwą
    zawartością).
    """

    suffix = Path(str(target_path)).suffix.lower()

    if suffix == ".sh":
        return False

    return bool(_SHELL_SCRIPT_MARKERS.search(code or ""))


def apply_patch_from_fixer_text(fixer_text):
    """
    Parsuje blok SZUKAJ/ZAMIEŃ z odpowiedzi CODE_FIXERA i
    NAPRAWDĘ nakłada go na agent.py:

        1. backup z znacznikiem czasu (agent.py.bak_YYYYMMDD_HHMMSS),
        2. dokładna, jednoznaczna podmiana tekstu (musi wystąpić
           w pliku dokładnie raz — inaczej patch jest odrzucany),
        3. python -m py_compile na wynikowym pliku,
        4. jeżeli kompilacja się nie powiedzie — automatyczny
           rollback z backupu.

    To jest właśnie ten mechanizm, który wcześniej istniał
    WYŁĄCZNIE jako punkty w CODE_FIXER_PROMPT ("1. backup,
    2. patch, 3. py_compile, 4. rollback") — bez żadnego kodu,
    który by to faktycznie robił. CODE_FIXER pisał, że to zrobi;
    nic tego nie wykonywało.

    WAŻNE OGRANICZENIE: modyfikuje plik NA DYSKU. Już uruchomiony
    proces Pythona ma stary kod załadowany w pamięci i będzie go
    używać do końca bieżącej sesji — nowa wersja zacznie
    obowiązywać dopiero przy KOLEJNYM uruchomieniu agent.py. To
    świadoma decyzja: bezpieczne, przewidywalne "napraw plik,
    zrestartuj" jest dużo pewniejsze niż próba podmiany kodu
    żywego procesu w trakcie działania (otwarte sesje ADB/CDP,
    kolejka, stan Gemini).
    """

    match = re.search(
        r"<<<<<<<\s*SZUKAJ\s*\n(.*?)\n=======\s*\n(.*?)\n>>>>>>>\s*ZAMIEŃ",
        fixer_text or "",
        re.DOTALL
    )

    if not match:
        return {
            "applied": False,
            "reason": (
                "Nie znaleziono bloku <<<<<<< SZUKAJ / ======= / "
                ">>>>>>> ZAMIEŃ w odpowiedzi CODE_FIXERA — patch "
                "nienałożony."
            )
        }

    old_block = match.group(1)
    new_block = match.group(2)

    target = Path(__file__)

    try:
        source = target.read_text(
            encoding="utf-8",
            errors="ignore"
        )
    except Exception as e:
        return {
            "applied": False,
            "reason": "Nie udało się odczytać pliku: " + str(e)
        }

    occurrences = source.count(old_block)

    if occurrences == 0:
        return {
            "applied": False,
            "reason": (
                "Fragment SZUKAJ nie występuje w pliku dokładnie "
                "(CODE_FIXER prawdopodobnie nie skopiował go "
                "1:1, np. inne wcięcia)."
            )
        }

    if occurrences > 1:
        return {
            "applied": False,
            "reason": (
                "Fragment SZUKAJ występuje w pliku "
                + str(occurrences)
                + " razy — patch odrzucony dla bezpieczeństwa "
                "(musi być jednoznaczny)."
            )
        }

    backup_path = target.with_name(
        target.name
        + ".bak_"
        + datetime.now().strftime("%Y%m%d_%H%M%S")
    )

    try:
        backup_path.write_text(
            source,
            encoding="utf-8"
        )
    except Exception as e:
        return {
            "applied": False,
            "reason": "Nie udało się utworzyć backupu: " + str(e)
        }

    new_source = source.replace(old_block, new_block, 1)

    try:
        target.write_text(
            new_source,
            encoding="utf-8"
        )
    except Exception as e:
        return {
            "applied": False,
            "reason": "Nie udało się zapisać patcha: " + str(e),
            "backup": str(backup_path)
        }

    try:
        compile_check = subprocess.run(
            [sys.executable, "-m", "py_compile", str(target)],
            capture_output=True,
            text=True,
            timeout=30
        )
    except Exception as e:
        compile_check = None
        compile_error = str(e)
    else:
        compile_error = compile_check.stderr

    compile_ok = bool(
        compile_check is not None
        and compile_check.returncode == 0
    )

    if not compile_ok:

        # ROLLBACK — nigdy nie zostawiamy uszkodzonego pliku.
        target.write_text(
            source,
            encoding="utf-8"
        )

        log(
            "CODE_FIXER",
            "py_compile nie przeszedł — rollback z "
            + str(backup_path)
        )

        return {
            "applied": False,
            "rolled_back": True,
            "reason": "py_compile nie przeszedł — przywrócono backup.",
            "compile_error": short(compile_error or "", 2000),
            "backup": str(backup_path)
        }

    log(
        "CODE_FIXER",
        "Patch nałożony i zweryfikowany (py_compile OK). "
        "Backup: " + str(backup_path)
        + " — zacznie obowiązywać po restarcie agenta."
    )

    log_event(
        "patch_applied",
        {
            "backup": str(backup_path),
            "old_block_preview": short(old_block, 300),
            "new_block_preview": short(new_block, 300)
        }
    )

    return {
        "applied": True,
        "backup": str(backup_path),
        "note": (
            "Plik na dysku jest naprawiony i przechodzi "
            "py_compile. Bieżący, już uruchomiony proces nadal "
            "działa na starym kodzie w pamięci — zrestartuj "
            "agent.py, żeby poprawka zaczęła obowiązywać."
        )
    }


def review_code(context=None):
    """
    CODE_REVIEWER analizuje RZECZYWIŚCIE relewantny fragment
    agent.py (konkretne funkcje, nie przypadkową końcówkę pliku).
    CODE_FIXER przygotowuje patch w formacie SZUKAJ/ZAMIEŃ, a
    apply_patch_from_fixer_text() nakłada go naprawdę: backup ->
    patch -> py_compile -> rollback przy błędzie.

    `context` to słownik, najczęściej dokładnie ten error_report,
    jaki gemini_execute_task() już i tak buduje przy
    GEMINI_TOOL_ERROR: task_id, tool, arguments, tool_result,
    interaction_id, attempt_count.
    """

    context = context or {}

    try:

        source = Path(__file__).read_text(
            encoding="utf-8",
            errors="ignore"
        )

        tool = context.get("tool", "")

        # Zawsze patrzymy na miejsca statystycznie najbardziej
        # prawdopodobne przy błędach narzędzi Gemini, plus
        # konkretną funkcję zgłoszonego narzędzia, jeśli istnieje
        # pod tą samą nazwą w pliku.
        candidate_names = [
            tool,
            "dispatch_tool",
            "_dispatch_tool_inner",
            "execute_shell",
            "termux_run",
        ]

        code_context = []
        seen = set()

        for fn_name in candidate_names:

            if not fn_name or fn_name in seen:
                continue

            seen.add(fn_name)

            snippet = extract_function_source(
                source,
                fn_name
            )

            if snippet:
                code_context.append(
                    "### " + fn_name + "()\n\n" + snippet
                )

        if not code_context:
            # Fallback — nic nie rozpoznaliśmy po nazwie,
            # lepszy przypadkowy kontekst niż żaden.
            code_context = [source[-16000:]]

        joined_context = short(
            "\n\n".join(code_context),
            16000
        )

        reviewer_message = f"""
MAIN zgłosił problem z kodem (ta sama czynność zawiodła
{context.get('attempt_count', '?')} razy z rzędu).

TASK ID: {context.get('task_id', '')}
NARZĘDZIE: {tool}
ARGUMENTY: {short(json.dumps(context.get('arguments', {}), ensure_ascii=False, default=str), 1500)}
WYNIK NARZĘDZIA: {short(json.dumps(context.get('tool_result', {}), ensure_ascii=False, default=str), 3000)}
INTERACTION ID: {context.get('interaction_id', '')}

PLIK:
{Path(__file__)}

RELEWANTNY KOD (rzeczywiste, nazwane funkcje — nie przypadkowa
końcówka pliku):
{joined_context}

Przeanalizuj rzeczywisty kod.

Nie wykonuj zmian.

Zwróć:
PLIK
PROBLEM
DOKŁADNE MIEJSCE (nazwa funkcji)
PRZYCZYNA
PROPONOWANA ZMIANA
RYZYKO
TEST
"""

        review = deepseek(
            "CODE_REVIEWER",
            reviewer_message
        )

        append_memory(
            MEMORY_DIR / "code_reviewer.md",
            datetime.now().isoformat(),
            review
        )

        fixer_message = f"""
MAIN potrzebuje przygotowania poprawki.

ANALIZA CODE_REVIEWERA:
{short(review, 9000)}

KONTEKST BŁĘDU:
{short(json.dumps(context, ensure_ascii=False, default=str), 3000)}

Nie zmieniaj architektury. Zmiana ma być minimalna.

Zwróć WYŁĄCZNIE jeden blok w dokładnie takim formacie — bez
niego patch NIE zostanie nałożony (nie ma tu człowieka, który
zinterpretuje opis słowny):

<<<<<<< SZUKAJ
...dokładny, unikalny fragment ISTNIEJĄCEGO kodu z sekcji
RELEWANTNY KOD powyżej, skopiowany 1:1 (te same wcięcia)...
=======
...nowa wersja tego fragmentu...
>>>>>>> ZAMIEŃ

Fragment SZUKAJ musi występować w pliku dokładnie raz.

Jeżeli nie jesteś pewien bezpiecznej poprawki, zamiast bloku
patcha napisz dokładnie: BRAK BEZPIECZNEGO PATCHA — i wyjaśnij
dlaczego. To poprawna, akceptowalna odpowiedź.
"""

        fixer = deepseek(
            "CODE_FIXER",
            fixer_message
        )

        append_memory(
            MEMORY_DIR / "code_fixer.md",
            datetime.now().isoformat(),
            fixer
        )

        patch_result = {
            "applied": False,
            "reason": "CODE_FIXER nie zaproponował patcha."
        }

        if "BRAK BEZPIECZNEGO PATCHA" not in (fixer or "").upper():
            patch_result = apply_patch_from_fixer_text(fixer)

        return {
            "review": short(review, 4000),
            "fixer": short(fixer, 4000),
            "patch_result": patch_result
        }

    except Exception as e:

        return {
            "error": str(e)
        }



# ============================================================
# RESEARCHER WEB SEARCH
# ============================================================

def researcher_web_search(
    researcher_response,
    original_context
):
    """
    Pozwala RESEARCHEROWI zażądać wyszukania informacji
    przez istniejący moduł web_search.py.

    RESEARCHER nie wykonuje narzędzia bezpośrednio.
    Python wykonuje search i przekazuje wyniki z powrotem
    do tej samej sesji RESEARCHER.
    """

    text = str(researcher_response or "").strip()

    match = re.search(
        r"WEB_SEARCH\s*:\s*(.+)",
        text,
        re.IGNORECASE
    )

    if not match:
        return text

    query = match.group(1).strip()

    # Usuń ewentualne przypadkowe cudzysłowy.
    if (
        len(query) >= 2
        and query[0] in ('"', "'")
        and query[-1] == query[0]
    ):
        query = query[1:-1].strip()

    if not query:
        return text

    log(
        "RESEARCHER",
        "WEB SEARCH -> " + query
    )

    try:

        result = web_search(
            query,
            max_results=5
        )

    except Exception as e:

        log(
            "RESEARCHER",
            "WEB SEARCH ERROR: "
            + type(e).__name__
            + ": "
            + str(e)
        )

        search_data = {
            "ok": False,
            "query": query,
            "results": [],
            "error":
                type(e).__name__
                + ": "
                + str(e)
        }

    else:

        search_data = result

        log(
            "RESEARCHER",
            "WEB SEARCH RESULT: "
            + str(
                search_data.get(
                    "count",
                    len(
                        search_data.get(
                            "results",
                            []
                        )
                    )
                )
            )
        )

    # ========================================================
    # PRZEKAZANIE WYNIKÓW DO TEJ SAMEJ SESJI RESEARCHER
    # ========================================================

    followup = f"""
WEB SEARCH RESULT.

To są wyniki rzeczywistego wyszukiwania
wykonanego przez moduł web_search.py.

Nie wykonuj żadnych innych narzędzi.

Przeanalizuj WYŁĄCZNIE poniższe wyniki.

QUERY:
{query}

RESULTS:
{json.dumps(
    search_data,
    ensure_ascii=False,
    indent=2
)}

ORYGINALNY KONTEKST:
{short(original_context, 5000)}

Teraz przygotuj finalną odpowiedź RESEARCHER-a.

Nie wymyślaj faktów.
Jeżeli wyników nie wystarcza do potwierdzenia informacji,
powiedz to wyraźnie.

Nie zwracaj ponownie WEB_SEARCH, chyba że naprawdę
potrzebne jest kolejne wyszukiwanie.
"""

    try:

        final_response = deepseek(
            "RESEARCHER",
            followup
        )

    except Exception as e:

        log(
            "RESEARCHER",
            "WEB SEARCH FOLLOWUP ERROR: "
            + type(e).__name__
            + ": "
            + str(e)
        )

        return (
            text
            + "\n\nWEB SEARCH RESULT:\n"
            + json.dumps(
                search_data,
                ensure_ascii=False,
                indent=2
            )
        )

    return str(final_response or "").strip()


# ============================================================
# SZACOWANIE POSTĘPU CELU (PROGRESS_ESTIMATOR)
# ============================================================

def _recent_task_summaries(n=8):
    """
    Czyta N ostatnio zakończonych zadań z RESULTS_DIR (już
    zapisywane tam przez run_next_task() niezależnie od tej
    funkcji) i redukuje je do zwięzłej listy status+raport/błąd —
    to jedyny kontekst, jaki dostaje PROGRESS_ESTIMATOR, więc musi
    być krótki, ale wystarczający do oceny.
    """

    files = sorted(
        RESULTS_DIR.glob("*.json")
    )[-n:]

    summaries = []

    for f in files:

        data = read_json(f, {})

        if not isinstance(data, dict):
            continue

        summaries.append({
            "task_id": f.stem,
            "status": data.get("status"),
            "report": short(
                str(
                    data.get("report")
                    or data.get("message")
                    or data.get("error")
                    or ""
                ),
                300
            )
        })

    return summaries


def estimate_progress(goal):
    """
    Pyta PROGRESS_ESTIMATOR o procentową ocenę realizacji celu na
    podstawie kilku ostatnio zakończonych zadań. Zwraca None, jeśli
    nie ma jeszcze żadnej historii albo odpowiedź nie sparsowała
    się do sensownego JSON — nigdy nie przerywa głównej pętli
    agenta z tego powodu.
    """

    summaries = _recent_task_summaries(8)

    if not summaries:
        return None

    prompt = f"""
CEL:
{goal}

OSTATNIE ZADANIA (od najstarszego do najnowszego):
{json.dumps(summaries, ensure_ascii=False, indent=2)}

Zwróć WYŁĄCZNIE JSON zgodnie z formatem z Twojego prompta
systemowego.
"""

    raw = deepseek(
        "PROGRESS_ESTIMATOR",
        prompt
    )

    parsed = parse_json(raw)

    if not parsed or "percent" not in parsed:
        return None

    try:
        percent = int(parsed.get("percent", 0))
    except Exception:
        percent = 0

    percent = max(0, min(100, percent))

    return {
        "percent": percent,
        "summary": short(
            str(parsed.get("summary", "")),
            300
        )
    }


def print_progress_bar(step, percent, summary):

    filled = int(round(percent / 5))
    bar = ("█" * filled) + ("░" * (20 - filled))

    print()
    print("═" * 60)
    print(f"  POSTĘP CELU (po kroku {step})")
    print(f"  [{bar}] {percent}%")

    if summary:
        print("  " + short(summary, 200))

    print("═" * 60)
    print()


# Ostatnie odpowiedzi RESEARCHER/BROWSER — trzymane MIĘDZY krokami,
# żeby MAIN nadal dostawał jakiś kontekst z tych ról nawet w
# krokach, w których nie są odpytywane (patrz consult_team()).
_role_response_cache = {}


def consult_team(
    goal,
    last_result,
    step=1
):
    """
    Konsultuje role SEKWENCYJNIE, nie na każdym kroku wszystkie.

    WCZEŚNIEJ ta funkcja odpytywała PLANNER/RESEARCHER/BROWSER
    równolegle przez ThreadPoolExecutor, zakładając że "opendeep i
    tak serializuje requesty po swojej stronie". W praktyce
    powodowało to POWTARZALNY błąd na każdym kroku:

        DEEPSEEK ERROR [...]: invalid message id

    Trzy wątki wysyłały wiadomości do wspólnego połączenia opendeep
    w tym samym momencie, a serwer gubił kolejność message-id
    między różnymi sesjami czatu. Powrót do sekwencyjnych wywołań
    pomógł, ale nie usunął problemu całkowicie — opendeep steruje
    STRONĄ CZATU (chat.deepseek.com), nie oficjalnym API, a to
    oznacza że 5-6 wiadomości NA KAŻDY krok, na jednym koncie, to
    realne obciążenie tego mechanizmu (i tak nieprzeznaczonego do
    automatyzacji w tym tempie) — czyli de facto spam.

    Dlatego PLANNER, CRITIC i ANDROID_GAME_ENGINEER (te trzy
    bezpośrednio napędzają decyzję MAIN) są pytane na KAŻDYM kroku,
    ale RESEARCHER i BROWSER — które w praktyce rzadko mają coś
    nowego do powiedzenia z kroku na krok — tylko co 3. krok, plus
    zawsze od razu po świeżym błędzie narzędzia (wtedy RESEARCHER
    faktycznie może pomóc znaleźć przyczynę). W pominiętych krokach
    MAIN dostaje ich OSTATNIĄ znaną odpowiedź z jasną adnotacją, że
    jest nieaktualna — lepsze to niż pusty kontekst, ale MAIN wie,
    że nie powinien na niej ślepo polegać.
    """

    tool_hint = ""

    if isinstance(last_result, dict):
        status = last_result.get("status", "")
        attempt = last_result.get("attempt_count", 0)

        if (
            status == "GEMINI_TOOL_ERROR"
            and attempt
        ):
            tool = last_result.get("tool", "?")
            err = (
                last_result.get("tool_result", {}) or {}
            ).get("error", "")
            tool_hint = (
                f"\n\n⚠️ UWAGA: narzędzie '{tool}' zawiodło "
                f"{attempt}x z rzędu tymi samymi argumentami."
            )
            if err:
                tool_hint += f" Błąd: {err}."
            tool_hint += (
                " Nie proponuj tego samego podejścia."
            )

    context = f"""
CEL:
{goal}

OSTATNI RAPORT:
{short(
    json.dumps(
        last_result,
        ensure_ascii=False
    ),
    4500
)}
{tool_hint}

AKTUALNY CHROME:
{short(chrome_summary(), 2000)}

AKTUALNY ANDROID:
{short(android_summary(), 2000)}
"""

    # Role odpytywane PO KOLEI — jedno realne połączenie do
    # opendeep na raz. CRITIC i ANDROID_GAME_ENGINEER dostają
    # dodatkowo wyjście PLANNERA/RESEARCHERA, więc muszą i tak
    # czekać, aż tamci skończą.

    results = {}

    results["PLANNER"] = deepseek(
        "PLANNER",
        context
    )

    fresh_tool_error = (
        isinstance(last_result, dict)
        and last_result.get("status") == "GEMINI_TOOL_ERROR"
    )

    consult_researcher = (
        (step % 3 == 1)
        or fresh_tool_error
    )

    consult_browser = (step % 3 == 1)

    if consult_researcher:

        results["RESEARCHER"] = researcher_web_search(
            deepseek(
                "RESEARCHER",
                context
            ),
            context
        )

        _role_response_cache["RESEARCHER"] = results["RESEARCHER"]

    else:

        log(
            "DEEPSEEK",
            "RESEARCHER pominięty w tym kroku "
            "(oszczędzanie limitu/sesji) — "
            "użyta ostatnia znana odpowiedź."
        )

        results["RESEARCHER"] = (
            "[NIEAKTUALNE — RESEARCHER nie był pytany w tym "
            "kroku, poniżej jego ostatnia znana odpowiedź]\n\n"
            + _role_response_cache.get(
                "RESEARCHER",
                "(RESEARCHER nie był jeszcze konsultowany.)"
            )
        )

    if consult_browser:

        results["BROWSER"] = deepseek(
            "BROWSER",
            context
        )

        _role_response_cache["BROWSER"] = results["BROWSER"]

    else:

        log(
            "DEEPSEEK",
            "BROWSER pominięty w tym kroku "
            "(oszczędzanie limitu/sesji) — "
            "użyta ostatnia znana odpowiedź."
        )

        results["BROWSER"] = (
            "[NIEAKTUALNE — BROWSER nie był pytany w tym kroku, "
            "poniżej jego ostatnia znana odpowiedź]\n\n"
            + _role_response_cache.get(
                "BROWSER",
                "(BROWSER nie był jeszcze konsultowany.)"
            )
        )

    planner_out = short(results.get("PLANNER", ""), 2000)
    researcher_out = short(results.get("RESEARCHER", ""), 2000)

    results["ANDROID_GAME_ENGINEER"] = deepseek(
        "ANDROID_GAME_ENGINEER",
        context
        + "\n\nPLAN PLANNERA:\n" + planner_out
        + "\n\nINFO RESEARCHER:\n" + researcher_out
    )

    results["CRITIC"] = deepseek(
        "CRITIC",
        context
        + "\n\nPLAN PLANNERA:\n" + planner_out
    )

    return {
        "planner":   short(results.get("PLANNER", ""), 4000),
        "researcher": short(results.get("RESEARCHER", ""), 4000),
        "engineer":  short(results.get("ANDROID_GAME_ENGINEER", ""), 4000),
        # Pełna, nieskrócona odpowiedź ANDROID_GAME_ENGINEER — NIE
        # trafia do prompta MAIN (żeby nie pompować mu kontekstu),
        # ale run_agent() jej potrzebuje w całości, żeby wyciąć z
        # niej blok kodu przy write_engineer_code_to (patrz
        # extract_code_block() / obsługa TASK w run_agent()).
        "engineer_full": results.get("ANDROID_GAME_ENGINEER", ""),
        "critic":    short(results.get("CRITIC", ""), 4000),
        "browser":   short(results.get("BROWSER", ""), 2000),
    }


# ============================================================
# MAIN DECISION
# ============================================================

def main_decide(
    goal,
    step,
    team,
    last_result
):

    prompt = f"""
CEL AGENTA:
{goal}

KROK:
{step}

OSTATNI WYNIK:
{short(
    json.dumps(
        last_result,
        ensure_ascii=False
    ),
    5000
)}

============================================================
INTERPRETACJA STATUSÓW
============================================================

GEMINI_TOOL_ERROR — Gemini próbował użyć narzędzia i się nie
  powiodło. Pole "tool" mówi co, "arguments" jak, "tool_result"
  dlaczego. Jeżeli "attempt_count" >= 2, agent już skonsultował
  CODE_REVIEWERA — sprawdź "code_review". Nie powtarzaj tej samej
  komendy. Zmień podejście lub narzędzie.

TOOL_LIMIT — Gemini wyczerpał limit wywołań narzędzi (zbyt
  skomplikowane zadanie). Podziel TASK na mniejsze kroki.

DONE_REJECTED_VERIFICATION_FAILED — TWOJE poprzednie DONE zostało
  odrzucone fizyczną weryfikacją. Pole "checks" mówi CO dokładnie
  brakuje. Utwórz TASK który uzupełni KONKRETNIE brakujące dowody.
  NIE zwracaj ponownie DONE — poczekaj na kolejny raport.

TASK_BLOCKED_BY_POLICY — zadanie naruszało zakaz pobierania
  gotowej gry/APK. Zaproponuj INNE podejście (build od zera).

COMPLETED — Gemini wykonał blok, pole "report" to jego raport.
  NIE oznacza automatycznie DONE całego projektu. Sprawdź raport.

============================================================
ZASADA NAPRAWY PO BŁĘDZIE
============================================================

Jeżeli OSTATNI WYNIK zawiera ok=false lub GEMINI_TOOL_ERROR:

1. NIE zwracaj DONE.
2. Przeczytaj dokładnie: tool, arguments, tool_result, error.
3. Zmień strategię — nie powtarzaj identycznej komendy.
4. Jeżeli błąd to Timeout — zleć tę samą operację przez
   termux_run_background z monitorowaniem procesu.
5. Utwórz konkretny TASK z MIERZALNYM warunkiem sukcesu.

============================================================

PLANNER:
{team['planner']}

ANDROID_GAME_ENGINEER:
{team['engineer']}

RESEARCHER:
{team['researcher']}

CRITIC:
{team['critic']}

BROWSER:
{team['browser']}

AKTUALNE KARTY CHROME:
{chrome_summary()}

AKTUALNY ANDROID:
{short(
    android_summary(),
    3500
)}

ZASADY DECYZJI:
- Nie powtarzaj tego samego kroku po raz trzeci.
- TASK ma być JEDNYM konkretnym blokiem (nie ogólnym "zrób grę"
  / "zrób program").
- Warunek sukcesu musi być MIERZALNY.
- DONE wolno zgłosić tylko gdy fizyczne dowody WŁAŚCIWE DLA TEGO
  CELU będą zweryfikowane — zawsze FINAL_OK.txt, a dodatkowo APK
  tylko jeśli cel faktycznie dotyczy budowy apki/gry Android.
  Agent sprawdzi to sam i sam rozpozna, czy APK jest wymagany.
- EXECUTED = Gemini skończył TASK, NIE = cel projektu zakończony.
- Jeżeli CRITIC mówi BLOKUJ — weź to poważnie i zmień podejście.
- OBOWIĄZKOWE: jeżeli ANDROID_GAME_ENGINEER powyżej podał gotowy
  blok kodu, a Twój TASK ma go zapisać do pliku — MUSISZ użyć
  "write_engineer_code_to" (ścieżka pliku) zamiast opisywać kod
  słownie w "task". Nie jest to opcja do rozważenia. Wtedy "task"
  dotyczy TYLKO uruchomienia/testowania już zapisanego pliku, nie
  jego tworzenia (patrz pełny opis w Twoim prompcie systemowym).

Zwróć WYŁĄCZNIE JSON.

TASK:
{{
  "type": "TASK",
  "reason": "...",
  "task": "...",
  "success_condition": "...",
  "priority": "high",
  "write_engineer_code_to": "WYMAGANE gdy dotyczy, patrz wyżej"
}}

DONE:
{{
  "type": "DONE",
  "reason": "..."
}}

FAILED:
{{
  "type": "FAILED",
  "reason": "..."
}}
"""

    return deepseek(
        "MAIN",
        prompt
    )


# ============================================================
# PARSE JSON
# ============================================================

def parse_json(text):

    if not text:
        return None

    text = str(
        text
    ).strip()

    try:

        obj = json.loads(text)

        if isinstance(
            obj,
            dict
        ):
            return obj

    except Exception:
        pass

    match = re.search(
        r"```(?:json)?\s*(\{.*?\})\s*```",
        text,
        re.DOTALL | re.IGNORECASE
    )

    if match:

        try:

            obj = json.loads(
                match.group(1)
            )

            if isinstance(
                obj,
                dict
            ):
                return obj

        except Exception:
            pass

    start = text.find("{")
    end = text.rfind("}")

    if (
        start >= 0
        and end > start
    ):

        try:

            obj = json.loads(
                text[start:end + 1]
            )

            if isinstance(
                obj,
                dict
            ):
                return obj

        except Exception:
            pass

    return None


# ============================================================
# REPEAT CONTROL
# ============================================================

def task_signature(decision):

    return (
        str(
            decision.get(
                "type",
                ""
            )
        ).upper()
        + "|"
        + short(
            decision.get(
                "task",
                decision.get(
                    "reason",
                    ""
                )
            ),
            500
        )
    )


def _tool_signature(tool, arguments):
    """
    Sygnatura KONKRETNEGO wywołania narzędzia — w odróżnieniu od
    task_signature() (która patrzy na treść decyzji MAIN), ta
    patrzy na to, co naprawdę zostało wykonane. Dwa różnie
    sformułowane TASK-i, które oba każą Gemini uruchomić
    identyczną, ginącą komendę, dostaną tu tę samą sygnaturę.
    """

    try:
        args_repr = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            default=str
        )
    except Exception:
        args_repr = str(arguments)

    return str(tool) + "|" + short(args_repr, 300)


def record_tool_attempt(tool, arguments, result):
    """
    Zlicza kolejne NIEUDANE próby tej samej (tool, arguments).
    Trwałe na dysku (TOOL_ATTEMPTS_FILE) — Ctrl+C i ponowne
    uruchomienie NIE zeruje historii wcześniejszych porażek tej
    samej czynności, więc agent "pamięta", że to już próbowano.
    """

    signature = _tool_signature(tool, arguments)

    data = read_json(TOOL_ATTEMPTS_FILE, {})

    entry = data.get(
        signature,
        {"count": 0, "history": []}
    )

    entry["count"] = entry.get("count", 0) + 1

    history = entry.get("history", [])

    history.append({
        "ts": datetime.now().isoformat(),
        "error": (
            result.get("error")
            if isinstance(result, dict) else None
        )
    })

    entry["history"] = history[-5:]

    data[signature] = entry

    write_json(TOOL_ATTEMPTS_FILE, data)

    return entry["count"]


def reset_tool_attempts(tool, arguments):
    """
    Czyści historię porażek po udanym wykonaniu tej samej
    (tool, arguments) — sukces oznacza, że problem faktycznie
    został rozwiązany, nie ma sensu dalej trzymać starych porażek.
    """

    try:
        signature = _tool_signature(tool, arguments)

        data = read_json(TOOL_ATTEMPTS_FILE, {})

        if signature in data:
            del data[signature]
            write_json(TOOL_ATTEMPTS_FILE, data)

    except Exception:
        pass


# ============================================================
# MAIN AGENT LOOP
# ============================================================



# ============================================================
# OCHRONA MAIN PRZED FAŁSZYWYM FAILED
# ============================================================

def protect_main_failed(decision, goal):
    """
    Jeżeli MAIN twierdzi, że nie może wykonać operacji
    systemowej z powodu braku terminala, przekazujemy ją
    do Gemini jako TASK.
    """

    if not isinstance(decision, dict):
        return decision

    dtype = str(
        decision.get("type", "")
    ).upper()

    if dtype != "FAILED":
        return decision

    reason = str(
        decision.get("reason", "")
    ).lower()

    phrases = (
        "brak terminala",
        "brak dostępu do terminala",
        "nie mam terminala",
        "nie ma dostępu do terminala",
        "brak execute_command",
        "brak narzędzia wykonawczego",
        "brak shell",
        "nie mogę użyć shell",
        "nie mogę utworzyć katalogu",
        "nie mogę zapisać pliku",
        "nie mogę wykonać polecenia",
        "nie mam dostępu do systemu plików",
        "nie mam dostępu do plików",
    )

    if not any(
        phrase in reason
        for phrase in phrases
    ):
        return decision

    log(
        "MAIN",
        "FAŁSZYWE FAILED -> TASK DLA GEMINI"
    )

    return {
        "type": "TASK",
        "reason": (
            "Operację może wykonać Gemini "
            "za pomocą Termux/Android/Shell."
        ),
        "task": (
            "Wykonaj cały cel użytkownika. "
            "Użyj Termux, Android i Shell w razie potrzeby. "
            "Utwórz wymagane pliki, wykonaj polecenia, "
            "uruchom wymagane programy i sprawdź rezultat."
        ),
        "success_condition": (
            "Cel został faktycznie wykonany "
            "i zweryfikowany."
        ),
        "priority": "high"
    }


# ============================================================
# FIZYCZNA WERYFIKACJA PRZED DONE
# ============================================================
#
# Wcześniej "DONE" oznaczało dosłownie: MAIN tak powiedziało.
# run_agent() przyjmowało to bez żadnego sprawdzenia — mimo że
# pierwotne polecenie użytkownika wprost mówiło:
# "na końcu fizycznie zweryfikuj projekt, APK, instalację i plik
# FINAL_OK" oraz "NIE uznawaj zadania za DONE" przy błędzie.
# Poniższa funkcja to egzekwuje w kodzie, a nie tylko w prompt-cie.
# ============================================================

def _guess_package_name(apk_path):
    """
    Best-effort: nazwa pakietu z APK przez aapt (jeśli dostępny
    w Termuxie). Jeśli aapt nie jest zainstalowany albo się nie
    powiedzie, zwracamy None — wywołujący ma to potraktować jako
    "nieznane", NIE jako "brak instalacji".
    """

    if not apk_path:
        return None

    try:
        result = execute_shell(
            "aapt dump badging "
            + str(apk_path)
            + " 2>/dev/null | grep \"package: name\"",
            timeout=20
        )

        if result.get("ok") and result.get("stdout"):

            match = re.search(
                r"name='([^']+)'",
                result["stdout"]
            )

            if match:
                return match.group(1)

    except Exception:
        pass

    return None


_APK_GOAL_PATTERN = re.compile(
    r"\bapk\b|\bandroid\b|\bgr[aeęąy]\b|\bgier\b|\bgrę\b|\bgrą\b"
    r"|\bgame\b|\bgames\b",
    re.IGNORECASE
)


def _goal_needs_apk(goal):
    """
    Czy TEN KONKRETNY cel jest w ogóle o budowaniu apki/gry
    Android — czy agent jest teraz uniwersalny (dowolny program,
    skrypt, strona, narzędzie), więc twardy wymóg pliku .apk nie
    może być bezwarunkowy dla każdego celu, tylko dla tych, które
    faktycznie o APK/grę proszą. Heurystyka na słowach kluczowych
    w treści celu — niedoskonała, ale bezpieczna w obie strony:
    fałszywy pozytyw (cel bez gry, ale ktoś wspomniał "android")
    tylko każe też potwierdzić APK, co nie zaszkodzi; fałszywy
    negatyw skutkowałby zaakceptowaniem DONE bez APK, co dla
    zwykłego skryptu/programu i tak jest poprawnym zachowaniem.
    """

    if not goal:
        return False

    return bool(_APK_GOAL_PATTERN.search(goal))


def verify_final(goal=""):
    """
    Twarda, fizyczna weryfikacja przed zaakceptowaniem DONE.

    Wymagane dowody (required=True — muszą przejść, inaczej DONE
    jest odrzucane) — UNIWERSALNE dla każdego celu:
      1. FINAL_OK.txt z treścią DOKŁADNIE równą FINAL_OK_TOKEN —
         wymuszony, jawny dowód, że wykonawca sam uznał zadanie za
         zakończone, niezależnie od rodzaju projektu.

    Dodatkowe dowody TYLKO gdy cel faktycznie dotyczy zbudowania
    apki/gry Android (patrz _goal_needs_apk) — required=True w
    tym wypadku, required=False (czysto informacyjne) gdy cel jest
    innego rodzaju (skrypt, narzędzie CLI, strona, automatyzacja
    itp.), bo wtedy brak .apk jest oczekiwany, a nie błędem:
      2. plik .apk w APK_OUTPUT_DIR, który wygląda jak prawdziwy
         APK (ZIP zawierający AndroidManifest.xml i classes.dex),
         nie pusty/uszkodzony plik.
      3. obecność pakietu wśród zainstalowanych (adb pm list
         packages), jeśli udało się odczytać nazwę pakietu z APK —
         zawsze tylko informacyjne, bo nazwy pakietu nie zawsze da
         się ustalić bez aapt.
    """

    needs_apk = _goal_needs_apk(goal)

    checks = []

    # --- 1. FINAL_OK.txt ------------------------------------

    final_ok_candidates = [
        AGENT_DIR / "FINAL_OK.txt",
        APK_OUTPUT_DIR / "FINAL_OK.txt",
    ]

    final_ok_found = False
    final_ok_path = None

    for candidate in final_ok_candidates:

        content = read_text(candidate).strip()

        if content == FINAL_OK_TOKEN:
            final_ok_found = True
            final_ok_path = candidate
            break

    checks.append({
        "check": "FINAL_OK.txt",
        "required": True,
        "ok": final_ok_found,
        "detail": (
            str(final_ok_path)
            if final_ok_found
            else "Nie znaleziono pliku z treścią dokładnie '"
            + FINAL_OK_TOKEN + "' w: "
            + ", ".join(str(c) for c in final_ok_candidates)
        )
    })

    # --- 2. Plik APK ------------------------------------------

    apk_ok = False
    apk_path = None
    apk_detail = (
        "Katalog " + str(APK_OUTPUT_DIR) + " nie istnieje."
    )

    if APK_OUTPUT_DIR.exists():

        apks = sorted(APK_OUTPUT_DIR.glob("*.apk"))

        if not apks:
            apk_detail = "Brak plików .apk w " + str(APK_OUTPUT_DIR)
        else:
            apk_path = apks[-1]

            listing = execute_shell(
                "unzip -l " + str(apk_path),
                timeout=20
            )

            looks_valid = (
                listing.get("ok")
                and "AndroidManifest.xml" in listing.get("stdout", "")
                and "classes.dex" in listing.get("stdout", "")
            )

            if looks_valid:
                apk_ok = True
                apk_detail = (
                    str(apk_path)
                    + " ("
                    + str(apk_path.stat().st_size)
                    + " B) — zawiera AndroidManifest.xml i classes.dex."
                )
            else:
                apk_detail = (
                    str(apk_path)
                    + " istnieje, ale nie wygląda jak poprawny APK "
                    "(brak AndroidManifest.xml/classes.dex w archiwum "
                    "ZIP)."
                )

    if not needs_apk and not apk_ok:
        apk_detail = (
            "Cel nie dotyczy budowania apki/gry Android — plik "
            ".apk nie jest wymagany, sprawdzenie ma charakter "
            "wyłącznie informacyjny. (" + apk_detail + ")"
        )

    checks.append({
        "check": "APK",
        "required": needs_apk,
        "ok": apk_ok,
        "detail": apk_detail
    })

    # --- 3. Instalacja (informacyjne) --------------------------

    package_name = _guess_package_name(apk_path)

    if package_name:

        packages = execute_shell(
            "adb shell pm list packages | grep " + package_name,
            timeout=20
        )

        installed = bool(
            packages.get("ok")
            and package_name in packages.get("stdout", "")
        )

        checks.append({
            "check": "Instalacja (pm list packages)",
            "required": False,
            "ok": installed,
            "detail": (
                package_name
                + (
                    " znaleziony wśród zainstalowanych."
                    if installed else
                    " NIE znaleziony wśród zainstalowanych pakietów."
                )
            )
        })
    else:
        checks.append({
            "check": "Instalacja (pm list packages)",
            "required": False,
            "ok": False,
            "detail": (
                "Nie udało się ustalić nazwy pakietu z APK (aapt "
                "niedostępny lub błąd) — wymaga ręcznej weryfikacji, "
                "nie blokuje DONE."
            )
        })

    all_required_ok = all(
        c["ok"]
        for c in checks
        if c.get("required", True)
    )

    return {
        "ok": all_required_ok,
        "checks": checks
    }


def run_agent(goal):

    # Zapamiętujemy cel NA DYSKU. Jeśli sesja zostanie przerwana
    # (Ctrl+C, awaria) zanim padnie DONE albo ostateczne FAILED,
    # main() przy kolejnym starcie zaproponuje jej wznowienie
    # zamiast zaczynać od zera / czekać, aż użytkownik ręcznie
    # odtworzy kontekst w innym czacie.
    write_text(GOAL_FILE, goal)

    last_result = {
        "status":
            "START",
        "goal":
            goal
    }

    signatures = []

    for step in range(
        1,
        MAX_STEPS + 1
    ):

        print()
        print(
            "--- KROK "
            + str(step)
            + " ---"
        )

        # Wykryj nowe/zmienione narzędzia w custom_tools/ — tanie
        # (stat() na plikach), gdy nic się nie zmieniło.
        load_custom_tools()

        # ------------------------------------------------------
        # Najpierw wykonujemy istniejący task.
        # ------------------------------------------------------

        if not gemini_disabled:

            path, task = pending_task()

            if task is not None:

                result = run_next_task()

                if result:

                    last_result = result

                    # TOOL_LIMIT to specjalny przypadek: Gemini
                    # skończył mu narzędzia zanim skończył zadanie.
                    # Nie jest to błąd narzędzia ani błąd agenta —
                    # TASK był za duży. Dodajemy hint, żeby MAIN
                    # wiedział, że ma podzielić zadanie, a nie
                    # traktował tego jak sukces lub zwykłą awarię.
                    if (
                        isinstance(result, dict)
                        and result.get("status") == "TOOL_LIMIT"
                    ):
                        last_result["hint"] = (
                            "Gemini wyczerpał limit wywołań narzędzi "
                            "(" + str(result.get("tool_calls", "?")) + "). "
                            "TASK był za duży lub za szeroki. "
                            "Podziel go na DWIE mniejsze operacje "
                            "i wyślij je jako osobne TASKi."
                        )
                        log_event(
                            "tool_limit_hit",
                            {"tool_calls": result.get("tool_calls")}
                        )

                continue

        # ------------------------------------------------------
        # Jeśli Gemini quota jest wyczerpana,
        # NIE twórz kolejnych tasków i nie konsultuj teamu —
        # to strata kredytów DeepSeek, bo i tak nie możemy nic
        # wykonać. Daj MAIN znać i poczekaj na reset limitu.
        # ------------------------------------------------------

        if gemini_disabled:

            log(
                "GEMINI",
                "WYKONAWCA ZABLOKOWANY — "
                "czekam na reset limitu API."
            )

            last_result = {
                "status":
                    "GEMINI_QUOTA_EXHAUSTED",
                "message":
                    "Gemini API wyczerpał limit. "
                    "Poczekaj na reset (zwykle 24h) "
                    "lub dodaj nowy klucz API do "
                    + str(GEMINI_KEYS_DIR)
                    + " i zrestartuj agenta."
            }

            import time
            time.sleep(30)

            continue

        # ------------------------------------------------------
        # Stan
        # ------------------------------------------------------

        log(
            "STATE",
            "Chrome: "
            + short(
                chrome_summary(),
                900
            )
        )

        log(
            "STATE",
            "Android: "
            + short(
                android_summary(),
                900
            )
        )

        # ------------------------------------------------------
        # POSTĘP CELU — co 5 kroków, żeby nie dokładać kolejnego
        # wywołania DeepSeeka na każdym kroku (ten sam powód co
        # ograniczenie RESEARCHER/BROWSER). Błąd tutaj nigdy nie
        # przerywa głównej pętli — to czysto informacyjne.
        # ------------------------------------------------------

        if step % 5 == 0:

            try:
                progress = estimate_progress(goal)

                if progress:
                    print_progress_bar(
                        step,
                        progress["percent"],
                        progress["summary"]
                    )

                    log_event(
                        "progress_estimate",
                        progress
                    )

            except Exception as e:
                log(
                    "PROGRESS_ESTIMATOR",
                    "Błąd oceny postępu (pomijam): " + str(e)
                )

        # ------------------------------------------------------
        # TEAM
        # ------------------------------------------------------

        team = consult_team(
            goal,
            last_result,
            step
        )

        # ------------------------------------------------------
        # MAIN
        # ------------------------------------------------------

        raw = main_decide(
            goal,
            step,
            team,
            last_result
        )

        decision = parse_json(raw)


        if decision is not None:

            decision = protect_main_failed(

                decision,

                goal

            )


        if decision is None:

            log(
                "MAIN",
                "Niepoprawny JSON. Naprawiam."
            )

            repair = deepseek(
                "MAIN",
                """
Poprzednia odpowiedź nie była poprawnym JSON.

Zwróć wyłącznie jeden obiekt:

{
  "type": "TASK",
  "reason": "...",
  "task": "...",
  "success_condition": "...",
  "priority": "high"
}

albo:

{
  "type": "DONE",
  "reason": "..."
}

albo:

{
  "type": "FAILED",
  "reason": "..."
}
"""
            )

            decision = parse_json(
                repair
            )


            if decision is not None:

                decision = protect_main_failed(

                    decision,

                    goal

                )


        if decision is None:

            last_result = {
                "status":
                    "MAIN_JSON_ERROR"
            }

            continue

        dtype = str(
            decision.get(
                "type",
                ""
            )
        ).upper()

        # ------------------------------------------------------
        # POWTARZANIE
        # ------------------------------------------------------

        signature = task_signature(
            decision
        )

        signatures.append(
            signature
        )

        if len(signatures) > 8:
            signatures = signatures[-8:]

        if (
            signatures.count(signature)
            >= REPEAT_LIMIT
        ):

            log(
                "MAIN",
                "Wykryto pętlę decyzji."
            )

            alternative = deepseek(
                "MAIN",
                f"""
Wykryto pętlę.

Powtarzana decyzja:
{signature}

CEL:
{goal}

Ostatni wynik:
{short(
    json.dumps(
        last_result,
        ensure_ascii=False
    ),
    3500
)}

Nie powtarzaj tej samej decyzji.

Wymyśl inną strategię.

Zwróć tylko JSON.
"""
            )

            alternative_decision = (
                parse_json(
                    alternative
                )
            )

            if alternative_decision:

                decision = (
                    alternative_decision
                )

                dtype = str(
                    decision.get(
                        "type",
                        ""
                    )
                ).upper()

                signatures = []

        # ------------------------------------------------------
        # TASK
        # ------------------------------------------------------

        if dtype == "TASK":

            task_text = str(
                decision.get(
                    "task",
                    ""
                )
            ).strip()

            success_condition = str(
                decision.get(
                    "success_condition",
                    ""
                )
            ).strip()

            if not task_text:

                last_result = {
                    "status":
                        "EMPTY_TASK"
                }

                continue

            # --------------------------------------------------
            # ZAPIS KODU ANDROID_GAME_ENGINEER BEZ UDZIAŁU GEMINI
            #
            # Jeżeli MAIN zdecydował, że gotowy blok kodu z
            # bieżącej odpowiedzi ANDROID_GAME_ENGINEER ma trafić
            # do pliku 1:1 — robi to Python, TERAZ, zanim TASK
            # w ogóle trafi do Gemini. Gemini dostaje wtedy zadanie
            # WYŁĄCZNIE uruchomienia/testowania, nigdy przepisania
            # kodu od zera z opisu — oszczędza to jego limit/tokeny
            # i eliminuje błędy przepisywania.
            # --------------------------------------------------

            write_target = str(
                decision.get("write_engineer_code_to", "")
            ).strip()

            if write_target:

                engineer_code = extract_code_block(
                    team.get("engineer_full", "")
                )

                if not engineer_code:

                    last_result = {
                        "status":
                            "ENGINEER_CODE_MISSING",
                        "message": (
                            "MAIN poprosił o zapisanie kodu "
                            "ANDROID_GAME_ENGINEER do "
                            + write_target
                            + ", ale w jego ostatniej odpowiedzi "
                            "nie znaleziono bloku kodu (```...```). "
                            "Zapytaj ANDROID_GAME_ENGINEER "
                            "ponownie o konkretny kod w bloku, albo "
                            "utwórz zwykły TASK bez "
                            "write_engineer_code_to."
                        )
                    }

                    continue

                target_path = Path(
                    write_target
                ).expanduser()

                # --------------------------------------------------
                # BEZPIECZEŃSTWO: blok kodu może być SKRYPTEM DO
                # URUCHOMIENIA (np. "cat > plik << EOF ..."), nie
                # treścią pliku do zapisania. Zaobserwowane naprawdę
                # — write_engineer_code_to o mało nie zapisało
                # komend powłoki jako zawartości build.gradle.
                # --------------------------------------------------

                if _looks_like_shell_script(
                    engineer_code,
                    target_path
                ):

                    last_result = {
                        "status":
                            "ENGINEER_CODE_LOOKS_LIKE_SHELL_SCRIPT",
                        "message": (
                            "write_engineer_code_to ODRZUCONE: "
                            "blok kodu od ANDROID_GAME_ENGINEER "
                            "wygląda jak SKRYPT POWŁOKI (zawiera "
                            "np. 'cat > plik << EOF' albo "
                            "podstawienie $(...)), nie treść "
                            "pliku " + str(target_path) + ". "
                            "Zapisanie tego dosłownie jako "
                            "zawartość pliku by go uszkodziło. "
                            "Jeżeli to naprawdę miał być skrypt do "
                            "WYKONANIA — zrób zwykły TASK i każ "
                            "Gemini uruchomić go przez termux_run, "
                            "NIE używaj write_engineer_code_to. "
                            "Jeżeli to miała być treść pliku — "
                            "poproś ANDROID_GAME_ENGINEER o czysty "
                            "kod pliku, bez komend powłoki wokół "
                            "niego."
                        )
                    }

                    continue

                # --------------------------------------------------
                # BEZPIECZEŃSTWO: write_engineer_code_to NADPISUJE
                # cały plik. Jeżeli plik już istnieje i jest sporo
                # większy niż nowy blok kodu, to prawie na pewno
                # oznacza, że ANDROID_GAME_ENGINEER podał tylko
                # FRAGMENT/poprawkę, a nie cały plik od nowa —
                # nadpisanie zniszczyłoby resztę. Odmawiamy zamiast
                # zgadywać; prompt sam w sobie to tylko sugestia dla
                # modelu, ta blokada obowiązuje niezależnie od tego,
                # czy MAIN się do niej zastosuje.
                # --------------------------------------------------

                if target_path.exists():

                    try:
                        existing_size = target_path.stat().st_size
                    except Exception:
                        existing_size = 0

                    new_size = len(
                        engineer_code.encode("utf-8")
                    )

                    if (
                        existing_size > 200
                        and new_size < existing_size * 0.4
                    ):

                        last_result = {
                            "status":
                                "ENGINEER_CODE_LOOKS_LIKE_PARTIAL_FIX",
                            "message": (
                                "write_engineer_code_to ODRZUCONE: "
                                "plik " + str(target_path)
                                + " ma już " + str(existing_size)
                                + "B, a nowy blok kodu od "
                                "ANDROID_GAME_ENGINEER ma tylko "
                                + str(new_size) + "B (mniej niż "
                                "40% obecnego rozmiaru). To wygląda "
                                "na FRAGMENT/poprawkę, nie cały "
                                "plik — nadpisanie zniszczyłoby "
                                "resztę. Jeżeli to naprawdę cała "
                                "nowa zawartość pliku, zmień "
                                "podejście (np. poproś "
                                "ANDROID_GAME_ENGINEER o "
                                "potwierdzenie że plik ma być "
                                "krótszy). Jeżeli to poprawka "
                                "fragmentu — utwórz zwykły TASK z "
                                "termux_patch_file (search/replace) "
                                "zamiast write_engineer_code_to."
                            )
                        }

                        continue

                try:
                    target_path.parent.mkdir(
                        parents=True,
                        exist_ok=True
                    )

                    target_path.write_text(
                        engineer_code,
                        encoding="utf-8"
                    )

                    _track_project_path(target_path)

                    log(
                        "MAIN",
                        "Zapisano kod ANDROID_GAME_ENGINEER "
                        "bezpośrednio do "
                        + str(target_path)
                        + " ("
                        + str(len(engineer_code))
                        + " znaków, bez zużycia Gemini)."
                    )

                    log_event(
                        "engineer_code_written",
                        {
                            "path": str(target_path),
                            "chars": len(engineer_code)
                        }
                    )

                except Exception as e:

                    last_result = {
                        "status":
                            "ENGINEER_CODE_WRITE_ERROR",
                        "error": str(e),
                        "path": write_target
                    }

                    continue

            # --------------------------------------------------
            # GEMINI ZABLOKOWANY
            # --------------------------------------------------

            if gemini_disabled:

                log(
                    "MAIN",
                    "Gemini jest zablokowany. "
                    "Nie tworzę nowego taska."
                )

                last_result = {
                    "status":
                        "GEMINI_QUOTA_EXHAUSTED",
                    "message":
                        "Brak wykonawcy."
                }

                # Nie wpadaj w pętlę.
                time.sleep(1)

                continue

            # --------------------------------------------------
            # NOWY TASK
            # --------------------------------------------------

            task_id = create_task(
                task=task_text,
                success_condition=
                    success_condition,
                reason=
                    decision.get(
                        "reason",
                        ""
                    ),
                priority=
                    decision.get(
                        "priority",
                        "normal"
                    )
            )

            if task_id is None:

                last_result = {
                    "status":
                        "TASK_BLOCKED_BY_POLICY",
                    "message": (
                        "Proponowane zadanie narusza jawne "
                        "wymagania użytkownika (np. pobranie "
                        "gotowej gry/APK zamiast utworzenia jej od "
                        "zera) i zostało odrzucone bez wykonania. "
                        "Zaproponuj inne podejście: wszystkie pliki "
                        "projektu ma utworzyć Gemini w Termux."
                    )
                }

                continue

            # --------------------------------------------------
            # OD RAZU WYKONUJEMY
            # --------------------------------------------------

            result = run_next_task()

            if result:

                last_result = result

            continue

        # ------------------------------------------------------
        # DONE
        # ------------------------------------------------------

        if dtype == "DONE":

            reason = decision.get(
                "reason",
                ""
            )

            # ----------------------------------------------------
            # MAIN mówi DONE. Nie wierzymy mu na słowo — sprawdzamy
            # fizyczne dowody, dokładnie jak w oryginalnym poleceniu:
            # "na końcu fizycznie zweryfikuj projekt, APK,
            # instalację i plik FINAL_OK".
            # ----------------------------------------------------

            verification = verify_final(goal)

            if not verification.get("ok"):

                log(
                    "MAIN",
                    "DONE odrzucone — weryfikacja fizyczna "
                    "nie przeszła."
                )

                missing = "\n".join(
                    "- " + c["check"] + ": " + c["detail"]
                    for c in verification["checks"]
                    if not c["ok"]
                )

                last_result = {
                    "status":
                        "DONE_REJECTED_VERIFICATION_FAILED",
                    "checks":
                        verification["checks"],
                    "message": (
                        "MAIN zgłosiło DONE, ale fizyczna "
                        "weryfikacja NIE potwierdziła realizacji "
                        "celu. Brakuje:\n"
                        + missing
                        + "\n\nUtwórz TASK, który faktycznie "
                        "uzupełni brakujące dowody — nie zgłaszaj "
                        "DONE ponownie, dopóki wszystkie wymagane "
                        "sprawdzenia nie przejdą."
                    )
                }

                write_json(
                    LAST_RESULT_FILE,
                    last_result
                )

                log_event(
                    "done_rejected",
                    {"checks": verification["checks"]}
                )

                continue

            print()
            print("=" * 72)
            print("CEL ZAKOŃCZONY")
            print("=" * 72)
            print(
                short(
                    reason,
                    5000
                )
            )

            write_json(
                LAST_RESULT_FILE,
                {
                    "status":
                        "DONE",
                    "goal":
                        goal,
                    "reason":
                        reason,
                    "step":
                        step,
                    "verification":
                        verification["checks"]
                }
            )

            # Sesja faktycznie i fizycznie potwierdzona jako
            # skończona — nie ma czego wznawiać.
            try:
                GOAL_FILE.unlink(missing_ok=True)
            except Exception:
                pass

            return

        # ------------------------------------------------------
        # FAILED
        # ------------------------------------------------------

        if dtype == "FAILED":

            reason = decision.get(
                "reason",
                ""
            )

            log(
                "MAIN",
                "FAILED: "
                + short(
                    reason,
                    3000
                )
            )

            # Jeżeli Gemini jest zablokowany,
            # nie twórz kolejnych tasków.

            if gemini_disabled:

                print(
                    "Brak wykonawcy Gemini — "
                    "agent zatrzymuje się bez pętli."
                )

                write_json(
                    LAST_RESULT_FILE,
                    {
                        "status":
                            "FAILED",
                        "goal":
                            goal,
                        "reason":
                            reason,
                        "step":
                            step
                    }
                )

                # Brak wykonawcy to trwały stan — nic tu nie
                # pomoże samo wznowienie sesji.
                try:
                    GOAL_FILE.unlink(missing_ok=True)
                except Exception:
                    pass

                return

            alternative = deepseek(
                "MAIN",
                f"""
MAIN zaproponował FAILED.

CEL:
{goal}

POWÓD:
{reason}

Sprawdź jeszcze raz.

Jeżeli istnieje sensowna alternatywa,
zwróć TASK.

Jeżeli naprawdę nie ma drogi,
zwróć FAILED.

Tylko JSON.
"""
            )

            alt = parse_json(
                alternative
            )

            if (
                alt
                and str(
                    alt.get(
                        "type",
                        ""
                    )
                ).upper()
                == "TASK"
            ):

                task_text = alt.get(
                    "task",
                    ""
                )

                condition = alt.get(
                    "success_condition",
                    ""
                )

                if task_text:

                    alt_task_id = create_task(
                        task_text,
                        condition,
                        "Alternatywa po FAILED",
                        "high"
                    )

                    if alt_task_id is None:

                        last_result = {
                            "status":
                                "TASK_BLOCKED_BY_POLICY",
                            "message": (
                                "Proponowana alternatywa po FAILED "
                                "narusza jawne wymagania użytkownika "
                                "(np. pobranie gotowej gry/APK) i "
                                "została odrzucona bez wykonania. "
                                "Zaproponuj podejście zgodne z "
                                "wymaganiem: wszystkie pliki "
                                "projektu tworzy Gemini w Termux."
                            )
                        }

                        continue

                    result = (
                        run_next_task()
                    )

                    if result:
                        last_result = result

                    continue

            print(
                "Agent nie znalazł dalszej sensownej drogi."
            )

            write_json(
                LAST_RESULT_FILE,
                {
                    "status":
                        "FAILED",
                    "goal":
                        goal,
                    "reason":
                        reason,
                    "step":
                        step
                }
            )

            try:
                GOAL_FILE.unlink(missing_ok=True)
            except Exception:
                pass

            return

        # ------------------------------------------------------
        # UNKNOWN
        # ------------------------------------------------------

        last_result = {
            "status":
                "UNKNOWN_DECISION",
            "decision":
                decision
        }

    print()
    print("=" * 72)
    print("OSIĄGNIĘTO LIMIT KROKÓW")
    print("=" * 72)

    write_json(
        LAST_RESULT_FILE,
        {
            "status":
                "MAX_STEPS",
            "goal":
                goal
        }
    )


# ============================================================
# MAIN
# ============================================================

def maybe_clear_previous_session_data():
    """
    Gdy użytkownik zaczyna NOWY cel (nie wznawia poprzedniego),
    pyta czy usunąć dane poprzedniej sesji: kolejkę zadań, zapisane
    wyniki, licznik powtarzających się porażek narzędzi, ostatni
    wynik.

    Bez tego: jeśli poprzednia sesja padła z zadaniem w stanie
    PENDING w kolejce, a użytkownik startuje zupełnie INNY cel,
    run_agent() na pierwszym kroku podjąłby i wykonał to stare
    zadanie — dotyczące poprzedniego, niepowiązanego celu — zanim
    w ogóle skonsultowałby nowy.

    Nie dotyka custom_tools/ (to trwałe, celowo dodane narzędzia,
    nie dane sesji) ani samego GOAL_FILE (to obsługiwane osobno,
    tam gdzie ta funkcja jest wołana).
    """

    queue_files = list(QUEUE_DIR.glob("*.json"))
    result_files = list(RESULTS_DIR.glob("*.json"))

    if not queue_files and not result_files:
        return

    description = (
        "Dane poprzedniej sesji: "
        + str(len(queue_files))
        + " zadań w kolejce, "
        + str(len(result_files))
        + " zapisanych wyników."
    )

    print()
    print(description)

    if not _confirm_destructive_action(
        "USUNIĘCIE DANYCH POPRZEDNIEJ SESJI (kolejka + wyniki) — "
        "nowy cel zacznie się na czysto"
    ):
        log(
            "MAIN",
            "Zachowano dane poprzedniej sesji (kolejka/wyniki)."
        )
        return

    for f in queue_files + result_files:
        try:
            f.unlink()
        except Exception:
            pass

    for extra in (
        TOOL_ATTEMPTS_FILE,
        LAST_RESULT_FILE,
        GEMINI_STATE_FILE
    ):
        try:
            extra.unlink(missing_ok=True)
        except Exception:
            pass

    log(
        "MAIN",
        "Usunięto dane poprzedniej sesji (kolejka, wyniki, "
        "licznik prób narzędzi, ostatni wynik)."
    )


def maybe_clear_generated_project_files():
    """
    OSOBNE pytanie od maybe_clear_previous_session_data() — to
    dotyczy PLIKÓW WYGENEROWANYCH przez Gemini/DeepSeek (kod gry,
    build.gradle, katalogi projektu), NIE wewnętrznych danych
    agenta. Celowo osobna decyzja: można wyczyścić kolejkę zadań,
    a mimo to zachować już napisany kod, albo odwrotnie.

    Lista kandydatów pochodzi WYŁĄCZNIE z PROJECT_DIRS_FILE —
    ścieżek faktycznie zaobserwowanych przez termux_mkdir/
    termux_write_file/termux_patch_file/write_engineer_code_to
    (patrz _track_project_path()). CELOWO nie ma tu żadnego
    "zapasowego skanu katalogu domowego" — wcześniejsza wersja to
    miała i skończyło się to skasowaniem ~/api_token.txt (prawdziwy
    token DeepSeek), ~/test/pow_helper.js (krytyczna zależność
    biblioteki opendeep) i wielu zupełnie niepowiązanych plików
    użytkownika (llama.cpp, wallets.json). Katalog domowy
    użytkownika może zawierać cokolwiek — agent nie ma prawa
    zgadywać, co w nim usunąć, tylko dlatego że sam tego nie
    stworzył.

    Dla plików sprzed wdrożenia śledzenia (v17): nie da się ich tu
    bezpiecznie pokazać automatycznie. Usuń je ręcznie przez
    termux_delete w trakcie sesji (poda konkretną nazwę do
    potwierdzenia) albo sam w Termuksie — nigdy nie przez ślepy
    skan całego $HOME.

    Twarda ochrona, niezależna od potwierdzenia: nigdy nie usuwa
    AGENT_DIR (klucze API, kod agenta, kolejka) ani katalogu
    domowego — to samo zabezpieczenie co w termux_delete().
    """

    tracked = read_json(PROJECT_DIRS_FILE, [])

    if not isinstance(tracked, list):
        tracked = []

    home = Path.home().resolve()
    agent_dir_resolved = AGENT_DIR.resolve()

    candidates = []

    for entry in tracked:

        try:
            p = Path(entry).expanduser().resolve()
        except Exception:
            continue

        if not p.exists():
            continue

        if p == home or p == agent_dir_resolved:
            # Nigdy nie powinno się tu znaleźć (patrz
            # _track_project_path()), ale sprawdzamy jeszcze raz —
            # druga warstwa tej samej ochrony.
            continue

        candidates.append(p)

    if not candidates:
        write_json(PROJECT_DIRS_FILE, [])
        return

    print()
    print("Wygenerowane pliki z poprzedniej sesji:")

    for p in candidates:

        if p.is_dir():
            try:
                count = sum(1 for _ in p.rglob("*"))
            except Exception:
                count = "?"
            print("  - " + str(p) + " (katalog, " + str(count) + " elementów)")
        else:
            try:
                size = p.stat().st_size
            except Exception:
                size = "?"
            print("  - " + str(p) + " (plik, " + str(size) + " B)")

    confirm_message = (
        "USUNIĘCIE WYGENEROWANYCH PLIKÓW PROJEKTU (kod/build "
        "powyżej) — klucze API i program agenta NIE są tym objęte"
    )

    if not _confirm_destructive_action(confirm_message):
        log(
            "MAIN",
            "Zachowano wygenerowane pliki projektu poprzedniej "
            "sesji."
        )
        return

    remaining = []

    for p in candidates:

        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()

            log(
                "MAIN",
                "Usunięto wygenerowany plik/katalog projektu: "
                + str(p)
            )

        except Exception as e:

            log(
                "MAIN",
                "Nie udało się usunąć " + str(p) + ": " + str(e)
            )

            remaining.append(str(p))

    write_json(PROJECT_DIRS_FILE, remaining)


def prompt_adb_target():
    """
    Pyta przy starcie programu o adres bezprzewodowego ADB
    (host:port).

    Android losuje NOWY port debugowania bezprzewodowego przy
    każdym jego ponownym włączeniu / restarcie WiFi na telefonie —
    dlatego zamiast trzymać port na sztywno w kodzie/env, agent
    pyta o niego na starcie i pamięta ostatnią wartość jako
    domyślną (ADB_CONNECT_FILE).

    Enter bez wpisywania niczego:
    - jest zapamiętany adres -> użyty ponownie (działa, jeżeli
      port akurat się nie zmienił),
    - nie ma zapamiętanego adresu -> pomijamy, zakładając USB albo
      że `adb devices` i tak znajdzie urządzenie samo.
    """

    saved = adb_connect_target()

    print(
        "Port debugowania bezprzewodowego zmienia się przy "
        "każdym włączeniu WiFi — znajdziesz go w: Ustawienia -> "
        "Opcje deweloperskie -> Debugowanie bezprzewodowe -> "
        "\"Adres IP i port\" (np. 192.168.1.23:41231)."
    )

    try:

        prompt_text = (
            "Adres ADB przez WiFi (host:port)"
            + (
                " [Enter = " + saved + "]"
                if saved else
                " [Enter = pomiń, np. USB]"
            )
            + " > "
        )

        typed = input(prompt_text).strip()

    except (EOFError, KeyboardInterrupt):

        print()
        typed = ""

    target = typed or saved

    if target:
        adb_try_connect(target)

    return target


def main():

    banner()

    # ----------------------------------------------------------
    # Wake lock — utrzymuje Termux aktywny w tle nawet gdy ekran
    # zgaśnie. BEZ TEGO Android (Doze mode) może dławić/usypiać
    # proces po dłuższej bezczynności ekranu, co przy wielogodzinnej
    # autonomicznej sesji ryzykuje ciche zawieszenie agenta bez
    # żadnego błędu w logach. Funkcja istniała w pliku od dawna, ale
    # nigdy nie była wołana — zwolnienie (agent_wake_unlock) już
    # było podpięte przez atexit, tylko nikt nigdy nie włączał blokady.
    # ----------------------------------------------------------

    agent_wake_lock()

    # ----------------------------------------------------------
    # DeepSeek
    # ----------------------------------------------------------

    if not init_deepseek():
        sys.exit(1)

    init_team()

    # ----------------------------------------------------------
    # ADB przez WiFi (opcjonalne — Enter pomija, np. przy USB)
    # ----------------------------------------------------------

    prompt_adb_target()

    # ----------------------------------------------------------
    # Android
    # ----------------------------------------------------------

    init_android()

    # ----------------------------------------------------------
    # Niestandardowe narzędzia (custom_tools/) — DeepSeek może je
    # dopisywać jako osobne pliki w trakcie działania agenta,
    # patrz load_custom_tools(). Ładujemy istniejące przy starcie;
    # run_agent() dogrywa nowe/zmienione co krok bez restartu.
    # ----------------------------------------------------------

    load_custom_tools()

    if CUSTOM_TOOLS:
        log(
            "CUSTOM_TOOL",
            "Wczytano przy starcie: "
            + ", ".join(CUSTOM_TOOLS.keys())
        )

    # ----------------------------------------------------------
    # Gemini
    # ----------------------------------------------------------

    init_gemini()

    # ----------------------------------------------------------
    # Chrome
    # ----------------------------------------------------------

    tabs = chrome_tabs()

    if tabs:

        log(
            "CHROME",
            "CDP OK — "
            + str(len(tabs))
            + " kart"
        )

        for tab in tabs:

            print(
                f"  [{tab['id']}] "
                f"{short(tab['title'], 100)} | "
                f"{short(tab['url'], 250)}"
            )

    else:

        log(
            "CHROME",
            "CDP niedostępne"
        )

    print()

    # ----------------------------------------------------------
    # CEL
    # ----------------------------------------------------------

    saved_goal = read_text(GOAL_FILE).strip()

    try:

        if saved_goal:

            print(
                "Wykryto niedokończoną sesję:"
            )
            print(
                "  " + short(saved_goal, 300)
            )
            print(
                "Enter = wznów ten cel. Albo wpisz nowy cel, "
                "żeby go zastąpić."
            )
            print()

            typed = input(
                "CEL AGENTA [Enter = wznów] > "
            ).strip()

            goal = typed if typed else saved_goal
            is_new_goal = bool(typed)

        else:

            goal = input(
                "Podaj CEL AGENTA > "
            ).strip()

            is_new_goal = True

    except KeyboardInterrupt:

        print()
        return

    if not goal:

        print(
            "Brak celu."
        )

        return

    if is_new_goal:
        maybe_clear_previous_session_data()
        maybe_clear_generated_project_files()

    print()

    print(
        "[CEL] "
        + goal
    )

    print()

    print(
        "DeepSeek MAIN -> DeepSeek team -> "
        "TASK -> Gemini -> Chrome/Android/Termux -> "
        "result -> MAIN"
    )

    print()

    try:

        run_agent(
            goal
        )

    except KeyboardInterrupt:

        print()
        print(
            "[AGENT] Zatrzymany przez użytkownika."
        )

    except Exception as e:

        print()
        print(
            "[KRYTYCZNY BŁĄD]"
        )
        print(e)

        traceback.print_exc()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
